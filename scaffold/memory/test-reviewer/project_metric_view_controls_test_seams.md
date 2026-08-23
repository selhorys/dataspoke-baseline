---
name: metric-view-controls-test-seams
description: Governance dashboard title search/sort — measured mutation table across two review cycles; which mutants die, the one that correctly survives, and the fixture invariants that must hold
metadata:
  type: project
---

Measured on the `title` search/sort rename (`/governance/dashboard`), Vitest
`app/(app)/governance/dashboard/page.test.tsx` (23 tests) + E2E
`tests/e2e/ground/governance/dashboard-view-controls.spec.ts` (6 tests).

**Killed** — sort key -> `description` (both directions); search key -> `description`;
search over `title + " " + description`; prefix instead of substring; case-sensitive
haystack; `aria-label`/placeholder -> "Search descriptions"; `<SelectItem>` labels ->
`Description A→Z`; dropped `.trim()`; `DEFAULT_METRIC_VIEW.sortDir` -> `"desc"`;
inverted `dir` mapping; swapped SelectItem value<->label; no sort at all; sort by `id`;
raw code-unit `<` instead of a human collation; empty `types` falls back to all;
cap note counts the post-filter set.

**Correctly survives (do NOT "fix" in a later cycle)**: filtered-empty copy reverted to
"type filter and description search". Both layers assert only `/controls/i` because
FRONTEND_GOVERNANCE.md §Dashboard states the *condition* of that empty state, not its
wording. Pinning it would be impl-copy pinning.

**Anchor altitude that was ruled correct** (cycle 2): `Title A→Z` / `Title Z→A` are
backticked in the §Dashboard prose *and* the ASCII mock, so `/^Title\b/` is strictly
weaker than spec — fine. `Search titles…` is fixed ONLY by the ASCII mock line, so the
tests anchor `/^Search titles\b/i` (leading words) and leave the U+2026 free — also fine.

**Fixture invariants any future edit must preserve** (verify with `localeCompare`, not
by eye): titles Alpha / bravado / Charlie against descriptions Yankee / Xray / Zulu make
description-asc a *third-order permutation* (`[bravo, alpha, charlie]`), so title-asc,
title-desc, description-asc and description-desc are four distinct orders — a reversed
description order lets "sort by description with an inverted `dir`" pass. The lower-case
middle title is what separates ICU from code-unit ordering (`b` 0x62 > `C` 0x43); a
`toLowerCase()` + `<` impl also passes, which is the right altitude since the spec says
only `A→Z`. Every needle sits mid-`title` and in no description, and vice versa.

Related: [[dashboard-cap-note-count-ambiguous]], [[project-uc5-governance-e2e-anchors]],
[[playwright-tohavetext-regex-not-normalized]].
