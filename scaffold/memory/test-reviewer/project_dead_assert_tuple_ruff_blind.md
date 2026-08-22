---
name: dead-assert-tuple-ruff-blind
description: TESTING.md bans trailing message tuples on mock assert_* calls, but ruff (select = E,F,I,UP) does not catch them — AST-scan changed test files instead of trusting "ruff clean"
metadata:
  type: project
---

`spec/TESTING.md §Assertion Discipline` forbids `mock.assert_called_once(), ("msg")` —
the mock `assert_*` family takes no message argument, so the trailing tuple is dead code.

**Why:** ruff in this repo selects only `["E", "F", "I", "UP"]` (`pyproject.toml:64`).
flake8-bugbear's `B018` (useless expression) is **not** enabled, so ruff reports
"All checks passed" on files containing the banned pattern. A test agent's
"`ruff check` clean" claim is therefore not evidence against this rule.

**How to apply:** on every test review, AST-scan the changed test files rather than
grepping or trusting the linter. `ast.Expr` whose `.value` is an `ast.Tuple` whose
first element is an `ast.Call` with `"assert"` in the unparsed func name catches all
occurrences in one pass across unit + integration files.

Seen introduced by a *fix pass*: a cycle-1 finding asking for a stronger
`assert_awaited_once_with(...)` was implemented with an explanatory trailing tuple,
adding the violation while resolving the original issue. Fix passes are a likely
source of this pattern.

Related: [[project-auth-email-storage-case-divergence]]
