---
name: peripherals-get-mask-no-plaintext-path
description: GET /admin/peripherals/{datahub,langfuse} mask handlers never serialize the plaintext secret — so "plaintext not in body" absence asserts are structurally vacuous; the real guard is the positive token=="********" + is_configured assertion
metadata:
  type: project
---

`src/api/routers/admin.py` `_datahub_dto_to_response` / `_langfuse_dto_to_response` build the GET
response by reading only the `datahub_token_is_set()` / `langfuse_secret_key_is_set()` **booleans**
and hardcoding `token="********" if set else ""` (secret_key likewise). They never call
`get_datahub_token()` / `get_langfuse_secret_key()`, admin.py does not even import those getters, and
`DatahubConfigDTO` / `LangfuseConfigDTO` carry **no** token/secret field. So there is no code path by
which the plaintext could enter the GET response body.

**Test-review implication:** an absence assertion like `assert "my-datahub-token" not in str(body)`
is **structurally vacuous** here regardless of how the plaintext is "injected." A
`patch("src.backend.admin.datahub_secret.get_datahub_token", return_value=...)` is **inert** — the
handler never references that symbol, and the patch is at the definition module (wrong boundary per
TESTING.md Mocking rules) so it would not intercept even a hypothetical regression that added a fresh
`from ... import get_datahub_token`. The load-bearing guard against a "handler serializes the real
value" regression is the **positive** pair already in the masked-when-set tests:
`body["token"] == "********"` and `body["is_configured"] is True` (a leak or empty fails `==`).

**Why:** surfaced in the #58 Part B1 G1 test review (2026-07-11). G1 item-3 tried to give the absence
assert teeth by injecting a plaintext getter; the injection reaches no read path, so the fix is
cosmetic and its comment ("a regression that serialized the plaintext would surface it") is false —
false-confidence risk. Related: [[recipe-mask-string-divergence]] (different masking pin).

**How to apply:** when reviewing admin-peripherals GET masking tests, do NOT credit a plaintext-injection
patch as making the absence assertion meaningful — confirm the positive `== "********"` + `is_configured`
assertions exist (those are the real T4 guard) and flag any inert injection + overstated comment as a
false-confidence finding. If the handler design changes so the getter/DTO does carry the plaintext,
re-evaluate — the vacuity claim is tied to the current boolean-only masking design.
