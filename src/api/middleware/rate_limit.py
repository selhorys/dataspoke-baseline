"""Rate-limit wiring — two limiters over one dedicated Redis logical DB.

Spec: spec/API.md §Middleware Stack, spec/feature/AUTH.md
§Client-IP attribution for rate limiting.

- ``limiter`` is the application-wide default (``rate_limit_per_minute``/minute),
  installed as ``RouteResolvingSlowAPIMiddleware`` and registered on
  ``app.state.limiter``. It keeps an in-memory fallback: denying every read route
  on a Redis outage is a worse outcome than an imprecise global budget.
- ``auth_limiter`` governs the credential-accepting auth routes and **fails
  closed** — no in-memory fallback, no swallowed storage errors — because it is
  the only brute-force control on ``POST /auth/token`` and degrading it to
  per-process counting would weaken it silently. Its storage errors surface as
  ``503 STORAGE_UNAVAILABLE``, the same posture ``/auth/token/refresh`` takes on
  revocation lookups.

The two share the storage URI, so every counter lives in one keyspace, but not
the key function: the default plane keys on caller identity (JWT/cookie ``sub``,
else an API-token fingerprint, else IP) for fairness, while the auth plane keys
on the client address alone because its callers are unauthenticated by
definition.

Four scoping decisions the default plane makes, stated here because they are
policy rather than mechanism:

1. **Bucket identity is the caller, and nothing else.** The default budget is
   registered as an *application* limit, which slowapi scopes to the literal
   string ``"global"``, so one caller holds one budget across the whole API —
   the single per-caller budget spec/API.md §Middleware Stack describes.
   Registered as a ``default_limit`` the scope would instead be the route, and
   the documented 120/min would be granted afresh on each of the app's ~130 leaf
   routes.

   The trade this makes is that everything the key function cannot identify
   shares one bucket. With the chart default ``config.trustedProxyIps:
   "127.0.0.1"`` (``helm-charts/dataspoke/values.yaml``) the observed address of
   every caller outside the cluster is the ingress pod, so all unattributable
   default-plane traffic — ``/ready``, ``/redoc``, ``/openapi.json`` — draws from
   a single deployment-wide budget, and one client hammering an unauthenticated
   route can exhaust it for everyone behind that ingress. Widening
   `trustedProxyIps` to the ingress controller's address is what separates those
   callers. The same collapse applies to the auth plane, whose key is that
   address by construction.
2. **``/health`` is never charged.** It is the target of all three Kubernetes
   probes (``helm-charts/dataspoke/templates/api-deployment.yaml`` startup,
   liveness, readiness), and a 429 served to a probe reads as an unhealthy
   container, so a traffic spike would restart the pod. ``/ready`` is *not*
   exempt: nothing probes it and it fans out to Postgres, Redis, and DataHub
   GMS, so an unlimited public caller there is an amplifier.
3. **``/internal/*`` is never charged.** That plane is the Airflow task-callback
   control plane, already gated by the ``X-Internal-Token`` shared secret, and
   every caller arrives from one pod IP, so an IP-keyed budget adds no control
   while capping DAG fan-out (``HttpOperator.partial(...).expand(...)`` issues
   one POST per active source and retries on 429 inside the same window). The
   trade is explicit: a leaked internal token is not rate-limited, and the
   shared secret is the control that stands behind that plane.
4. **A path that matches no route is charged.** slowapi's own middleware reads
   an unresolved handler as exempt, which would leave a 404 flood as the one
   unmetered surface on this plane; the dispatch below substitutes a sentinel
   endpoint instead. The application limit carries its own ``"global"`` scope, so
   the sentinel selects no bucket of its own — unmatched requests draw down the
   caller's one budget like every other request.

Rate-limit headers (``Retry-After``, ``X-RateLimit-*``) are rendered on the 429
only, which is the whole of what spec/API.md asks for. slowapi's own
``headers_enabled`` is left off: it would add a ``get_window_stats`` round trip
to every successful request, and its route-decorator injection path raises on
the two password-reset routes, which return ``None`` and take no ``response``
parameter for it to write into.
"""

