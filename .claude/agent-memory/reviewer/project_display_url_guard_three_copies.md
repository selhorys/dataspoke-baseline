---
name: display-url-guard-three-copies
description: Peripheral display-URL safety regex is hand-duplicated in 3 places (pydantic write pattern, router read sanitizer, lib/safe-url.ts) with no shared source or contract test — how to diff-verify them
metadata:
  type: project
---

Operator-supplied display URLs (`datahub.frontend_url`, `langfuse.host`,
`langfuse.project_id`) are guarded in three independent copies:

1. write boundary — `Field(pattern=SAFE_DISPLAY_URL_PATTERN)` on the admin PATCH schemas (rust-regex)
2. read boundary — `sanitize_display_url()` / `sanitize_project_id()` in `src/api/schemas/common.py` (Python `re.fullmatch`)
3. browser — `sanitizeDisplayUrl()` / `sanitizeProjectId()` in `src/frontend/lib/safe-url.ts` (JS `RegExp.test`)

**Why:** `peripheral_config.settings` is untyped JSONB, so a row written by raw SQL
bypasses (1); the values land in an `<a href>`, so the FE re-checks. Three copies of
one regex with no shared source and no cross-language contract test = drift risk.

**How to apply:** when any diff touches one copy, verify all three still agree — do
not eyeball the patterns. Recipe that works: dump a case corpus + Python verdicts to
JSON, strip the TS types out of `safe-url.ts` into a `.cjs`, require it in node, and
diff. Key semantics: Python `$` matches before a trailing newline so the read-side
MUST use `fullmatch` (it does); JS `$` without `/m` has no such exception, so
`.test()` with `^…$` is already fullmatch-equivalent. A 4k-case fuzz over
`http(s)://`, `javascript:`, userinfo, CRLF, bidi (U+200E/200F/202A-202E/2066-2069)
and IPv6 showed zero divergence as of issue #66. Note the pattern rejects IPv6
literals (`http://[::1]:8080`) and bare-host query/fragment (`https://a.com?q=1`) on
all three sides. Related: [[verify-generator-dead-code-claims]].
