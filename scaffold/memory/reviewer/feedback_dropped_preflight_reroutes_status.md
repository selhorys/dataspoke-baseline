---
name: dropped-preflight-reroutes-status
description: Deleting a pre-flight guard reroutes its failure into an existing status branch whose prose names only the old cause — plus the flat-vs-nested DataSpoke error envelope that makes error_code parse empty
metadata:
  type: feedback
---

When a refactor deletes a client-side pre-flight check, the condition it caught
does not disappear — it now reaches the server and comes back as some HTTP
status that already has a branch. Re-read that branch's operator-facing text:
it was written when only one cause could produce that status.

**Why:** the in-cluster-seed change deleted
`seed-admin-user.sh`'s "Could not read `DATASPOKE_INTERNAL_TOKEN` from
dataspoke-api pod" guard (the in-pod script now defaults the header to `""`).
A blank token in the pod now returns **503 INTERNAL_AUTH_NOT_CONFIGURED**
(`src/api/auth/internal.py:21-32`), which lands in the script's existing 503
branch asserting "a 503 here means the API's own storage (Postgres) is
unavailable". Symmetrically, the 401/403 branch ("X-Internal-Token mismatch")
became **unreachable**: the pod signs with the same env var the API validates
against, so the two can no longer disagree.

**Envelope shapes differ and break `error_code` extraction.** DataSpoke's own
handlers go through `_error_json` (`src/api/main.py:118-144`) and emit a **flat**
`{"error_code", "message", "trace_id", "resp_time"}`. A raw
`HTTPException(detail={...})` — which `require_internal_token` and other
dependency guards raise — goes through FastAPI's default handler and emits
**nested** `{"detail": {"error_code": ...}}`. Any shell/JS parser doing
`d.get("error_code")` on the top level silently yields empty for the second
shape, so the operator sees `error_code=unknown` next to prose naming the wrong
cause.

**How to apply:** enumerate the statuses the deleted guard used to pre-empt,
stub each one (a fake `kubectl`/`fetch` returning the exact envelope is enough)
and read the message the script actually prints. Related:
[[status-change-silent-skip-and-catalogue]],
[[verify-branch-reachability-rationales]].
