---
name: validation-no-status-aspect-divergence
description: Spec says validation emits NO status aspect; impl still emits StatusClass(removed=False) on PUT — flag tests pinning the leftover emission
metadata:
  type: project
---

VALIDATION.md (L215-218 "writes two aspects: assertionInfo + assertionRunEvent",
L304 "DataSpoke therefore emits no `status` aspect") and DATAHUB_INTEGRATION.md
(L405 status cell: "no `status` write") say the validation feature writes NO
`status` aspect at all — DELETE hard-deletes the assertion entity instead of
tombstoning.

But the impl `src/backend/validation/assertions.py::register_assertion` emits
`StatusClass(removed=False)` on every PUT/PATCH — a PUT-time status emit with no
spec basis. There is no tombstone path; DELETE hard-deletes.

**Why:** This is the T2 test-concealment trap. The spot test
`test_out_of_band_tombstone_reverted_on_put` asserts the impl's leftover
status(removed=False) emission and cites a "VALIDATION.md §DataHub Aspect Mapping §status"
paragraph that the rewrite deleted. A spec/impl divergence stays green.

**How to apply:** When reviewing validation DataHub-aspect tests, flag any assertion that
DataSpoke emits/reverts a `status` aspect on PUT/PATCH — it has no current spec basis.
Escalate the spec-vs-impl divergence (either impl drops the status emit to match
VALIDATION.md, or the spec re-documents PUT-time status). Note DATAHUB_INTEGRATION.md L405
only explicitly negates status-write *on DELETE*, so the two specs are in mild tension, but
VALIDATION.md (most specific, the rewrite target) is unambiguous: no status aspect.
Related: [[recipe-mask-string-divergence]] (same pattern — test pins impl over spec).
