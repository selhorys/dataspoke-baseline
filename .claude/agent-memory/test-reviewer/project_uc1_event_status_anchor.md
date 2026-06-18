---
name: uc1-event-status-anchor
description: The strongest spec anchor for INGESTION event status='success' is USE_CASE_en.md ~L107-108, not BACKEND.md step 4
metadata:
  type: project
---

UC1 ingestion-event `status` value vocabulary is golden-spec'd: `USE_CASE_en.md` ~line 107-108 —
"Each event row carries an event_type (INGESTION.COMPLETE on success, INGESTION.FAIL on failure)
and a matching status (success / failure)."

**Why:** test authors keep citing `feature/BACKEND.md §Sync sweep step 4` for `status=='success'`,
but step 4 (BACKEND.md ~L354-366) only maps DataHub exec status → event_TYPE (COMPLETE/FAIL), not to
the `status` string. The status string vocabulary lives in USE_CASE (golden) and API.md ~L723
(`event/success`/`event/failure` narrowing). The EventStatus enum (src/shared/models/enums.py) is
impl, not the anchor.

**How to apply:** when reviewing UC1 ingestion tests asserting `status=='success'`, accept the
assertion — it is spec-derived — but the cleanest citation is USE_CASE_en.md L107-108. A BACKEND.md
step-4 citation is one inferential hop off; flag only as a low/advisory citation-precision nit, never
as impl-pinning. INGESTION Event Catalogue row (BACKEND.md ~L948) names NO detail keys, so
`detail.execution_request_urn` / `detail.source` are impl-only — see [[recipe-mask-string-divergence]]
for the analogous spec-vs-impl distinction.
