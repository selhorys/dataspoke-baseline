Analyze the following GitHub issue and produce an implementation plan.

## Issue #{number}: {title}

{body}

## Instructions

Produce an implementation plan for the issue above:
- Files to create or modify
- Order of changes
- Existing patterns to follow
- Tests needed (unit and integration)
- Risks or open questions

The plan is executed by `.claude/workflows/wf-minimal.js`, which runs generator stages each paired with an adversarial reviewer. End the plan with a metadata block the orchestrator parses. Use this exact heading and these exact field names, and put NO `---` separator after it:

## PRauto Execution Metadata

- **Stages**: a JSON array of the generator stages this plan needs, in execution order, drawn ONLY from `spec`, `backend`, `airflow-dag`, `test`, `frontend`. Group stages that may run concurrently in an inner array. NEVER include `k8s-helm` — deploys are orchestrator-owned. Lead with `spec` when the plan changes specs, so later stages read the updated spec. Example: `["spec", ["backend", "airflow-dag"], "test", "frontend"]`.
- **Security**: a JSON array naming the subset of Stages whose diff touches the sensitive paths listed in `.claude/agents/security-reviewer.md` (auth, DataHub emission/write paths, secret resolution, `src/shared/settings.py`, dependency manifests, helm secrets/values, `.prauto/**`, install-time orchestration). Empty array `[]` if none.
- **Skip-plan eligible**: `yes` only if your plan meets ALL of CLAUDE.md's skip-plan criteria — touches < 3 files AND < 60 lines of logic; introduces no new API endpoint, DB table/column, pgvector collection, or Airflow DAG; and requires no cross-layer coordination. Otherwise `no`. Judge your own plan, not the issue's self-description. When unsure, answer `no`.
- **Skip-plan rationale**: one line stating which criteria decided the answer.

When your plan is complete, use the Write tool to save the full plan — including the metadata block — to this exact path:

  {plan_file}

This file is the ONLY artifact captured from this session and will be posted to the GitHub issue for human review.

Do NOT make code changes. Analysis only.
