"""Frontend-call conformance: every API path ``src/frontend`` calls resolves to a real route.

The frontend is a *thin reference client*, so every URL it builds must land on a route the
API actually registers. Nothing else in the suite enforces that: `test_route_catalogue.py`
compares the app against ``spec/API.md`` and the frontend Vitest suite mocks ``fetch``, so a
call to an endpoint that no longer exists (or never did) is invisible to both. This module
closes that gap by extracting the path literals out of ``src/frontend/lib/api/*.ts`` and
matching each against the routes registered on the FastAPI app.

Spec: spec/feature/FRONTEND_BASIC.md (intro) — "This frontend is a thin reference client
over the routes catalogued in `API.md`; UI elements MUST trace to a real API surface —
invented features (settings APIs, streaming endpoints, score axes, recommendation panels)
are out of scope."
Spec: spec/feature/FRONTEND_BASIC.md §Stack — "The API client at
`src/frontend/lib/api/client.ts` prepends `/api/v1`" (hence the extracted literals carry no
``/api/v1`` prefix, matching :func:`normalise_path`'s stripped form).
Spec: spec/feature/FRONTEND_BASIC.md §Per-dataset page (`/data/[urn]`) — "It consumes only
the per-dataset `/spoke/common/data/{urn}/…` routes verbatim, no invented endpoints."
Spec: spec/TESTING.md §Assertion Discipline — "Author assertions so that a passing result is
only reachable when the spec'd behavior actually occurred."

**Scope and precision of the check.** The comparison is on *paths*, not on
``(method, path)``: a TypeScript call site carries its method in an options object that is
frequently built conditionally, so binding a method to a literal would require evaluating
the module. A path is therefore "resolved" when some registered route matches it
segment-wise. Path parameters are wildcards on **both** sides (see
:func:`_path_matches`), which makes the check conservative — it catches a path that reaches
no route at all, not a path that reaches the wrong route.

**Why only ``lib/api/``.** In that directory every literal beginning a URL *is* a request
path, which is what makes a purely textual extraction sound. Elsewhere in ``src/frontend``
it is not: the app-shell's navigation links are page routes that share the namespace
textually — ``/admin/conf``, ``/admin/users`` and ``/admin/peripherals`` are Next.js routes
under ``src/frontend/app/(app)/admin/``, and the last of those is deliberately *not* an API
route (the API exposes ``/admin/peripherals/datahub`` and ``/admin/peripherals/langfuse``).
Scanning them with the same rule would manufacture unresolved paths. The consequence is a
known limit rather than a hidden one: a request issued from outside ``lib/api/`` — today
``POST /auth/token/revoke`` in ``components/app-shell.tsx`` — is not covered here.

Unit-tier: reads ``.ts`` files as text and imports the app object. No dev environment,
network, or bundler (spec/TESTING.md §Unit Testing → Scope).
"""

from __future__ import annotations

from src.api.main import app

from ._api_md import REPO_ROOT, assert_drift_allowlist, normalise_path, registered_routes

# ── What is scanned ──────────────────────────────────────────────────────────
#
# spec/feature/FRONTEND_BASIC.md §Stack places the API client at
# `src/frontend/lib/api/client.ts`; its sibling modules in that directory are the hooks
# that build every request path.
FRONTEND_API_DIR = REPO_ROOT / "src" / "frontend" / "lib" / "api"

#: Test and mock modules are excluded: their literals are fixtures asserting on the paths
#: the production modules build, so counting them would double-count at best and, for
#: mocks, introduce paths no browser ever requests.
EXCLUDED_SUFFIXES: tuple[str, ...] = (".test.ts", ".mock.ts")

#: The route-namespace roots a DataSpoke request path can start with. Taken from
#: spec/API.md §Route Catalogue, whose subsections mount `/auth`, `/admin`, `/spoke/…` and
#: `/internal`. A literal that contains none of these is not a request path.
PATH_PREFIXES: tuple[str, ...] = ("/spoke", "/admin", "/auth", "/internal")

