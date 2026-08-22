---
name: metagen-dropall-run-is-specced
description: BACKEND_LLM.md 309-310 explicitly sanctions a real-LLM metagen run persisting ZERO llm_approved candidates — the anchor for judging any UC4 test that hard-fails on a missing candidate
metadata:
  type: project
---

`spec/feature/BACKEND_LLM.md` §Metagen Adversarial Debate (the ontogen-vs-metagen
differences table) states two rules that make a **candidate-free metagen run a normal,
spec'd outcome under a real LLM**:

- L309 *Persistence threshold*: "**Below-threshold candidates are dropped** — metagen has
  no `llm_pending` state. Only candidates with `outcome=accept` AND
  `confidence_score >= METAGEN_CONFIDENCE_THRESHOLD` persist as `status='llm_approved'`."
- L310 *Termination*: "`accept` → persist surviving candidates as `llm_approved`;
  `turns_exhausted` / `cycle_detected` → **drop all candidates from this run**. The next
  scheduled run is the recovery path."

L254 adds there is no `llm_rejected` status, and L262 that metagen has no `llm_pending`
— so a dropped candidate leaves **no row at all**, not a differently-statused one.

**Why it matters:** UC4 tests (E2E `use-case/uc4-01-metadata-generation.spec.ts` steps 7/8,
and the api-wired twin) assert an `llm_approved` candidate exists on a specific seeded slot.
Under the stub that is deterministic (L350-351: one candidate per TARGET ITEM, Reviewer
returns `overall_verdict="accept"`). Under a real LLM it is **not** guaranteed by the spec,
so a hard failure there reports a spec-sanctioned drop-all run as a product defect. The
seeded-masking precondition (`--uc4-seed`) only proves the slot is *in scope*; persistence
still depends on `debate_outcome` + the confidence threshold, i.e. model behaviour.

**How to apply:** when a UC4 test claims its cross-mode candidate assertion "rests on a
seeded precondition rather than on model behaviour", that claim is false — say so. The
spec-derived refinement is available in-file: `METAGEN.RUN_COMPLETE` detail carries
`debate_outcome` and `counts.candidates_added` (BACKEND.md §Event Catalogue L1262), and
uc4's real-LLM add-on already reads them. Note also BACKEND_LLM.md L356-358: the spec's own
dual-mode guidance is that *content* assertions are stub-guarded and UC4 keeps separate
`_under_stub` / `_with_real_llm` tests. Related: [[e2e-beforeall-skip-blast-radius]].
