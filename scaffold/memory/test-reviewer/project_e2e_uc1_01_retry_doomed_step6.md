---
name: e2e-uc1-01-retry-doomed-step6
description: uc1-01 step 6's pipeline_name/high assertion cannot survive a Playwright serial-group retry — the retry creates a new source URN and DataHub aspect dedup keeps the old one
metadata:
  type: project
---

`tests/e2e/use-case/uc1-01-datahub-managed.spec.ts` keeps the project-level `retries: 1` under
`mode: "serial"`, justified in-file as "every step is re-runnable" because step 1 pre-deletes the
Secret + same-named IngestionSource. **That claim does not hold for step 6.**

Step 6's 6e assertion requires ≥1 dataset row with `derivation='pipeline_name'` /
`authority='high'`. That upgrade fires only when `systemMetadata.pipelineName == datahub_source_urn`.
Per issue #77 (root-caused, see the user-memory `project_pipeline_name_only_on_subtypes`),
`pipelineName` lands on **only the `subTypes` aspect**, because the reset-seed pre-emits every other
aspect without it and DataHub **dedups an unchanged aspect on re-ingest**. A group retry deletes
source URN A and creates URN B; the second managed run re-emits `subTypes="Table"` unchanged → no-op
→ the aspect keeps pipelineName=A → no match for B → the upgrade never fires.

So a step-6 failure burns a second, structurally doomed ~5-minute attempt, and the retry fails at 6e
for a harness reason unrelated to the original defect. Steps 1–5 *are* cleanly re-runnable.

**Why:** the E2E suite has no per-file dataset reset — `global-setup.ts` runs
`uv run python -m tests.integration.util --reset-seed` once for the whole run, and the util CLI
exposes no platform-scoped hard-delete flag. The api-wired twin solves this in its module fixture
(`reset_datasets(platform=PG_PLATFORM)` + `ingest_pg_datasets`); E2E has no equivalent.

**How to apply:** when auditing an E2E retry stance (`spec/TESTING.md` §E2E §Execution discipline —
"a file either makes every step re-runnable or sets `retries: 0`"), do not accept "setup pre-deletes
by natural key" as proof of re-runnability. Check what the *later* steps depend on in DataHub, not
just what setup creates. See the user-memory `project_e2e_skips_breakdown` for the cascade-skip
history (no corresponding note exists in this evaluator-memory corpus).