#: Every module in the scanned directory that builds at least one request path. Pinned by
#: name rather than derived from the glob, because the glob is the thing under test: a
#: module that is renamed, moved out of the directory, or silently skipped disappears from
#: a derived set without trace, taking its paths out of the comparison with it. Asserted as
#: an *exact* set, so adding a module here is a deliberate act.
PATH_BUILDING_MODULES: frozenset[str] = frozenset(
    {
        "admin.ts",
        "auth.ts",
        "client.ts",
        "data.ts",
        "datasets.ts",
        "governance.ts",
        "ingestion.ts",
        "metagen.ts",
        "ontogen.ts",
        "peripheral-links.ts",
        "validation.ts",
    }
)

#: Modules in the scanned directory that legitimately build no request path, and why.
#: Also asserted as an exact set, so one of these growing a request path fails until it is
#: promoted to :data:`PATH_BUILDING_MODULES` and its paths enter the comparison.
#:
#: * ``error-policy.ts`` classifies error envelopes; it issues no requests.
#: * ``types.ts`` declares request/response interfaces only.
PATHLESS_MODULES: frozenset[str] = frozenset({"error-policy.ts", "types.ts"})

# ── Known drift ──────────────────────────────────────────────────────────────
#
# Every extracted frontend path currently resolves to a registered route, so the allowlist
# is empty. It is declared (rather than omitted) so a future author records an unresolved
# path here — with the reason and the issue that resolves it — instead of loosening the
# assertion. `assert_drift_allowlist` fails on a stale entry too, so a path listed here
# that starts resolving again must be deleted from the list.
KNOWN_DRIFT_FRONTEND_PATH_UNRESOLVED: frozenset[str] = frozenset()

# ── Anti-vacuity bounds ──────────────────────────────────────────────────────
#
# Floors at the counts observed when this check was written. They bound *partial* loss of
# the extraction, not merely total loss: a scanner change that drops one shape of literal
# (say, every interpolated path) fails here rather than passing an emptier comparison.
# Raise them as the frontend grows; lower one only as a deliberate decision.
MIN_TOTAL_PATHS = 60
MIN_INTERPOLATED_PATHS = 25

#: Specific paths that must survive extraction, each pinning one literal *shape*. If any
#: goes missing the corresponding branch of the scanner has regressed, whatever the totals
#: above say.
ANCHOR_PATHS: frozenset[str] = frozenset(
    {
        # Plain quoted literal, no interpolation at all.
        "/auth/token",
        # Collection root: the whole path is literal and an appended query string follows.
        "/spoke/common/data",
        # Trailing `${qs ? `?${qs}` : ""}` — an appended query string, not a path segment,
        # and a *nested* template literal inside the interpolation.
        "/spoke/metagen/conf",
        # Interpolated segment in the middle, literal segments after it.
        "/spoke/ingestion/sources/{}/method/run",
        # Three interpolated segments in one path.
        "/spoke/common/data/{}/attr/metagen/item/{}/candidate/{}/method/review",
        # Two adjacent interpolations: `${kind}${qs}` — the first is a path segment, the
        # second is an appended query string.
        "/spoke/ontogen/result/{}",
        # Interpolated *leading* segment (`kind`) against three concrete backend routes.
        "/spoke/ontogen/result/{}/{}/method/review",
        # Interpolated base URL in front of the path: `${apiBase()}/auth/...`.
        "/auth/token/refresh",
    }
)

# The scanner reduces every ``${…}`` interpolation to this sentinel while it walks the
# source, so that an interpolation can still be told apart from surrounding literal text
# (a trailing one is an appended query string; one after a ``/`` is a path segment). It is
# a character that cannot occur in TypeScript source, so it can never collide with real
# path text. It becomes ``{}`` only once the path shape has been decided.
_INTERPOLATION = "\x00"


# ── TypeScript literal scanner ───────────────────────────────────────────────
#
# A regex cannot do this job. Paths are built as template literals whose `${…}`
# expressions contain nested braces and nested template literals — `` `/spoke/metagen/conf${qs
# ? `?${qs}` : ""}` `` — so a non-greedy `\$\{[^}]*\}` stops at the inner `}` and leaves
# `: ""}` glued onto the path. The scanner below tracks brace depth instead, and skips
# comments so that an apostrophe in prose ("the app's own") cannot be read as opening a
# string literal and desynchronise everything after it.


