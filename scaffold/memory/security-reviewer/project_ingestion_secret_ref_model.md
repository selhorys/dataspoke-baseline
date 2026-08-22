---
name: project-ingestion-secret-ref-model
description: Ingestion recipes carry ${name__key} secret REFERENCES, not plaintext; API never returns secret values
metadata:
  type: project
---

The ingestion feature handles credentials as `${name__key}` secret *references*, never
plaintext. The `GET /spoke/ingestion/secrets` endpoint returns metadata only
(`SecretRefInfo`: `ref`, `secret_name`, `key`) — never values. Recipe YAML round-trips these
refs verbatim; "masking" in the UI means preserving the ref token plus a visual highlight,
not redacting a real value. Underlying credentials live in pre-created Kubernetes Secrets,
resolved server-side at ingestion time.

**Why:** This is the intended design, so a `${...}__password` token appearing in a YAML
template or test fixture is NOT a hardcoded-secret finding.

**How to apply:** When reviewing ingestion frontend/backend, treat `${name__key}` tokens as
references, not leaked secrets. Only flag an actual credential VALUE (real password/token
string) or a code path that returns/logs resolved secret values. The secrets hook should be
gated by `canWrite` and not retried on 403/503 — verify that gating exists.
