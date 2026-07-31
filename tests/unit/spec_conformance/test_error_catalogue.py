"""Error-code conformance: every declarative code catalogue ↔ ``spec/API.md``.

``spec/API.md §Application Error Codes`` is the priority-1 catalogue of every
``error_code`` the API may return, with its HTTP status. Four other places enumerate
codes statically and must agree with it:

* ``Valid error_code values:`` / ``Valid entity_type values:`` blocks on the class
  docstrings in ``src/shared/exceptions.py``;
* class-level ``error_code: str = "…"`` defaults in the same module;
* every ``EntityNotFoundError("<entity_type>", …)`` call site under ``src/``;
* ``spec/feature/BACKEND.md §Exception-to-HTTP Mapping``.

``exceptions.py``'s **module** docstring is not one of them, and is asserted to stay that
way: it names the per-class blocks and the BACKEND.md table as the catalogues instead of
restating either, so a re-added copy is caught here rather than left to drift.

**On enumerating raise sites.** Error-code *strings* are frequently derived rather than
written out (``EntityNotFoundError`` builds ``f"{entity_type.upper()}_NOT_FOUND"``), so
grepping ``src/`` for code strings yields false "never raised" results and is not done
here. The ``entity_type`` *argument*, however, is a string literal at every call site
and is extracted exactly by AST; the code it yields is then obtained by instantiating
the exception, so no test re-implements the naming rule.

Spec: spec/API.md §Application Error Codes
Spec: spec/feature/BACKEND.md §Exception-to-HTTP Mapping
Spec: spec/TESTING.md §Assertion Discipline ("Author assertions so that a passing result
is only reachable when the spec'd behavior actually occurred.")

The two-directional treatment of the ``KNOWN_DRIFT_*`` allowlists is a design choice of
this package, not a rule quoted from a spec; ``assert_drift_allowlist`` implements it and
``test_drift_allowlist.py`` proves both of its branches fire.

Unit-tier: reads markdown and parses source ASTs. No dev environment.
"""

import pytest

from src.shared.exceptions import EntityNotFoundError

from ._api_md import (
    api_md_error_codes,
    assert_drift_allowlist,
    backend_md_exception_mapping,
    entity_not_found_call_site_types,
    entity_not_found_dynamic_call_sites,
    entity_not_found_map,
    exception_class_attr_error_codes,
    exception_docstring_error_codes,
    module_docstring_mapping_blocks,
    src_python_roots,
)

# ── Known drift ──────────────────────────────────────────────────────────────
#
# Every entry below is a real, reproduced mismatch, recorded rather than silently fixed
# or suppressed, and justified either by the issue-#86 phase that resolves it
# (1 = API-centric, 2 = backend, 3 = frontend) or by a spec/ citation making it a
# permanent documented exception. `assert_drift_allowlist` fails on a stale entry as well
# as an undeclared one, so an entry whose mismatch is resolved must be deleted here.
#
# Several of the sets below are empty. They stay declared so a future author records a
# mismatch with its justification here rather than loosening the assertion that found it.

#: Codes enumerated in a class-docstring block that spec/API.md does not catalogue.
#: Empty: spec/API.md §Application Error Codes carries a row for every code the class
#: docstring blocks in src/shared/exceptions.py enumerate.
KNOWN_DRIFT_DOCSTRING_NOT_IN_API_MD: frozenset[str] = frozenset()

