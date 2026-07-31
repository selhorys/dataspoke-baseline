"""Unit tests for the ``/auth/google/*`` browser-redirect failure surface.

The two Google routes are reached only as full-page browser navigations, so every
outcome their **handlers** produce is a 302 — success to the configured post-login
target, failure to the UI's ``/oauth-error`` page. The limiter plane in front of the
handler is the stated exception: it still answers the ordinary JSON error envelope.

Concerns covered:
- ``/oauth-error`` location derivation across the configured shapes of
  ``oauth_post_login_redirect`` — absolute with a trailing slash, absolute without one,
  absolute carrying a path (discarded), and the bare ``/`` default (relative location).
- the ``error`` query parameter: present and URL-encoded for the five catalogued codes,
  **absent** for an uncatalogued ``DataSpokeError`` and for an exception outside the
  taxonomy, which are logged at ERROR instead.
- no failure path sets a cookie.
- both routes sit on the fail-closed auth limiter, whose rejections (``429`` over
  budget, ``503 STORAGE_UNAVAILABLE`` on a storage outage) precede the handler and are
  therefore not redirects.

Log assertions use ``structlog.testing.capture_logs`` rather than pytest's ``caplog``:
structlog is unconfigured in this project, so it falls back to ``PrintLogger`` and never
reaches stdlib logging — a caplog-based assertion would observe nothing. Each test
asserts a non-empty capture as its backstop.

spec: spec/API.md §OAuth browser-redirect contract
spec: spec/API.md §Middleware Stack
spec: spec/feature/AUTH.md §Callback failure surface
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch
from urllib.parse import parse_qs, urlsplit

import pytest
import structlog
from starlette.requests import Request
from starlette.responses import RedirectResponse

from src.shared.exceptions import DataSpokeError, OAuthNotConfiguredError, StorageUnavailableError
from src.shared.settings import Settings

# The five codes spec/API.md §OAuth browser-redirect contract states reach the page:
# "Five codes reach the error page: `OAUTH_NOT_CONFIGURED` (both routes), plus
# `OAUTH_STATE_MISMATCH`, `OAUTH_EMAIL_NOT_VERIFIED`, `GOOGLE_ACCOUNT_LINKED_ELSEWHERE`,
# and `EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT` on the callback."
SPEC_ERROR_CODES: tuple[str, ...] = (
    "OAUTH_NOT_CONFIGURED",
    "OAUTH_STATE_MISMATCH",
    "OAUTH_EMAIL_NOT_VERIFIED",
    "GOOGLE_ACCOUNT_LINKED_ELSEWHERE",
    "EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT",
)


def _settings(post_login_redirect: str) -> Settings:
    """A settings double carrying one configured post-login redirect target."""
    settings = MagicMock(spec=Settings)
    settings.oauth_post_login_redirect = post_login_redirect
    return settings


def _request(path: str = "/api/v1/auth/google/callback") -> Request:
    """A real starlette ``Request`` — the handler reads ``.url``, ``.state``, ``.client``."""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "server": ("api.dataspoke.test", 80),
            "root_path": "",
            "path": path,
            "query_string": b"",
            "headers": [],
            "client": ("203.0.113.7", 51234),
        }
    )


# ── /oauth-error location derivation ─────────────────────────────────────────
#
# spec/API.md §OAuth browser-redirect contract: "`<ui>/oauth-error` is the origin of the
# configured post-login redirect target (`DATASPOKE_OAUTH_POST_LOGIN_REDIRECT`) plus the
# absolute path `/oauth-error` — any path component on the configured value is discarded,
# and a bare `/` (the default, for a same-host deployment) degrades to the relative
# location `/oauth-error`."


@pytest.mark.parametrize(
    "post_login_redirect,expected",
    [
        # Absolute, trailing slash.
        ("https://app.example.com/", "https://app.example.com/oauth-error"),
        # Absolute, no trailing slash.
        ("https://app.example.com", "https://app.example.com/oauth-error"),
        # Absolute carrying a path — the path component is discarded, origin retained.
        (
            "https://app.example.com/governance/dashboard",
            "https://app.example.com/oauth-error",
        ),
        # The bare "/" default degrades to a relative location.
        ("/", "/oauth-error"),
    ],
)
def test_error_page_location_is_the_origin_plus_absolute_oauth_error_path(
    post_login_redirect: str, expected: str
) -> None:
    """Each configured shape resolves to the origin plus ``/oauth-error``.

    spec: spec/API.md §OAuth browser-redirect contract — "the origin of the configured
    post-login redirect target … plus the absolute path `/oauth-error` — any path
    component on the configured value is discarded, and a bare `/` … degrades to the
    relative location `/oauth-error`".
    """
    from src.api.routers.auth import _oauth_error_url

    assert _oauth_error_url(_settings(post_login_redirect), None) == expected


@pytest.mark.parametrize("code", SPEC_ERROR_CODES)
def test_catalogued_code_is_carried_as_the_error_query_parameter(code: str) -> None:
    """A catalogued code reaches the page as ``?error=<code>`` on the same location.

    spec: spec/API.md §OAuth browser-redirect contract — "`302` to
    `<ui>/oauth-error?error=<code>` for the codes listed below".
    """
    from src.api.routers.auth import _oauth_error_url

    assert (
        _oauth_error_url(_settings("https://app.example.com/"), code)
        == f"https://app.example.com/oauth-error?error={code}"
    )


def test_error_value_is_url_encoded() -> None:
    """The ``error`` value is URL-encoded before it is placed on the location.

    None of the five codes needs escaping — spec/API.md §OAuth browser-redirect contract
    states the value "originates from DataSpoke's own error codes, never from request
    input" — so the encoding rule is only observable on a synthetic value. Asserting it
    here keeps the stated rule from silently disappearing behind codes that happen to be
    URL-safe.

    The assertion is a round-trip closure rather than a byte string: the spec fixes only
    that the value is encoded, not which characters a conformant encoder escapes (RFC 3986
    permits a bare ``/`` inside a query component), so pinning ``%2F`` would pin an
    implementation choice. Decoding back to exactly the supplied value — and to nothing
    else — is the whole of the stated rule, and still fails if the encoding is dropped
    (the raw form parses as two parameters).

    spec: spec/API.md §OAuth browser-redirect contract — "the `error` value is
    URL-encoded".
    """
    from src.api.routers.auth import _oauth_error_url

    location = _oauth_error_url(_settings("https://app.example.com/"), "A&B=C/D")

    assert urlsplit(location).path == "/oauth-error"
    assert parse_qs(urlsplit(location).query) == {"error": ["A&B=C/D"]}


def test_no_error_parameter_when_no_code_is_forwarded() -> None:
    """A failure with no forwarded code lands on a bare ``/oauth-error``.

    spec: spec/API.md §OAuth browser-redirect contract — an uncatalogued failure
    "redirects to `<ui>/oauth-error` with no `error` parameter".
    """
    from src.api.routers.auth import _oauth_error_url

    assert "?" not in _oauth_error_url(_settings("https://app.example.com/"), None)


# ── exception → emitted code ─────────────────────────────────────────────────


@pytest.mark.parametrize("code", SPEC_ERROR_CODES)
def test_catalogued_error_redirects_with_its_code_and_sets_no_cookie(code: str) -> None:
    """Each of the five catalogued codes is delivered as ``?error=<code>`` on a 302.

    Driven through the ``DataSpokeError`` base with the code set explicitly, so the test
    pins the contract's own selector — the *code* — rather than whichever exception class
    a given raise site happens to use today.

    spec: spec/API.md §OAuth browser-redirect contract — "`302` to
    `<ui>/oauth-error?error=<code>` for the codes listed below"; "No failure on either
    route sets a cookie."
    """
    from src.api.routers.auth import _oauth_error_redirect

    exc = DataSpokeError("refused")
    exc.error_code = code

    with structlog.testing.capture_logs() as logs:
        response = _oauth_error_redirect(_request(), _settings("https://app.example.com/"), exc)

    assert response.status_code == 302, (
        "every outcome of these browser-navigation handlers is a 302 per spec/API.md "
        "§OAuth browser-redirect contract"
    )
    assert (
        response.headers["location"] == f"https://app.example.com/oauth-error?error={code}"
    )
    assert "set-cookie" not in response.headers, (
        "spec/API.md §OAuth browser-redirect contract: 'No failure on either route sets "
        "a cookie.'"
    )
    # Backstop: the capture must be non-empty, or the level assertion below proves
    # nothing about a logger that never emitted.
    #
    # Invariant (no spec §): spec/API.md §OAuth browser-redirect contract fixes ERROR for
    # the *uncatalogued* case only, and says nothing about whether or at what level a
    # catalogued refusal is logged. WARNING is asserted here as the contrast that makes
    # the spec'd ERROR rule observable — if both levels were ERROR, the uncatalogued-case
    # assertions below would pass on a logger that had stopped distinguishing them.
    assert logs, "nothing was captured — the level assertion below would be vacuous"
    assert [entry["log_level"] for entry in logs] == ["warning"], (
        "a catalogued refusal is ordinary user error and must stay distinguishable from "
        "the uncatalogued case, which spec/API.md §OAuth browser-redirect contract fixes "
        "at ERROR"
    )


def test_concrete_not_configured_exception_reaches_the_page() -> None:
    """``OAuthNotConfiguredError`` — the one code both routes can raise — flows through.

    The parametrized test above drives the base class with the code set explicitly; this
    one proves a real exception class raised by the handlers resolves the same way, so a
    class whose ``error_code`` default drifted would fail here.

    spec: spec/API.md §OAuth browser-redirect contract — "`/auth/google/login` | OAuth
    not configured | `302` to `<ui>/oauth-error?error=OAUTH_NOT_CONFIGURED`".
    """
    from src.api.routers.auth import _oauth_error_redirect

    response = _oauth_error_redirect(
        _request("/api/v1/auth/google/login"),
        _settings("https://app.example.com/"),
        OAuthNotConfiguredError("Google OAuth not configured."),
    )

    assert response.status_code == 302
    assert (
        response.headers["location"]
        == "https://app.example.com/oauth-error?error=OAUTH_NOT_CONFIGURED"
    )


def test_uncatalogued_dataspoke_error_redirects_without_an_error_parameter() -> None:
    """A ``DataSpokeError`` outside the five is not passed off as user-facing copy.

    spec: spec/API.md §OAuth browser-redirect contract — "Any other failure on **either**
    route — an uncatalogued `DataSpokeError`, or an exception outside the error taxonomy
    — redirects to `<ui>/oauth-error` with no `error` parameter and is logged at ERROR".
    """
    from src.api.routers.auth import _oauth_error_redirect

    exc = DataSpokeError("redis is down")
    exc.error_code = "STORAGE_UNAVAILABLE"

    with structlog.testing.capture_logs() as logs:
        response = _oauth_error_redirect(_request(), _settings("https://app.example.com/"), exc)

    assert response.status_code == 302
    assert response.headers["location"] == "https://app.example.com/oauth-error", (
        "an uncatalogued code must not be forwarded as ?error= — the page falls back to "
        "generic wording per spec/API.md §OAuth browser-redirect contract"
    )
    assert "STORAGE_UNAVAILABLE" not in response.headers["location"]
    assert "set-cookie" not in response.headers
    assert logs, "the failure must be logged, or the ERROR-level rule is unobservable"
    assert [entry["log_level"] for entry in logs] == ["error"], (
        "spec/API.md §OAuth browser-redirect contract: an uncatalogued failure 'is "
        "logged at ERROR'"
    )


def test_exception_outside_the_taxonomy_redirects_without_an_error_parameter() -> None:
    """A non-``DataSpokeError`` behaves identically — 302, no code, logged at ERROR.

    spec: spec/API.md §OAuth browser-redirect contract — "an exception outside the error
    taxonomy — redirects to `<ui>/oauth-error` with no `error` parameter and is logged at
    ERROR".
    """
    from src.api.routers.auth import _oauth_error_redirect

    with structlog.testing.capture_logs() as logs:
        response = _oauth_error_redirect(
            _request(),
            _settings("/"),
            RuntimeError("connection reset by peer"),
        )

    assert response.status_code == 302
    assert response.headers["location"] == "/oauth-error", (
        "the bare `/` default degrades to the relative location per spec/API.md §OAuth "
        "browser-redirect contract"
    )
    assert "set-cookie" not in response.headers
    assert logs, "the failure must be logged, or the ERROR-level rule is unobservable"
    assert [entry["log_level"] for entry in logs] == ["error"]


def test_failure_log_withholds_the_exception_message() -> None:
    """The logged line carries the code or class name, never the exception's message.

    Invariant (no spec §): no section of spec/ states this rule. It is a regression guard
    for a property the review stage won — the failure log names the code (or the exception
    class) and nothing else, because an exception message on this path can carry the
    authenticating identity or a driver's bound parameters. Keeping it asserted here
    prevents a future ``exc_info=True`` from silently reinstating the leak; making it a
    first-class contract would need a one-sentence addition to spec/feature/AUTH.md
    §Callback failure surface, which is a spec-stage edit.

    The absence assertion is meaningful because the message is injected by this test.
    """
    from src.api.routers.auth import _oauth_error_redirect

    secret = "person@imazon.example.com"

    with structlog.testing.capture_logs() as logs:
        _oauth_error_redirect(_request(), _settings("/"), RuntimeError(secret))

    assert logs, "nothing was captured — the absence assertion below would be vacuous"
    rendered = repr(logs)
    assert secret not in rendered, (
        f"the exception message reached the log line: {rendered}"
    )
    assert "RuntimeError" in rendered, "the exception class name is what monitoring gets"


# ── handler outcome: the login route with OAuth unconfigured ─────────────────


@pytest.mark.asyncio
async def test_login_route_redirects_when_oauth_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GET /auth/google/login`` answers 302 (not 503) when OAuth is unconfigured.

    spec: spec/API.md §OAuth browser-redirect contract — "`/auth/google/login` | OAuth
    not configured | `302` to `<ui>/oauth-error?error=OAUTH_NOT_CONFIGURED`".
    """
    from src.api.middleware import rate_limit as _rate_limit
    from src.api.routers import auth as auth_router
    from src.shared import settings as _settings_module

    # The handler is wrapped by the fail-closed auth limiter, which needs live storage;
    # its own outcomes are asserted in the limiter-plane tests below.
    monkeypatch.setattr(_rate_limit.auth_limiter, "enabled", False)
    monkeypatch.setattr(
        _settings_module.settings, "oauth_post_login_redirect", "https://app.example.com/"
    )

    with patch("src.api.routers.auth.oauth_google.is_configured", return_value=False):
        response = await auth_router.get_google_login(request=_request("/api/v1/auth/google/login"))

    assert response.status_code == 302
    assert (
        response.headers["location"]
        == "https://app.example.com/oauth-error?error=OAUTH_NOT_CONFIGURED"
    )
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_login_route_redirects_to_google_when_oauth_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GET /auth/google/login`` hands a configured install to Google, not to the error page.

    The sibling tests all assert the *failure* half of this route, so without this one a
    handler that sent every sign-in attempt to ``/oauth-error?error=OAUTH_NOT_CONFIGURED``
    — Google login broken outright — passes the entire suite. That is the first row of the
    contract table, and it is the row a user hits on every successful sign-in.

    spec: spec/API.md §OAuth browser-redirect contract — "`/auth/google/login` | OAuth
    configured | `302` to the Google consent screen, state cookie set".
    """
    from src.api.middleware import rate_limit as _rate_limit
    from src.api.routers import auth as auth_router

    monkeypatch.setattr(_rate_limit.auth_limiter, "enabled", False)

    consent_redirect = RedirectResponse(
        "https://accounts.google.com/o/oauth2/auth", status_code=302
    )
    client = MagicMock()
    client.google.authorize_redirect = AsyncMock(return_value=consent_redirect)

    request = _request("/api/v1/auth/google/login")
    # `authorize_redirect` needs the callback's absolute URL, which the handler builds
    # with `request.url_for` — that requires a router in the ASGI scope this bare Request
    # does not carry.
    with (
        patch("src.api.routers.auth.oauth_google.is_configured", return_value=True),
        patch("src.api.routers.auth.oauth_google.build_oauth_client", return_value=client),
        patch.object(
            Request,
            "url_for",
            return_value="https://api.example.com/api/v1/auth/google/callback",
        ),
    ):
        response = await auth_router.get_google_login(request=request)

    assert response.status_code == 302
    assert response.headers["location"] == "https://accounts.google.com/o/oauth2/auth"
    assert "/oauth-error" not in response.headers["location"], (
        "a configured install must reach Google, not the error page"
    )
    client.google.authorize_redirect.assert_awaited_once()


# ── handler outcome: an exception outside the taxonomy, at the route boundary ─
#
# The helper-level tests above prove `_oauth_error_redirect` renders a non-DataSpokeError
# correctly; these prove the two route bodies actually *route* one into it. The rule in
# spec/API.md §OAuth browser-redirect contract is stated of the routes ("Any other failure
# on **either** route … redirects"), and a route that caught only `DataSpokeError` would
# satisfy every helper-level test while letting an authlib discovery failure or a raw
# DBAPIError out as the 500 JSON envelope this feature exists to prevent.
#
# The failure is injected at `oauth_google.is_configured`, the first call in both bodies,
# because it is inside the try block on each and raises before any OAuth or DB work.


@pytest.mark.asyncio
async def test_login_route_catches_an_exception_outside_the_taxonomy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-``DataSpokeError`` escaping the login body still becomes a bare 302.

    spec: spec/API.md §OAuth browser-redirect contract — "Any other failure on **either**
    route — an uncatalogued `DataSpokeError`, or an exception outside the error taxonomy —
    redirects to `<ui>/oauth-error` with no `error` parameter and is logged at ERROR".
    """
    from src.api.middleware import rate_limit as _rate_limit
    from src.api.routers import auth as auth_router
    from src.shared import settings as _settings_module

    monkeypatch.setattr(_rate_limit.auth_limiter, "enabled", False)
    monkeypatch.setattr(
        _settings_module.settings, "oauth_post_login_redirect", "https://app.example.com/"
    )

    with patch(
        "src.api.routers.auth.oauth_google.is_configured",
        side_effect=RuntimeError("OIDC discovery document unreachable"),
    ):
        response = await auth_router.get_google_login(request=_request("/api/v1/auth/google/login"))

    assert response.status_code == 302, (
        "an exception outside the taxonomy must not surface as the 500 JSON envelope — "
        "spec/API.md §OAuth browser-redirect contract"
    )
    assert response.headers["location"] == "https://app.example.com/oauth-error", (
        "the uncatalogued case carries no `error` parameter"
    )
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_callback_route_catches_an_exception_outside_the_taxonomy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The callback behaves identically, and discards the uncommitted transaction.

    spec: spec/API.md §OAuth browser-redirect contract — "Any other failure on **either**
    route … redirects to `<ui>/oauth-error` with no `error` parameter"; "Nothing is
    committed when the failure is raised before the callback's bind commits."
    """
    from src.api.middleware import rate_limit as _rate_limit
    from src.api.routers import auth as auth_router
    from src.shared import settings as _settings_module

    monkeypatch.setattr(_rate_limit.auth_limiter, "enabled", False)
    monkeypatch.setattr(
        _settings_module.settings, "oauth_post_login_redirect", "https://app.example.com/"
    )

    db = AsyncMock()
    with patch(
        "src.api.routers.auth.oauth_google.is_configured",
        side_effect=RuntimeError("connection reset by peer"),
    ):
        response = await auth_router.get_google_callback(
            request=_request("/api/v1/auth/google/callback"), db=db
        )

    assert response.status_code == 302
    assert response.headers["location"] == "https://app.example.com/oauth-error"
    assert "set-cookie" not in response.headers
    db.commit.assert_not_awaited()
    assert db.rollback.await_count >= 1, (
        "the failure precedes the bind commit, so nothing may be left pending on the "
        "session per spec/API.md §OAuth browser-redirect contract"
    )


# ── handler outcome: the callback success row of the contract table ──────────


@pytest.mark.asyncio
async def test_callback_success_redirects_to_the_configured_target_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Success uses the configured value **verbatim** — path included — and sets the cookie.

    The asymmetry with the error location is deliberate and is what this test guards: the
    error page discards any path component and appends ``/oauth-error``, while success
    keeps the configured target whole. Both derive from the same setting, so a refactor
    that unifies them would silently send every sign-in to the wrong page. The configured
    value here therefore carries a path that the error derivation would strip.

    spec: spec/API.md §OAuth browser-redirect contract, contract table — "`/auth/google/
    callback` | Success | `302` to the configured post-login redirect target
    (`DATASPOKE_OAUTH_POST_LOGIN_REDIRECT`) verbatim, refresh cookie set".
    spec: spec/API.md §OAuth browser-redirect contract — "the bind necessarily commits
    before the refresh token is issued, so that the token carries the post-reset
    `session_epoch`".
    """
    from src.api.middleware import rate_limit as _rate_limit
    from src.api.routers import auth as auth_router
    from src.shared import settings as _settings_module

    configured_target = "https://app.example.com/governance/dashboard"
    monkeypatch.setattr(_rate_limit.auth_limiter, "enabled", False)
    monkeypatch.setattr(
        _settings_module.settings, "oauth_post_login_redirect", configured_target
    )

    google_client = MagicMock()
    google_client.google.authorize_access_token = AsyncMock(
        return_value={
            "userinfo": {
                "sub": "google-sub-1",
                "email": "engineer@imazon.example.com",
                "name": "Imazon Engineer",
                "email_verified": True,
            }
        }
    )
    user = MagicMock(id="user-1", session_epoch=7)

    # One parent recorder, so the commit-before-issue ordering is observable rather than
    # inferred from two independent call counts.
    recorder = MagicMock()
    db = AsyncMock()
    db.commit.side_effect = lambda: recorder.commit()

    def _issue_refresh_token(*args: Any) -> str:
        recorder.issue_refresh_token(*args)
        return "refresh-token-value"

    with (
        patch("src.api.routers.auth.oauth_google.is_configured", return_value=True),
        patch(
            "src.api.routers.auth.oauth_google.build_oauth_client", return_value=google_client
        ),
        patch(
            "src.api.routers.auth.oauth_google.resolve_or_create_user",
            AsyncMock(return_value=user),
        ),
        patch(
            "src.api.routers.auth._tokens.issue_refresh_token",
            side_effect=_issue_refresh_token,
        ),
    ):
        response = await auth_router.get_google_callback(
            request=_request("/api/v1/auth/google/callback"), db=db
        )

    assert response.status_code == 302
    assert response.headers["location"] == configured_target, (
        "the success target is the configured value verbatim — its path component is "
        "kept, unlike the /oauth-error derivation which discards it"
    )
    assert "/oauth-error" not in response.headers["location"]

    set_cookie = response.headers.get("set-cookie", "")
    assert "refresh_token=refresh-token-value" in set_cookie, (
        "the contract table's Success row sets the refresh cookie; got: " + repr(set_cookie)
    )
    assert "httponly" in set_cookie.lower()

    assert recorder.mock_calls == [
        call.commit(),
        call.issue_refresh_token(user.id, user.session_epoch),
    ], (
        "the bind must commit before the refresh token is issued, so the token carries "
        "the post-reset session_epoch"
    )


