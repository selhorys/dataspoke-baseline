---
name: verify-spec-citations-verbatim
description: Docstring `spec:` citations in tests/ are sometimes aspirational or fabricated — grep the quoted sentence in spec/ before accepting it as an anchor
metadata:
  type: feedback
---

Never accept a `spec: <FILE>.md §<Section> — "<quote>"` line in a docstring as evidence that the
spec says it. Grep the quoted words in `spec/` first.

**Why:** `tests/integration/util/datahub.py::wait_until_datasets_searchable` carries
`spec: TESTING.md §Per-Module Dummy-Data Reset — ingest post-condition is "emitted AND
searchable" so the sync sweep sees the full universe.` The word **searchable** appears nowhere in
`spec/TESTING.md`, at HEAD or after the paragraph that section later gained. The citation was
written to describe a rule the author believed *should* exist. A reviewer who treats these as
anchors inherits fabricated authority — and this repo's own review brief demands verbatim
anchoring, so a fabricated citation launders a convention into a "requirement".

**How to apply:** for every `spec:` line in changed code, run
`grep -n "<5-8 distinctive words>" spec/<file>.md`. Empty result = report it as a finding, and say
plainly that the claim is unsupported rather than paraphrasing the docstring. The same check
catches the inverse: a diff that edits the exact spec paragraph a dangling citation points at and
still leaves the citation dangling. Related: [[grammar-mirror-has-no-guard]],
[[grep-old-rule-prose-in-consumers]].