#: Class-level `error_code` defaults that spec/API.md does not catalogue.
KNOWN_DRIFT_CLASS_ATTR_NOT_IN_API_MD: frozenset[str] = frozenset(
    {
        # Documented exceptions, not scheduled mismatches: neither code can appear in an
        # HTTP response, and spec/API.md §Application Error Codes is a client contract
        # ("`error_code` | HTTP | Description"), so a row for either would tell clients to
        # write a permanently dead handler branch. Both classes state this at their
        # definition site (src/shared/exceptions.py::NotificationError and
        # ::EventProcessingError), and each raise path is contained before any handler:
        #   - NotificationError: the one request-path sender converts it to
        #     StorageUnavailableError → 503 STORAGE_UNAVAILABLE
        #     (src/backend/auth/reset.py:119-128); the digest/alert senders swallow and
        #     log it (src/shared/notifications/service.py:13-15).
        #   - EventProcessingError: raised only inside the Kafka consumer loop, which
        #     catches it, logs, and commits the offset (src/shared/datahub/consumer.py:
        #     417-424) — no request is in flight to answer.
        # These entries are therefore expected to stay. `assert_drift_allowlist`'s stale
        # branch keeps them honest: if either class or its default code is removed, the
        # entry fails as stale instead of lingering.
        "NOTIFICATION_FAILED",
        "EVENT_PROCESSING_FAILED",
    }
)

#: `entity_type` values passed at a call site but absent from EntityNotFoundError's
#: docstring block, which claims to enumerate the valid values.
#: Empty: the block declares every type used at a call site under src/.
KNOWN_DRIFT_ENTITY_TYPE_UNDECLARED: frozenset[str] = frozenset()

#: `entity_type` values the docstring block declares that no call site under src/ passes.
#: Empty: every declared type has at least one raise site. A declared-but-unused type is
#: a phantom — it advertises a 404 code the API can never return, and (being catalogued
#: in spec/API.md as well) would otherwise survive indefinitely.
KNOWN_DRIFT_ENTITY_TYPE_UNUSED: frozenset[str] = frozenset()

#: Codes actually produced by an EntityNotFoundError call site that spec/API.md does not
#: catalogue. These would reach clients as a 404 `error_code` no client can look up.
#: Empty: every entity code a call site can produce is catalogued at 404.
KNOWN_DRIFT_ENTITY_CODE_NOT_IN_API_MD: frozenset[str] = frozenset()

#: spec/feature/BACKEND.md agrees with spec/API.md on every code and status today, so
#: these are empty. They are declared so a future author records a mismatch here — with
#: its issue-#86 phase — rather than loosening the assertion.
KNOWN_DRIFT_BACKEND_MD_CODE: frozenset[str] = frozenset()
KNOWN_DRIFT_BACKEND_MD_STATUS: frozenset[str] = frozenset()

# ── Parser backstops ─────────────────────────────────────────────────────────
#
# Each conformance assertion below compares a parsed set against a catalogue. A parser
# that degrades — matching some entries but not all — would shrink both sides and stay
# green, so every source is pinned to a per-item floor plus anchor entries, not merely
# to "non-empty". Counts are floors at the current value: raise them as catalogues grow.

#: `{class: (minimum block size, anchor codes that must be present)}`. Anchors are drawn
#: from the start, middle and end of each block so a parser that stops early is caught,
#: and include multi-segment codes a narrowed identifier pattern would drop.
DOCSTRING_BLOCK_ANCHORS: dict[str, tuple[int, frozenset[str]]] = {
    "EntityNotFoundError": (
        8,
        frozenset({"DATASET_NOT_FOUND", "INGESTION_SOURCE_NOT_FOUND", "TRIPLE_NOT_FOUND"}),
    ),
    "ConflictError": (
        17,
        frozenset({"DUPLICATE_CONFIG", "METAGEN_CONF_EXISTS", "GOOGLE_IS_ONLY_AUTH_METHOD"}),
    ),
    "PreconditionFailedError": (
        7,
        frozenset(
            {
                "DATASET_NOT_IN_DATAHUB",
                "SECRET_REF_NOT_FOUND",
                "METAGEN_DATASET_NOT_IN_BOUNDARY",
            }
        ),
    ),
    "AuthenticationError": (4, frozenset({"UNAUTHORIZED", "TOKEN_REVOKED"})),
    "BadRequestError": (
        4,
        frozenset({"BAD_REQUEST", "INVALID_RESET_TOKEN", "OAUTH_STATE_MISMATCH"}),
    ),
    "ForbiddenError": (2, frozenset({"FORBIDDEN", "READ_ONLY_ROLE"})),
}

