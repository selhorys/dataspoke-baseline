---
name: peripheral-links-db-sole-source
description: datahub_url / langfuse_url / langfuse_project_id come only from GET /spoke/common/peripheral-links; runtime config keeps only apiBaseUrl + airflowUrl
metadata:
  type: project
---

Issue #78 (branch `fix/78-peripheral-links-db-sole-source`, 2026-07-22) deleted the env/chart
plane for the three peripheral display links. `peripheral_config` served by
`GET /spoke/common/peripheral-links` is now the **sole** source of `datahub_url`,
`langfuse_url`, `langfuse_project_id`. `RuntimeConfig` carries only `apiBaseUrl` and
`airflowUrl` (Airflow and ReDoc are deployment-local).

**Why:** any non-empty chart value permanently masked the DB value and the chart shipped a
placeholder default, so admin-UI peripheral wiring never reached the link.

**How to apply when reviewing tests here:**
- Spec anchors: `spec/feature/FRONTEND_BASIC.md` §Shell (resolution table + "nothing can mask
  what the DB holds") and its `DatahubDatasetLink` bullet under §Shared Component Notes;
  `spec/API.md` §Data Resource (endpoint row + Display-link safety table);
  `spec/feature/FRONTEND_ONTOGEN.md` §**Page contracts** (Evidence cell) — there is no
  `§Result table` heading, despite several tests citing one.
- What pins the regression: the exact-key-set assertions in `lib/runtime-config.test.ts`
  ("carries only the deployment-local fields") and `app/layout.runtime-config.test.tsx`
  ("injects exactly the two deployment-local keys"). Nothing pins it *inside*
  `lib/api/peripheral-links.ts` — a hook reading `process.env.NEXT_PUBLIC_DATAHUB_URL`
  directly would pass every Vitest and E2E test, because the chart no longer sets the var.
- Unspecced impl details that tests keep drifting onto: TanStack Query `data` retention across
  a refetch ("no flash"), `retry: 1`, the module-level query key, and
  `evidence-link.tsx`'s trailing-slash strip on `langfuse_url`.
- Dev cluster seeds `frontend_url` via `helm-charts/bin/post-install/seed-peripheral-config.sh`,
  so E2E DataHub-link assertions have a value; Langfuse seeding is gated on
  `DATASPOKE_TEST_LANGFUSE_HOST` + public key, so the Evidence-cell em-dash branch is reachable.

Related: [[display-link-safety-spec-landed]], [[waitfor-presettlement-race]]
