---
name: token-urlsafe-scrub-charset
description: Redaction regexes for dsk_/token_urlsafe secrets must include _ and -; [A-Za-z0-9]+ leaks base64url tail
metadata:
  type: project
---

DataSpoke API PATs are `dsk_<secrets.token_urlsafe(32)>` (src/backend/auth/api_tokens.py:79);
password-reset raw tokens are bare `token_urlsafe(32)` (src/backend/auth/reset.py:62).
`secrets.token_urlsafe` emits **base64url** — alphabet `[A-Za-z0-9_-]`.

**Why:** A scrub/redaction regex using `dsk_[A-Za-z0-9]+` stops at the first `_` or `-`. Empirically
that fully redacts only ~26% of real tokens: ~71% leak a tail, ~3% don't match at all (body starts
with a special char) and leak whole. The JWT rule in the same scrubber correctly uses
`[A-Za-z0-9_-]`, so the mismatch is easy to spot.

**How to apply:** When reviewing any secret scrubber/redactor (e.g. `.prauto/lib/pr.sh scrub_secrets`),
verify the token-body charset matches the real generator's alphabet. For token_urlsafe secrets the
charset must be `[A-Za-z0-9_-]+`, not `[A-Za-z0-9]+`. Also check bare (non-`dsk_`) reset tokens,
Basic-auth base64, and `ghp_`/`sk-ant-` shapes if the scrubbed output can carry them. Prove leak rate
by generating sample tokens, not by eyeballing the regex.