#: Anchor codes spanning several HTTP statuses in spec/API.md §Application Error Codes.
ANCHOR_API_MD_CODES: dict[str, int] = {
    "DATASET_NOT_FOUND": 404,
    "INGESTION_RUNNING": 409,
    "UNKNOWN_VARIABLE": 422,
    "UNAUTHORIZED": 401,
    "DATAHUB_UNAVAILABLE": 502,
    "INTERNAL_ERROR": 500,
}

#: Entity types that must be recovered from EntityNotFoundError call sites. Includes
#: multi-word values, which a narrowed literal pattern would silently drop.
ANCHOR_CALL_SITE_ENTITY_TYPES: frozenset[str] = frozenset(
    {"dataset", "ingestion_source", "metagen_conf", "metagen_boundary", "user", "dag_group"}
)


class TestCatalogueParsing:
    """Backstops proving each parsed catalogue is complete, not merely non-empty."""

    def test_api_md_error_codes_parsed(self) -> None:
        """Floor at the current row count — raise it as the catalogue grows."""
        codes = api_md_error_codes()
        assert len(codes) >= 55, (
            f"Only {len(codes)} rows parsed from spec/API.md §Application Error Codes — "
            f"the table format likely changed and the conformance checks below would "
            f"compare against a truncated catalogue."
        )

    @pytest.mark.parametrize("code,status", sorted(ANCHOR_API_MD_CODES.items()))
    def test_api_md_anchor_codes_parsed_with_status(self, code: str, status: int) -> None:
        """Each anchor code is parsed with the HTTP status spec/API.md assigns it."""
        assert api_md_error_codes().get(code) == status

    @pytest.mark.parametrize("class_name", sorted(DOCSTRING_BLOCK_ANCHORS))
    def test_exception_docstring_block_fully_parsed(self, class_name: str) -> None:
        """Each class's ``Valid … values:`` block parses to its full size and anchors.

        A block that degrades from 18 codes to 1 would still be "non-empty"; the size
        floor and the anchor codes catch that partial failure.
        """
        minimum, anchors = DOCSTRING_BLOCK_ANCHORS[class_name]
        parsed = exception_docstring_error_codes().get(class_name, frozenset())
        assert len(parsed) >= minimum, (
            f"{class_name}: parsed {len(parsed)} codes from its docstring block, expected "
            f"at least {minimum} — the block is only partially matching."
        )
        missing = sorted(anchors - parsed)
        assert not missing, f"{class_name}: anchor codes {missing} not parsed from its block."

    def test_backend_md_mapping_parsed(self) -> None:
        mapping = backend_md_exception_mapping()
        assert len(mapping) >= 7, (
            f"Only {len(mapping)} rows parsed from spec/feature/BACKEND.md "
            f"§Exception-to-HTTP Mapping — the table format likely changed."
        )
        names = " ".join(name for name, _, _ in mapping)
        for expected in ("EntityNotFoundError", "ConflictError", "PreconditionFailedError"):
            assert expected in names, (
                f"{expected} row missing from the parsed BACKEND.md mapping table: {names}"
            )

    def test_class_attr_error_codes_parsed(self) -> None:
        parsed = exception_class_attr_error_codes()
        assert parsed.get("DataSpokeError") == "INTERNAL_ERROR"
        assert parsed.get("NotImplementedAPIError") == "NOT_IMPLEMENTED"
        assert len(parsed) >= 13, (
            f"Only {len(parsed)} class-level error_code defaults parsed from "
            f"src/shared/exceptions.py — the extractor is missing declarations."
        )

    def test_src_python_roots_are_all_scanned(self) -> None:
        """Each named Python package must exist and hold modules.

        The call-site scan enumerates these roots by name (``src/frontend`` is excluded
        because its ``node_modules`` vendors third-party ``.py`` files). A package that
        is renamed or moved would otherwise drop out of the scan silently, taking its
        raise sites with it.
        """
        for name, root in src_python_roots().items():
            assert root.is_dir(), f"src/{name} is not a directory — the scan misses it"
            assert any(root.rglob("*.py")), f"src/{name} contains no Python modules"

    def test_entity_call_sites_parsed(self) -> None:
        parsed = entity_not_found_call_site_types()
        missing = sorted(ANCHOR_CALL_SITE_ENTITY_TYPES - parsed.keys())
        assert not missing, (
            f"entity_type(s) {missing} not recovered from EntityNotFoundError call sites "
            f"— the AST extraction is incomplete."
        )
        assert len(parsed) >= 15, (
            f"Only {len(parsed)} distinct entity types found at call sites; floor is 15."
        )
        for entity, sites in parsed.items():
            assert sites, f"{entity} recorded with no call site"

    def test_no_dynamic_entity_type_call_sites(self) -> None:
        """Every call site must pass a literal, or the AST check silently under-covers.

        A dynamic ``entity_type`` would be invisible to the extraction, so the
        conformance checks below would stop covering that raise site without saying so.
        """
        dynamic = entity_not_found_dynamic_call_sites()
        assert not dynamic, (
            f"EntityNotFoundError call sites with a non-literal entity_type: {list(dynamic)}. "
            f"The AST-based conformance checks cannot see these; make the argument a "
            f"literal or extend the extractor."
        )

    def test_module_docstring_mapping_detector_fires(self) -> None:
        """The no-fourth-copy assertion below is only meaningful if the detector fires.

        It runs against a clean docstring and would pass identically if
        ``module_docstring_mapping_blocks`` always returned ``()``. Three synthetic
        layouts of the same mapping are fed in — arrow, pipe table, and aligned columns —
        because a re-added copy would not necessarily be written the way the deleted one
        was, and a detector tied to one layout would miss the other two.
        """
        layouts = {
            "arrow": "  EntityNotFoundError → 404  DATASET_NOT_FOUND | CONFIG_NOT_FOUND",
            "pipe table": "  | ConflictError | 409 | DUPLICATE_CONFIG |",
            "aligned columns": "  PreconditionFailedError    422    INVALID_SCORE",
        }
        for layout, block in layouts.items():
            found = module_docstring_mapping_blocks(f"Module summary.\n\nMapping:\n{block}\n")
            assert found, f"detector missed a {layout} mapping block: {block!r}"

    def test_module_docstring_mapping_detector_ignores_prose(self) -> None:
        """Naming a class, or listing codes, is not by itself a mapping.

        Without this the detector could pass the test above by flagging every block, which
        would make the no-fourth-copy assertion unfailable-in-reverse: it would fail on any
        module docstring at all, and the next author would delete it rather than the copy.
        """
        prose = (
            "All backend services raise subclasses of DataSpokeError.\n"
            "The API layer catches these and maps them to HTTP responses.\n"
            "\n"
            "Unrelated block naming codes only: DATASET_NOT_FOUND, INVALID_SCORE.\n"
        )
        assert module_docstring_mapping_blocks(prose) == ()


