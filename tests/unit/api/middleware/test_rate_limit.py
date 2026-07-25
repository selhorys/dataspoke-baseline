"""Unit tests for rate-limit middleware configuration and client-IP attribution.

Tests spec-mandated behavior at the configuration level (not ASGI exercise, which
would require a real Redis or in-memory SlowAPI that can be triggered in a unit test
context). Tests verify:
- The limiter is configured with a per-user key function (JWT sub → IP fallback).
- In-memory fallback is enabled (Redis outage must not crash the API).
- The per-minute limit string is derived from settings.rate_limit_per_minute.
- Distinct clients land in distinct buckets — the key varies with the client address
  the API observes.
- The chart actually supplies that trust list: ``config.trustedProxyIps`` is loopback-only
  by default, the ConfigMap binds it to uvicorn's ``FORWARDED_ALLOW_IPS``, and the API
  container pulls that ConfigMap. Tests that assert on the *shipped default* read it from
  ``helm-charts/dataspoke/values.yaml`` rather than restating it as a literal, so deleting
  or widening the chart configuration fails a test here.

spec: API.md §Middleware Stack — rate limiting uses a per-user key (JWT sub, falling
      back to client IP); Redis outage must not block requests
      (in_memory_fallback_enabled=True).
spec: feature/AUTH.md §Client-IP attribution for rate limiting — the observed address
      is the real client only if every hop preserves it; the API's own trust boundary
      is closed by default and per-client bucketing is opt-in.
"""

import asyncio
import re
from pathlib import Path
from typing import Any

import yaml
from starlette.requests import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from src.api.middleware.rate_limit import _get_user_key, limiter

_CHART_DIR = Path(__file__).resolve().parents[4] / "helm-charts" / "dataspoke"
_CHART_VALUES = _CHART_DIR / "values.yaml"
_CHART_CONFIGMAP = _CHART_DIR / "templates" / "configmap.yaml"
_CHART_API_DEPLOYMENT = _CHART_DIR / "templates" / "api-deployment.yaml"


# ── Test drivers ──────────────────────────────────────────────────────────────


def _shipped_trusted_proxy_ips() -> str:
    """Return the chart's shipped ``config.trustedProxyIps`` default.

    Read from ``helm-charts/dataspoke/values.yaml`` rather than hardcoded, so that a
    test asserting "the shipped default behaves thus" fails when the shipped default
    changes instead of silently continuing to assert about a value nobody deploys.
    """
    values = yaml.safe_load(_CHART_VALUES.read_text())
    config = values["config"]
    assert "trustedProxyIps" in config, (
        "config.trustedProxyIps is missing from the chart values — it is the only knob "
        "that opens the API's proxy trust boundary. spec: feature/AUTH.md §Client-IP "
        "attribution for rate limiting."
    )
    default = config["trustedProxyIps"]
    assert isinstance(default, str), (
        f"config.trustedProxyIps must be a string trust list; got {default!r}."
    )
    return default


def _key_behind_proxy_headers(
    *,
    trusted_hosts: str,
    peer_ip: str,
    headers: list[tuple[bytes, bytes]],
) -> str:
    """Return the rate-limit key a request gets after uvicorn's proxy-header handling.

    Wires the *real* ``uvicorn.middleware.proxy_headers.ProxyHeadersMiddleware`` in front
    of a minimal ASGI app that computes ``_get_user_key`` on the resulting request.

    uvicorn installs that middleware whenever ``proxy_headers=True``, which is the
    default (``uvicorn/config.py:207``); ``FORWARDED_ALLOW_IPS`` does not switch it on,
    it only supplies the trust list, defaulting to ``"127.0.0.1"``
    (``uvicorn/config.py:343-344``, applied at ``config.py:513-514``). That distinction is
    why a deployment can have the middleware installed and still discard every
    ``X-Forwarded-For`` it receives.

    ``trusted_hosts`` is the value ``config.trustedProxyIps`` ends up supplying; ``peer_ip``
    is the immediate TCP peer (the ingress-controller pod, for external traffic).

    The trust logic is uvicorn's own — this driver deliberately does not reimplement it.
    """
    captured: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        captured.append(_get_user_key(Request(scope)))

    async def receive() -> Any:  # pragma: no cover - never awaited
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Any) -> None:  # pragma: no cover - app sends nothing
        return None

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/auth/token",
        "raw_path": b"/api/v1/auth/token",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": (peer_ip, 54321),
        "server": ("10.4.7.7", 8002),
    }

    asyncio.run(ProxyHeadersMiddleware(app, trusted_hosts=trusted_hosts)(scope, receive, send))

    assert captured, "driver bug: the inner ASGI app never ran, so no key was computed"
    return captured[0]