import functools
import hashlib
import logging
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextvars import ContextVar
from typing import Any, TypeVar, cast
from urllib.parse import quote

from anyio import to_thread
from fastapi import routing as _fastapi_routing
from redis.exceptions import RedisError
from slowapi import Limiter
from slowapi import _rate_limit_exceeded_handler as _slowapi_rate_limit_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware, _should_exempt
from slowapi.util import get_remote_address

# get_route_path strips any mounted root_path; it is where Starlette and FastAPI
# both read the routed path from (starlette.routing, fastapi.routing).
from starlette._utils import get_route_path
from starlette.applications import Starlette
from starlette.middleware.base import RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Match
from starlette.types import Scope

from src.shared.exceptions import StorageUnavailableError
from src.shared.settings import settings

logger = logging.getLogger(__name__)

# Redis logical DB holding the rate-limit counters (the `limits` library's
# `LIMITS:LIMITER/*` keys). Dedicated so nothing in the key-eviction path of
# ordinary cached data — the application cache, the SET NX concurrency locks,
# and the `revoked_refresh:*` set all live in DB 0 — can clear a brute-force
# counter. Exported so anything that has to flush the same keyspace (the
# integration-test fixtures) follows the limiter instead of duplicating the
# index.
RATE_LIMIT_REDIS_DB = 1

# Socket bounds for the limiters' own Redis client. Without them
# `limits.storage.RedisStorage` builds `redis.Redis.from_url` with
# `socket_connect_timeout=None`, i.e. the OS default — 5s on macOS, up to ~130s
# against a Linux SYN blackhole. That matters more here than for the application
# cache: `auth_limiter` has no in-memory fallback, so slowapi never marks its
# storage dead and *every* auth request re-attempts the connect. The house bound
# is 5s (`src/shared/cache/client.py`); the limiter takes a tighter one because
# the fail-closed answer (503) is cheap and correct, whereas a 5s wait per
# request is not.
RATE_LIMIT_STORAGE_TIMEOUT_SECONDS = 2.0

# How long `auth_route_limit` fast-denies after a storage failure, without
# touching the socket. The fail-closed limiter has no in-memory fallback, so
# slowapi never sets its sticky `_storage_dead` flag and each request would
# otherwise re-pay a full connect timeout. Short enough that a recovered Redis
# resumes counting almost immediately, long enough that a sustained outage costs
# microseconds per request instead of seconds.
AUTH_STORAGE_FAILURE_COOLDOWN_SECONDS = 5.0

# Exact routed paths the default limiter never charges (see the module docstring
# for why each is here). Matched on the routed path, so a mounted `root_path`
# does not defeat the exemption.
DEFAULT_LIMIT_EXEMPT_PATHS: frozenset[str] = frozenset({"/health"})

# Routed-path prefixes the default limiter never charges.
DEFAULT_LIMIT_EXEMPT_PATH_PREFIXES: tuple[str, ...] = ("/internal/",)


def _is_exempt_path(path: str) -> bool:
    """Whether *path* is outside the default limiter's remit."""
    return path in DEFAULT_LIMIT_EXEMPT_PATHS or path.startswith(
        DEFAULT_LIMIT_EXEMPT_PATH_PREFIXES
    )


@functools.cache
def _refresh_cookie_name() -> str:
    """Name of the refresh-token cookie, read from the route that sets it.

    Imported lazily and cached: the auth router imports `auth_route_limit` from
    this module, so a module-level import would close the cycle.
    """
    from src.api.routers import auth as auth_routes

    return auth_routes._REFRESH_COOKIE


@functools.cache
def _api_token_prefix() -> str:
    """Literal prefix of an opaque DataSpoke API token, from its issuer."""
    from src.backend.auth import api_tokens

    return api_tokens._TOKEN_PREFIX


