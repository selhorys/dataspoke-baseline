---
name: no-references-remain-brace-grep
description: A generator's "no /X references remain" claim can be true for the literal pattern yet false for brace-expansion/list forms like {a,b,X} — grep broader
metadata:
  type: feedback
---

When a removal-stage generator claims "no `/X` references remain anywhere," do NOT
verify with only the literal slash-prefixed pattern. Doc/comment surface listings
collapse paths into brace-expansion or comma lists (e.g. `/api/v1/{auth,spoke,hub}`)
where the removed token appears as `,hub}` — never as the literal `/hub`. A
`grep -rn '/hub'` returns clean while a stale advertisement of the removed surface
survives.

**Why:** In the /hub relay-removal plugin stage, the generator's report asserted
"no `/hub` references remain anywhere in plugin/." True for `grep '/hub'` and
`grep 'hub/'`, but `plugin/README.md` still listed the public surface as
`/api/v1/{auth,spoke,hub}` — advertising a removed API surface to end users.

**How to apply:** For any "remove route/symbol X" stage, verify with a token-level
case-insensitive grep minus the legitimate homographs, e.g.
`grep -rni 'hub' plugin/ | grep -vi 'datahub'`, not just the path-literal form.
Brace lists, prose surface tables, and README "talks to …" sentences are the usual
hiding spots. Report a surviving advertisement of a removed surface as a major
finding (false completion claim + user-facing inaccuracy), even when the literal
grep the generator ran is clean. Pairs with [[verify-generator-dead-code-claims]].