# ── Configuration assertions ──────────────────────────────────────────────────


def test_limiter_has_in_memory_fallback_enabled() -> None:
    """Rate limiter must have in_memory_fallback_enabled=True to survive Redis outages.

    spec: API.md §Middleware — a transient Redis outage must not block every request.
    """
    # slowapi Limiter stores this flag on the storage backend
    # The attribute is accessible via limiter._storage_uri or the in_memory flag.
    # We verify the constructor flag is set rather than the internals.
    from slowapi import Limiter

    assert isinstance(limiter, Limiter), "limiter must be a slowapi.Limiter instance"
    # slowapi sets _in_memory_fallback_enabled on the limiter object
    assert getattr(limiter, "_in_memory_fallback_enabled", False) is True, (
        "Limiter must have in_memory_fallback_enabled=True so Redis outages do not "
        "block all requests. spec: API.md §Middleware."
    )


def test_rate_limit_per_minute_derived_from_settings() -> None:
    """default_limits must include a '/minute' limit from settings.rate_limit_per_minute.

    spec: API.md §Middleware — rate limit is configured per settings.

    SlowAPI wraps each limit string in a LimitGroup object; the raw limit string is
    stored on the ``_LimitGroup__limit_provider`` private attribute.
    """
    from src.shared.settings import settings

    default_limits = limiter._default_limits
    expected_fragment = f"{settings.rate_limit_per_minute}/minute"

    def _extract(lim) -> str:
        """Return the raw rate-string from a LimitGroup or stringifiable limit."""
        provider = getattr(lim, "_LimitGroup__limit_provider", None)
        if provider is not None:
            return str(provider)
        return str(lim)

    limit_strings = [_extract(lim) for lim in default_limits]
    assert any(expected_fragment in s for s in limit_strings), (
        f"default_limits must contain '{expected_fragment}'; got: {limit_strings}. "
        "spec: API.md §Middleware — per-minute limit derived from settings."
    )


# ── _get_user_key: per-user extraction ───────────────────────────────────────


def test_get_user_key_falls_back_to_ip_without_auth_header() -> None:
    """_get_user_key falls back to remote address when Authorization header is absent.

    spec: API.md §Middleware Stack — "The per-user key is the JWT `sub` claim when
    present, falling back to client IP." The key must *be* the client address, not
    merely some non-empty string: a constant would satisfy "non-empty" while collapsing
    every unauthenticated caller into one bucket.
    """
    from unittest.mock import MagicMock

    req = MagicMock(spec=Request)
    req.headers = {}
    req.client = MagicMock()
    req.client.host = "192.168.1.1"

    key = _get_user_key(req)
    assert key == "192.168.1.1", (
        f"Expected the client address '192.168.1.1' as key; got {key!r}. "
        "spec: API.md §Middleware Stack — falling back to client IP."
    )


def test_get_user_key_falls_back_to_ip_on_invalid_jwt() -> None:
    """_get_user_key falls back to IP when Bearer token is invalid.

    spec: API.md §Middleware Stack — graceful fallback to client IP on JWT decode
    failure. An unparseable token must not raise, and must not be allowed to act as a
    shared key for every holder of a malformed token.
    """
    from unittest.mock import MagicMock

    req = MagicMock(spec=Request)
    req.headers = {"authorization": "Bearer this-is-not-a-valid-jwt"}
    req.client = MagicMock()
    req.client.host = "10.0.0.5"

    key = _get_user_key(req)
    assert key == "10.0.0.5", (
        f"Expected the client address '10.0.0.5' as key; got {key!r}. "
        "spec: API.md §Middleware Stack — falling back to client IP."
    )