def _get_user_key(request: Request) -> str:
    """Per-caller bucket key: JWT ``sub``, else API token, else cookie, else IP.

    Four cases, in the order the credential arrives:

    * a JWT bearer token — the ``sub`` claim, which is the user id;
    * an opaque API token (``dsk_…``) — a truncated SHA-256 of the token. It is
      the only credential the end-user plugin carries and it is not a JWT, so
      without this branch every API-token client in the deployment would fall
      through to the address branch and share one budget. Hashing rather than
      resolving the token keeps this off the database; the bucket is per token
      rather than per user, which ``_MAX_ACTIVE_PER_USER`` in
      ``src/backend/auth/api_tokens.py`` bounds at ten per user;
    * a refresh cookie — ``POST /auth/token/refresh`` is a public route that
      authenticates by cookie alone and is charged against the default budget,
      and keyed on address it would share one bucket across every caller behind
      the ingress, letting one client exhaust every user's ability to refresh.
      The signature is verified before the ``sub`` is trusted, so the bucket
      cannot be spoofed into another user's;
    * nothing recognisable — the observed client address (see the module
      docstring on what that address is behind an ingress).

    This is a fairness key, not a security boundary: the caller picks which of
    these it presents. The fail-closed auth plane uses ``_get_client_ip_key``
    for exactly that reason.
    """
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        bearer = auth.removeprefix("Bearer ")
        if bearer.startswith(_api_token_prefix()):
            return "pat:" + hashlib.sha256(bearer.encode()).hexdigest()[:32]
        try:
            from src.backend.auth.tokens import decode_access_token

            payload = decode_access_token(bearer)
            sub = payload.get("sub")
            if sub:
                return cast(str, sub)
        except Exception:
            pass
    else:
        try:
            cookie = request.cookies.get(_refresh_cookie_name())
            if cookie:
                from src.backend.auth.tokens import decode_refresh_token

                sub = decode_refresh_token(cookie).get("sub")
                if sub:
                    return cast(str, sub)
        except Exception:
            pass
    return get_remote_address(request)


def _get_client_ip_key(request: Request) -> str:
    """Bucket key for the fail-closed auth limiter: the client address, always.

    The routes this limiter governs accept credentials, so by definition their
    callers are unauthenticated and every identity in the request is one the
    caller chose. Deriving the key from a bearer token or the refresh cookie —
    as ``_get_user_key`` does for the default plane — would let the party being
    limited pick its own bucket, and both ``POST /auth/register`` and
    ``POST /auth/token`` hand out exactly such a credential, so each one
    acquired would mint another full budget.

    spec/feature/AUTH.md §Client-IP attribution makes this limiter the only
    brute-force control on ``POST /auth/token`` — DataSpoke has no account
    lockout — and a bound the attacker can reset is not a bound on guessing
    rate. The same section already states that the credential-accepting routes
    are bucketed by the observed client address.
    """
    return get_remote_address(request)


def _storage_options() -> dict[str, Any]:
    """Fresh per-limiter storage kwargs.

    A fresh dict per call on purpose: ``Limiter.__init__`` keeps the mapping it
    is handed and later mutates it in place (``self._storage_options.update``),
    so sharing one object between the two limiters would let either one's
    config leak into the other.
    """
    return {
        "socket_connect_timeout": RATE_LIMIT_STORAGE_TIMEOUT_SECONDS,
        "socket_timeout": RATE_LIMIT_STORAGE_TIMEOUT_SECONDS,
    }


def _build_storage_uri(host: str, port: int, db: int, password: str) -> str:
    """Redis URI for the limiters' storage, with the password percent-encoded.

    ``limits`` takes a URI string rather than connection kwargs, so unlike
    ``src/shared/cache/client.py`` this layer cannot hand the password to the
    driver as a field and has to escape it. ``quote(..., safe="")`` is the exact
    inverse of the ``unquote`` redis-py's ``parse_url`` applies, so the password
    arrives verbatim: without it a ``/``, ``#`` or ``?`` makes ``parse_url``
    split the netloc in the wrong place and the API dies at import, and a ``%``
    decodes into a different password. spec/feature/BACKEND.md §Cache Key
    Conventions: the storage URI percent-encodes the password, so
    ``DATASPOKE_REDIS_PASSWORD`` accepts any character. ``port`` and ``db`` are
    typed ``int`` and reach the URI through ``f``-string rendering of that int.
    """
    auth = f":{quote(password, safe='')}@" if password else ""
    return f"redis://{auth}{host}:{port}/{db}"


