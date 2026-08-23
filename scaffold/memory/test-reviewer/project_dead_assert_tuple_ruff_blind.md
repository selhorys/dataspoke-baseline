---
name: dead-assert-tuple-ruff-blind
description: TESTING.md bans trailing message tuples on mock assert_* calls, but ruff (select = E,F,I,UP) does not catch them. Verification technique promoted to scaffold/roles/test-reviewer.md §Verification methodology.
metadata:
  type: project
---

The rule itself is `spec/TESTING.md` §Assertion Discipline ("No dead assertion-message tuples").
The verification technique this note added — AST-scan rather than grep/ruff-trust, since this
repo's ruff selects only `E,F,I,UP` and does not catch it — now lives in
`scaffold/roles/test-reviewer.md` §Verification methodology.

**Incident kept for reference:** seen introduced by a *fix pass* — a cycle-1 finding asking for a
stronger `assert_awaited_once_with(...)` was implemented with an explanatory trailing tuple,
adding the violation while resolving the original issue.

Related: [[project-auth-email-storage-case-divergence]].
