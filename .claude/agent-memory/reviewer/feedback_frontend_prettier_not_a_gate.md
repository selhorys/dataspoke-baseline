---
name: frontend-prettier-not-a-gate
description: src/frontend is NOT prettier-clean at HEAD (129 files warn), so format drift is never evidence of a bad change — tsc --noEmit is the real gate
metadata:
  type: feedback
---

Never raise a `prettier --check` warning as a finding against a reviewed
`src/frontend` change without first running the same check on the HEAD version of
that file.

**Why:** `.prettierrc` sets `printWidth: 100`, but `package.json` has no `format`
or `format:check` script and nothing in CI enforces it. A repo-wide
`npx prettier --check "components/**/*.tsx" "lib/**/*.ts"` reports
**"Code style issues found in 129 files"** — the checked-in tree is broadly
non-conformant, including files no one has touched in months. On the governance
dashboard title-search/sort review, `prettier --check` flagged the reviewed
`page.tsx`; `git show HEAD:<file>` through the same check produced an identical
warning set, proving the drift was pre-existing and unrelated.

**How to apply:** the reliable frontend gates are `npx tsc --noEmit` (clean at HEAD
even with concurrent agents editing `lib/api/`) and Vitest. For any suspected
formatting finding, run
`git show HEAD:<path> > <scratchpad>/f.tsx && npx prettier --config .prettierrc <scratchpad>/f.tsx | diff - <scratchpad>/f.tsx`
from `src/frontend` and report only the delta the change itself introduced.
Formatting is a linter concern the review rubric already excludes — this memory
exists so the check does not get re-litigated. See
[[frontend-probe-silent-noop]] for the sibling trap in Vitest probes and
[[isolate-failures-concurrent-edit]] for the HEAD-isolation recipe.
