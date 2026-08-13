---
name: runtime-conf-field-lifecycle
description: /admin/conf runtime_config fields live in six layers that must move together; the PATCH schema silently ignores unknown keys (measured), and extra="forbid" would echo a mistyped llm_api_key into the 422 body
metadata:
  type: project
---

A field on the `runtime_config` singleton exists in **six** places, and any add/remove diff must move
all six or leave a hole:

1. `migrations/versions/001_initial_schema.py` — the `runtime_config` `create_table` block
2. `src/shared/db/models.py` `RuntimeConfig` mapped column
3. `src/backend/admin/config_service.py` `RUNTIME_CONFIG_DEFAULTS`
4. same file — `RuntimeConfigDTO` field **and** `from_orm` assignment (two edits, one layer)
5. `src/api/schemas/admin.py` — `RuntimeConfResponse` *and* `RuntimeConfPatchRequest`
6. `src/api/routers/admin.py` `_dto_to_response` mapping

Check it mechanically instead of by eye — set-difference the five in-process collections
(`RUNTIME_CONFIG_DEFAULTS`, `dataclasses.fields(RuntimeConfigDTO)`,
`RuntimeConfig.__table__.columns`, and both schemas' `model_fields`); the only legitimate
differences are `id`/`updated_at` (ORM-only), `resp_time`/`updated_at` (response-only) and
`llm_api_key` (schema-only — it is routed to the k8s Secret, never to the DB).

**Two properties that make holes silent rather than loud:**

- `RuntimeConfPatchRequest` declares no `model_config`, so pydantic v2's default `extra='ignore'`
  applies. Measured: a body of `{'removed_field': 99, 'bogus': 'x', 'metagen_debate_rag_k': 4}`
  dumps to `{'metagen_debate_rag_k': 4}` and returns **200 with the full config**. A mistyped
  `stub_llm_clients` or `auth_datahub_corp_groups` therefore reads as success while the old value
  stands — on the surface that gates the four `stub_*` real-vs-stub dependency toggles and the
  DataHub auth-mirror group.
- The hole is doubled: `/api/v1/admin/conf` (`require_admin`) and `/internal/admin/conf`
  (`require_internal_token`, used by install scripts and dev seeding) share
  `_apply_patch_and_respond`, so install-time automation gets the same silent no-op.

**Before recommending `extra="forbid"`:** the 422 envelope carries `jsonable_encoder(exc.errors())`
and pydantic's `extra_forbidden` error includes `input` — the rejected *value*. A body that
misspells the **key** `llm_api_key` would then echo the plaintext LLM API key into the response
body and anything that logs it. See [[api-422-echoes-rejected-input]]. Pair the forbid with an
input-stripping error serializer for this model, or the fix creates a credential sink.

**Why:** the fix-metric-time-window run removed `validation_score_n_intervals` from all six layers
cleanly, but the frontend and every existing automation still sending it get a 200 and believe it
persisted.

**How to apply:** on any `/admin/conf` diff, run the five-way set difference first, then check
whether the removed/renamed key is still sent by `src/frontend/app/(app)/admin/conf/`,
`tests/`, or `helm-charts/bin/post-install/**` — those callers, not the schema, are where the
silent-accept becomes an operator-visible lie.