def _read_quoted(source: str, i: int) -> tuple[str, int]:
    """Read the ``'…'`` / ``"…"`` literal starting at ``source[i]``; return content and end."""
    quote = source[i]
    j = i + 1
    buf: list[str] = []
    while j < len(source):
        char = source[j]
        if char == "\\":
            buf.append(source[j + 1 : j + 2])
            j += 2
            continue
        if char == quote:
            return "".join(buf), j + 1
        if char == "\n":
            # Unterminated single-line literal: not valid TypeScript. Bail rather than
            # swallowing the rest of the file as string content.
            return "", j
        buf.append(char)
        j += 1
    return "".join(buf), j


def _read_template(source: str, i: int) -> tuple[str, int, list[str]]:
    """Read the template literal at ``source[i]``.

    Returns the content with each ``${…}`` replaced by :data:`_INTERPOLATION`, the index
    just past the closing backtick, and any string literals found *inside* the
    interpolations (a conditional expression may itself pick between two paths).
    """
    j = i + 1
    buf: list[str] = []
    nested: list[str] = []
    while j < len(source):
        char = source[j]
        if char == "\\":
            buf.append(source[j + 1 : j + 2])
            j += 2
            continue
        if char == "`":
            return "".join(buf), j + 1, nested
        if char == "$" and source[j + 1 : j + 2] == "{":
            end = _skip_interpolation(source, j + 1)
            nested.extend(string_literals(source[j + 2 : end - 1]))
            buf.append(_INTERPOLATION)
            j = end
            continue
        buf.append(char)
        j += 1
    return "".join(buf), j, nested


def _skip_interpolation(source: str, k: int) -> int:
    """Skip the balanced ``{…}`` block starting at ``source[k]``; return the index past it.

    Strings and nested template literals inside the block are consumed by their own
    readers, so a brace *inside* a string cannot unbalance the count.
    """
    depth = 0
    while k < len(source):
        char = source[k]
        if char in "\"'":
            _, k = _read_quoted(source, k)
            continue
        if char == "`":
            _, k, _ = _read_template(source, k)
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return k + 1
        k += 1
    return k


def string_literals(source: str) -> list[str]:
    """Every string/template literal in ``source``, interpolations reduced to a sentinel.

    Comments are skipped, so neither a documented route in a JSDoc block nor an apostrophe
    in prose is read as source.
    """
    found: list[str] = []
    i, n = 0, len(source)
    while i < n:
        char = source[i]
        if char == "/" and source[i + 1 : i + 2] == "/":
            newline = source.find("\n", i)
            i = n if newline < 0 else newline
        elif char == "/" and source[i + 1 : i + 2] == "*":
            end = source.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif char in "\"'":
            literal, i = _read_quoted(source, i)
            found.append(literal)
        elif char == "`":
            literal, i, nested = _read_template(source, i)
            found.append(literal)
            found.extend(nested)
        else:
            i += 1
    return found


def request_path(literal: str) -> str | None:
    """The request path a literal builds, or ``None`` if it is not one.

    A request path either opens the literal or follows an interpolated base
    (``` `${apiBase()}/auth/token/refresh` ```). Requiring one of those two positions is
    what keeps module specifiers out: ``"@/lib/auth/store"`` contains ``/auth`` but the
    prefix is preceded by ordinary text, so it is not a path.

    The query string is dropped (routes are identified by path), and a *trailing*
    interpolation that is not preceded by ``/`` is dropped as well — it is an appended
    query string such as ``${qs ? `?${qs}` : ""}``, not a path segment.
    """
    starts = [
        index
        for prefix in PATH_PREFIXES
        for index in [literal.find(prefix)]
        if index == 0 or (index > 0 and literal[index - 1] == _INTERPOLATION)
    ]
    if not starts:
        return None
    path = literal[min(starts) :].split("?")[0].split("#")[0]
    while path.endswith(_INTERPOLATION) and not path.endswith("/" + _INTERPOLATION):
        path = path[: -len(_INTERPOLATION)]
    return normalise_path(path.replace(_INTERPOLATION, "{}"))