def test_get_user_key_extracts_sub_from_valid_jwt() -> None:
    """_get_user_key extracts sub claim from valid JWT.

    spec: API.md §Middleware Stack — rate-limit key is the JWT sub claim when the token
    is valid. In the new auth model, sub is the user UUID string.
    """
    import uuid

    from src.backend.auth.tokens import issue_access_token

    known_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    token, _ = issue_access_token(known_id, "alice@example.com", 0)

    from unittest.mock import MagicMock

    req = MagicMock(spec=Request)
    req.headers = {"authorization": f"Bearer {token}"}
    req.client = MagicMock()
    req.client.host = "10.0.0.5"

    key = _get_user_key(req)
    assert key == str(known_id), (
        f"Expected key='{known_id}', got {key!r}. "
        "spec: API.md §Middleware Stack — JWT sub (user UUID) used as rate-limit key."
    )


# ── Per-client bucketing: distinct clients must not share one bucket ──────────


def test_distinct_client_addresses_get_distinct_rate_limit_keys() -> None:
    """Two unauthenticated clients at different addresses land in different buckets.

    spec: API.md §Middleware Stack — "The per-user key is the JWT `sub` claim when
    present, falling back to client IP." A key that does not vary with the client IP
    is not a fallback to client IP: it collapses every unauthenticated caller into one
    bucket, so one attacker exhausts POST /auth/register and POST /auth/token for
    everybody (feature/AUTH.md §Client-IP attribution for rate limiting: "if it
    collapses to a single value, all unauthenticated traffic shares one bucket").

    No forwarded headers here — this pins the key function itself, independent of any
    proxy configuration.
    """
    key_alice = _key_behind_proxy_headers(
        trusted_hosts="127.0.0.1", peer_ip="203.0.113.10", headers=[]
    )
    key_bob = _key_behind_proxy_headers(
        trusted_hosts="127.0.0.1", peer_ip="198.51.100.7", headers=[]
    )
    key_alice_again = _key_behind_proxy_headers(
        trusted_hosts="127.0.0.1", peer_ip="203.0.113.10", headers=[]
    )

    assert key_alice == "203.0.113.10", (
        f"Unauthenticated key must be the observed client IP; got {key_alice!r}. "
        "spec: API.md §Middleware Stack."
    )
    assert key_bob == "198.51.100.7", (
        f"Unauthenticated key must be the observed client IP; got {key_bob!r}. "
        "spec: API.md §Middleware Stack."
    )
    assert key_alice != key_bob, (
        "Two clients at different addresses must get different rate-limit keys, "
        f"but both got {key_alice!r} — a single shared bucket. "
        "spec: API.md §Middleware Stack — falling back to client IP."
    )
    assert key_alice_again == key_alice, (
        "The same client address must map to the same bucket on every request "
        f"({key_alice!r} vs {key_alice_again!r}); otherwise the limit never bites. "
        "spec: API.md §Middleware Stack."
    )


