"""Spot integration test: the Google routes answer failures with a 302, not an envelope.

Concerns covered:
- `GET /auth/google/login` with OAuth unconfigured → `302`
  `<ui>/oauth-error?error=OAUTH_NOT_CONFIGURED`.
- `GET /auth/google/callback` with OAuth unconfigured → the same.
- `GET /auth/google/callback` with OAuth configured and no session state → `302`
  `<ui>/oauth-error?error=OAUTH_STATE_MISMATCH` (authlib rejects the state before any
  call reaches Google, so no consent round trip is involved).

Both routes are full-page browser navigations, so every outcome their handlers produce
is a redirect. Which code is reachable over REST depends on the install: an unconfigured
one can only produce `OAUTH_NOT_CONFIGURED`, a configured one can only produce
`OAUTH_STATE_MISMATCH`. The guards below are therefore **symmetric** — exactly one of the
two groups runs per dev-env configuration, and each names the precondition it needs and
how to supply it, rather than reporting an unrun contract as a pass.

`httpx` does not follow redirects by default and is deliberately left that way here: the
target is the frontend origin, which need not be deployed for this contract to hold.

Rate-limit note: both routes sit on the fail-closed auth limiter at a 10/minute budget
each. This module issues at most two `login` calls (the shared probe plus one test) and
two `callback` calls, one of each pair being skipped in either configuration.

spec: spec/API.md §OAuth browser-redirect contract — "`/auth/google/login` | OAuth not
configured | `302` to `<ui>/oauth-error?error=OAUTH_NOT_CONFIGURED`"; "`302` to
`<ui>/oauth-error?error=<code>` for the codes listed below"; the location is "the origin
of the configured post-login redirect target … plus the absolute path `/oauth-error`";
"No failure on either route sets a cookie."
spec: spec/feature/AUTH.md §Callback failure surface — "**Every outcome their handlers
produce is therefore a 302, never the JSON error envelope**".
spec: spec/feature/AUTH.md §Failure Modes — "Google OAuth state mismatch on callback …
302 to `/oauth-error?error=OAUTH_STATE_MISMATCH`".
"""

import functools
import os
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

#: Path of the public UI page that renders an OAuth failure (spec/API.md §OAuth
#: browser-redirect contract; spec/feature/FRONTEND_BASIC.md §OAuth error page).
OAUTH_ERROR_PATH = "/oauth-error"

_CONFIGURED_SKIP = (
    "this dev install provisions Google OAuth credentials, so the "
    "OAUTH_NOT_CONFIGURED precondition cannot be established. Clear "
    "DATASPOKE_GOOGLE_OAUTH_CLIENT_ID / _SECRET and DATASPOKE_OAUTH_STATE_SECRET on the "
    "API deployment to run this test."
)

_UNCONFIGURED_SKIP = (
    "this dev install has no Google OAuth credentials, so the callback refuses with "
    "OAUTH_NOT_CONFIGURED before it ever validates state. Set "
    "DATASPOKE_GOOGLE_OAUTH_CLIENT_ID / _SECRET and DATASPOKE_OAUTH_STATE_SECRET on the "
    "API deployment to run this test."
)


@functools.lru_cache(maxsize=1)
def _login_route_location() -> str:
    """`Location` of one un-followed `GET /auth/google/login`, probed once per session.

    Probed synchronously so the guards can run as the first statement of a test body.
    A non-302 is a contract violation rather than an absent precondition, so it fails
    here instead of skipping the module.
    """
    domain = os.environ["DATASPOKE_KUBE_INGRESS_DOMAIN"]
    resp = httpx.get(
        f"http://api.{domain}/api/v1/auth/google/login",
        follow_redirects=False,
        timeout=15.0,
    )
    assert resp.status_code == 302, (
        "GET /auth/google/login is a browser-navigation route: every outcome its handler "
        "produces is a 302 per spec/API.md §OAuth browser-redirect contract, got "
        f"{resp.status_code}: {resp.text}"
    )
    return resp.headers.get("location", "")


def _oauth_is_configured() -> bool:
    """True when the login route redirects to the consent screen rather than the page."""
    return urlsplit(_login_route_location()).path != OAUTH_ERROR_PATH


