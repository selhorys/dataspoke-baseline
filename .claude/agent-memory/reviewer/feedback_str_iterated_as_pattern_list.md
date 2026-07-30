---
name: str-iterated-as-pattern-list
description: A helper typed `list[str]` that loops its argument silently accepts a bare str and compiles per character — check every call site fed unvalidated JSONB
metadata:
  type: feedback
---

When a helper is annotated `def f(patterns: list[str])` and its body is
`for p in patterns: compile(p)`, a caller that passes a bare `str` gets **no
error** — the string is iterated character by character. `{"allow": "public"}`
becomes six single-character patterns and matches anything starting with p, u, b,
l, i or c.

**Why:** found in a rejected issue-#114 attempt that replaced DataHub's
`AllowDenyPattern` with a plain helper. `src/backend/ingestion/extractors.py` fed
`sp.get("allow", [".*"])` (writer-supplied JSONB, unvalidated) straight into it,
so a bare-string `allow` silently mis-scoped ingestion where the SDK's Pydantic
`List[str]` had raised loudly — a silent regression on the exact trust boundary
the change was hardening. mypy cannot see it because `dict.get` returns `Any`.
The attempt was reverted, so the SDK model guards this again today.

**How to apply:** whenever a generator replaces a validating construct (a Pydantic
model, an SDK config class) with a plain function, grep every call site for the
argument's provenance. If it comes from JSONB / a request body / YAML, prove the
shape is validated — do not assume the type hint is enforced. Run the degenerate
case (`f("string")`) in a REPL rather than reasoning about it.
Related: [[regex-bounded-matcher-facts]], [[shared-response-model-unpopulated-field]]