def test_distinct_authenticated_users_from_one_address_get_distinct_keys() -> None:
    """Two users behind one NAT address are bucketed by JWT sub, not by the shared IP.

    spec: API.md §Middleware Stack — "The per-user key is the JWT `sub` claim when
    present, falling back to client IP." The sub takes precedence, so co-located users
    do not consume each other's budget.
    """
    import uuid

    from src.backend.auth.tokens import issue_access_token

    alice_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    bob_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    alice_token, _ = issue_access_token(alice_id, "alice@imazon.example", 0)
    bob_token, _ = issue_access_token(bob_id, "bob@imazon.example", 0)

    shared_office_ip = "203.0.113.10"
    key_alice = _key_behind_proxy_headers(
        trusted_hosts="127.0.0.1",
        peer_ip=shared_office_ip,
        headers=[(b"authorization", f"Bearer {alice_token}".encode())],
    )
    key_bob = _key_behind_proxy_headers(
        trusted_hosts="127.0.0.1",
        peer_ip=shared_office_ip,
        headers=[(b"authorization", f"Bearer {bob_token}".encode())],
    )

    assert key_alice == str(alice_id), f"Expected the JWT sub as key; got {key_alice!r}."
    assert key_bob == str(bob_id), f"Expected the JWT sub as key; got {key_bob!r}."
    assert key_alice != key_bob, (
        "Two authenticated users sharing one client IP must not share a bucket. "
        "spec: API.md §Middleware Stack — JWT sub claim when present."
    )
    assert shared_office_ip not in {key_alice, key_bob}, (
        "The IP fallback must not be used while a valid Bearer token is present. "
        "spec: API.md §Middleware Stack."
    )


# ── Trust list: which peer may name the client address ───────────────────────


def test_chart_default_trust_list_ignores_forwarded_for_from_a_non_loopback_peer() -> None:
    """Under the shipped default, a proxy's X-Forwarded-For is ignored.

    spec: feature/AUTH.md §Client-IP attribution for rate limiting — "The API's own
    trust boundary, which is closed by default … It defaults to loopback only,
    trusting no proxy at all, so per-client bucketing is opt-in and an unconfigured
    deployment buckets all unauthenticated traffic together."

    Drives the trust list actually shipped in values.yaml, not a restated literal.
    """
    shipped_default = _shipped_trusted_proxy_ips()

    key_forged_a = _key_behind_proxy_headers(
        trusted_hosts=shipped_default,
        peer_ip="10.4.1.9",  # the ingress-controller pod: untrusted under the default
        headers=[(b"x-forwarded-for", b"203.0.113.10")],
    )
    key_forged_b = _key_behind_proxy_headers(
        trusted_hosts=shipped_default,
        peer_ip="10.4.1.9",
        headers=[(b"x-forwarded-for", b"198.51.100.7")],
    )

    assert key_forged_a == "10.4.1.9", (
        "An untrusted peer's X-Forwarded-For must not be honoured; expected the peer "
        f"address '10.4.1.9', got {key_forged_a!r}. spec: feature/AUTH.md §Client-IP "
        "attribution for rate limiting."
    )
    assert key_forged_a == key_forged_b, (
        "Under the loopback-only default, changing X-Forwarded-For must not change the "
        f"bucket ({key_forged_a!r} vs {key_forged_b!r}) — otherwise anyone can mint a "
        "fresh bucket per request. spec: feature/AUTH.md §Client-IP attribution."
    )


def test_chart_default_trust_list_honours_forwarded_for_from_a_loopback_peer() -> None:
    """The shipped default trusts loopback — and only loopback — as a forwarding peer.

    spec: feature/AUTH.md §Client-IP attribution for rate limiting — "It defaults to
    loopback only."

    The companion to the test above: that one proves a non-loopback peer is refused,
    this one proves the default is not the degenerate "trust nothing at all" list. Both
    halves are needed — a broken trust list that honoured nobody would pass the refusal
    test on its own.
    """
    shipped_default = _shipped_trusted_proxy_ips()

    key = _key_behind_proxy_headers(
        trusted_hosts=shipped_default,
        peer_ip="127.0.0.1",  # e.g. a sidecar or a node-local probe
        headers=[(b"x-forwarded-for", b"203.0.113.10")],
    )

    assert key == "203.0.113.10", (
        "A loopback peer is inside the shipped trust list, so the address it forwards "
        f"must become the key; got {key!r}. spec: feature/AUTH.md §Client-IP attribution."
    )


