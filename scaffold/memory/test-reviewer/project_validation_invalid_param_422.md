---
name: validation-invalid-param-422
description: INVALID_PARAMETER is 422 (not 400) per API.md and VALIDATION.md; watch tests claiming a 400 spec/impl discrepancy
metadata:
  type: project
---

For the Validation feature, `INVALID_PARAMETER` maps to **HTTP 422**, confirmed in
two spec locations: API.md §Application Error Codes table (the `INVALID_PARAMETER`
row) and VALIDATION.md §Validation rules on POST ("data_time ... otherwise 422
INVALID_PARAMETER"). The impl agrees: `src/api/main.py` `_handle_validation` /
`_handle_request_validation` both return 422 for Pydantic/RequestValidationError.

**Why:** The UC2 test pass (2026-06) shipped test docstrings/comments asserting
`INVALID_PARAMETER` is HTTP 400 and that there is a spec-vs-impl "discrepancy
to track separately." There is no such discrepancy — spec and impl both say 422.
The `assert ... == 422` assertions were correct; only the prose was wrong.

**How to apply:** When reviewing validation tests, if a comment/docstring claims
INVALID_PARAMETER is 400 or references a 400-vs-422 discrepancy, flag it as a
documentation defect (low severity — assertion is right, prose misleads a future
maintainer into weakening a correct 422 assertion). Do NOT accept "carried over
verbatim, pre-existing discrepancy" as justification without re-reading API.md
line ~783 and VALIDATION.md §Validation rules on POST.