storage_uri = _build_storage_uri(
    settings.redis_host,
    settings.redis_port,
    RATE_LIMIT_REDIS_DB,
    settings.redis_password,
)

limiter = Limiter(
    key_func=_get_user_key,
    storage_uri=storage_uri,
    storage_options=_storage_options(),
    # An *application* limit, not a default limit: slowapi builds it with the
    # literal scope "global", so the bucket is the caller alone and the budget
    # is the single per-caller one spec/API.md documents. A default limit is
    # scoped to the route instead, which hands the same budget out once per
    # route function.
    application_limits=[f"{settings.rate_limit_per_minute}/minute"],
    # slowapi derives an internal endpoint key from either the URL or the view
    # function. The application limit above carries its own explicit scope, so
    # this key never selects the bucket; "endpoint" keeps it stable across the
    # parameterised paths that make up most of the surface.
    key_style="endpoint",
    # Fall back to in-process memory storage when Redis is unreachable so that
    # a transient Redis outage (or the unit-test environment where Redis is
    # absent) does not turn every request into an unhandled exception.
    in_memory_fallback_enabled=True,
)

auth_limiter = Limiter(
    # Client address only — never a caller-supplied identity. See
    # `_get_client_ip_key`.
    key_func=_get_client_ip_key,
    storage_uri=storage_uri,
    storage_options=_storage_options(),
    # Per (client address, auth route): registration and login carry different
    # budgets and must not drain one another.
    key_style="endpoint",
    # Fail closed: no in-memory fallback, and storage errors are raised rather
    # than swallowed so the API answers 503 instead of counting per process.
    in_memory_fallback_enabled=False,
    swallow_errors=False,
)

# Endpoint names (slowapi's "<module>.<qualname-less function name>" form) whose
# limits are charged to `auth_limiter`. Populated by `auth_route_limit`.
_AUTH_LIMITED_ENDPOINTS: set[str] = set()

# Set once an auth handler body has started running, so `auth_route_limit` can
# tell a limiter-storage failure (the request never took effect) from a Redis
# failure inside the handler (it may already have committed). A ContextVar, not
# request state, because the marker has to be readable from the wrapper that
# owns the `try` without reaching into slowapi's argument handling. Coroutines
# share their caller's context, so the set is visible to the awaiting frame.
_AUTH_HANDLER_ENTERED: ContextVar[bool] = ContextVar(
    "dataspoke_auth_handler_entered", default=False
)

# Monotonic timestamp of the last auth-limiter storage failure, or None.
_auth_storage_failed_at: float | None = None

F = TypeVar("F", bound=Callable[..., Any])


def _endpoint_name(func: Callable[..., Any]) -> str:
    """Name slowapi registers a route under — same form its middleware looks up."""
    return f"{func.__module__}.{func.__name__}"


