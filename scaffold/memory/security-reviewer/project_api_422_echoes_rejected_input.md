---
name: api-422-echoes-rejected-input
description: The API's 422 envelope carries pydantic's `input` verbatim, so any caller that prints a response body is a credential sink
metadata:
  type: project
---

`src/api/main.py:_handle_request_validation` returns
`detail={"errors": jsonable_encoder(exc.errors())}`. Pydantic v2 error dicts include
an `"input"` key holding the **full rejected value** (verified against the repo's own
pydantic: a `string_too_long` on a `token` field echoed the whole token).

The secret-routed fields that can trip it are all `max_length=8192` — DataHub `token`,
`kafka_sasl_password`, Langfuse `secret_key`, `llm_api_key`. The Kafka cross-field
rules in `src/shared/datahub/kafka_security.py` never echo a value, so a length bound
is the realistic trigger.

**Why it matters:** every shell/CI caller that does `error "... Response body: $body"`
turns a 422 into a plaintext credential on the operator's terminal and in the CI log —
which silently undoes an argv/stdin hardening pass done in the same commit. The
`helm-charts/bin/post-install/seed-*.sh` scripts do exactly this.

**How to apply:** when reviewing anything that PATCHes a secret-routed field and prints
the response, require the non-2xx branch to print status plus `detail.errors[].loc`,
never the raw body. Same reasoning class as [[peripheral-health-error-redaction]] —
the value leaves through the error path, not the success path.