class TestExceptionDeclarationsAreCatalogued:
    """Everything ``src/shared/exceptions.py`` declares must exist in spec/API.md.

    Spec: spec/API.md §Application Error Codes.
    """

    def test_docstring_codes_exist_in_api_md(self) -> None:
        catalogued = api_md_error_codes()
        declared = {
            code for codes in exception_docstring_error_codes().values() for code in codes
        }
        assert declared, "No error codes parsed from src/shared/exceptions.py docstrings"
        assert_drift_allowlist(
            declared - catalogued.keys(),
            KNOWN_DRIFT_DOCSTRING_NOT_IN_API_MD,
            what="error code declared in an src/shared/exceptions.py docstring but "
            "absent from spec/API.md §Application Error Codes",
            allowlist_name="KNOWN_DRIFT_DOCSTRING_NOT_IN_API_MD",
        )

    def test_class_attr_defaults_exist_in_api_md(self) -> None:
        catalogued = api_md_error_codes()
        declared = set(exception_class_attr_error_codes().values())
        assert declared, "No class-level error_code defaults parsed"
        assert_drift_allowlist(
            declared - catalogued.keys(),
            KNOWN_DRIFT_CLASS_ATTR_NOT_IN_API_MD,
            what="class-level `error_code` default in src/shared/exceptions.py but "
            "absent from spec/API.md §Application Error Codes",
            allowlist_name="KNOWN_DRIFT_CLASS_ATTR_NOT_IN_API_MD",
        )

    def test_entity_not_found_codes_are_404_in_api_md(self) -> None:
        """Every declared ``entity_type`` maps to a code spec/API.md lists as 404.

        spec/API.md §Application Error Codes assigns 404 to each entity code;
        ``EntityNotFoundError``'s docstring is the declarative list of valid types.
        """
        catalogued = api_md_error_codes()
        pairs = entity_not_found_map()
        assert pairs, "EntityNotFoundError docstring produced no entity_type → code pairs"
        wrong = sorted(
            f"{entity} → {code} (spec/API.md: {catalogued.get(code, 'absent')})"
            for entity, code in pairs.items()
            if catalogued.get(code) != 404
        )
        assert not wrong, f"entity_type codes not catalogued as 404 in spec/API.md: {wrong}"


