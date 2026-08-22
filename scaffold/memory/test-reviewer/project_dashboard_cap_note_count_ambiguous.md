---
name: dashboard-cap-note-count-ambiguous
description: Governance dashboard cap-note — spec prose says "the first 100", impl renders the returned row count; the unit fixture models a state (returned < 100 < total) the real read cannot produce
metadata:
  type: project
---

`FRONTEND_GOVERNANCE.md` §Dashboard "Cap disclosure" says the note "states that only the
first **100** enabled metrics are shown", but its trigger is "when `total_count` exceeds the
**returned row count**". The impl (`app/(app)/governance/dashboard/page.tsx`) renders
`data.metrics.length`, not the literal 100.

In production the two always coincide: if `total_count > returned`, the read hit `limit=100`,
so `returned == 100`. `page.test.tsx` seeds `total_count=7` with 3 rows — a state the real
read cannot produce — and asserts the note shows **3**, i.e. it adopts the returned-row-count
reading. That expected value is impl-derived, not spec-derived, and an impl that literally
hardcoded "100" per the prose would fail it.

**Why:** cycle-2 review of issue #148. The test's own comment discloses the interpretation, so
it is defensible, but the number is only pinned by the impl.

**How to apply:** don't flag the "3" as bare impl-pinning without noting the spec ambiguity;
the durable fix is a spec sentence ("states how many rows are shown and the total"), not a
weaker assertion. Both directions now use the same whole-document matcher — positive
`/\b3\b[\s\S]*\b7\b[\s\S]*enabled metrics/`, negative `not.toMatch(/enabled metrics/)`. The
general trap: loosening one direction to a connector-agnostic regex while the other keeps a
`queryByText` element-scoped matcher lets the negative go vacuous on the very re-wordings the
positive was loosened to tolerate. See [[waitfor-presettlement-race]] for the sibling
"assertion that cannot fail" class.
