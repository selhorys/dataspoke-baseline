# Backend LLM Pipeline

Cross-cutting patterns for every LLM call DataSpoke makes — the bounded ReAct
loop, the per-service validator tools, the adversarial debate layer, and
the test-mode toggles. Consumed by the Ontology Generation Service (UC3)
and the Metadata Generation Service (UC4).

## Table of Contents

1. [Scope](#scope)
2. [Inference Loop](#inference-loop)
3. [Validator Rules](#validator-rules)
   - [Ontogen Validator](#ontogen-validator)
   - [Metagen Validator](#metagen-validator)
4. [Adversarial Debate Framework](#adversarial-debate-framework)
   - [Metagen Adversarial Debate](#metagen-adversarial-debate)
5. [Test Mode](#test-mode)
6. [Settings Reference](#settings-reference)
7. [Open Questions](#open-questions)

## Scope

| In scope | Out of scope |
|---|---|
| Loop semantics for structured-output LLM calls (UC3 ontogen, UC4 metagen) | Provider-specific request shapes — LangChain owns those |
| Per-service validator tool contracts and rule tables | Validator implementations — `src/backend/{service}/validator.py` |
| Adversarial Producer/Reviewer debate layered on the inference loop (UC3 ontogen and UC4 metagen, always on for both) | Single-call completions (e.g. ad-hoc summarisation) — no loop, no validator |
| Test-mode stubbing conventions for both Producer and Reviewer | Provider credential resolution — see `SECRET_RESOLUTION.md` |

Single-call completions go through `LLMClient.complete` / `complete_json` /
`embed` directly. Anything that emits *structured business output* must use
the inference loop.

## Inference Loop

LLM calls that produce structured business outputs run inside a bounded ReAct
loop, not single-shot completions. The model is bound to one mandatory tool —
`{service}_validate(payload)` — and must call it with its proposed output
before the loop accepts a result. The tool enforces semantic rules the JSON
schema cannot express (ID-reference integrity, slug format, in-scope URN
provenance, etc.) and returns `{ok: true}` or `{ok: false, errors: [...]}`.
On errors the model receives the consolidated error list as a `ToolMessage`
and revises.

Two enforcement layers stack:

| Layer | Mechanism | Provider wiring |
|-------|-----------|-----------------|
| Shape | LangChain `with_structured_output(OutputModel)` — Pydantic schema bound at the provider layer | OpenAI `response_format=json_schema`, Gemini `response_schema`, Anthropic forced tool-call. Uniform across providers. |
| Semantic rules | LangChain `bind_tools([validator])` — service-supplied validator with full rule table | Same three providers; one tool per service. |

**Common loop parameters**:

| Parameter | Value |
|-----------|-------|
| Max iterations | `3` per service, set by the `ontogen_llm_max_iterations` / `metagen_llm_max_iterations` runtime config (`/api/v1/admin/conf`). One iteration = one model invocation. |
| Exhaustion behavior | Soft. The last candidate is accepted; rows that fail individual rules are dropped before persistence. The run is **not** marked failed on validation exhaustion — UC3/UC4 gate persistence through a human reviewer. |
| Observability | The run-complete event carries `producer_iterations` (1–`max`) and `producer_errors_dropped` (row count) reflecting the Producer-turn inference loop. Per-rule error samples are logged but not surfaced in the synchronous response. |

The loop lives in `LLMClient.complete_with_tools`. Validators ship alongside
their service code (`src/backend/{service}/validator.py`).

## Validator Rules

### Ontogen Validator

Tool name `ontogen_validate(payload)`. Schema model `OntogenLLMOutput`
(`nodes` / `edges` / `triples` arrays).

| Rule | Failure code |
|------|--------------|
| Pydantic shape of `OntogenLLMOutput` | `SCHEMA` |
| `node.id` and `edge.id` match `^[a-z0-9_]{1,64}$` (lowercase snake_case only — `a-z`, `0-9`, `_`; no hyphens, max 64 chars); no `__` | `SLUG_FORMAT` / `DOUBLE_UNDERSCORE` |
| No duplicate ids within `nodes`, within `edges` | `DUP_ID` |
| Every `triple.subject_node_id` and `triple.object_node_id` resolves to a node in the payload | `UNKNOWN_NODE_REF` |
| Every `triple.edge_id` resolves to an edge in the payload | `UNKNOWN_EDGE_REF` |
| `confidence_score ∈ [0.0, 1.0]` on every node, edge, triple | `CONF_OUT_OF_RANGE` |
| `node.dataset_urns` is non-empty and every entry ∈ in-scope dataset URNs supplied as evidence | `MISSING_DATASET_URNS` / `OUT_OF_SCOPE_URN` |
| No duplicate `(subject_node_id, edge_id, object_node_id)` triples | `DUP_TRIPLE` |

The in-scope dataset URN set is the keys of `evidence_per_dataset` for the
current run — fabricated URNs (model invented a dataset the prompt never
mentioned) are rejected.

### Metagen Validator

Tool name `metagen_validate(payload)`. Schema model `MetagenLLMOutput` — a
list of candidate entries, each `{dataset_urn, item_id, value,
confidence_score}`.

| Rule | Failure code |
|------|--------------|
| Pydantic shape of `MetagenLLMOutput` | `SCHEMA` |
| `dataset_urn` ∈ the run's in-scope dataset set (intersection of `dataset_filter` and `metagen_boundary.is_enabled=true`) | `OUT_OF_SCOPE_URN` |
| `item_id` matches `^dataset\.description$` or `^column\.[^.]+\.description$` | `INVALID_ITEM_ID` |
| For `column.<field_path>.description`: `field_path` resolves to a real column in the dataset's `schemaMetadata` | `UNKNOWN_FIELD_PATH` |
| `item_id`'s element kind (`dataset.description` / `column.description`) ∈ the dataset's `metagen_boundary.allowed` | `KIND_NOT_ALLOWED` |
| `value` is non-empty Markdown ≤ 16 KiB | `EMPTY_VALUE` / `VALUE_TOO_LARGE` |
| `confidence_score ∈ [0.0, 1.0]` | `CONF_OUT_OF_RANGE` |
| No duplicate `(dataset_urn, item_id)` within a single Producer turn | `DUP_ITEM` |
| For items that currently have an `approved` candidate: rejected at the validator | `ITEM_ALREADY_APPROVED` |

## Adversarial Debate Framework

Second loop layered on top of the inference loop. A **Reviewer** agent
critiques the **Producer's** validated output and gates acceptance until
both the deterministic validator and the Reviewer's adversarial verdict
agree. Runs on every UC3 ontogen call and every UC4 metagen call. The
loop shape, Reviewer tool, issue taxonomy, RAG anchors, Producer revision
rules, cycle detection, and termination outcomes documented in the
following subsections are shared by both services; the differences are
isolated in [§Metagen Adversarial Debate](#metagen-adversarial-debate)
below.

### Loop shape

```
turn 0  Producer  → emits OntogenLLMOutput candidate (ontogen_validate ok)
turn 1  Reviewer  → consumes candidate + RAG anchors, calls ontogen_review
        accept    → terminate, persist each row tagged with the run_id
        revise|reject → feed verdicts back to Producer
turn 2  Producer  → revises (apply, drop, or rebut per item) + revalidate
turn 3  Reviewer  → re-reviews
...     bounded by ontogen_debate_max_turns (default 4)
exit    accept | turns_exhausted | cycle_detected
```

Whole-output verdict only. Per-item verdicts are emitted by the Reviewer for
the human reviewer's benefit (traced to the run's Langfuse session) but do not
gate the loop — ontogen runs persist as a single batch at the end of the
inference job, so partial acceptance has no operational benefit.

### Reviewer tool

Tool name `ontogen_review`. Producer's tools and validator are unchanged.

```json
{
  "name": "ontogen_review",
  "input_schema": {
    "required": ["overall_verdict", "item_verdicts", "summary"],
    "properties": {
      "overall_verdict": {"enum": ["accept", "revise", "reject"]},
      "summary":         {"type": "string"},
      "item_verdicts": {
        "type": "array",
        "items": {
          "required": ["item_kind", "item_id", "verdict", "issues", "comment"],
          "properties": {
            "item_kind":          {"enum": ["node", "edge", "triple"]},
            "item_id":            {"type": "string"},
            "verdict":            {"enum": ["accept", "revise", "reject"]},
            "issues":             {"type": "array", "items": {"enum": [
              "naming_format",
              "confidence_miscalibrated",
              "duplicates_existing",
              "weak_evidence",
              "ontology_incoherent",
              "out_of_scope"
            ]}},
            "suggested_revision": {"type": "object"},
            "comment":            {"type": "string"}
          }
        }
      }
    }
  }
}
```

### Issue taxonomy

| Issue | Meaning |
|-------|---------|
| `naming_format` | id is not a lowercase snake_case slug (`a-z0-9_` only), or display name drifts from RAG-ANCHOR style (not business-friendly, not singular, etc.). Display names (`name`, `label`) may contain whitespace and mixed case; only `id` fields are slug-checked. |
| `confidence_miscalibrated` | Score does not match evidence weight; calibrate against the score distribution of RAG anchors |
| `duplicates_existing` | Semantically the same as an approved item (different spelling/casing); suggested_revision should reuse the approved id |
| `weak_evidence` | `dataset_urns` produce no schema fields or descriptions matching the proposed concept |
| `ontology_incoherent` | Triple's `(subject, edge, object)` has no logical relationship in the domain, or edge predicate is too generic |
| `out_of_scope` | `dataset_urns` lie outside the in-scope filter set |

These are the canonical issue codes the Reviewer may attach to a per-item
verdict. Adding a new code is a spec change.

### RAG anchors

Reviewer-only context. For each proposed item, pgvector-sample top-K
(`ontogen_debate_rag_k`, default 5) anchor items by similarity. The
anchor pool is `status IN ('approved', 'llm_approved')` — i.e. anything that
passed at least one gate (human review or the Adversarial Debate's auto-approval
path) qualifies, keeping cold-start ergonomics workable on fresh installs:

| Kind | Embed text | Source table (anchor pool: `status IN ('approved','llm_approved')`) |
|------|-----------|--------------|
| node | `name + description` | `node_embeddings` (status cached on the embedding row) |
| edge | `label + semantics`  | `edge_embeddings` JOIN `ontogen_edges` for status filter |
| triple | composite of (subject_node.embed_text, edge.embed_text, object_node.embed_text) | `triple_embeddings` JOIN `ontogen_triples` for status filter |

Anchors are injected as a `RAG ANCHORS` block in the Reviewer prompt. The
Producer never sees them. This asymmetric context is the source of
adversariality — the Reviewer judges against a quality bar the Producer
never had access to.

**Cold start.** When the anchor pool is empty (first runs on a fresh
DataSpoke install), the Reviewer simply runs without anchor grounding. No
special fallback prompt — the Reviewer relies on its own training and the
canonical issue taxonomy. Quality improves as the anchor corpus grows.

### Producer revision (turn 2+)

The Producer's prompt is prefixed with the Reviewer's full payload and the
prior candidate. For each non-accept item the Producer must either:

1. Apply the `suggested_revision` and re-emit the item.
2. Drop the item from the output.
3. Keep the item as-is and attach a `producer_rebuttal` field to its evidence
   with a one-sentence rationale.

Then call `ontogen_validate` exactly as on turn 0. The `producer_rebuttal`
channel is the explicit adversarial affordance — without it the Producer is
incentivised to always concede, which collapses the debate.

### Cycle detection

After each Producer turn, the candidate payload is canonicalised
(`json.dumps(payload, sort_keys=True, separators=(",", ":"))`) and hashed
(SHA-256). If the new hash matches any prior Producer turn's hash, the loop
terminates with `outcome=cycle_detected` and the last candidate is kept.
Prevents infinite revise/rebut ping-pong when Producer and Reviewer
disagree on the same item across turns.

### Termination

| Outcome | Meaning |
|---------|---------|
| `accept` | Reviewer returned `overall_verdict='accept'`; persist each row tagged with the `run_id`. Rows whose `confidence_score >= ONTOLOGY_CONFIDENCE_THRESHOLD` are persisted as `status=llm_approved` (LLM gate cleared, awaiting human review); rows below the threshold persist as `status=llm_pending`. |
| `turns_exhausted` | Reached `max_turns` without an accept; last candidate is kept. Rows persist with `status=llm_pending` regardless of confidence_score — non-accept outcomes always require a human gate. |
| `cycle_detected` | Producer's revised payload duplicated a prior turn's payload; last candidate is kept. Rows persist with `status=llm_pending` regardless of confidence_score. |

Soft-fail in all cases — the human review queue remains the final gate, so
the debate layer never blocks a run from emitting candidates.

### Status lifecycle

Ontogen rows carry a four-state status that surfaces governance lineage:

| State | Set by | Meaning |
|-------|--------|---------|
| `llm_pending` | server default (DB) and `_status_for_outcome` fallback | LLM created the row but the Adversarial Debate did not converge to `accept`, OR confidence was below `ONTOLOGY_CONFIDENCE_THRESHOLD`. Awaiting any review. |
| `llm_approved` | `_status_for_outcome` after `outcome=accept` + high confidence | The LLM Reviewer accepted and confidence cleared the threshold. Not yet seen by a human. |
| `approved` | Human review endpoint (`POST .../method/review` with `verdict=approve`) | A human explicitly approved the row. |
| `rejected` | Human review endpoint (`verdict=reject`) | A human explicitly rejected the row. |

There is no `llm_rejected` — the Reviewer's negative verdicts trigger
Producer revision attempts; when revisions are exhausted the row lands in
`llm_pending` for human disposition.

Downstream query rules:
- **RAG anchors** (this section): `status IN ('approved','llm_approved')`.
- **Reuse lookup** ([BACKEND §Inference Pipeline](BACKEND.md#ontology-generation-service-srcbackendontogen)): `status IN ('llm_pending','llm_approved','approved')` — anything non-`rejected`.
- **Triple-review dependency gate**: dependencies must be `status='approved'` (strict; an `llm_approved` dep does not satisfy the gate).
- **Metagen reads of UC3 ontogen** (UC4): `status='approved'` only — only human-curated ontology entities feed metagen's generation context. UC4's own candidate statuses (`llm_approved` / `approved` / `rejected`) are independent — there is no `llm_pending` for metagen, since debate-rejected candidates are dropped rather than persisted (see [§Metagen Adversarial Debate](#metagen-adversarial-debate)).

### Evidence — the run's Langfuse session

The debate transcript is **not** persisted on the result rows. Every
producer/reviewer LLM call of a run is already traced to Langfuse under
`session_id = run_id` (see [§Observability](#observability)), which is the
single source of truth for debate evidence. Each result row instead records
only the `run_id` that produced it — a `run_id uuid NULL` column on
`ontogen_nodes` / `_edges` / `_triples` (see
[BACKEND_SCHEMA](BACKEND_SCHEMA.md#ontogen_nodes)). Seeded rows
have no run and carry `run_id = NULL`.

`run_id` is written **only on row insert** and never overwritten when a later
run reuses or updates an existing row, so a row always points at the session
where its own debate happened. The UC3 review UI turns `run_id` into a link to
the run's Langfuse session
(`{langfuse_url}/project/{langfuse_project_id}/sessions/{run_id}`); it renders no
link when `run_id` is `NULL` or Langfuse is not configured (tracing disabled).

Per-run debate outcome remains queryable from the run's events — the
`ONTOGEN.RUN_COMPLETE` event detail carries `debate_outcome` and
`producer_iterations` (sourced from the run, not from per-row columns).

### Wiring

`OntogenService._run_inner` invokes `run_debate(...)` at the LLM-call site
unconditionally — there is no toggle to skip the Reviewer. `run_debate`
owns the Producer/Reviewer loop, RAG sampling, hash-cycle detection, and
transcript assembly; it lives in `src/backend/ontogen/debate.py`. The rest
of `_run_inner` (enumeration, evidence gathering, persistence) is
unchanged. The debate's per-turn LLM stubbing is governed by the
`stub_llm_client` toggle in §Test Mode below.

### Metagen Adversarial Debate

The debate shape above is reused verbatim by `MetagenService._run_inner`
via `src/backend/metagen/debate.py`. Differences from ontogen:

| Concern | Ontogen | Metagen |
|---------|---------|---------|
| Producer output schema | `OntogenLLMOutput` (nodes / edges / triples) | `MetagenLLMOutput` (list of `{dataset_urn, item_id, value, confidence_score}` candidates) |
| Validator tool | `ontogen_validate` | `metagen_validate` |
| Reviewer tool | `ontogen_review` (per-item verdict `item_kind ∈ {node, edge, triple}`) | `metagen_review` (per-item verdict `item_kind ∈ {dataset_description, column_description}`, addresses `dataset_urn` + `item_id`) |
| Issue taxonomy | `naming_format`, `confidence_miscalibrated`, `duplicates_existing`, `weak_evidence`, `ontology_incoherent`, `out_of_scope` | `value_too_generic`, `value_factually_wrong`, `value_redundant_with_approved`, `confidence_miscalibrated`, `style_inconsistent`, `out_of_scope` |
| RAG anchors | Approved nodes / edges / triples | Approved candidate `value`s grouped by `kind` (dataset descriptions in one pool, column descriptions in another); embedded in `metagen_candidate_embeddings` |
| Confidence threshold | `ONTOLOGY_CONFIDENCE_THRESHOLD` (default `0.7`) | `METAGEN_CONFIDENCE_THRESHOLD` (default `0.7`) |
| Persistence threshold | Below-threshold rows persist as `status='llm_pending'` for human triage | **Below-threshold candidates are dropped** — metagen has no `llm_pending` state. Only candidates with `outcome=accept` AND `confidence_score >= METAGEN_CONFIDENCE_THRESHOLD` persist as `status='llm_approved'`. |
| Termination | `accept` → persist; `turns_exhausted` / `cycle_detected` → keep last candidate with `status='llm_pending'` | `accept` → persist surviving candidates as `llm_approved`; `turns_exhausted` / `cycle_detected` → **drop all candidates from this run**. The next scheduled run is the recovery path. |

The shared scaffolding (cycle detection by SHA-256 hash, soft-fail
philosophy, per-turn Langfuse trace, test-mode stub behaviour) is
identical.

A metagen run is scoped to one conf: the per-`(conf, item)` candidate budget
and the `metagen:running:{conf_id}` lock are per-conf. The Reviewer's anchor
RAG, however, stays **global per `kind`** — `metagen_candidate_embeddings`
indexes every `approved` candidate `value` regardless of which conf produced
it, because approved descriptions land in dataset-global DataHub aspects, so a
prior approval is a valid style/consistency anchor for any conf documenting the
same kind.

**Metagen has two distinct RAG paths.** The Reviewer-side anchor RAG above
(`metagen_candidate_embeddings`, grouped by `kind`) is one. The other is a
**Producer-side per-dataset ontology RAG** that runs at evidence-fetch time,
before the debate starts: for each in-scope dataset the service embeds the
dataset's textual context and pulls bounded top-k hits from the three
ontogen pgvector collections (`node_embeddings`, `edge_embeddings`,
`triple_embeddings`). Hits surface in the Producer prompt as approved
ontology fragments scoped to that dataset's semantics. Reviewer never sees
them — they are evidence, not anchors. Tunable via
`metagen_ontology_rag_{node,edge,triple}_k`; see
[BACKEND §Metadata Generation Service §Generation Pipeline step 3](BACKEND.md#metadata-generation-service-srcbackendmetagen)
for the evidence-assembly flow.

## Test Mode

LLM stubbing is gated by the `stub_llm_client` boolean on the singleton `RuntimeConfig` row, flippable online via `PATCH /api/v1/admin/conf`. Changes propagate in ≤30s via the existing `RuntimeConfigDTO` TTL cache; no pod restart. Default is `false` (real LLM). The dev profile's `helm-charts/bin/post-install/seed-runtime-config.sh` seeds it to `true` so the dev API runs stubbed by default.

| RuntimeConfig field | Default | Effect when `true` |
|---------|---------|--------|
| `stub_llm_client` | `false` | `make_llm_client(stub=True, ...)` returns `StubLLMClient`. Calls to `complete_with_tools` and `complete_json` return deterministic schema-valid payloads; `embed()` returns a deterministic unit vector. The API key in `dataspoke-llm-secret` is not read. |

**Stub behaviour** when `stub_llm_client=true`:

| Surface | Stub returns |
|---------|--------------|
| `complete_with_tools` (Producer side — ontogen) | One schema-valid empty payload (`OntogenLLMOutput(nodes=[], edges=[], triples=[])`) on iteration 1. The loop never iterates. |
| `complete_with_tools` (Producer side — metagen) | One candidate per target item parsed from the prompt's TARGET ITEMS block; falls back to `{"candidates": []}` if the block is absent. |
| `complete_with_tools` (Reviewer side, when debate enabled) | `overall_verdict="accept"`, empty `item_verdicts`, summary `"stub-accept"`. The debate terminates after turn 1. |
| `embed` | Deterministic unit vector `[1.0, 0.0, …]` of `EMBEDDING_DIMENSION` length — non-zero norm so pgvector cosine yields a finite score. |

**Dual-mode test code.** A single api-wired test file may be written to pass under both `stub_llm_client` values:

- Assertions on response *shape* (e.g. `OntogenRunSummary.counts` is a dict of non-negative ints) hold under both.
- Assertions on response *content* (specific node names, triple counts) are guarded by `if runtime_conf.get("stub_llm_client"):` against the session-scoped `runtime_conf` fixture.
- Mode-specific tests guard inline on `runtime_conf` as the first statement of the test body — `runtime_conf` is a fixture, so it is unavailable to a `skipif` decorator. UC3 is one test parametrized over `llm_mode` (`["stub", "real"]`), each case skipping when the dev-env conf does not match it; UC4 keeps separate `_under_stub` / `_with_real_llm` tests. `spec/TESTING.md §Running` owns this rule.

The stub toggle is consulted by `make_llm_client()` regardless of consumer service (ontogen, metagen, or future LLM-backed flows). Companion stub toggles (`stub_redis_client`, `stub_pgvector_manager`, `stub_notification_service`) follow the same pattern — see `spec/TESTING.md §Stub Toggles`.

## Observability

DataSpoke ships self-hosted [Langfuse](https://langfuse.com/) as its LLM trace store in dev. The
Langfuse instance is a dev-only peripheral (chart `helm-charts/dev-peripherals/langfuse/`, installed
by `helm-charts/bin/dev-peripherals/langfuse.sh`) deployed in its own `langfuse-01` namespace. In
production the operator supplies their own Langfuse and wires the connection via the admin API.

As observability-only infrastructure Langfuse **fails open**: when the connection is unconfigured
or unreachable, tracing is disabled and the LLM call plus its enclosing endpoint still succeed.
This is the deliberate counterpart of DataHub's fail-closed behavior as the metadata SSOT — see
[`ARCHITECTURE.md` §Peripheral availability contract](../ARCHITECTURE.md#peripheral-availability-contract).

Beyond `host`/`public_key`/`secret_key`, the Langfuse peripheral carries two non-secret
settings (see [`spec/API.md`](../API.md) `/admin/peripherals/langfuse`): `environment_tag`
is passed as the Langfuse trace `environment` so operators can segment dev/staging/prod
traces in one project, and `project_id` is surfaced as trace metadata. Both are optional —
absence omits them and tracing still works.

### Test-side Langfuse env contract

App-runtime Langfuse connection is read from `peripheral_config.langfuse` per `HELM_CHART.md §Configuration Flow`; the table below describes only the test-tooling env vars consumed by `tests/integration/util/langfuse.py`.

| Env var | Required | Notes |
|---------|----------|-------|
| `DATASPOKE_DEV_LANGFUSE_HOST` | No | Full URL, e.g. `http://langfuse.10.0.0.1.nip.io` |
| `DATASPOKE_DEV_LANGFUSE_PUBLIC_KEY` | No | Langfuse project public key |
| `DATASPOKE_DEV_LANGFUSE_SECRET_KEY` | No | Langfuse project secret key |

All three must be set for tracing to activate in tests. When any one is absent, the test utility skips trace assertions.

### What lands in traces

`LLMClient` attaches a `langfuse.langchain.CallbackHandler` to every `ainvoke` call via
LangChain's `RunnableConfig`. Langfuse captures:

- Prompt messages and completion content for each turn.
- Tool calls and tool responses (for `complete_with_tools`).
- Token-count metadata reported by the provider.
- `session_id` = `run_id` (uuid4 generated once per `OntogenService.run()` call), grouping all
  turns of one ontogen run into a single Langfuse session.
- `metadata.actor` = `"producer"` or `"reviewer"`.
- `metadata.turn` = integer turn index within the debate.
- Trace `environment` = the configured `environment_tag`, and `metadata.project_id` = the
  configured `project_id`, when those Langfuse peripheral settings are present.

Session grouping uses LangChain `RunnableConfig.metadata.langfuse_session_id`, set equal to the ontogen run_id. The dedicated `session_id` argument on `LLMClient` methods takes precedence over any caller-supplied `metadata.langfuse_session_id`.

Embedding calls (`LLMClient.embed`) use a separate embeddings-model client that does not pass
through the LangChain callback pipeline; embedding spans are not captured in Langfuse traces.
This is noted as future work.

### Operator workflow

1. A completed run emits an `ONTOGEN_RUN_COMPLETE` event. The `detail` JSONB contains
   `run_id` (uuid4).
2. Open the Langfuse UI at `${DATASPOKE_DEV_LANGFUSE_HOST}/sessions/<run_id>` to see all LLM
   turns for that run — prompts, completions, tool calls, and token counts in one view.

No DataSpoke API endpoint exists for log retrieval. Langfuse UI is the sole operator surface
for per-run LLM trace browsing.

### Data exposure

Langfuse traces capture the full content of LLM interactions: dataset URNs, schema field names,
evidence text (DataHub descriptions, owner identities), completion text, and tool-call payloads.
For prod deployments the operator's own Langfuse persists this prompt corpus in
operator-controlled storage. Operator responsibility: enable SSO/auth
on the Langfuse UI before exposing the hostname; consider Langfuse's `mask=` callable on the `Langfuse()`
constructor for field-level redaction if sensitive schema names must be excluded from traces.

### Production auth requirement

Production deployments MUST disable Langfuse public sign-up (`AUTH_DISABLE_SIGNUP=true` upstream env)
once the bootstrap admin is created, configure SSO/OIDC via NextAuth providers, and restrict ingress
access (auth annotation or IP allowlist). With Langfuse's default open-signup, any reachable user can
create an account and read all traces including raw prompts and completions. Dev deployments under
`.nip.io` are intentionally open for developer convenience.

### Availability notes

Langfuse unavailability does not affect LLM call success — the exporter buffers and retries in the
background; on permanent failure traces are dropped silently.

## Settings Reference

Configuration splits into two surfaces: **runtime configuration** (behavioral
tunables stored in the DB and edited at runtime) and **process environment**
(connection, secret, and test settings read once at startup via the `Settings`
Pydantic class, `src/shared/settings.py`).

### Runtime configuration

The behavioral tunables live in the `runtime_config` singleton (see
[`BACKEND_SCHEMA.md`](BACKEND_SCHEMA.md)) and are read and updated through
`GET`/`PATCH /api/v1/admin/conf`. They are seeded with the factory defaults
below on first read and cached process-side with a short TTL, so a `PATCH`
propagates to in-flight workers within the cache window. The LLM API key is
edited through the same `/admin/conf` surface but is stored in a Kubernetes
Secret rather than the DB — see [LLM API key](#llm-api-key) below.

| Field | Default | Bounds | Owner |
|-------|---------|--------|-------|
| `llm_provider` | `gemini` | length-capped | shared LLM client |
| `llm_model` | `gemini-3.5-flash` | length-capped | shared LLM client |
| `ontogen_llm_max_iterations` | `3` | [1, 20] | ontogen inference loop |
| `ontogen_debate_max_turns` | `4` | [2, 10] | ontogen debate |
| `ontogen_debate_rag_k` | `5` | [0, 20] | ontogen debate |
| `ontogen_debate_reviewer_model` | null → reuse `llm_model` | length-capped | ontogen debate |
| `metagen_llm_max_iterations` | `3` | [1, 20] | metagen inference loop |
| `metagen_debate_max_turns` | `4` | [2, 10] | metagen debate |
| `metagen_debate_rag_k` | `5` | [0, 20] | metagen debate |
| `metagen_debate_reviewer_model` | null → reuse `llm_model` | length-capped | metagen debate |
| `metagen_confidence_threshold` | `0.7` | [0.0, 1.0] | metagen persistence gate |
| `metagen_ontology_rag_node_k` | `5` | [0, 20] | metagen Producer-evidence ontology RAG (`0` disables) |
| `metagen_ontology_rag_edge_k` | `5` | [0, 20] | metagen Producer-evidence ontology RAG (`0` disables) |
| `metagen_ontology_rag_triple_k` | `5` | [0, 20] | metagen Producer-evidence ontology RAG (`0` disables) |
| `auth_datahub_corp_group` | `dataspoke-users` | length-capped, URN-safe charset | auth mirror (DataSpoke-user provenance corpGroup) |

The ontogen persistence gate uses the fixed `ONTOLOGY_CONFIDENCE_THRESHOLD`
backend constant (see [`BACKEND.md`](BACKEND.md)), not a runtime tunable.

### LLM API key

The LLM provider API key is a DataSpoke-owned secret stored in a dedicated
Kubernetes Secret **`dataspoke-llm-secret`** (key `api_key`, base64) in the API
pod's own namespace. It is **not** an injected environment variable and **not**
a `runtime_config` column.

- **Read** — the backend resolves the key from the Secret via the in-cluster
  Kubernetes API at LLM-call time, behind a short-TTL process cache (the same
  in-cluster client + cache machinery as the source-credential resolver, see
  [`SECRET_RESOLUTION.md`](SECRET_RESOLUTION.md)). `make_llm_client` consumes the
  resolved value; the provider/model come from `runtime_config`.
- **Write (online)** — `PATCH /api/v1/admin/conf` accepts `llm_api_key`; the
  handler writes it to the Secret (create-or-patch) and invalidates the cache,
  so a subsequent LLM call on that replica uses it immediately and other
  replicas converge within the TTL. No pod restart or Helm upgrade is needed.
- **Masked read** — `GET /api/v1/admin/conf` returns `llm_api_key` as `""`
  (unset) or `"********"` (set); the plaintext is never returned.
- **Distinct from source-credential resolution** — that subsystem governs
  *user-supplied* source credentials and guards writes with the
  `dataspoke-source-cred-` name prefix to keep callers away from DataSpoke's own
  Secrets. The LLM key is a DataSpoke-owned Secret, so its accessor targets the
  fixed `dataspoke-llm-secret` name and is gated by the `admin` group (or
  `X-Internal-Token`) — the fixed target plus admin auth are the controls, so
  the source-cred prefix guard does not apply.

### Process environment

| Env var | Default | Owner |
|---------|---------|-------|
| `DATASPOKE_DEV_LANGFUSE_HOST` | unset | observability |
| `DATASPOKE_DEV_LANGFUSE_PUBLIC_KEY` | unset | observability |
| `DATASPOKE_DEV_LANGFUSE_SECRET_KEY` | unset | observability |

Stub-mode wiring is governed by RuntimeConfig (see `§Test Mode` above), not env vars.

## Open Questions

- [ ] Reviewer model separation: when `ontogen_debate_reviewer_model` (or its metagen counterpart) differs from the Producer model, the debate has two API keys / two SDK clients in flight. Pricing telemetry and rate-limit accounting need a per-role split.
- [ ] Per-item Reviewer verdicts emitted on turn N can contradict turn N-1 verdicts on the same item; spec currently relies on the Reviewer's own consistency. If this drifts in practice, fold prior-turn verdicts into the Reviewer prompt as a constraint.