# ── limiter plane: the stated exception to the always-302 rule ───────────────


class _StubLimit:
    """Minimal stand-in for a slowapi ``Limit`` — enough to build ``RateLimitExceeded``."""

    error_message = None
    limit = "10 per 1 minute"


@pytest.mark.parametrize(
    "endpoint_name",
    ["get_google_login", "get_google_callback"],
)
def test_google_routes_are_charged_on_the_fail_closed_auth_limiter(endpoint_name: str) -> None:
    """Both routes are registered on the auth limiter and exempt from the default plane.

    spec: spec/API.md §Middleware Stack — "The credential-accepting and
    credential-issuing auth routes — `/auth/register`, `/auth/token`, the password-reset
    pair, and `/auth/google/login` + `/auth/google/callback` … are governed by a
    **separate fail-closed limiter** … they are charged on that limiter *instead of* the
    default budget, not in addition to it."
    """
    from slowapi.middleware import _should_exempt

    from src.api.middleware.rate_limit import _AUTH_LIMITED_ENDPOINTS, _endpoint_name, limiter
    from src.api.routers import auth as auth_router

    handler = getattr(auth_router, endpoint_name)

    assert _endpoint_name(handler) in _AUTH_LIMITED_ENDPOINTS, (
        f"{endpoint_name} is not registered on the fail-closed auth limiter"
    )
    assert _should_exempt(limiter, handler), (
        f"{endpoint_name} is still charged on the default limiter as well — spec/API.md "
        f"§Middleware Stack says 'instead of', not 'in addition to'"
    )
    # Backstop: a route that is *not* on the auth plane must fail both checks, so the
    # assertions above cannot pass by testing something universally true.
    assert _endpoint_name(auth_router.get_me) not in _AUTH_LIMITED_ENDPOINTS
    assert not _should_exempt(limiter, auth_router.get_me)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint_name,path",
    [
        ("get_google_login", "/api/v1/auth/google/login"),
        ("get_google_callback", "/api/v1/auth/google/callback"),
    ],
)
async def test_limiter_storage_outage_answers_storage_unavailable_before_the_handler(
    endpoint_name: str, path: str
) -> None:
    """A limiter-storage outage is a ``503 STORAGE_UNAVAILABLE``, never a redirect.

    spec: spec/API.md §OAuth browser-redirect contract — "The middleware and limiter
    plane is unaffected: both routes sit on the fail-closed auth limiter …, and a request
    rejected there never reaches the handler — it still answers with the envelope, `429`
    when the caller is over budget and `503 STORAGE_UNAVAILABLE` when that limiter's
    storage is unreachable."
    """
    from redis.exceptions import ConnectionError as RedisConnectionError

    from src.api.middleware import rate_limit as _rate_limit
    from src.api.routers import auth as auth_router

    handler = getattr(auth_router, endpoint_name)
    kwargs: dict[str, Any] = {"request": _request(path)}
    if endpoint_name == "get_google_callback":
        kwargs["db"] = MagicMock()

    def _storage_down(*args: Any, **kwargs: Any) -> None:
        raise RedisConnectionError("Error connecting to redis")

    body_entered = MagicMock(return_value=False)
    try:
        with (
            patch.object(_rate_limit.auth_limiter, "enabled", True),
            patch.object(_rate_limit.auth_limiter, "_check_request_limit", _storage_down),
            patch("src.api.routers.auth.oauth_google.is_configured", body_entered),
        ):
            with pytest.raises(StorageUnavailableError) as excinfo:
                await handler(**kwargs)
    finally:
        # `auth_route_limit` records a global fast-deny window on a storage failure.
        _rate_limit.reset_auth_storage_cooldown()

    assert excinfo.value.error_code == "STORAGE_UNAVAILABLE"
    body_entered.assert_not_called()
    assert not _rate_limit._auth_storage_in_cooldown(), (
        "the module-global fast-deny window must be cleared, or later tests inherit it"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint_name,path",
    [
        ("get_google_login", "/api/v1/auth/google/login"),
        ("get_google_callback", "/api/v1/auth/google/callback"),
    ],
)
async def test_over_budget_raises_rate_limit_exceeded_before_the_handler(
    endpoint_name: str, path: str
) -> None:
    """Over budget the limiter's rejection propagates — the handler never runs.

    Propagating ``RateLimitExceeded`` is what lets the app's registered handler render
    the standard ``429 RATE_LIMIT_EXCEEDED`` envelope; swallowing it into a redirect
    would put the rejection on the page instead.

    spec: spec/API.md §OAuth browser-redirect contract — "a request rejected there never
    reaches the handler — it still answers with the envelope, `429` when the caller is
    over budget".
    """
    from slowapi.errors import RateLimitExceeded

    from src.api.middleware import rate_limit as _rate_limit
    from src.api.routers import auth as auth_router

    handler = getattr(auth_router, endpoint_name)
    kwargs: dict[str, Any] = {"request": _request(path)}
    if endpoint_name == "get_google_callback":
        kwargs["db"] = MagicMock()

    def _over_budget(*args: Any, **kwargs: Any) -> None:
        raise RateLimitExceeded(_StubLimit())  # type: ignore[arg-type]  # stub stands in for slowapi's Limit.

    body_entered = MagicMock(return_value=False)
    with (
        patch.object(_rate_limit.auth_limiter, "enabled", True),
        patch.object(_rate_limit.auth_limiter, "_check_request_limit", _over_budget),
        patch("src.api.routers.auth.oauth_google.is_configured", body_entered),
    ):
        with pytest.raises(RateLimitExceeded) as excinfo:
            await handler(**kwargs)

    assert excinfo.value.status_code == 429
    body_entered.assert_not_called()
