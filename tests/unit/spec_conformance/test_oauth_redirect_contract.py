"""Conformance for ``spec/API.md §OAuth browser-redirect contract``.

That section adds two things the other conformance modules in this package do not
otherwise see:

* a **three-column contract table** (Route | Outcome | Response) nested inside
  §Route Catalogue, whose first cell is a backticked *path* rather than an HTTP method.
  The route-row parser keys on a method in the first cell, so the table must contribute
  no route rows — and the two Google routes must still be catalogued exactly once each
  by the real route table above it;
* **five error-catalogue rows carrying status 302**, a status no other row uses.

Both are checked here rather than by loosening the parsers in ``_api_md.py``. The impl
side of the contract — which codes the router actually forwards to the page — is pinned
against the same five rows, so a code added to one side without the other fails.

Spec: spec/API.md §OAuth browser-redirect contract
Spec: spec/API.md §HTTP Status Codes ("`302 Found` … Used only by
`GET /auth/google/{login,callback}`")
Spec: spec/API.md §Application Error Codes

Unit-tier: reads ``spec/API.md`` and imports the router module. No dev environment.
"""

from __future__ import annotations

import re

from ._api_md import api_md_error_codes, api_md_sections, spec_route_rows

#: Heading of the section under test.
CONTRACT_HEADING = "OAuth browser-redirect contract"

#: The two browser-navigation routes the contract governs.
GOOGLE_ROUTES: tuple[str, ...] = ("/auth/google/login", "/auth/google/callback")

#: ``| `/auth/google/login` | OAuth configured | `302` to … |`` — a contract-table row:
#: backticked path, then two further cells.
_CONTRACT_ROW_RE = re.compile(r"^\|\s*`(/[^`]+)`\s*\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|\s*$")


def _contract_section_lines() -> tuple[str, ...]:
    """Body lines of §OAuth browser-redirect contract."""
    for section in api_md_sections():
        if section.heading == CONTRACT_HEADING:
            return section.lines
    return ()


def _contract_rows() -> list[tuple[str, str, str]]:
    """``(route, outcome, response)`` for every row of the contract table."""
    return [
        (match.group(1), match.group(2), match.group(3))
        for line in _contract_section_lines()
        if (match := _CONTRACT_ROW_RE.match(line)) is not None
    ]


class TestContractSectionParsing:
    """Backstops: the section, and its table, must actually be there to reason about."""

    def test_contract_section_exists(self) -> None:
        lines = _contract_section_lines()
        assert lines, (
            f"spec/API.md has no §{CONTRACT_HEADING} section — the assertions below "
            f"would all reason about an empty document slice."
        )

    def test_contract_table_rows_are_parsed(self) -> None:
        """The table exists and names both routes.

        Without this, every "the table leaked nothing" assertion below would pass
        against a table that was never found.
        """
        rows = _contract_rows()
        assert len(rows) >= 4, (
            f"Only {len(rows)} contract-table rows parsed from §{CONTRACT_HEADING}; the "
            f"section documents an outcome row per route per outcome."
        )
        named = {route for route, _, _ in rows}
        missing = sorted(set(GOOGLE_ROUTES) - named)
        assert not missing, f"contract table names no outcome for {missing}"


class TestContractTableIsNotReadAsRouteRows:
    """The three-column table must not leak into the §Route Catalogue comparison.

    Spec: spec/API.md §Route Catalogue is the contract for what the API exposes; the
    contract table is an outcome table nested under it, not a route table.
    """

    def test_google_routes_are_catalogued_exactly_once_each_as_GET(self) -> None:
        rows = spec_route_rows()
        for path in GOOGLE_ROUTES:
            matching = sorted(f"{method} {p}" for method, p in rows if p == path)
            assert matching == [f"GET {path}"], (
                f"expected exactly one catalogued row for {path} (GET), got {matching} — "
                f"the §{CONTRACT_HEADING} table may be parsing as route rows."
            )

    def test_no_route_row_was_derived_from_a_contract_outcome_cell(self) -> None:
        """No catalogued path equals an Outcome cell of the contract table.

        The parser reads the first two cells as (method, path). Were it to shift by a
        column on this table, the catalogue would gain rows whose "path" is prose such as
        ``OAuth not configured``.
        """
        catalogued_paths = {path for _, path in spec_route_rows()}
        outcomes = {outcome for _, outcome, _ in _contract_rows()}
        assert outcomes, "no outcome cells parsed — this check would be vacuous"
        leaked = sorted(outcomes & catalogued_paths)
        assert not leaked, f"contract-table outcome cells parsed as route paths: {leaked}"


