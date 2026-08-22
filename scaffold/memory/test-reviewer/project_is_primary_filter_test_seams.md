---
name: is-primary-filter-test-seams
description: is_primary dataset_filter column — post-fix-pass mutation table (22 targeted mutants, 20 killed); what the fix pass closed, and the one live spec-vs-impl contradiction held green by the repo's first xfail
metadata:
  type: project
---

Measured on the `is_primary` + `criterion met:` change set, **after** the cycle-2
fix pass. Re-run the mutants rather than reading the diff.

**Closed by the fix pass (all mutation-verified KILLED):** flag-before-list branch
order; the no-flag/readable-list truth-table row (flipping it to `False` fails);
non-dict aspect; `is_primary` dropped from the on-conflict `set_` and from the
insert payload; sweep writing a constant; `= true` → `IS true` compile; uppercase
format; quoted boolean parsing; `_BOOL_LITERALS` false→True; python default
False; index no longer partial; `DATASET_FILTER_FIELD_DESCRIPTION` losing the
boolean clause; the guide's trailing `-- bare word, never quoted`; the guide's
`is_primary` COLUMNS entry; the `criterion met:` caption text and its position
relative to the checkboxes; `except AttributeError: raise`.

**Live survivors (2, both benign):**
1. Removing the `if _require_attribute_read(attribute)` clause from the record
   comprehension in `src/backend/ingestion/service.py:2473` leaves all 142
   `test_service.py` tests green. The helper was added outside the approved plan;
   its only effect was choosing between two log-message names the fix pass
   correctly stopped asserting. Unobservable by design now.
2. Dropping `boolean columns are {…}` from the unknown-column parse error is
   unpinned — correct, that prose is unspecced.

**The one thing that must not ship as-is.** `src/backend/ingestion/service.py`
logs the attribute-sync degrade branches at `logger.error` (`:2440` pre-existing,
`:2480`, `:2490` new), but `spec/feature/BACKEND.md:1892` puts the estate-wide
dataset attribute read among the rows "logged at WARNING with `exc_info=True`"
and enumerates only two ERROR exceptions (health reporter, `last_used_at`);
`:1918` adds "The exemption stops there." The test agent encoded this as
`tests/unit/backend/ingestion/test_service.py::test_a_degraded_attribute_read_is_logged_at_warning`
with `pytest.mark.xfail(strict=True)` — **the only xfail in the whole tests tree**,
and `spec/TESTING.md` has no rule sanctioning one. Verified with `--runxfail`: it
fails on exactly the level assertion, not the backstop. Resolution is a src/ or
spec/ edit, not a test edit.

**Still unspecced, still untested (correctly):** `_SiblingRead.degraded` and the
`datahub_dataset_siblings_shape_degraded` counter — grep DATAHUB_INTEGRATION.md
for "degraded" and only the derivation prose comes back, not the counter.

**Verified anchors** (don't re-litigate): the `dataspoke.` schema qualifier in
`assert sql.strip() == "dataspoke.dataset_registry.is_primary = true"` IS specced
(BACKEND_SCHEMA.md:27-29). The guide test's grammar block is byte-identical to
API.md's fence except the `term` line's `(see below)`. Every long spec quote in
the changed tests exists verbatim in `spec/`.

Related: [[dataset-filter-verdict-test-seams]], [[dataset-filter-editor-mutation-seams]].
