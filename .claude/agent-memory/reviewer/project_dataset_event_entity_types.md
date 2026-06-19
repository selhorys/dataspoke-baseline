---
name: dataset-event-entity-types
description: Only validation + metagen-candidate events are booked on entity_type="dataset"; ingestion runs live on the source
metadata:
  type: project
---

In the `events` table, `entity_type="dataset"` rows are written by ONLY two writers:
`ValidationService` (`VALIDATION.*` prefix) and metagen candidate review
(`_record_dataset_event` → `METAGEN.*`). Ingestion run events are booked on
`entity_type="ingestion_source"` (entity_id = source_id), never on the dataset, and
are projected onto a dataset's timeline via `IngestionService.reverse_lookup` +
`get_events_for_source` (the source+CLI-wrapper union).

**Why:** the unified per-dataset timeline (`DatasetService.get_events`,
`GET /spoke/common/data/{urn}/event`) unions dataset-level events with the covering
source's runs. When reviewing that union, no stray governance/other event types leak
into the dataset branch — so a default (unfiltered) timeline = ingestion ∪ validation
∪ metagen exactly, matching API.md and BACKEND.md.

**How to apply:** when reviewing changes to the dataset event timeline, you can trust
the `entity_type="dataset"` query returns only VALIDATION/METAGEN rows without an
explicit event_type prefix filter. Re-verify with `grep -rn 'entity_type="dataset"'`
over event writers if a new feature might add a third dataset-event writer.
