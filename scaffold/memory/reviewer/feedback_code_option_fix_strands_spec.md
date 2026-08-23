---
name: code-option-fix-strands-spec
description: When a finding offers "fix the code OR record it in the spec" and the generator picks the code option, the spec silently becomes wrong — re-read the spec text on the re-review, because the generator is forbidden from editing spec/
metadata:
  type: feedback
---

A finding phrased as "either change the code or have the spec agent record the caveat"
leaves a trap: whichever option the generator picks, **the other document moves**. Code
stages are forbidden from editing `spec/`, so choosing the code option strands a spec
paragraph that now describes behaviour the tree no longer has — and the generator can only
"defer to the spec agent" in its report, which nothing enforces.

**Why:** on the `is_primary` backend re-review two spec statements were left contradicting
the fixed code, both introduced by the *same* change set's Stage A one step earlier:

- `spec/DATAHUB_INTEGRATION.md` still ordered the `siblings` derivation emptiness-first
  while `_sibling_is_primary` had been reordered flag-first, so
  `{"isPrimary": false, "siblings": []}` was documented `true` and implemented `false`.
- `spec/feature/BACKEND.md` still said "**Every literal compiles to a bound parameter** —
  string literals and the `is_primary` boolean alike" after the boolean was deliberately
  switched to an inline `= false` constant. That one is worse than stale: it prescribes
  the exact bug the fix pass had just removed, so a maintainer obeying the spec would
  reintroduce `= $1` and silently lose the partial index.

**How to apply:** on any re-review after a fix pass, diff the spec sections the original
findings cited, not just `src/`. A fix that changes an observable branch or a compiled SQL
shape almost always invalidates a sentence Stage A wrote. Report those at the same severity
as the code bug and tag them for the spec agent / orchestrator; do not send them back to the
code generator. Related: [[grep-old-rule-prose-in-consumers]],
[[pg-is-false-vs-partial-index]].
