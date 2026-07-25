---
name: spec-conformance-86-anchors
description: Issue #86 spec-conformance suite — verified counts, the TESTING.md allowlist-citation gap, and the drift left unencoded for later phases
metadata:
  type: project
---

`tests/unit/spec_conformance/` (issue #86 Phase 0) compares FastAPI routes and error codes
against `spec/API.md`. Independently verified at review time (2026-07-25):

- 124 catalogued registered routes == 124 `§Route Catalogue` rows, 0 drift both ways.
  134 walked leaves - 2 framework (`/openapi.json`, `/redoc`; `docs_url` is `None`) - 8
  `/internal/activities/*`. No normalisation collisions on either side, so the clean
  result is real, not collapse-induced.
- 57 `§Application Error Codes` rows; 7 `BACKEND.md §Exception-to-HTTP Mapping` rows;
  26 codes overlap. Only `BAD_REQUEST` + `INGESTION_DISABLED` drift.

**Why:** later #86 phases will re-run these comparisons; these are the baselines that
prove a future green run is not vacuous.

**How to apply:**
- `spec/TESTING.md` contains **no** allowlist rule — the string "allowlist" appears
  nowhere in it. Any `Spec: spec/TESTING.md §Assertion Discipline (allowlists are
  asserted in both directions)` citation is a citation-existence violation until the
  `spec` agent adds the bullet. See [[dead-assert-tuple-ruff-blind]] for the sibling
  case where TESTING.md *does* carry the rule but the linter cannot see it.
- Drift found but deliberately left untested by Phase 0, so it is NOT caught by any
  test: `EntityNotFoundError` raise sites use 15 entity types vs 8 in its docstring —
  `USER_NOT_FOUND`, `SEED_NOT_FOUND`, `METAGEN_{BOUNDARY,CANDIDATE,ITEM}_NOT_FOUND` are
  absent from API.md; so are the class defaults `NOTIFICATION_FAILED` /
  `EVENT_PROCESSING_FAILED`; and the `exceptions.py` **module** docstring mapping block
  is stale (unparsed by the checker — it reads ClassDefs only).