def frontend_api_paths() -> dict[str, frozenset[str]]:
    """``{module filename: request paths it builds}`` for the scanned frontend modules."""
    modules: dict[str, frozenset[str]] = {}
    for source_file in sorted(FRONTEND_API_DIR.glob("*.ts")):
        if source_file.name.endswith(EXCLUDED_SUFFIXES):
            continue
        literals = string_literals(source_file.read_text(encoding="utf-8"))
        paths = {path for path in map(request_path, literals) if path is not None}
        modules[source_file.name] = frozenset(paths)
    return modules


def all_frontend_paths() -> frozenset[str]:
    """Every distinct request path the scanned frontend modules build."""
    return frozenset().union(*frontend_api_paths().values())


# ── Matching ─────────────────────────────────────────────────────────────────


def _path_matches(frontend_path: str, registered_path: str) -> bool:
    """Whether a frontend path can reach a registered route.

    ``{}`` is a wildcard on **both** sides. On the registered side it is the route's own
    path parameter; on the frontend side it is an interpolated expression, which is not
    always an id — ``/spoke/ontogen/result/${kind}/${id}/method/review`` interpolates the
    *kind*, and the backend registers that as three concrete routes (`node`, `edge`,
    `triple`). Treating the placeholder as a wildcard only on the registered side would
    read that legitimate call as unresolved.

    A wildcard never spans a ``/``: segment counts must be equal.
    """
    frontend_segments = frontend_path.split("/")
    registered_segments = registered_path.split("/")
    if len(frontend_segments) != len(registered_segments):
        return False
    return all(
        left == right or left == "{}" or right == "{}"
        for left, right in zip(frontend_segments, registered_segments, strict=True)
    )


def registered_paths() -> frozenset[str]:
    """Distinct paths registered on the app (methods dropped — see the module docstring)."""
    return frozenset(path for _, path in registered_routes(app))


def unresolved_frontend_paths() -> frozenset[str]:
    """Frontend paths that no registered route can serve."""
    known = registered_paths()
    return frozenset(
        path
        for path in all_frontend_paths()
        if not any(_path_matches(path, candidate) for candidate in known)
    )


# ── Tests ────────────────────────────────────────────────────────────────────


class TestLiteralScanner:
    """Direct tests of the scanner on synthetic sources.

    The conformance assertion consumes whatever this scanner produces, so a silent
    extraction bug would make it pass while comparing the wrong thing (or nothing). These
    pin the shapes that actually appear in ``src/frontend/lib/api/*.ts``.
    """

    def test_plain_literal_is_extracted(self) -> None:
        assert request_path("/auth/token") == "/auth/token"

    def test_interpolated_segment_becomes_a_wildcard(self) -> None:
        (literal,) = [
            lit
            for lit in string_literals("const u = `/spoke/a/${encodeURIComponent(id)}/b`;")
            if lit.startswith("/spoke")
        ]
        assert request_path(literal) == "/spoke/a/{}/b"

    def test_nested_template_in_interpolation_does_not_leak(self) -> None:
        """The trap a non-greedy `\\$\\{[^}]*\\}` regex falls into.

        The inner ``` `?${qs}` ``` closes a brace before the outer interpolation ends, so a
        non-greedy match stops early and leaves ``: ""}`` glued onto the path.
        """
        source = 'return `/spoke/metagen/conf${qs ? `?${qs}` : ""}`;'
        literals = [lit for lit in string_literals(source) if lit.startswith("/spoke")]
        assert literals, f"the outer template was not read at all: {string_literals(source)!r}"
        assert request_path(literals[0]) == "/spoke/metagen/conf"

    def test_adjacent_interpolations_keep_the_segment_and_drop_the_query(self) -> None:
        source = 'return `/spoke/ontogen/result/${kind}${qs ? `?${qs}` : ""}`;'
        (literal,) = [lit for lit in string_literals(source) if lit.startswith("/spoke")]
        assert request_path(literal) == "/spoke/ontogen/result/{}"

    def test_literal_query_string_is_dropped(self) -> None:
        (literal,) = [
            lit
            for lit in string_literals("apiFetch(`/admin/users?offset=${offset}`);")
            if lit.startswith("/admin")
        ]
        assert request_path(literal) == "/admin/users"

    def test_interpolated_base_url_is_recognised(self) -> None:
        (literal,) = [
            lit
            for lit in string_literals("await fetch(`${apiBase()}/auth/token/refresh`);")
            if "/auth" in lit
        ]
        assert request_path(literal) == "/auth/token/refresh"

    def test_module_specifier_is_not_a_request_path(self) -> None:
        """``"@/lib/auth/store"`` contains ``/auth`` but is an import, not a URL."""
        assert request_path("@/lib/auth/store") is None

    def test_comments_are_not_scanned(self) -> None:
        """Two ways a comment could corrupt the scan, both must be inert.

        A JSDoc block documents routes in prose (``GET /spoke/documented``) — reading it
        would invent a path the code never requests. And an apostrophe in prose would open
        a bogus string literal that swallows the rest of the file, silently dropping every
        later path.
        """
        source = (
            "/** GET /spoke/documented — prose, not code. */\n"
            "// the app's own policy\n"
            'const url = "/spoke/real";\n'
        )
        paths = {path for path in map(request_path, string_literals(source)) if path}
        assert paths == {"/spoke/real"}, (
            f"expected only the code literal to be extracted, got {sorted(paths)}"
        )