@pytest.mark.asyncio
async def test_google_login_redirects_to_the_error_page_when_oauth_is_not_configured(
    api_client: httpx.AsyncClient,
) -> None:
    """GET /auth/google/login → 302 `<ui>/oauth-error?error=OAUTH_NOT_CONFIGURED`.

    spec: spec/API.md §OAuth browser-redirect contract — the contract table's
    "`/auth/google/login` | OAuth not configured" row.
    """
    if _oauth_is_configured():
        pytest.skip(_CONFIGURED_SKIP)

    resp = await api_client.get("/api/v1/auth/google/login")

    assert resp.status_code == 302, (
        "every outcome of this browser-navigation handler is a 302 per spec/API.md "
        f"§OAuth browser-redirect contract, got {resp.status_code}: {resp.text}"
    )

    location = urlsplit(resp.headers["location"])
    assert location.path == OAUTH_ERROR_PATH, (
        "the location is the origin of the configured post-login redirect target plus "
        f"the absolute path {OAUTH_ERROR_PATH}; got {resp.headers['location']!r}"
    )
    assert parse_qs(location.query) == {"error": ["OAUTH_NOT_CONFIGURED"]}, (
        "the code is delivered as the sole ?error= value per spec/API.md §OAuth "
        f"browser-redirect contract; got {resp.headers['location']!r}"
    )
    assert "set-cookie" not in resp.headers, (
        "spec/API.md §OAuth browser-redirect contract: 'No failure on either route sets "
        "a cookie.'"
    )


@pytest.mark.asyncio
async def test_google_callback_redirects_to_the_error_page_when_oauth_is_not_configured(
    api_client: httpx.AsyncClient,
) -> None:
    """GET /auth/google/callback → 302 `<ui>/oauth-error?error=OAUTH_NOT_CONFIGURED`.

    The callback refuses before any token exchange, so the fabricated `code`/`state`
    below never reach Google.

    spec: spec/API.md §OAuth browser-redirect contract — `OAUTH_NOT_CONFIGURED` is
    raised by "`GET /auth/google/{login,callback}`" alike and delivered as a redirect.
    """
    if _oauth_is_configured():
        pytest.skip(_CONFIGURED_SKIP)

    resp = await api_client.get(
        "/api/v1/auth/google/callback",
        params={"code": "not-a-real-authorization-code", "state": "not-a-real-state"},
    )

    assert resp.status_code == 302, (
        "every outcome of this browser-navigation handler is a 302 per "
        f"spec/feature/AUTH.md §Callback failure surface, got {resp.status_code}: {resp.text}"
    )

    location = urlsplit(resp.headers["location"])
    assert location.path == OAUTH_ERROR_PATH, (
        f"expected the absolute path {OAUTH_ERROR_PATH}; got {resp.headers['location']!r}"
    )
    assert parse_qs(location.query) == {"error": ["OAUTH_NOT_CONFIGURED"]}, (
        f"expected ?error=OAUTH_NOT_CONFIGURED; got {resp.headers['location']!r}"
    )
    assert "set-cookie" not in resp.headers, (
        "spec/API.md §OAuth browser-redirect contract: 'No failure on either route sets "
        "a cookie.'"
    )


@pytest.mark.asyncio
async def test_google_callback_redirects_to_the_error_page_on_a_state_mismatch(
    api_client: httpx.AsyncClient,
) -> None:
    """GET /auth/google/callback with no session state → 302 `?error=OAUTH_STATE_MISMATCH`.

    The request below carries a `state` value that matches nothing in the (absent)
    session cookie, which is the shape of an expired or interrupted sign-in. authlib
    rejects it before any request reaches Google.

    spec: spec/feature/AUTH.md §Failure Modes — "Google OAuth state mismatch on callback
    | Callback aborts before token issuance. | 302 to
    `/oauth-error?error=OAUTH_STATE_MISMATCH`".
    spec: spec/API.md §OAuth browser-redirect contract — "Catalogued failure | `302` to
    `<ui>/oauth-error?error=<code>`".
    """
    if not _oauth_is_configured():
        pytest.skip(_UNCONFIGURED_SKIP)

    resp = await api_client.get(
        "/api/v1/auth/google/callback",
        params={"code": "not-a-real-authorization-code", "state": "state-with-no-session"},
    )

    assert resp.status_code == 302, (
        "a catalogued failure is delivered as a redirect, never an error envelope, per "
        f"spec/feature/AUTH.md §Callback failure surface, got {resp.status_code}: {resp.text}"
    )

    location = urlsplit(resp.headers["location"])
    assert location.path == OAUTH_ERROR_PATH, (
        f"expected the absolute path {OAUTH_ERROR_PATH}; got {resp.headers['location']!r}"
    )
    assert parse_qs(location.query) == {"error": ["OAUTH_STATE_MISMATCH"]}, (
        f"expected ?error=OAUTH_STATE_MISMATCH; got {resp.headers['location']!r}"
    )
    assert "set-cookie" not in resp.headers, (
        "spec/API.md §OAuth browser-redirect contract: 'No failure on either route sets "
        "a cookie.'"
    )
