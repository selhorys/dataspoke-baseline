---
name: pydantic-v2-pattern-anchoring
description: Verified — pydantic v2 Field(pattern=...) uses rust-regex with ^ = start-of-haystack (not re.search, not multiline), so ^scheme:// prefix guards are not bypassable
metadata:
  type: project
---

`Field(pattern=...)` in pydantic v2 (2.13.x here) is rust-regex `is_match` with
`multi_line` off, so a leading `^` anchors to start-of-haystack.

**Why:** a scheme allowlist like `^$|^https?://` is only a real control if the
anchor holds. Empirically verified against this repo's pydantic:
`javascript:alert(1)#https://x`, `\nhttps://x`, `  https://x`, `//evil.com`,
`data:text/html,...`, and `HTTPS://x` are all **rejected**.

**How to apply:** do not report `^scheme://` prefix patterns as bypassable via
search-semantics or newline-multiline tricks. Do still report what such a pattern
genuinely permits, since there is no trailing `$`: embedded credentials
(`https://user:pass@evil.com`), CRLF and control characters after the scheme
(`https://evil.com\r\n...`), and unicode bidi/confusables (`https://‮evil.com`)
all pass. Relevant wherever a validated string lands in an href or a server-side
fetch — see [[peripheral-config-to-href-trust-chain]].