class TestExtractionIsNotVacuous:
    """Backstops bounding *partial* loss of the extraction, not just total loss.

    Without these, a scanner that quietly stopped recognising a whole class of literal
    would leave the conformance assertion below comparing a handful of paths — and passing.
    """

    def test_total_path_count_holds_its_floor(self) -> None:
        paths = all_frontend_paths()
        assert len(paths) >= MIN_TOTAL_PATHS, (
            f"Only {len(paths)} frontend request paths extracted (floor "
            f"{MIN_TOTAL_PATHS}) — the scanner is losing literals, so the conformance "
            f"check below proves much less than it appears to. Extracted: {sorted(paths)}"
        )

    def test_interpolated_paths_are_a_substantial_share(self) -> None:
        """Most real paths carry an id. A scanner that dropped them would still hit the
        total floor if the literal-only paths were counted twice over, so bound this class
        of literal on its own."""
        interpolated = {path for path in all_frontend_paths() if "{}" in path}
        assert len(interpolated) >= MIN_INTERPOLATED_PATHS, (
            f"Only {len(interpolated)} extracted paths contain an interpolated segment "
            f"(floor {MIN_INTERPOLATED_PATHS}) — interpolation handling has regressed."
        )

    def test_every_declared_module_contributes_a_path(self) -> None:
        """Each module named in PATH_BUILDING_MODULES must yield at least one path.

        This is the backstop for *partial* loss. A module that stops contributing — moved
        out of the directory, renamed, excluded by a widened skip rule, or written in a
        literal shape the scanner no longer recognises — vanishes from the extraction
        without changing anything else, and the conformance assertion below would go on
        passing over the smaller set. Comparing against a pinned list rather than the glob
        is the point: a derived list shrinks silently along with the extraction.
        """
        modules = frontend_api_paths()
        assert modules, f"No modules scanned under {FRONTEND_API_DIR} — the directory moved."
        contributing = {name for name, paths in modules.items() if paths}
        assert contributing == PATH_BUILDING_MODULES, (
            f"Modules contributing request paths: {sorted(contributing)}; declared: "
            f"{sorted(PATH_BUILDING_MODULES)}. A missing module means its paths dropped out "
            f"of the comparison; an unexpected one must be added to PATH_BUILDING_MODULES."
        )

    def test_declared_pathless_modules_are_exactly_the_silent_ones(self) -> None:
        """A module that legitimately issues no request must be declared, and only those.

        Together with the test above this pins the whole scanned set: every file the glob
        returns is either a declared path builder or a declared pathless module, so a file
        appearing or disappearing cannot pass unnoticed.
        """
        modules = frontend_api_paths()
        silent = {name for name, paths in modules.items() if not paths}
        assert silent == PATHLESS_MODULES, (
            f"Modules building no request path: {sorted(silent)}; declared pathless: "
            f"{sorted(PATHLESS_MODULES)}. A newly-silent module means the scanner stopped "
            f"seeing it; a no-longer-silent module must move to PATH_BUILDING_MODULES."
        )

    def test_declared_modules_all_exist_on_disk(self) -> None:
        """A declared module naming a deleted file would be an assertion about nothing."""
        present = {source_file.name for source_file in FRONTEND_API_DIR.glob("*.ts")}
        missing = sorted((PATH_BUILDING_MODULES | PATHLESS_MODULES) - present)
        assert not missing, (
            f"Declared modules that do not exist under {FRONTEND_API_DIR}: {missing}. "
            f"Delete them from PATH_BUILDING_MODULES / PATHLESS_MODULES."
        )

    def test_anchor_paths_are_all_extracted(self) -> None:
        """Each anchor pins one literal shape; totals alone cannot catch a lost shape."""
        missing = sorted(ANCHOR_PATHS - all_frontend_paths())
        assert not missing, (
            f"Anchor paths missing from the extraction: {missing}. The scanner has stopped "
            f"handling the literal shape each one represents."
        )

    def test_no_unreduced_typescript_syntax_survives(self) -> None:
        """A path still carrying source syntax was mis-parsed and would match nothing.

        Left unchecked, such a path would be reported as unresolved and pushed onto the
        allowlist as if the *frontend* were at fault.
        """
        corrupt = sorted(
            path
            for path in all_frontend_paths()
            if any(token in path for token in ("$", "`", "?", "#", _INTERPOLATION, " "))
        )
        assert not corrupt, f"Paths still carrying TypeScript syntax: {corrupt}"


