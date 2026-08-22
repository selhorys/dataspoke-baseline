---
name: per-run-workflow-fail-open
description: Per-run .claude/workflows/wf-*.js scripts are hand-written per task and routinely regress wf-minimal.js's fail-closed reviewer handling — the three patterns to diff for, and the one that actually skipped a security sign-off
metadata:
  type: project
---

Every multi-stage run gets a **fresh, hand-written** `.claude/workflows/wf-<task>.js`
(plan approval is the opt-in). `wf-minimal.js` is the checked-in reference and
gets the reviewer semantics right; the per-run scripts are re-derived from
scratch and drift.

**Why this is mine to review:** `.claude/workflows/**` is on my own
sensitive-path glob list. The script decides whether a `security-reviewer`
verdict is collected, merged, and acted on at all — a bug there is
indistinguishable from an APPROVE.

**The three regressions to diff for** (all correct in `wf-minimal.js`; all were
present in `wf-is-primary.js` at first review and all three are fixed there now):

1. **Retry by index, not by identity.** A bogus-ESCALATE retry of
   `reviewSpecs.slice(0, bogus.length)` re-runs the *wrong* reviewer: with
   `[reviewer, security-reviewer]`, a lone bogus ESCALATE from
   `security-reviewer` re-runs `reviewer`, and the merged verdict then holds two
   spec-compliance reviews and zero security reviews. Fix is to filter the
   pairs whose review was bogus and re-run *those specs*.
2. **`results.filter(Boolean)` with no length check.** If every reviewer agent
   fails (returns null), `reviews` is `[]`, no REVISE is found, and the stage
   logs "APPROVED on the first pass". Must compare against `reviewers.length`
   and halt.
3. **Re-review by `reviewSpecs[0]` only.** After a fix pass driven by security
   findings, only `reviewer` verifies the fix. **This one has actually fired:**
   the `is_primary` run's backend fix pass was re-reviewed by `reviewer` alone
   and the security sign-off was skipped outright, recovered only by a manual
   re-review afterwards. A null `recheck` falling through to "approved after one
   fix pass" is the same bug's second half.

**How to apply:** on any run where a `wf-*.js` is new or changed, diff it
against `.claude/workflows/wf-minimal.js` and check exactly these three things
before reading a line of the generator's code — (a) is a failed/absent reviewer
verdict fail-closed, (b) are retries keyed on *which* reviewer failed rather
than on a count, (c) does the post-fix re-review re-run the **full** reviewer
set including `security-reviewer`. Also confirm the stage wiring actually
attaches `security-reviewer` to whichever stage owns the sensitive paths.
Report to the orchestrator; do not edit the workflow yourself — same rule as
[[reviewer-config-is-generator-writable]]. Distinct from
`project_workflow_escalate_vs_agent_failure` in the *user's* memory, which
covers how to react to a bogus ESCALATE, not how the script mishandles one.

Related: [[reviewer-config-is-generator-writable]]
