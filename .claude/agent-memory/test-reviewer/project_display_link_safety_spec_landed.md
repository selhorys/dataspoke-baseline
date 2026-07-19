---
name: display-link-safety-spec-landed
description: The display-URL safety guard IS now specced (API.md §Data Resource "Display-link safety" 5-row table); residual gap is the Length row having no Python coverage
metadata:
  type: project
---

`spec/API.md` §Data Resource now carries a **Display-link safety** block: a rule-class
table (Scheme / Authority / Characters / Shape / Length) plus a dual-boundary paragraph
("On write, `PATCH /admin/peripherals/{datahub,langfuse}` rejects … with `422`. On read,
`GET /spoke/common/peripheral-links` coerces one to `""`"). `spec/feature/FRONTEND_BASIC.md`
§Shell covers the client re-check of env-sourced values. This supersedes the earlier
"guard is unspecced" finding — **the amendment landed**; do not repeat it.

Shape artifacts that used to look like impl-pinning are now spec-sanctioned and correctly
labelled in `tests/fixtures/safe-url-cases.json`:
- `https://evil.com?x=1` rejected / `https://evil.com/?x=1` accepted — the spec says Shape
  "is a grammar constraint, not an anti-spoofing rule", and the corpus asserts both sides
  with an integrity test that fails a Shape rejection lacking its accepted slash twin.
- `HTTPS://host` rejected — spec calls lowercase-only "a strictness choice … not a safety
  property".

**Residual gap (verified by mutation, 2026-07-19):** every `project_id` corpus case is
labelled `rule: "Length"` (defensible — the spec's Length row contains the "alphanumeric
slug" clause), which makes the corpus integrity test's "every rule class is exercised"
assertion pass while **no Python test covers an actual length bound**. Deleting both
`if len(value) > …MAX_LENGTH: return ""` guards from `sanitize_display_url` /
`sanitize_project_id` leaves all 2354 unit tests green; widening the constants is caught
only by a pre-existing Langfuse-`host` test. The TS side does cover at/over bounds.

**How to apply:** treat `spec/API.md` §Data Resource → Display-link safety as the valid
anchor for these tests. If new corpus cases appear, check the Length row specifically —
it is the one class whose citation is satisfied without exercising the constraint.

Related: [[waitfor-presettlement-race]]