def test_configured_trust_list_honours_forwarded_for_from_a_peer_inside_the_cidr() -> None:
    """Naming the ingress pod CIDR turns on real per-client bucketing.

    spec: feature/AUTH.md §Client-IP attribution for rate limiting — "The API's uvicorn
    server honours the forwarded headers only when the immediate peer is in the trusted
    list supplied by the `config.trustedProxyIps` chart value."

    This is the opt-in configuration documented in helm-charts/dataspoke/values.yaml
    (`"127.0.0.1,10.4.0.0/14"`): the ingress pod at 10.4.1.9 is inside the CIDR, so the
    address it forwards becomes the rate-limit key.
    """
    operator_trust_list = "127.0.0.1,10.4.0.0/14"

    key_alice = _key_behind_proxy_headers(
        trusted_hosts=operator_trust_list,
        peer_ip="10.4.1.9",
        headers=[(b"x-forwarded-for", b"203.0.113.10")],
    )
    key_bob = _key_behind_proxy_headers(
        trusted_hosts=operator_trust_list,
        peer_ip="10.4.1.9",
        headers=[(b"x-forwarded-for", b"198.51.100.7")],
    )

    assert key_alice == "203.0.113.10", (
        f"Expected the forwarded client address as key; got {key_alice!r}."
    )
    assert key_bob == "198.51.100.7", (
        f"Expected the forwarded client address as key; got {key_bob!r}."
    )
    assert key_alice != key_bob, (
        "With the ingress CIDR trusted, two external clients must get distinct buckets. "
        "spec: API.md §Middleware Stack — falling back to client IP."
    )
    assert "10.4.1.9" not in {key_alice, key_bob}, (
        "The ingress pod's own address must not remain the key once it is trusted — "
        "that is the single-bucket collapse this configuration exists to remove."
    )


def test_configured_trust_list_ignores_forwarded_for_from_a_peer_outside_the_cidr() -> None:
    """A peer outside the trusted CIDR cannot name the client, even when a CIDR is set.

    spec: feature/AUTH.md §Client-IP attribution for rate limiting — "Every entry is
    therefore a party permitted to *name* the client address, not merely to relay it."
    Membership of the list is the whole gate, so a non-member is refused.

    Seeds both sides of the predicate: the same forwarded header arrives once from
    inside the CIDR (honoured) and once from outside it (ignored).
    """
    operator_trust_list = "127.0.0.1,10.4.0.0/14"
    forwarded = [(b"x-forwarded-for", b"203.0.113.10")]

    key_from_inside = _key_behind_proxy_headers(
        trusted_hosts=operator_trust_list, peer_ip="10.4.1.9", headers=forwarded
    )
    key_from_outside = _key_behind_proxy_headers(
        trusted_hosts=operator_trust_list,
        peer_ip="192.0.2.50",  # outside 10.4.0.0/14 — e.g. a caller reaching the pod directly
        headers=forwarded,
    )

    assert key_from_inside == "203.0.113.10", (
        f"A peer inside the trusted CIDR must be honoured; got {key_from_inside!r}."
    )
    assert key_from_outside == "192.0.2.50", (
        "A peer outside the trusted CIDR must be keyed by its own address, not by the "
        f"header it sent; got {key_from_outside!r}. spec: feature/AUTH.md §Client-IP "
        "attribution for rate limiting."
    )


