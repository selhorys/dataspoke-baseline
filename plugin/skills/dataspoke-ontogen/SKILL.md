---
name: dataspoke-ontogen
description: "Operate DataSpoke Ontology Generation (UC3) on a deployed instance: configure the singleton, curate Markdown seeds, dry-run and run inference, inspect events and ontology results, and review nodes, edges, then eligible triples. Use for ontology inference, business concepts, relationships, ontology seeds, or ontology review."
allowed-tools: Read, Write, Bash(dataspoke-api *), Bash(dataspoke-schema *), AskUserQuestion
---

## Purpose and boundary

Drive UC3 only through the deployment's authenticated **public API** using `dataspoke-api`. This
skill never reads or changes DataHub directly, source code, the database, Helm, Kubernetes, or an
LLM provider. If access is missing, send the user to `/dataspoke:dataspoke-access`.

Ontology Generation is one global artifact: its operational configuration, inference seeds, runs,
and results are shared across the estate. Explain that a node is a business concept, an edge a
relationship predicate, and a triple a subject--predicate--object fact. It is not a metadata
description writer; use `/dataspoke:dataspoke-metagen` for that.

## Live contract and safe operating sequence

The deployment OpenAPI is authoritative. Before every meaningful write, load the narrow live
operation schema with `dataspoke-schema` (first `--list` to discover a route where needed, then
without it for fields, content type, and response). Do not invent fields or enums from this guide.
Give `/redoc` from the configured `redoc_url` to a human who wants to browse the contract.

1. Read `GET /auth/me`, then inspect the current conf, relevant seed/result, and events.
2. Load the matching live write schema immediately before constructing its payload.
3. Show the exact method, public path, body, scope, and state effect. Obtain explicit confirmation
   **immediately before** every conf/seed write, seed enable/disable, delete, non-dry run, and
   node/edge/triple verdict. A dry run also needs the user to request it, but does not persist.
4. For inference, always run `?dry_run=true` first and show its result before asking to run it
   non-dry. Then inspect `/event` and result collections; do not claim a run completed merely
   because its trigger accepted.

Writes need Editor or Admin. Surface `403 READ_ONLY_ROLE` and direct the user to access instead
of trying another interface. All list feeds are paginated: use `limit`, `offset`, and their
`total_count`; repeat pages before describing a collection as complete.

## Capabilities to public routes

| Intent | Public call |
|---|---|
| Read singleton configuration | `GET /spoke/ontogen/attr/conf` |
| Create/replace or partially edit configuration | `PUT` or `PATCH /spoke/ontogen/attr/conf` with live-schema JSON |
| Remove configuration (disables ontogen) | `DELETE /spoke/ontogen/attr/conf` |
| List seed summaries / fetch one Markdown seed | `GET /spoke/ontogen/attr/seed?limit=…&offset=…`; `GET /spoke/ontogen/attr/seed/{seed_id}` |
| Create / replace a Markdown seed | `POST /spoke/ontogen/attr/seed`; `PATCH /spoke/ontogen/attr/seed/{seed_id}` (`Content-Type: text/markdown`) |
| Enable or disable a retained seed | `PATCH /spoke/ontogen/attr/seed/{seed_id}/attr/enabled` with `{"is_enabled":true|false}` |
| Hard-delete a seed | `DELETE /spoke/ontogen/attr/seed/{seed_id}` |
| Preview or persist an inference run | `POST /spoke/ontogen/method/run?dry_run=true`; then `POST /spoke/ontogen/method/run` |
| Global run history | `GET /spoke/ontogen/event?limit=…&offset=…` |
| List/detail/history for a result type | `GET /spoke/ontogen/result/{node|edge|triple}`; `…/{id}`; `…/{id}/event` |
| Review a result type | `POST /spoke/ontogen/result/{node|edge|triple}/{id}/method/review` with `{"verdict":"approve"|"reject","reason":"…"}` |

