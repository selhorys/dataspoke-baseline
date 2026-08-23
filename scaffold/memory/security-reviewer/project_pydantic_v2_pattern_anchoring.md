---
name: pydantic-v2-pattern-anchoring
description: Verified — pydantic v2 Field(pattern=...) uses rust-regex with ^ = start-of-haystack, so ^scheme:// prefix guards are not bypassable. Promoted to scaffold/roles/security-reviewer.md §4 Input validation.
metadata:
  type: project
---

The standing rule now lives in `scaffold/roles/security-reviewer.md` §4 Input validation at trust
boundaries: an anchored `^scheme://` pydantic v2 pattern is genuinely anchored, not bypassable via
search/multiline semantics — but still check what it permits without a trailing `$` (embedded
credentials, CRLF, unicode bidi/confusables).

Empirically verified against this repo's pydantic (2.13.x): `javascript:alert(1)#https://x`,
`\nhttps://x`, `  https://x`, `//evil.com`, `data:text/html,...`, `HTTPS://x` are all **rejected**.

Related: [[peripheral-config-to-href-trust-chain]].
