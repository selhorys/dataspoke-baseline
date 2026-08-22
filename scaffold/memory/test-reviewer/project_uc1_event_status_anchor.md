---
name: uc1-event-status-anchor
description: The strongest spec anchor for INGESTION event status='success' is USE_CASE_en.md ~L107-108, not BACKEND.md step 4; the Event Catalogue DOES spec detail.execution_request_urn as the identity key
metadata:
  type: project
---

UC1 ingestion-event `status` value vocabulary is golden-spec'd: `USE_CASE_en.md` ~line 107-108 —
"Each event row carries an event_type (INGESTION.COMPLETE on success, INGESTION.FAIL on failure)
and a matching status (success / failure)."

**Why:** test authors keep citing `feature/BACKEND.md §Sync sweep step 4` for `status=='success'`,
but step 4 only maps DataHub exec status → event_TYPE (COMPLETE/FAIL), not to the `status` string.
The status string vocabulary lives in USE_CASE (golden) and API.md (`event/success`/`event/failure`
narrowing). The EventStatus enum (src/shared/models/enums.py) is impl, not the anchor.

**How to apply:** when reviewing UC1 ingestion tests asserting `status=='success'`, accept the
assertion — it is spec-derived — but the cleanest citation is USE_CASE_en.md L107-108. A BACKEND.md
step-4 citation is one inferential hop off; flag only as a low/advisory citation-precision nit,
never as impl-pinning.

**`detail.execution_request_urn` IS spec'd** (verified 2026-07-31 against
`spec/feature/BACKEND.md` §Event Catalogue, the INGESTION row ~L1260): "For sync-mirrored
`DATAHUB_MANAGED` rows, `detail.execution_request_urn` is the **identity key** (not merely
informational): the sweep upserts at most one event per execution-request URN per source (see step
4)." An earlier revision of the catalogue named no detail keys — do not carry that forward. A test
citing `BACKEND.md §Event Catalogue` for that key is correct; a comment calling it "an impl detail,
not a spec'd event-detail key" (still present in the E2E twin
`tests/e2e/use-case/uc1-01-datahub-managed.spec.ts:36-37`) is wrong. Related:
[[recipe-mask-string-divergence]], [[uc1-01-wrapper-flag-assertion-gap]].