class TestPathMatcher:
    """The matcher must be able to say *no*.

    The conformance assertion is `unresolved == allowlist`, with the allowlist empty — so a
    matcher that always returned ``True`` would pass it for any frontend path whatsoever.
    Both outcomes are pinned here, on real registered paths.
    """

    def test_exact_path_resolves(self) -> None:
        assert "/admin/conf" in registered_paths()
        assert _path_matches("/admin/conf", "/admin/conf")

    def test_unknown_path_does_not_resolve(self) -> None:
        invented = "/spoke/metagen/definitely-not-a-route"
        assert not any(_path_matches(invented, path) for path in registered_paths()), (
            "an invented path resolved — the matcher cannot fail, so the conformance "
            "assertion below is vacuous"
        )

    def test_extra_segment_does_not_resolve(self) -> None:
        """A wildcard must not span ``/``, or any deep path would match a shallow route."""
        assert not _path_matches("/admin/conf/extra", "/admin/conf")
        assert not _path_matches("/spoke/common/data/{}", "/spoke/common/data/{}/event")

    def test_concrete_frontend_segment_matches_a_route_parameter(self) -> None:
        assert _path_matches("/spoke/metagen/conf/abc", "/spoke/metagen/conf/{}")

    def test_frontend_wildcard_matches_a_concrete_route_segment(self) -> None:
        """Trap 2: ``result/${kind}/${id}/method/review`` vs three concrete backend routes.

        The backend registers the kind as literal path segments, so the placeholder has to
        be a wildcard on the frontend side too.
        """
        concrete = {
            path
            for path in registered_paths()
            if path.startswith("/spoke/ontogen/result/") and path.endswith("/method/review")
        }
        assert len(concrete) >= 3, (
            f"expected the per-kind review routes to be registered concretely, got {concrete}"
        )
        assert all(_path_matches("/spoke/ontogen/result/{}/{}/method/review", p) for p in concrete)


class TestFrontendPathsResolve:
    """Every path the frontend's API layer builds is served by a registered route.

    Spec: spec/feature/FRONTEND_BASIC.md (intro) — the frontend is "a thin reference client
    over the routes catalogued in `API.md`", and invented endpoints "are out of scope".
    """

    def test_every_frontend_path_resolves_to_a_registered_route(self) -> None:
        assert_drift_allowlist(
            unresolved_frontend_paths(),
            KNOWN_DRIFT_FRONTEND_PATH_UNRESOLVED,
            what="path built by src/frontend/lib/api that matches no route registered on the app",
            allowlist_name="KNOWN_DRIFT_FRONTEND_PATH_UNRESOLVED",
        )