def _request_argument(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Request | None:
    """The ``Request`` an endpoint was called with, if it is there.

    FastAPI invokes endpoints with keyword arguments, so the kwarg is the real
    path; the positional scan is a cheap guard for a direct call. Returning
    ``None`` rather than raising leaves slowapi's own (clearer) error in place
    for a route that declares no ``request`` parameter.
    """
    candidate = kwargs.get("request")
    if isinstance(candidate, Request):
        return candidate
    for value in args:
        if isinstance(value, Request):
            return value
    return None


def _auth_storage_in_cooldown() -> bool:
    """Whether the auth limiter is inside its post-failure fast-deny window."""
    failed_at = _auth_storage_failed_at
    return (
        failed_at is not None
        and (time.monotonic() - failed_at) < AUTH_STORAGE_FAILURE_COOLDOWN_SECONDS
    )


def _record_auth_storage_failure() -> None:
    global _auth_storage_failed_at
    _auth_storage_failed_at = time.monotonic()


def reset_auth_storage_cooldown() -> None:
    """Clear the fast-deny window. For test setup between scenarios."""
    global _auth_storage_failed_at
    _auth_storage_failed_at = None


def auth_route_limit(limit_value: str) -> Callable[[F], F]:
    """Rate-limit a credential-accepting auth route on the fail-closed limiter.

    Does four things at once, which is why it exists instead of a bare
    ``@auth_limiter.limit(...)``:

    1. Registers *limit_value* on ``auth_limiter``, so an unreachable Redis
       denies the route rather than degrading it to per-process counting.
    2. Exempts the route from ``limiter``, whose middleware would otherwise also
       charge it against the shared default budget. slowapi exempts a decorated
       route from its own middleware automatically, but only for limits
       registered on the *same* ``Limiter`` instance — with the limit living on a
       second instance the exemption has to be stated.
    3. Translates the ``redis.RedisError`` that the fail-closed limiter raises on
       a storage outage into ``StorageUnavailableError`` → ``503
       STORAGE_UNAVAILABLE``. The translation is scoped to the limit check: once
       the handler body has started, a ``RedisError`` from inside it is left
       alone, because at that point the request may already have committed and
       "request denied" would be a false claim.
    4. Runs the limit check on a worker thread and fast-denies for
       ``AUTH_STORAGE_FAILURE_COOLDOWN_SECONDS`` after a storage failure.
       ``limits.storage.RedisStorage`` is the *synchronous* redis client, and
       slowapi's own ``async_wrapper`` calls it inline, so an unreachable Redis
       would park the whole uvicorn worker's event loop for the socket timeout
       on every auth request — freezing every other in-flight request with it,
       including the Kubernetes readiness probe on ``/health`` (3s timeout), so
       a Redis outage would roll the pods. Doing the check here and setting
       ``request.state._rate_limiting_complete`` makes ``async_wrapper`` skip
       its inline call (slowapi ``extension.py``); the cooldown then keeps a
       sustained outage from re-paying a connect timeout per request.

    It also records the endpoint name so ``limiter_for_request`` can tell which
    limiter owns the route when the 429 headers are rendered.
    """

    def decorator(func: F) -> F:
        name = _endpoint_name(func)
        _AUTH_LIMITED_ENDPOINTS.add(name)

        @functools.wraps(func)
        async def marked(*args: Any, **kwargs: Any) -> Any:
            _AUTH_HANDLER_ENTERED.set(True)
            return await func(*args, **kwargs)

        # slowapi's exempt() is untyped; it returns a functools.wraps passthrough,
        # so the endpoint signature FastAPI and slowapi introspect is unchanged.
        exempted = limiter.exempt(marked)  # type: ignore[no-untyped-call]
        limited = auth_limiter.limit(limit_value)(exempted)

        @functools.wraps(func)
        async def guarded(*args: Any, **kwargs: Any) -> Any:
            # Gated on `enabled` like every other branch here: a disabled
            # limiter has to be wholly inert, or a single storage failure would
            # keep denying requests after it is switched off.
            if auth_limiter.enabled and _auth_storage_in_cooldown():
                logger.warning(
                    "rate_limit_storage_cooldown_denied", extra={"endpoint": name}
                )
                raise StorageUnavailableError(
                    "Rate limit storage unavailable; request denied."
                )
            request = _request_argument(args, kwargs)
            if request is None and auth_limiter.enabled and auth_limiter._auto_check:
                # Falls through to slowapi's inline check, which runs on the
                # event loop — the behaviour the off-loop pre-check exists to
                # avoid. Unreachable for the routes decorated today (slowapi
                # raises at decoration time for an endpoint that declares no
                # `request` parameter), so this is here to keep a future auth
                # route that loses it from degrading silently.
                logger.warning("rate_limit_precheck_skipped", extra={"endpoint": name})
            token = _AUTH_HANDLER_ENTERED.set(False)
            try:
                # slowapi would run this check inline on the event loop; run it
                # on a worker thread instead and tell `async_wrapper` it is
                # already done. Mirrors `async_wrapper`'s own guard conditions
                # so a disabled or non-auto-checking limiter behaves the same.
                if (
                    request is not None
                    and auth_limiter.enabled
                    and auth_limiter._auto_check
                    and not getattr(request.state, "_rate_limiting_complete", False)
                ):
                    await to_thread.run_sync(
                        auth_limiter._check_request_limit, request, exempted, False
                    )
                    request.state._rate_limiting_complete = True
                return await limited(*args, **kwargs)
            except RedisError as exc:
                if _AUTH_HANDLER_ENTERED.get():
                    raise
                _record_auth_storage_failure()
                logger.warning(
                    "rate_limit_storage_unavailable",
                    extra={"endpoint": name},
                    exc_info=True,
                )
                raise StorageUnavailableError(
                    "Rate limit storage unavailable; request denied."
                ) from exc
            finally:
                _AUTH_HANDLER_ENTERED.reset(token)

        return cast(F, guarded)

    return decorator


def limiter_for_request(request: Request) -> Limiter:
    """Return the ``Limiter`` that evaluated *request*'s rate limit.

    The 429 handler renders ``X-RateLimit-*`` from the limiter's own storage, so
    with two instances in play it has to resolve the right one rather than
    assume ``app.state.limiter`` (which is always the default limiter, because
    that is the instance the rate-limit middleware reads). Starlette puts the
    matched endpoint in the request scope, and auth-limited endpoints are
    recorded at decoration time; an unmatched request has no route limit and
    belongs to the default limiter by definition.
    """
    endpoint = request.scope.get("endpoint")
    if endpoint is not None and _endpoint_name(endpoint) in _AUTH_LIMITED_ENDPOINTS:
        return auth_limiter
    return limiter


async def rate_limit_headers(request: Request) -> dict[str, str]:
    """Advisory ``Retry-After`` / ``X-RateLimit-*`` headers for a 429.

    Rendered here rather than through slowapi's ``headers_enabled`` machinery so
    the round trip is paid only on the 429 that spec/API.md attaches them to.
    ``request.state.view_rate_limit`` is set by the limiter before it raises, and
    names the limit that failed.

    Reading the window stats is another synchronous Redis round trip, run off
    the event loop and best-effort: serving the 429 without its advisory headers
    beats raising inside an exception handler, which would surface the rate
    limit as a 500.
    """
    current_limit = getattr(request.state, "view_rate_limit", None)
    if not current_limit:
        return {}
    item, identifiers = current_limit
    try:
        reset_at, remaining = await to_thread.run_sync(
            limiter_for_request(request).limiter.get_window_stats, item, *identifiers
        )
    except Exception:
        logger.warning("rate_limit_header_render_failed", exc_info=True)
        return {}
    reset_in = 1 + int(reset_at)
    return {
        "X-RateLimit-Limit": str(item.amount),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(reset_in),
        "Retry-After": str(max(0, reset_in - int(time.time()))),
    }


# ── Route resolution ──────────────────────────────────────────────────────────
#
# Depends on two FastAPI internals, both part of the lazy `include_router`
# implementation (FastAPI 0.138.x):
#
#   * `fastapi.routing._IncludedRouter` — what `app.include_router()` appends to
#     `app.router.routes`. It is a `BaseRoute` that matches but carries no
#     `endpoint`, so slowapi's own `_find_route_handler` (which requires
#     `hasattr(route, "endpoint")`) resolves `None` for every included route and
#     `_should_exempt(limiter, None)` then exempts it. The practical effect is
#     that a stock `SlowAPIMiddleware` enforces nothing on any router-mounted
#     path — which is every application route in this app.
#   * `_IncludedRouter.effective_candidates()` — the prefix-applied children of
#     an included router. It must be used instead of `original_router.routes`
#     because the latter holds *pre-prefix* paths, which mis-match (a sub-router
#     route declared as `/{id}` would claim `/health`).
#
# `effective_candidates()` memoises against the router's route version, so in the
# steady state a per-request call is a version comparison and a list return.
#
# If a future FastAPI drops `_IncludedRouter`, the tuple below is empty and every
# included route resolves to None, which `_should_exempt` reads as exempt — the
# default plane would silently enforce nothing. `verify_route_resolution` is
# called at app construction to make that loud.
_INCLUDED_ROUTER_TYPES: tuple[type, ...] = tuple(
    t for t in (getattr(_fastapi_routing, "_IncludedRouter", None),) if isinstance(t, type)
)


def _route_endpoint(route: Any) -> Callable[..., Any] | None:
    """Endpoint callable behind a matched route, if it has one.

    Handles both a plain Starlette/FastAPI route and FastAPI's
    `_EffectiveRouteContext`, whose `endpoint` is populated only for `APIRoute`
    children; other route kinds keep theirs on the rebuilt `starlette_route`.
    """
    endpoint = getattr(route, "endpoint", None)
    if endpoint is None:
        endpoint = getattr(getattr(route, "starlette_route", None), "endpoint", None)
    return cast("Callable[..., Any] | None", endpoint)


def resolve_route_endpoint(
    routes: Iterable[BaseRoute] | Sequence[Any], scope: Scope
) -> Callable[..., Any] | None:
    """Find the endpoint that *scope* routes to, descending included routers.

    First full match wins, and an `_IncludedRouter` is descended into rather
    than skipped. First-match is what actually gets served: `Router.app`
    dispatches to the first `Match.FULL` and returns (starlette/routing.py), and
    `_IncludedRouter._match` does the same over its candidates. The limiter has
    to reason about that same endpoint, because it is the endpoint that decides
    exemption and — under `key_style="endpoint"` — the bucket.
    """
    for route in routes:
        match, _ = route.matches(scope)
        if match != Match.FULL:
            continue
        if _INCLUDED_ROUTER_TYPES and isinstance(route, _INCLUDED_ROUTER_TYPES):
            included = cast(Any, route)
            return resolve_route_endpoint(included.effective_candidates(), scope)
        endpoint = _route_endpoint(route)
        if endpoint is not None:
            return endpoint
    return None


def _iter_leaf_routes(routes: Iterable[Any]) -> Iterator[Any]:
    """Flatten included routers down to the routes that carry a path."""
    for route in routes:
        if _INCLUDED_ROUTER_TYPES and isinstance(route, _INCLUDED_ROUTER_TYPES):
            yield from _iter_leaf_routes(cast(Any, route).effective_candidates())
        else:
            yield route


def _probe_scope(app: Starlette) -> Scope | None:
    """An HTTP scope for one static route mounted behind an included router.

    Behind an included router on purpose: a top-level route resolves through the
    plain `hasattr(route, "endpoint")` path and would pass the self-test even if
    the lazy-router descent were broken, which is the only part at risk. Static
    (no path parameters) so the probe scope is the route's own literal path.
    """
    for route in app.router.routes:
        if not _INCLUDED_ROUTER_TYPES or not isinstance(route, _INCLUDED_ROUTER_TYPES):
            continue
        for leaf in _iter_leaf_routes(cast(Any, route).effective_candidates()):
            path = getattr(leaf, "path", "")
            methods = getattr(leaf, "methods", None)
            if not path or "{" in path or not methods:
                continue
            method = "GET" if "GET" in methods else sorted(methods)[0]
            return {
                "type": "http",
                "method": method,
                "path": path,
                "root_path": "",
                "headers": [],
            }
    return None


def verify_route_resolution(app: Starlette) -> None:
    """Log loudly if this FastAPI version defeats `resolve_route_endpoint`.

    Degradation here is fail-open — unresolvable routes are treated as exempt —
    so losing the internals `resolve_route_endpoint` depends on would silently
    switch the default rate limit off across the whole app. Called from
    `create_app` after every router is mounted.

    Two checks, because the type probe alone proves nothing: `_IncludedRouter`
    existing does not mean resolution through it still yields an endpoint. So
    after the type check, actually resolve a real route of this app.

    Every way of not reaching a resolved endpoint logs, including failing to
    build the probe at all: `_probe_scope` walks the same router internals, so
    "no probe could be constructed" is itself evidence that they moved.
    """
    if not _INCLUDED_ROUTER_TYPES:
        opaque = [route for route in app.router.routes if not hasattr(route, "endpoint")]
        if not opaque:
            return
        logger.error(
            "rate_limit_route_resolution_unsupported",
            extra={
                "reason": "included_router_type_missing",
                "opaque_route_types": sorted({type(route).__name__ for route in opaque}),
                "opaque_route_count": len(opaque),
            },
        )
        return

    probe = _probe_scope(app)
    if probe is None:
        logger.error(
            "rate_limit_route_resolution_unsupported",
            extra={
                "reason": "probe_unconstructible",
                "included_route_count": sum(
                    isinstance(route, _INCLUDED_ROUTER_TYPES)
                    for route in app.router.routes
                ),
            },
        )
        return
    if resolve_route_endpoint(app.router.routes, probe) is not None:
        return
    logger.error(
        "rate_limit_route_resolution_unsupported",
        extra={
            "reason": "probe_unresolved",
            "probe_method": probe["method"],
            "probe_path": probe["path"],
        },
    )


def _unmatched_route() -> None:
    """Stand-in endpoint for a request that routes nowhere.

    slowapi keys its bookkeeping off the endpoint's `<module>.<name>`, and an
    empty key makes `_check_request_limit` return without charging anything
    (slowapi `extension.py`). Handing it this function instead is what puts
    404-bound traffic on the meter; the application limit's own `"global"` scope
    means the name never becomes a bucket of its own.
    """


class RouteResolvingSlowAPIMiddleware(SlowAPIMiddleware):
    """`SlowAPIMiddleware` that can actually see router-mounted endpoints.

    Five differences from the stock middleware:

    1. Route resolution goes through `resolve_route_endpoint`, so the default
       limits apply to routes mounted via `include_router` (see the note above
       `_INCLUDED_ROUTER_TYPES`).
    2. The exempt paths and prefixes are honoured before anything else.
    3. A request that matches no route is charged against the caller's budget
       rather than waved through as "unresolved, therefore exempt".
    4. The limit check runs on a worker thread. `limits.storage.RedisStorage` is
       the synchronous redis client, so calling it inline would block the whole
       uvicorn worker's event loop for the socket timeout on every request while
       Redis is unreachable, not just the request being charged.
    5. The 429 is rendered by the app's own `RateLimitExceeded` handler even when
       that handler is a coroutine — slowapi's `sync_check_limits` silently falls
       back to its plain-text default in that case, which would drop the standard
       error envelope. Storage failures that escape the limiter are logged and
       the request is served: the default plane is explicitly fail-open
       (spec/feature/AUTH.md §Failure Modes).
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        app: Starlette = request.app
        limiter_: Limiter = app.state.limiter

        if not limiter_.enabled:
            return await call_next(request)

        if _is_exempt_path(get_route_path(request.scope)):
            return await call_next(request)

        # `or _unmatched_route`: `_should_exempt(limiter, None)` is True, so a
        # path that resolves to no endpoint would otherwise pass unmetered.
        handler = resolve_route_endpoint(app.router.routes, request.scope) or _unmatched_route
        if _should_exempt(limiter_, handler):
            return await call_next(request)

        if limiter_._auto_check and not getattr(
            request.state, "_rate_limiting_complete", False
        ):
            try:
                await to_thread.run_sync(
                    limiter_._check_request_limit, request, handler, True
                )
            except RateLimitExceeded as exc:
                return await self._render_rate_limited(app, request, exc)
            except Exception:
                # The default limiter falls back to in-memory counting, so this
                # is the residual case where even that failed. Serving the
                # request beats 500-ing every caller on a storage outage.
                logger.warning("rate_limit_check_failed", exc_info=True)

        return await call_next(request)

    @staticmethod
    async def _render_rate_limited(
        app: Starlette, request: Request, exc: RateLimitExceeded
    ) -> Response:
        """Render the 429 through the app's handler.

        Exceptions raised inside a `BaseHTTPMiddleware` never reach Starlette's
        `ExceptionMiddleware`, which sits *below* the user middleware stack, so
        the handler has to be invoked directly rather than by raising.
        """
        handler = app.exception_handlers.get(type(exc), _slowapi_rate_limit_handler)
        result = handler(request, exc)
        if isinstance(result, Response):
            return result
        return cast(Response, await result)