class TestEntityNotFoundCallSitesAreCatalogued:
    """Every ``EntityNotFoundError`` raise site must be declared and catalogued.

    The ``entity_type`` argument is a string literal at every call site, so the set of
    codes this exception can actually produce is statically knowable. Each is checked
    against the class's own docstring enumeration and against spec/API.md.
    """

    def test_call_site_entity_types_are_declared(self) -> None:
        declared = set(entity_not_found_map())
        used = set(entity_not_found_call_site_types())
        assert used, "No EntityNotFoundError call sites found under src/"
        assert_drift_allowlist(
            used - declared,
            KNOWN_DRIFT_ENTITY_TYPE_UNDECLARED,
            what="entity_type used at an EntityNotFoundError call site but absent from "
            "the class's `Valid entity_type values` docstring block",
            allowlist_name="KNOWN_DRIFT_ENTITY_TYPE_UNDECLARED",
        )

    def test_declared_entity_types_have_a_call_site(self) -> None:
        """The reverse direction: a declared ``entity_type`` no call site passes fails.

        ``test_call_site_entity_types_are_declared`` above only pins ``used − declared``.
        Unpinned, the other direction is how a phantom survives: delete the last
        ``EntityNotFoundError("seed", …)`` raise site and ``"seed" → SEED_NOT_FOUND``
        stays in the docstring block and in spec/API.md, advertising a 404 code the API
        can no longer return, with nothing failing. Pinning both directions makes the
        block an exact statement of what the exception can produce.

        Safe to derive statically: every call site passes a literal, which
        ``test_no_dynamic_entity_type_call_sites`` enforces, so an entity type absent from
        this scan is genuinely unused rather than merely invisible to it.
        """
        declared = set(entity_not_found_map())
        used = set(entity_not_found_call_site_types())
        assert declared, "EntityNotFoundError's docstring declared no entity types"
        assert used, "No EntityNotFoundError call sites found under src/"
        assert_drift_allowlist(
            declared - used,
            KNOWN_DRIFT_ENTITY_TYPE_UNUSED,
            what="entity_type declared in the class's `Valid entity_type values` "
            "docstring block but passed at no EntityNotFoundError call site under src/",
            allowlist_name="KNOWN_DRIFT_ENTITY_TYPE_UNUSED",
        )

    def test_call_site_codes_are_catalogued_as_404(self) -> None:
        """The code each call site actually produces must be a 404 row in spec/API.md.

        The expected code is obtained by *instantiating* the exception rather than by
        re-applying its ``upper() + "_NOT_FOUND"`` rule, so this cannot pass by agreeing
        with a broken derivation.
        """
        catalogued = api_md_error_codes()
        produced = {
            EntityNotFoundError(entity, "conformance-probe").error_code
            for entity in entity_not_found_call_site_types()
        }
        assert produced, "No codes produced from EntityNotFoundError call sites"
        assert_drift_allowlist(
            {code for code in produced if catalogued.get(code) != 404},
            KNOWN_DRIFT_ENTITY_CODE_NOT_IN_API_MD,
            what="error code produced by an EntityNotFoundError call site but not "
            "catalogued as 404 in spec/API.md §Application Error Codes",
            allowlist_name="KNOWN_DRIFT_ENTITY_CODE_NOT_IN_API_MD",
        )