class TestErrorCatalogueCarriesTheRedirectStatus:
    """The five codes that reach ``/oauth-error`` are catalogued at 302, and only they.

    Spec: spec/API.md §OAuth browser-redirect contract — "Five codes reach the error
    page: `OAUTH_NOT_CONFIGURED` (both routes), plus `OAUTH_STATE_MISMATCH`,
    `OAUTH_EMAIL_NOT_VERIFIED`, `GOOGLE_ACCOUNT_LINKED_ELSEWHERE`, and
    `EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT` on the callback."
    Spec: spec/API.md §HTTP Status Codes — "`302 Found` | Browser-navigation redirect.
    Used only by `GET /auth/google/{login,callback}`".
    """

    #: Read verbatim from the §OAuth browser-redirect contract prose above.
    SPEC_CODES: frozenset[str] = frozenset(
        {
            "OAUTH_NOT_CONFIGURED",
            "OAUTH_STATE_MISMATCH",
            "OAUTH_EMAIL_NOT_VERIFIED",
            "GOOGLE_ACCOUNT_LINKED_ELSEWHERE",
            "EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT",
        }
    )

    def test_each_code_is_catalogued_at_302(self) -> None:
        catalogued = api_md_error_codes()
        wrong = sorted(
            f"{code} → {catalogued.get(code, 'absent')}"
            for code in self.SPEC_CODES
            if catalogued.get(code) != 302
        )
        assert not wrong, (
            f"codes that reach /oauth-error must be catalogued at 302 in spec/API.md "
            f"§Application Error Codes: {wrong}"
        )

    def test_302_rows_are_exactly_those_five(self) -> None:
        """No other code claims the redirect status.

        The 302 row in §HTTP Status Codes says it is "Used only by
        `GET /auth/google/{login,callback}`", so any further 302 row would be a code no
        route can deliver.
        """
        catalogued = api_md_error_codes()
        redirect_codes = {code for code, status in catalogued.items() if status == 302}
        assert redirect_codes == self.SPEC_CODES, (
            f"302 rows in spec/API.md §Application Error Codes are {sorted(redirect_codes)}, "
            f"expected {sorted(self.SPEC_CODES)}"
        )

    def test_error_row_parser_still_reads_non_redirect_rows(self) -> None:
        """Adding the 302 rows must not have disturbed the rest of the catalogue.

        A parser that started matching only the new rows would satisfy the two checks
        above and quietly shrink every other error-catalogue comparison in this package.
        """
        catalogued = api_md_error_codes()
        statuses = set(catalogued.values())
        assert statuses >= {400, 401, 403, 404, 409, 422, 500, 502, 503}, (
            f"only {sorted(statuses)} statuses parsed from spec/API.md §Application Error "
            f"Codes — the table format likely changed."
        )


class TestRouterForwardsExactlyTheCataloguedCodes:
    """The router's forwarded-code set equals the 302 rows in spec/API.md.

    Spec: spec/API.md §OAuth browser-redirect contract — the five codes are delivered as
    ``?error=`` on the redirect; "Any other failure … redirects to `<ui>/oauth-error`
    with no `error` parameter".
    """

    def test_impl_code_set_matches_the_catalogue(self) -> None:
        from src.api.routers.auth import _OAUTH_ERROR_CODES

        catalogued = {code for code, status in api_md_error_codes().items() if status == 302}
        assert catalogued, "no 302 rows parsed — the comparison would be vacuous"
        assert set(_OAUTH_ERROR_CODES) == catalogued, (
            f"src/api/routers/auth.py forwards {sorted(_OAUTH_ERROR_CODES)} to /oauth-error, "
            f"while spec/API.md catalogues {sorted(catalogued)} at 302."
        )
