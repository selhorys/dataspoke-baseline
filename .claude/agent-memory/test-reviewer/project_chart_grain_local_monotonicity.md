---
name: chart-grain-local-monotonicity
description: DST fall-back does NOT break lexicographic monotonicity of chart-grain bucket labels — it breaks distinctness; a common but wrong rationale for restricting sweeps to tz="utc"
metadata:
  type: project
---

A recurring (and wrong) claim in `lib/chart-grain` test comments: *"a local wall clock is not
monotonic in absolute time across a DST fall-back, so the label-ordering property would be
FALSE for `tz='local'`."*

**Measured false.** Over the 500-instant seeded sweep in `lib/chart-grain.test.ts`, the
non-decreasing property `bucket(tᵢ₋₁) <= bucket(tᵢ)` holds with **zero violations** at
`tz="local"` under `America/New_York`, `Europe/Berlin`, `Australia/Lord_Howe` and `Asia/Seoul`,
at all three grains. A fall-back makes the repeated hour produce the *same* label twice
(01:00 EDT and 01:00 EST both → `"… 01:00"`), which still satisfies `<=`. What DST actually
breaks is **distinctness** — the spec's "Every x label is therefore distinct" — not ordering.

**Why:** the rationale reads plausibly and justifies narrowing a property test's domain, so it
survives review unchallenged and quietly retires a case the impl does satisfy.

**How to apply:** restricting such a sweep to `tz="utc"` is still fine — the *host-independence*
argument is sound (the companion non-degeneracy guards like `weeks.size > 100` would become
host-dependent at local). Ask for the comment to be corrected to that reason rather than the
DST-monotonicity one. Related: [[vitest-tz-pin-isolation]].
