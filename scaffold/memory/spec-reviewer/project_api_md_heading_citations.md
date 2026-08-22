---
name: api-md-heading-citations
description: API.md section headings are cited as `spec: API.md §<Heading>` throughout src/ and tests/ — renaming one is never a local edit
metadata:
  type: project
---

`spec/API.md` section headings are load-bearing traceability anchors. Source and
test files cite them in module docstrings and test headers as
`spec: API.md §<Heading>`. Renaming a heading silently dangles every citation.

**Why:** In the issue-#66 spec review, the `spec` generator renamed
`### Data Resource (/spoke/common/data)` → `### Common Surface (/spoke/common)`
as an unrequested side edit while adding one route row. Grep showed **106
occurrences across 19 files** citing `API.md §Data Resource` — including
`src/api/routers/spoke/common/data/*.py`, `tests/integration/spot/`,
`tests/integration/api_wired/`, and `tests/e2e/ground/`. No spec file or TOC
entry pointed at the heading, so a spec-only anchor check reported clean and
missed the entire blast radius.

**How to apply:** Whenever a diff changes an `API.md` heading, grep `src/` and
`tests/` for `API.md §<old heading>` before judging the change — not just
`spec/` and not just markdown anchors. Report the citation count in the finding;
it converts an aesthetic argument into a cost argument. Treat an unrequested
heading rename on a priority-1 document as a high-severity finding on its own
(CLAUDE.md §Spec Convention: "Never modify unless explicitly requested"), and
require the citation sweep to be costed into the plan if the rename is genuinely
wanted. See [[architecture-md-duplicates-api-route-tree]].