def test_chart_default_trust_list_denies_bucket_rotation_to_an_in_range_caller() -> None:
    """A rogue in-cluster pod cannot rotate its bucket under the shipped default.

    spec: feature/AUTH.md §Client-IP attribution for rate limiting — "a caller whose own
    source address falls inside the list can forge the header, rotate it per request, and
    mint a fresh bucket each time. The value names the deployment's ingress controller and
    nothing wider — a private-range envelope would admit every in-cluster pod."

    The assertion of record is on the *shipped* trust list from values.yaml: an arbitrary
    in-cluster pod stays pinned to its own address no matter what it forwards. The
    over-broad list is exercised first only as a control, proving the forgery scenario is
    capable of rotating a bucket at all — without it, a typo'd header name would make the
    refusal assertion pass for the wrong reason.
    """
    # An arbitrary in-cluster pod — not the ingress controller — inside 10.0.0.0/8.
    rogue_pod_ip = "10.9.9.9"
    forged_first = [(b"x-forwarded-for", b"203.0.113.10")]
    forged_second = [(b"x-forwarded-for", b"203.0.113.11")]

    # Control: under a private-range envelope the forgery works and the bucket rotates.
    control_a = _key_behind_proxy_headers(
        trusted_hosts="127.0.0.1,10.0.0.0/8", peer_ip=rogue_pod_ip, headers=forged_first
    )
    control_b = _key_behind_proxy_headers(
        trusted_hosts="127.0.0.1,10.0.0.0/8", peer_ip=rogue_pod_ip, headers=forged_second
    )
    assert control_a != control_b, (
        "Control failed: the forgery scenario could not rotate a bucket even with an "
        f"over-broad trust list ({control_a!r} vs {control_b!r}), so the refusal "
        "assertion below would prove nothing. Check the X-Forwarded-For header name."
    )

    # Assertion of record: the shipped default refuses that same forgery.
    shipped_default = _shipped_trusted_proxy_ips()
    shipped_a = _key_behind_proxy_headers(
        trusted_hosts=shipped_default, peer_ip=rogue_pod_ip, headers=forged_first
    )
    shipped_b = _key_behind_proxy_headers(
        trusted_hosts=shipped_default, peer_ip=rogue_pod_ip, headers=forged_second
    )

    assert shipped_a == shipped_b == rogue_pod_ip, (
        "Under the shipped config.trustedProxyIps default an in-cluster caller must stay "
        f"pinned to its own address ({shipped_a!r}, {shipped_b!r}); expected "
        f"{rogue_pod_ip!r}. A default that admits this pod hands it unlimited bucket "
        "rotation. spec: feature/AUTH.md §Client-IP attribution for rate limiting."
    )


def test_uvicorn_resolves_forwarded_chain_to_the_rightmost_untrusted_hop() -> None:
    """Dependency canary: uvicorn's X-Forwarded-For chain semantics are as documented.

    NOT a DataSpoke behavior test and not counted toward spec coverage — no DataSpoke
    code can make it fail. It guards a dependency assumption: pyproject.toml pins
    ``uvicorn[standard]>=0.49`` with no upper bound, and the operator guidance in
    helm-charts/dataspoke/values.yaml and helm-charts/README.md (how wide a trust list
    is safe, and why) is written against these semantics. A silent change in a future
    uvicorn release would invalidate that guidance while every behavioral test above
    still passed, so this fails loudly instead.

    Semantics under test (uvicorn/middleware/proxy_headers.py
    ``_TrustedHosts.get_trusted_client_address``): walk the chain right to left and take
    the first entry that is not itself trusted.
    """
    operator_trust_list = "127.0.0.1,10.4.0.0/14"

    key_all_internal_hops = _key_behind_proxy_headers(
        trusted_hosts=operator_trust_list,
        peer_ip="10.4.1.9",
        headers=[(b"x-forwarded-for", b"203.0.113.10, 10.4.2.2, 10.4.3.3")],
    )
    key_untrusted_hop_midchain = _key_behind_proxy_headers(
        trusted_hosts=operator_trust_list,
        peer_ip="10.4.1.9",
        headers=[(b"x-forwarded-for", b"203.0.113.10, 198.51.100.7, 10.4.2.2")],
    )

    assert key_all_internal_hops == "203.0.113.10", (
        "With every intermediate hop trusted, the leftmost (original client) entry is "
        f"the key; got {key_all_internal_hops!r}."
    )
    assert key_untrusted_hop_midchain == "198.51.100.7", (
        "Resolution walks right to left and stops at the first untrusted entry, so the "
        f"mid-chain hop wins over the leftmost entry; got {key_untrusted_hop_midchain!r}."
    )


