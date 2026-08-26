---
name: dataspoke-metagen
description: "Operate DataSpoke Metadata Generation (UC4) on a deployed instance: manage named generation configurations and per-dataset boundaries, dry-run and run generation, inspect coverage and candidates, and review documentation proposals. Use for AI documentation proposals, metadata generation, description candidates, or metadata review."
allowed-tools: Read, Bash(dataspoke-api *), Bash(dataspoke-schema *), AskUserQuestion
---

## Purpose and boundary

Drive UC4 only through DataSpoke's authenticated **public API** via `dataspoke-api`. Never access
DataHub directly, source code, the database, Helm, Kubernetes, or an LLM provider. If access is
missing, send the user to `/dataspoke:dataspoke-access`.

Metadata Generation proposes Markdown for editable DataHub descriptions only:
`dataset.description` maps to `editableDatasetProperties.description`, and
`column.<fieldPath>.description` maps to editable schema-field descriptions. It does not author
ontology structure (use ontogen) or write non-editable ingestion-owned descriptions.

## Live contract and safe operating sequence

The live deployment OpenAPI is the source of truth. Use `dataspoke-schema <fragment> --list` to
discover operations and the narrow command without `--list` immediately before every write for
the request shape, enums, and response. Give the configured `/redoc` URL to users who want a
human-readable browser.

Read the target conf/boundary/item first; then show exact method, public route, JSON body, target
scope, and consequences. Get explicit confirmation **immediately before** each configuration or
boundary create/update/delete, enable/disable, non-dry run, and candidate approval/rejection. Run
the same scoped request dry first, show its result, and only then ask for confirmation to run it
non-dry. Writes require Editor/Admin; surface `403 READ_ONLY_ROLE` rather than working around it.

Every list is paginated. Use `limit`, `offset`, and `total_count`, repeating pages before calling
a queue, catalog, coverage set, or event history complete.

## Capabilities to public routes

| Intent | Public call |
|---|---|
| List configurations | `GET /spoke/metagen/conf?limit=…&offset=…` |
| Create a configuration | `POST /spoke/metagen/conf` with live-schema JSON |
| Read, replace, patch, delete a named conf | `GET` / `PUT` / `PATCH` / `DELETE /spoke/metagen/conf/{conf_id}` |
| Dry-run / run a conf, optionally narrowed to URNs | `POST /spoke/metagen/conf/{conf_id}/method/run?dry_run=true`; then `POST …/method/run` with optional `{"dataset_urns":[…]}` |
| Per-conf run history and covered dataset boundaries | `GET /spoke/metagen/conf/{conf_id}/event`; `GET …/dataset?include_disallowed=true` |
| No-conf / boundary-blocked coverage gap list | `GET /spoke/metagen/uncovered?include_disallowed=true` |
| Cross-conf events, global items, and per-dataset rollup | `GET /spoke/metagen/event`; `GET /spoke/metagen/item`; `GET /spoke/metagen/dataset` |
| Global item detail | `GET /spoke/metagen/item/{dataset_urn}::{item_id}` |
| Read/write a dataset participation boundary | `GET` / `PUT` / `PATCH` / `DELETE /spoke/common/data/{dataset_urn}/attr/metagen/boundary` |
| Dataset item list/detail and events | `GET …/attr/metagen/item`; `GET …/item/{item_id}`; `GET …/event/metagen` |
| Review a candidate | `POST …/attr/metagen/item/{item_id}/candidate/{candidate_id}/method/review` with `{"verdict":"approve"|"reject","reason":"…"}` |

Conf bodies have `name`, `is_enabled`, `schedule_tier`, `dataset_filter`, `result_limit`, and
`overwrite_pending`; verify exact requirements live. `result_limit` is 1--20 and the filter is a
DataSpoke dataset-registry predicate, not arbitrary SQL or DataHub search. Do not alter an invalid
filter's scope silently: show `422 INVALID_DATASET_FILTER` detail and position. Explain that a
conf's enabled schedule starts work according to its tier before asking confirmation.

## Configuration, boundaries, and runs

Confs are a collection, not an upsert: duplicate names return `409 METAGEN_CONF_EXISTS`; inspect
the existing conf and ask whether the user wants an explicit patch/replacement or a new name.
Deleting a conf hard-deletes the definition but retains its items, candidates, and embeddings as
orphaned parentless results; it does **not** undo descriptions already approved in DataHub. State
that consequence and confirm immediately before deletion.

A dataset is eligible only when it matches an enabled conf's `dataset_filter` **and** has a
boundary with `is_enabled: true`; the boundary `allowed` array caps permitted kinds for every
conf. Read it before changing it. A missing boundary reads as `200 null`; it is not an error.
Deleting a boundary excludes future runs, while disabling or setting `allowed: []` blocks
generation but retains the row. Explain the precise scope change before confirmation.

For a run, first use the same conf and optional `dataset_urns` body with `?dry_run=true`. A dry
run evaluates without persisting candidates. Only after displaying it may a separately confirmed
non-dry run create candidates. Check per-conf and global events afterward. Surface and stop on:

- `409 METAGEN_RUNNING`: this conf already has a run; inspect its event feed.
- `409 METAGEN_DISABLED`: deliberately enable it or use dry-run; do not bypass the policy.
- `404 METAGEN_CONF_NOT_FOUND`: inspect the identifier; do not treat a failed update as create.

Use `/conf/{id}/dataset?include_disallowed=true` to explain matching datasets and blocked reasons;
use `/uncovered?include_disallowed=true` for `no_conf_match` versus `boundary_blocked`, not as a
claim that all datasets are eligible.

## Browse and review the global mutable queue

Items are shared by `(dataset_urn, item_id)` across all confs. Filter the global list by
`dataset_urn`, `kind`, `status`, or `conf_id`; inspect the global composite detail before a
verdict, then send reviews only through the dataset route. Per-dataset item lists have a separate
candidate count from their item `total_count`; keep those concepts distinct.

Before every review, show candidate value, confidence, producing `conf_id`/`conf_name`, current
item siblings, and exact `{verdict, reason}` JSON. State these consequences and obtain immediate
confirmation:

- **Approve is global and mutable.** It writes the candidate Markdown to the editable DataHub
  description and locks the item across every conf. Approving a different sibling later, including
  from another conf, atomically demotes the prior approved candidate to `llm_approved` and replaces
  the editable description.
- **Reject is also mutable.** Rejecting an `llm_approved` candidate only changes its status.
  Rejecting an already `approved` candidate changes it to `rejected` **and removes the editable
  DataHub description it wrote**. Never portray rejection as a harmless queue cleanup.
- While any candidate remains approved, every conf skips the item on later runs. A conf clears its
  rejected candidates at the start of its next run. Do not promise rejected content is retained.

The server returns `422 METAGEN_DATASET_NOT_IN_BOUNDARY` if the dataset lacks an enabled boundary:
stop and show it; do not create or enable a boundary without a new, explicit confirmation.

## Reading results and errors

Use `/metagen/dataset` for rollups; its counts are candidate-level and can be scoped with
`conf_id`. Use `/metagen/item/{composite_id}` for all candidates across confs; a list item does not
itself expose candidate provenance. Report candidate confidence as model output, not fact.

Candidate Markdown is capped at 16 KiB and review reason at 2,000 characters. For schema or route
differences, reload the narrow live OpenAPI and follow it. Surface 401/403/404/409/422 bodies and
never retry by silently broadening a dataset filter, writing a boundary, changing a verdict, or
exposing credentials.