`attr/conf` has `is_enabled`, `schedule_tier`, `dataset_filter`, and
`default_run_prompt`; validate them against the live schema. Explain the effect of enabling a
schedule and confirm it as a meaningful write. `dataset_filter` uses the DataSpoke dataset-registry
grammar, not arbitrary SQL or DataHub search; show `422 INVALID_DATASET_FILTER` detail and its
position without silently broadening scope. Prompt is capped at 16,000 chars, filters at 8,000
chars / 1,000 literals, Markdown seeds and one-shot prompts at 128 KiB, and review reasons at
2,000 characters.

## Seeds and runs

Seeds are human-authored Markdown domain hints, prompts, or naming conventions. A newly created
seed is **disabled**: it remains visible but does not influence inference until an explicitly
confirmed enable. Read the whole seed before replacing or deleting it; a delete is hard deletion.

Seed bodies and optional one-shot run prompts require `Content-Type: text/markdown`. Write the
Markdown to a scratch file with the `Write` tool and send it via `dataspoke-api`'s `@PATH` body
form — never serialize Markdown as JSON, and never pass a multi-line body as a literal shell
argument (a real seed routinely spans multiple lines and can contain characters that break naive
shell quoting; `@PATH` sidesteps the whole class of mistakes). After the live-schema check and
immediate confirmation:

```bash
dataspoke-api --confirm POST /spoke/ontogen/attr/seed --content-type text/markdown @/tmp/seed.md
dataspoke-api --confirm PATCH /spoke/ontogen/attr/seed/{seed_id} --content-type text/markdown @/tmp/seed.md
dataspoke-api --confirm POST '/spoke/ontogen/method/run?dry_run=true' --content-type text/markdown @/tmp/prompt.md
```

Use the same `--content-type text/markdown` `@PATH` form for the separately confirmed non-dry
prompted run. A bodyless run is simply `dataspoke-api --confirm POST /spoke/ontogen/method/run`;
do not pass `{}` or claim a one-shot prompt was persisted.

A run without a body uses `default_run_prompt`; a one-shot Markdown prompt augments persistent
enabled seeds for that run only and is not saved. A dry run evaluates but persists nothing. A
non-dry run requires enabled conf, explicit immediate confirmation, and a successful dry-run
result first. After it, poll the event list and inspect results. Never paper over:

- `409 ONTOGEN_RUNNING`: another inference is active; inspect events rather than starting one.
- `409 ONTOGEN_DISABLED`: enable/configure deliberately, or use a dry run.
- `404` for a missing conf, seed, or result: inspect the identifier; never turn an update into an
  implicit create.

## Review workflow: nodes → edges → triples

Default queues to `?status=llm_pending` or `llm_approved`, inspect each detail and its event
history, confidence score, `run_id`, and (for nodes) member datasets. `run_id` identifies the
inference run behind the row; it is not itself a review verdict.

Review each proposed result only after displaying its exact `{verdict, reason}` body and getting
immediate confirmation. Review **nodes first**, then **edges**, then triples:

1. Approve or reject the endpoint nodes; `llm_approved` is not human approval.
2. Approve or reject the edge.
3. Fetch the triple detail and verify its subject node, object node, and edge all have
   `status: "approved"`; only then offer its review.

If the API returns `422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING`, stop, name the unapproved
dependencies from the detail response, and return to the node/edge queues. Never bypass the gate
or infer eligibility from confidence. A reject is a meaningful mutable verdict and needs the same
confirmation as approval.

## Reading results and errors

Use `created_at_asc|created_at_desc` and `status` filters with pagination for nodes, edges, and
triples. Report confidence as model output, not proof. A `run_id: null` denotes a seeded row,
whereas a UUID identifies the inference run that produced it. Read item events for lifecycle
context and global events for run completion/failure.

For any route/schema mismatch, return to the narrow live OpenAPI fragment. Surface `401`, `403`,
`404`, `409`, and `422` response bodies verbatim enough for the user to act; never retry with a
different scope, silently create a new resource, or expose credentials.
