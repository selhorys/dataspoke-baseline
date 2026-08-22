---
name: architecture-md-duplicates-api-route-tree
description: ARCHITECTURE.md:178 holds a near-verbatim copy of the API.md route tree — sync it whenever that tree changes
metadata:
  type: project
---

`spec/ARCHITECTURE.md` (~line 178) contains a near-verbatim duplicate of the
`/api/v1/spoke/…` route tree block in `spec/API.md` (~line 39). Editing one
without the other leaves priority-3 `ARCHITECTURE.md` contradicting priority-1
`API.md`.

**Why:** In the issue-#66 spec review the generator amended the `API.md` tree to
add `/spoke/common/peripheral-links` and propagated into five files the plan had
not even named — yet missed the one file holding a literal copy of the very
block it was editing. Duplicated blocks are the reliable blind spot, because
propagation instincts follow topic links rather than copied text.

**How to apply:** On any diff touching the `API.md` route tree, open
`ARCHITECTURE.md` and diff the two blocks directly. The §Feature-to-Route
mapping table (~line 286) is a separate question — it takes an entry only for a
MANIFESTO §2.1 feature namespace, not for every route. More generally, when a
spec change edits a block that reads like boilerplate, grep a distinctive line
of it across `spec/` to find the other copies. See
[[api-md-heading-citations]].
