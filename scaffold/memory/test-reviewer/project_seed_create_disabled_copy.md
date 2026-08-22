---
name: seed-create-disabled-copy
description: Ontogen seed-page Vitest pins the exact copy "new seeds ship disabled"; spec (FRONTEND_ONTOGEN §Seed library) only says "creates the seed disabled" — flag as copy-pinning
metadata:
  type: project
---

The ontogen seed library page (`src/frontend/app/(app)/ontogen/seed/page.tsx:42`)
renders the body copy "...new seeds ship disabled." Its Vitest
(`ontogen/seed/page.test.tsx`, test "the page advertises that new seeds ship
disabled") asserts `getByText(/new seeds ship disabled/i)`.

**Why:** That exact phrase is NOT in FRONTEND_ONTOGEN.md — the spec (§Seed library,
~L37-42) says the library "creates the seed **disabled**" and surfaces create-disabled,
but does not mandate the literal string "new seeds ship disabled". The phrase
"Seeds ship disabled" appears only in metagen/governance contexts
(USE_CASE_en.md:757, BACKEND.md:975), not the seed-library frontend spec the test
cites. So this assertion is calibrated to incidental impl copy: a copy reword with no
behavior change breaks the test (T2 impl-pinning, low severity — the surrounding tests
already cover the real contract: badges render, toggle sends negated flag, create sends
markdown-only).

**How to apply:** When reviewing ontogen seed-page UI tests, treat the
"new seeds ship disabled" string assertion as the one copy-pinned assertion in an
otherwise spec-derived file. Acceptable to keep if relaxed to a stable substring
(e.g. /disabled/i scoped to the page intro) or removed; not a blocker.
Related: [[recipe-mask-string-divergence]] (same pattern — test pins impl copy).