def test_empty_trust_list_trusts_nothing() -> None:
    """An empty trust list trusts nobody, which is why the chart's fail guard rejects it.

    spec: feature/AUTH.md §Client-IP attribution for rate limiting — the trust boundary
    determines whether "it collapses to a single value, all unauthenticated traffic
    shares one bucket".

    templates/configmap.yaml fails the install on an empty config.trustedProxyIps,
    stating that empty "silently defeat[s] the rate limiter's client-IP attribution".
    This pins the behavior behind that rationale: with an empty list no peer at all can
    forward a client address, so every external client is bucketed by the ingress pod's
    own address.
    """
    from uvicorn.middleware.proxy_headers import _TrustedHosts

    trusted = _TrustedHosts("")

    assert trusted.always_trust is False, "An empty trust list must not mean trust-all."
    assert "127.0.0.1" not in trusted, "An empty trust list must not trust loopback."
    assert "10.4.1.9" not in trusted, "An empty trust list must not trust the ingress pod."

    key = _key_behind_proxy_headers(
        trusted_hosts="",
        peer_ip="10.4.1.9",
        headers=[(b"x-forwarded-for", b"203.0.113.10")],
    )
    assert key == "10.4.1.9", (
        "With no trusted proxy, every external client collapses onto the ingress pod's "
        f"address; got {key!r}. This is the attribution loss the chart's fail guard "
        "exists to prevent."
    )


# ── The chart must actually supply the trust list ────────────────────────────


def test_chart_default_trusted_proxy_ips_is_loopback_only() -> None:
    """The shipped default trusts loopback and nothing else.

    spec: feature/AUTH.md §Client-IP attribution for rate limiting — "The value names the
    deployment's ingress controller and nothing wider — a private-range envelope would
    admit every in-cluster pod and every VPN or peered-network caller able to reach the
    API pod. It defaults to loopback only, trusting no proxy at all."

    Asserts that proposition directly rather than the weaker "not `*` and not empty":
    the chart's own fail guard already blocks those two, and neither layer would catch a
    private-range default such as "127.0.0.1,172.16.0.0/12", which is precisely the
    envelope the spec forbids.
    """
    from uvicorn.middleware.proxy_headers import _TrustedHosts

    default = _shipped_trusted_proxy_ips()
    trusted = _TrustedHosts(default)

    assert trusted.always_trust is False, (
        f"config.trustedProxyIps default {default!r} parses to trust-all."
    )
    assert trusted.trusted_hosts and all(h.is_loopback for h in trusted.trusted_hosts), (
        f"config.trustedProxyIps default {default!r} must trust loopback and nothing else; "
        f"parsed hosts: {trusted.trusted_hosts}. spec: feature/AUTH.md §Client-IP "
        "attribution — 'defaults to loopback only, trusting no proxy at all'."
    )
    assert trusted.trusted_networks == set(), (
        f"config.trustedProxyIps default {default!r} trusts network range(s) "
        f"{trusted.trusted_networks}; a private-range envelope admits every in-cluster "
        "pod. spec: feature/AUTH.md §Client-IP attribution for rate limiting."
    )
    assert trusted.trusted_literals == set(), (
        f"config.trustedProxyIps default {default!r} contains non-IP literal(s) "
        f"{trusted.trusted_literals}, which uvicorn matches by exact string."
    )


