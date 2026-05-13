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
5. [Test Mode](#test-mode)
6. [Settings Reference](#settings-reference)
7. [Open Questions](#open-questions)

## Scope

| In scope | Out of scope |
|---|---|
| Loop semantics for structured-output LLM calls (UC3 ontogen, UC4 metagen) | Provider-specific request shapes — LangChain owns those |
| Per-service validator tool contracts and rule tables | Validator implementations — `src/backend/{service}/validator.py` |
| Adversarial Producer/Reviewer debate layered on the inference loop (UC3, always on) | Single-call completions (e.g. ad-hoc summarisation) — no loop, no validator |
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
| Max iterations | `3` per service, overridable via `DATASPOKE_ONTOGEN_LLM_MAX_ITERATIONS` / `DATASPOKE_METAGEN_LLM_MAX_ITERATIONS`. One iteration = one model invocation. |
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
| `node.id` and `edge.id` match `^[a-z0-9][a-z0-9_-]*$`; no `__` | `SLUG_FORMAT` / `DOUBLE_UNDERSCORE` |
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

Tool name `metagen_validate(payload)`. Schema model `MetagenLLMOutput`
(per-target proposal entries).

| Rule | Failure code |
|------|--------------|
| Pydantic shape of `MetagenLLMOutput` | `SCHEMA` |
| Every proposal entry's `target` ∈ the config's configured `targets` | `TARGET_NOT_CONFIGURED` |
| For `column.description`: `field_path` resolves to a real column in the dataset's `schemaMetadata` | `UNKNOWN_FIELD_PATH` |
| For `cross_data.md`: `action_type ∈ {create, modify, delete}` | `INVALID_ACTION_TYPE` |
| For `cross_data.md` `modify` / `delete` actions: `document_urn` references an existing document with `relatedAssets` overlapping the in-scope dataset set | `UNKNOWN_DOCUMENT_URN` |
| For `cross_data.md` `create` / `modify` actions: `related_dataset_urns` is non-empty and every entry ∈ in-scope dataset URNs | `OUT_OF_SCOPE_URN` |
| For `cross_data.md` `create` / `modify`: Markdown body is non-empty | `EMPTY_BODY` |
| `action_id` (cross_data.md only) matches `^[a-z0-9][a-z0-9_-]*$`; no `__` | `SLUG_FORMAT` / `DOUBLE_UNDERSCORE` |
| No duplicate `action_id` within a single proposal | `DUP_ACTION_ID` |
| For `dataset.description` / `column.description`: proposed text is non-empty | `EMPTY_DESCRIPTION` |

The Metadata Generation pipeline currently uses a single-call LLM
implementation; the validator rules above describe the target shape when it
adopts the inference loop.

## Adversarial Debate Framework

Second loop layered on top of the inference loop. A **Reviewer** agent
critiques the **Producer's** validated output and gates acceptance until
both the deterministic validator and the Reviewer's adversarial verdict
agree. Runs on every UC3 ontogen call; UC4 metagen wiring deferred (see
§Open Questions).

### Loop shape

```
turn 0  Producer  → emits OntogenLLMOutput candidate (ontogen_validate ok)
turn 1  Reviewer  → consumes candidate + RAG anchors, calls ontogen_review
        accept    → terminate, persist with debate transcript on each row
        revise|reject → feed verdicts back to Producer
turn 2  Producer  → revises (apply, drop, or rebut per item) + revalidate
turn 3  Reviewer  → re-reviews
...     bounded by DATASPOKE_ONTOGEN_DEBATE_MAX_TURNS (default 4)
exit    accept | turns_exhausted | cycle_detected
```

Whole-output verdict only. Per-item verdicts are emitted by the Reviewer for
the human reviewer's benefit (persisted into per-row `evidence`) but do not
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
| `naming_format` | id is not a lowercase slug, or display name drifts from RAG-ANCHOR style (not business-friendly, not singular, etc.) |
| `confidence_miscalibrated` | Score does not match evidence weight; calibrate against the score distribution of RAG anchors |
| `duplicates_existing` | Semantically the same as an approved item (different spelling/casing); suggested_revision should reuse the approved id |
| `weak_evidence` | `dataset_urns` produce no schema fields or descriptions matching the proposed concept |
| `ontology_incoherent` | Triple's `(subject, edge, object)` has no logical relationship in the domain, or edge predicate is too generic |
| `out_of_scope` | `dataset_urns` lie outside the in-scope filter set |

These are the canonical issue codes the Reviewer may attach to a per-item
verdict. Adding a new code is a spec change.

### RAG anchors

Reviewer-only context. For each proposed item, pgvector-sample top-K
(`DATASPOKE_ONTOGEN_DEBATE_RAG_K`, default 5) approved items by similarity:

| Kind | Embed text | Source table |
|------|-----------|--------------|
| node | `name + description` | `OntogenNode where status='approved'` |
| edge | `label + semantics`  | `OntogenEdge where status='approved'` |
| triple | composite of (subject_node.embed_text, edge.embed_text, object_node.embed_text) | approved triple set |

Anchors are injected as a `RAG ANCHORS` block in the Reviewer prompt. The
Producer never sees them. This asymmetric context is the source of
adversariality — the Reviewer judges against a quality bar the Producer
never had access to.

**Cold start.** When approved-item sets are empty (first runs on a fresh
DataSpoke install), the Reviewer simply runs without anchor grounding. No
special fallback prompt — the Reviewer relies on its own training and the
canonical issue taxonomy. Quality improves as the approved corpus grows.

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
| `accept` | Reviewer returned `overall_verdict='accept'`; persist with transcript. |
| `turns_exhausted` | Reached `max_turns` without an accept; last candidate is kept and persisted with `outcome=turns_exhausted` so human reviewers can see the debate did not converge. |
| `cycle_detected` | Producer's revised payload duplicated a prior turn's payload; last candidate is kept. |

Soft-fail in all cases — the human review queue remains the final gate, so
the debate layer never blocks a run from emitting candidates.

### Evidence shape

The debate transcript is stored in the existing `evidence` JSONB column of
`ontogen_nodes` / `_edges` / `_triples`. No schema migration needed.

```json
{
  "source": "ontogen-run",
  "run_id": "ru_<iso8601-utc>_<short>",
  "debate": {
    "turns_completed": 4,
    "outcome": "accept",
    "final_reviewer_verdict": "accept",
    "rag_anchors": [
      {"kind": "node",  "approved_id": "order",       "similarity": 0.82},
      {"kind": "node",  "approved_id": "order_line",  "similarity": 0.74}
    ],
    "history": [
      {"turn": 0, "actor": "producer", "candidate_hash": "<sha256-prefix>"},
      {"turn": 1, "actor": "reviewer", "verdict": "revise",
       "item_verdicts_count": 4, "issues_seen": ["confidence_miscalibrated"],
       "comment_summary": "0.95 too high; only 2 schema fields support this node"},
      {"turn": 2, "actor": "producer", "candidate_hash": "<sha256-prefix>",
       "applied": ["confidence_score: 0.95→0.7"], "rebuttals": []},
      {"turn": 3, "actor": "reviewer", "verdict": "accept"}
    ]
  }
}
```

Per-item Reviewer verdicts (matching the `ontogen_review` schema) are merged
into the same row's `evidence` so the UC3 review UI can render naming /
confidence / coherence flags in the per-item detail pane:

```json
{
  "reviewer_verdicts": [
    {"verdict": "revise",
     "issues": ["confidence_miscalibrated"],
     "comment": "...",
     "suggested_revision": {"confidence_score": 0.7}}
  ]
}
```

A row may carry multiple entries when the same item was revised across
turns; the array is ordered oldest to newest.

### Wiring

`OntogenService._run_inner` invokes `run_debate(...)` at the LLM-call site
unconditionally — there is no toggle to skip the Reviewer. `run_debate`
owns the Producer/Reviewer loop, RAG sampling, hash-cycle detection, and
transcript assembly; it lives in `src/backend/ontogen/debate.py`. The rest
of `_run_inner` (enumeration, evidence gathering, persistence) is
unchanged. The debate's per-turn LLM stubbing is governed by the test-mode
env vars in §Test Mode below.

## Test Mode

Two orthogonal env vars govern LLM behaviour during integration tests.

| Env var | Default | Effect |
|---------|---------|--------|
| `DATASPOKE_TEST_MODE` | unset | When `true`, activates `StubLLMClient` so calls to `complete_with_tools` and `complete_json` return deterministic schema-valid payloads. Used by every integration tier to keep tests fast and reproducible. |
| `DATASPOKE_TEST_LLM_REAL` | `false` | When `true` *and* `DATASPOKE_TEST_MODE=true`, the stub is bypassed and the real LLM in `.env` is used. Lets a single test file serve both deterministic CI runs (`false`) and live manual exploration via `/test-api-wired-manual` (`true`). |

`DATASPOKE_TEST_LLM_REAL` is read once at `LLMClient` construction. It has
no effect outside test mode — production never falls back to stub
behaviour.

**Stub behaviour** under `DATASPOKE_TEST_MODE=true, DATASPOKE_TEST_LLM_REAL=false`:

| Surface | Stub returns |
|---------|--------------|
| `complete_with_tools` (Producer side) | One schema-valid empty payload (`OntogenLLMOutput(nodes=[], edges=[], triples=[])` or the metagen equivalent) on iteration 1. The loop never iterates. |
| `complete_with_tools` (Reviewer side, when debate enabled) | One tool call to `ontogen_review` with `overall_verdict="accept"`, empty `item_verdicts`, summary `"stub-accept"`. The debate terminates after turn 1. |
| `embed` | Deterministic zero vector of `EMBEDDING_DIMENSION` length. |

**Dual-mode test code.** A single api-wired test file may be written to
pass under both `DATASPOKE_TEST_LLM_REAL` values:

- Assertions on response *shape* (e.g. `OntogenRunSummary.counts` is a dict
  of non-negative ints) hold under both.
- Assertions on response *content* (specific node names, triple counts) are
  guarded by `if not _llm_real()`.
- The shared assertion set is what gets reviewed alongside fixtures during
  `/test-api-wired-manual` runs.

The same env var is honored by `LLMClient` regardless of consumer service
(ontogen, metagen, or future LLM-backed flows).

## Observability

DataSpoke ships self-hosted [Langfuse](https://langfuse.com/) as its LLM trace store. The
Langfuse instance is an independent subsystem (`dev_env/langfuse/`, chart
`helm-charts/langfuse/`) installed in its own `langfuse-01` namespace, following the same
`values.yaml` + `values-dev.yaml` overlay convention as the umbrella chart.

### Env contract

| Env var | Required | Notes |
|---------|----------|-------|
| `DATASPOKE_LANGFUSE_HOST` | No | Full URL, e.g. `http://langfuse.10.0.0.1.nip.io` |
| `DATASPOKE_LANGFUSE_PUBLIC_KEY` | No | Langfuse project public key |
| `DATASPOKE_LANGFUSE_SECRET_KEY` | No | Langfuse project secret key |

All three must be set for tracing to activate. When any one is absent, `LLMClient` skips
callback construction and emits no traces — zero overhead, no failures.

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

Session grouping uses LangChain `RunnableConfig.metadata.langfuse_session_id`, set equal to the ontogen run_id. The dedicated `session_id` argument on `LLMClient` methods takes precedence over any caller-supplied `metadata.langfuse_session_id`.

Embedding calls (`LLMClient.embed`) use a separate embeddings-model client that does not pass
through the LangChain callback pipeline; embedding spans are not captured in Langfuse traces.
This is noted as future work.

### Operator workflow

1. A completed run emits an `ONTOGEN_RUN_COMPLETE` event. The `detail` JSONB contains
   `run_id` (uuid4).
2. Open the Langfuse UI at `${DATASPOKE_LANGFUSE_HOST}/sessions/<run_id>` to see all LLM
   turns for that run — prompts, completions, tool calls, and token counts in one view.

No DataSpoke API endpoint exists for log retrieval. Langfuse UI is the sole operator surface
for per-run LLM trace browsing.

### Data exposure

Langfuse traces capture the full content of LLM interactions: dataset URNs, schema field names,
evidence text (DataHub descriptions, owner identities), completion text, and tool-call payloads.
For prod deployments using external Postgres/S3 configured in `helm-charts/langfuse/values.yaml`,
this prompt corpus persists in operator-controlled storage. Operator responsibility: enable SSO/auth
on the Langfuse UI before exposing the hostname; consider Langfuse's `mask=` callable on the `Langfuse()`
constructor for field-level redaction if sensitive schema names must be excluded from traces.

### Production auth requirement

Production deployments MUST disable Langfuse public sign-up (`AUTH_DISABLE_SIGNUP=true` upstream env)
once the bootstrap admin is created, configure SSO/OIDC via NextAuth providers, and restrict ingress
access (auth annotation or IP allowlist). With Langfuse's default open-signup, any reachable user can
create an account and read all traces including raw prompts and completions. Dev deployments under
`.nip.io` are intentionally open for developer convenience.

### Partial coverage and availability notes

Metagen (UC4) LLM calls flow through the same `LLMClient` and produce Langfuse traces but without
`session_id`/`actor`/`turn` metadata; full UC4 wiring is deferred alongside the metagen debate framework.

Langfuse unavailability does not affect LLM call success — the exporter buffers and retries in the
background; on permanent failure traces are dropped silently.

## Settings Reference

All env vars are read once at process startup via the `Settings` Pydantic
class (`src/shared/settings.py`).

| Env var | Default | Owner |
|---------|---------|-------|
| `DATASPOKE_LLM_PROVIDER` | — | shared |
| `DATASPOKE_LLM_MODEL` | — | shared |
| `DATASPOKE_LLM_API_KEY` | — | shared |
| `DATASPOKE_ONTOGEN_LLM_MAX_ITERATIONS` | `3` | ontogen inference loop |
| `DATASPOKE_METAGEN_LLM_MAX_ITERATIONS` | `3` | metagen inference loop |
| `DATASPOKE_ONTOGEN_DEBATE_MAX_TURNS` | `4` | ontogen debate |
| `DATASPOKE_ONTOGEN_DEBATE_RAG_K` | `5` | ontogen debate |
| `DATASPOKE_ONTOGEN_DEBATE_REVIEWER_MODEL` | unset → reuse producer model | ontogen debate |
| `DATASPOKE_TEST_MODE` | unset | test infra |
| `DATASPOKE_TEST_LLM_REAL` | `false` | test infra |
| `DATASPOKE_LANGFUSE_HOST` | unset | observability |
| `DATASPOKE_LANGFUSE_PUBLIC_KEY` | unset | observability |
| `DATASPOKE_LANGFUSE_SECRET_KEY` | unset | observability |

## Open Questions

- [ ] Reviewer model separation: when `DATASPOKE_ONTOGEN_DEBATE_REVIEWER_MODEL` differs from the Producer model, the debate has two API keys / two SDK clients in flight. Pricing telemetry and rate-limit accounting need a per-role split.
- [ ] Per-item Reviewer verdicts emitted on turn N can contradict turn N-1 verdicts on the same item; spec currently relies on the Reviewer's own consistency. If this drifts in practice, fold prior-turn verdicts into the Reviewer prompt as a constraint.
- [ ] Metagen debate: UC4 has the same Producer/Validator shape as UC3, so the debate framework is theoretically portable. Deferred until the Metagen pipeline migrates to the inference loop and the human-review surface for cross_data.md / column.description proposals lands.