class TestModuleDocstringHoldsNoCatalogue:
    """``exceptions.py``'s module docstring must carry no exception→code mapping.

    The exception→code/status mapping has exactly three homes: the per-class docstring
    blocks in ``src/shared/exceptions.py``, ``spec/API.md §Application Error Codes``, and
    ``spec/feature/BACKEND.md §Exception-to-HTTP Mapping`` — each checked against the
    others above. A copy in the module docstring is a fourth, ranks below all three, and
    is what this forbids.
    """

    def test_module_docstring_has_no_mapping_block(self) -> None:
        offenders = module_docstring_mapping_blocks()
        assert not offenders, (
            f"src/shared/exceptions.py's module docstring pairs an exception class with "
            f"an error code or HTTP status: {offenders}. That mapping already lives on "
            f"the per-class docstrings and in spec/API.md §Application Error Codes / "
            f"spec/feature/BACKEND.md §Exception-to-HTTP Mapping; a module-docstring copy "
            f"ranks below all three and drifts from them. Point at those catalogues "
            f"instead of restating them."
        )


class TestBackendMappingIsCatalogued:
    """``spec/feature/BACKEND.md`` mapping agrees with ``spec/API.md`` on code and status.

    Spec: spec/feature/BACKEND.md §Exception-to-HTTP Mapping — "Error response format
    matches [API](../API.md#error-catalogue)."
    """

    def test_mapped_codes_exist_in_api_md(self) -> None:
        catalogued = api_md_error_codes()
        mapped = {code for _, _, codes in backend_md_exception_mapping() for code in codes}
        assert mapped, "No error codes parsed from the BACKEND.md mapping table"
        assert_drift_allowlist(
            mapped - catalogued.keys(),
            KNOWN_DRIFT_BACKEND_MD_CODE,
            what="error code in spec/feature/BACKEND.md §Exception-to-HTTP Mapping but "
            "absent from spec/API.md §Application Error Codes",
            allowlist_name="KNOWN_DRIFT_BACKEND_MD_CODE",
        )

    def test_mapped_statuses_agree_with_api_md(self) -> None:
        catalogued = api_md_error_codes()
        mismatched = {
            f"{code}: BACKEND.md {status} vs API.md {catalogued[code]}"
            for _, status, codes in backend_md_exception_mapping()
            for code in codes
            if code in catalogued and catalogued[code] != status
        }
        # Backstop: the comparison above only runs for codes present on both sides, so
        # prove that overlap is the full BACKEND.md set rather than a truncated slice.
        compared = {
            code
            for _, _, codes in backend_md_exception_mapping()
            for code in codes
            if code in catalogued
        }
        assert len(compared) >= 26, (
            f"Only {len(compared)} codes compared for status agreement — the two "
            f"catalogues barely overlap, so this check proves little."
        )
        assert_drift_allowlist(
            mismatched,
            KNOWN_DRIFT_BACKEND_MD_STATUS,
            what="HTTP status disagreement between spec/feature/BACKEND.md "
            "§Exception-to-HTTP Mapping and spec/API.md §Application Error Codes",
            allowlist_name="KNOWN_DRIFT_BACKEND_MD_STATUS",
        )