def test_chart_configmap_binds_forwarded_allow_ips_to_trusted_proxy_ips() -> None:
    """The ConfigMap must supply config.trustedProxyIps to uvicorn as FORWARDED_ALLOW_IPS.

    spec: feature/AUTH.md §Client-IP attribution for rate limiting — uvicorn "honours the
    forwarded headers only when the immediate peer is in the trusted list supplied by the
    `config.trustedProxyIps` chart value."

    This is the supply link that makes the chart value reach the server, and it is the
    only functional artifact of the fix for issue #76. Every behavioral test above drives
    a trust list handed to the middleware in-process, so all of them stay green if this
    line is deleted — this test is what notices. Asserted as text, not by rendering: a
    unit test must not shell out to `helm`.
    """
    configmap = _CHART_CONFIGMAP.read_text()

    entries = re.findall(r"^\s*FORWARDED_ALLOW_IPS:\s*(.+?)\s*$", configmap, re.MULTILINE)
    assert len(entries) == 1, (
        f"templates/configmap.yaml must define FORWARDED_ALLOW_IPS exactly once; found "
        f"{len(entries)}: {entries}. Without it uvicorn falls back to its own default and "
        "the chart value is inert. spec: feature/AUTH.md §Client-IP attribution."
    )
    assert ".Values.config.trustedProxyIps" in entries[0], (
        f"FORWARDED_ALLOW_IPS must be bound to .Values.config.trustedProxyIps, not "
        f"hardcoded; got {entries[0]!r}. spec: feature/AUTH.md §Client-IP attribution — "
        "'the trusted list supplied by the `config.trustedProxyIps` chart value'."
    )

    # The render-time guard is the chart's own enforcement of the two values that
    # silently defeat client-IP attribution: "" trusts nobody (XFF discarded, one
    # global bucket) and "*" trusts everybody (any caller names its own bucket).
    guard = re.search(r"\{\{-?\s*fail\s.*?\}\}", configmap, re.S)
    assert guard is not None, (
        "templates/configmap.yaml must fail the render on an empty or '*' "
        "config.trustedProxyIps rather than shipping a silently-inert trust list. "
        "spec: feature/HELM_CHART.md — '`*` is never correct'."
    )
    guard_condition = configmap[: guard.start()].rsplit("{{", 1)[-1]
    for token in ("trustedProxyIps", "not ", '"*"'):
        assert token in guard_condition, (
            f"The fail guard's condition must cover {token!r}; got {guard_condition!r}. "
            "Both empty and '*' must abort the install."
        )


def test_chart_api_container_consumes_the_app_config_configmap() -> None:
    """The API container must pull the ConfigMap that carries FORWARDED_ALLOW_IPS.

    spec: feature/AUTH.md §Client-IP attribution for rate limiting — the trust list is
    "supplied by the `config.trustedProxyIps` chart value"; supply requires the env var
    to reach the uvicorn process, not merely to exist in a ConfigMap.

    Closes the last untested link in the chain: chart value → ConfigMap key → container
    env → uvicorn (`uvicorn/config.py:343-344`). Asserts the api container's configMapRef
    names the same resource the ConfigMap template declares, so renaming one without the
    other fails here.
    """
    configmap = _CHART_CONFIGMAP.read_text()
    deployment = _CHART_API_DEPLOYMENT.read_text()

    # Compare template expressions by semantic content, not byte-for-byte: Helm whitespace
    # is cosmetic, and a test that fails on a reformat gets deleted rather than fixed.
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", s.strip())

    declared = re.search(r"^kind: ConfigMap$.*?^\s*name:\s*(.+?)\s*$", configmap, re.M | re.S)
    assert declared is not None, "Could not find the ConfigMap's metadata.name in configmap.yaml."
    configmap_name = _norm(declared.group(1))

    # Restrict to the `containers:` block so an initContainer's ref cannot satisfy this.
    _, marker, containers_block = deployment.partition("\n      containers:\n")
    assert marker, "Could not locate the `containers:` block in api-deployment.yaml."
    api_container = containers_block.partition("- name: api\n")[2]
    assert api_container, "Could not locate the `api` container in api-deployment.yaml."
    # Bound the block at the next sibling container, so a later sidecar's configMapRef
    # cannot satisfy an assertion that is specifically about the `api` container.
    api_container = re.split(r"\n {8}- name: ", api_container)[0]

    referenced = [
        _norm(m)
        for m in re.findall(r"configMapRef:\s*\n\s*name:\s*(.+?)\s*$", api_container, re.MULTILINE)
    ]
    assert configmap_name in referenced, (
        f"The api container must mount the app-config ConfigMap ({configmap_name!r}) via "
        f"envFrom.configMapRef so FORWARDED_ALLOW_IPS reaches uvicorn; found refs: "
        f"{referenced}. spec: feature/AUTH.md §Client-IP attribution for rate limiting."
    )
