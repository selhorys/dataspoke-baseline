---
name: redaction-control-wiring-untested
description: RESOLVED — sanitize_error_message's three wiring layers are now guarded; what remains is the marker-literal duplication and an outer bound that no assertion can observe
metadata:
  type: project
---

`src/shared/redaction.py::sanitize_error_message` (added with #102/#103) was tested only
as a **pure function**. The #102/#103 fix pass closed all three wiring gaps — re-verified
by mutation on 2026-07-30, each now fails a test:

1. `report_peripheral_health` — reverting to `(error or "")` fails 2 tests in
   `tests/unit/backend/admin/test_peripheral_health.py`.
2. `DataHubClient.sanitize` → `return message` fails 5 tests in
   `tests/unit/shared/datahub/test_client.py` (incl. both `DataHubUnavailableError`
   raise sites).
3. `IngestionService._describe_failure` — the `callable(sanitize)` true branch and the
   no-`Traceback` clause are both covered in `tests/unit/backend/ingestion/test_service.py`.

**Two residues worth remembering.**

- **The marker literal is duplicated.** `tests/unit/shared/test_redaction.py` imports
  `REDACTED`; five other sites hard-code `"<redacted>"`. Changing `REDACTED` to any other
  string fails 3 unit tests, though the spec (BACKEND.md §Health reporting) requires only
  credential-freedom and names no marker text. Import the constant at every site.
- **`_MAX_INPUT_LENGTH` (the outer bound) cannot be observed in output length.** It bounds
  *cost* of normalization + the exact-value scrub; the inner `_MAX_SCAN_LENGTH` slice runs
  after it and clamps the returned text regardless. Deleting the outer slice leaves the
  whole redaction suite green. Any docstring claiming a length test covers "both bounds" is
  wrong — the honest claim is the inner bound plus input/output decoupling.

Related: [[peripherals-get-mask-no-plaintext-path]],
[[owning-source-last-seen-tiebreak-untested]].
