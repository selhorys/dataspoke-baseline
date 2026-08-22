---
name: regex-bounded-matcher-facts
description: The `regex`-module + timeout= mitigation for issue #114 was built, reviewed and REJECTED — it trades a bounded match time for an unbounded, uninterruptible compile time that stdlib `re` does not have
metadata:
  type: project
---

Issue #114 (writer-supplied recipe regexes run unbounded in the sync sweep) proposed
swapping the matching path from stdlib `re`/`AllowDenyPattern` to the `regex` module,
whose per-call `timeout=` bounds catastrophic backtracking. That approach was
implemented end to end, reviewed twice, and **rejected by the human architect**. The
tree stays on stdlib `re`; #114 remains open. Measured on this tree (Python 3.13,
regex 2026.5.9, API pod limit 1024Mi, cpu limit 1, liveness 10s x 6 = 60s):

- The `timeout=` mechanism itself **works** — `(a|a)+$` raises `TimeoutError` at the
  cap instead of hanging. That part was never in doubt.
- **What killed it: a size cap cannot bound `regex` compile *time*.** At an identical
  `product x length` of ~500k units, cost spans five orders of magnitude by *construct
  shape*: 990 literals x{500} = 0.001 s, but a repeated empty capturing group
  `(?:()x495){500}` (999 chars) = **464 s**, growing superlinearly (n=20 → 0.4s,
  40 → 1.7s, 80 → 8.2s, 160 → 44s). stdlib `re` compiles the same pattern in
  **0.0004 s**. Compilation is one uninterruptible call, so no budget checked
  *between* compiles bounds it — the swap introduced a worse DoS than the one it fixed.
- **`regex` expands mandatory counted repeats at compile time and `re` does not.**
  `(?:a{1000}){1000}` (17 chars): `re` 0.000s / +0 MB, `regex` 0.110s / **+291 MB**.
  Cost scales as `repeat_product x body_size`, so a cap on repeat count or length
  alone is decorative.
- **Compile memory worst case inside any size cap is ~200 MB.** `(?:(|)x330){500}`
  retains **212 MB**. Per-node cost by construct: literal ~4 B, `[a-z]`/`\p{L}` ~150 B,
  `(a)` ~555 B, `(|)` ~640 B — a ~150x spread, which is why two successive
  "certified worst cases" were both wrong.
- `regex` + `timeout=` is also **~3.9x slower** than the `re`-based `AllowDenyPattern`
  (~2.1 microseconds per name), and `regex` VERSION0 is not byte-identical to `re`:
  `\p{L}`, `\X`, `(?R)`, fuzzy `{e<=n}` and duplicate group names compile under
  `regex` and raise under `re`, widening the accepted grammar at a trust boundary.

**Why:** these numbers are what ruled out the issue's own option 1 (the `regex`
module), leaving out-of-process evaluation as the only mitigation that bounds compile
time, compile memory and match time together.

**How to apply:** if #114 is picked up again, do not re-propose the in-process `regex`
swap without an out-of-process compile gate — and re-run the construct-shape sweep
(not the plan's example pattern) before accepting any claim that a cap bounds
compilation. Related: [[str-iterated-as-pattern-list]],
[[resource-cap-search-worst-case-inside-the-cap]]
