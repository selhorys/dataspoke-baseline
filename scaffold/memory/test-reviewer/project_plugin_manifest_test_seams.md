---
name: plugin-manifest-test-seams
description: Anchors and survivor set for tests/unit/spec_conformance/test_plugin_manifests.py (issue #150) — which plugin/ regressions the guard kills and which it lets through
metadata:
  type: project
---

`tests/unit/spec_conformance/test_plugin_manifests.py` is the only thing under `tests/`
that reads `plugin/`. Measured with a scratchpad mirror (see [[feedback-review-method]] §6).

**Spec anchors** (all cited in the module docstring after the #150 fix pass):
- version-absence → `spec/AI_PLUGIN.md` §Distribution ("Neither manifest declares a
  `version` … resolves to the commit SHA of `./plugin`"), added in the same change set.
- the skill set → §Skills ("Six skills, each tracing to routes in `spec/API.md`") and its
  six-row table, parsed and compared set-wise.
- plugin manifest `name == "dataspoke"` → §Packaging layout diagram + `/plugin install
  dataspoke@dataspoke`.
- `bin/` helper trio → §Packaging layout diagram + the `bin/dataspoke-api` prose block.
- out-of-scope boundary (no helm/kubectl/`src/`/`admin/*`) → §Audience & Boundary.

**Killed** (verified): YAML-breaking `": "` in a plain scalar; `version` in either manifest
incl. `null`-valued; plugin name change; marketplace `source` != `./plugin`; skill
`name` != directory; missing/empty/non-string/boolean-coerced `description`; missing
frontmatter fence; tab-indented frontmatter; renamed or emptied `skills/` dir; a skill dir
nested one level deeper; deleted `SKILL.md`; deleted/renamed `.claude-plugin/`.

**Closed by the fix pass** (were survivors in the first draft; all now killed and
mutation-verified — re-check these first if the module is ever refactored):
- deleting all three `plugin/bin/` helpers, or dropping their `+x` bit — every skill's
  `Bash(dataspoke-api *)` breaks silently. The plan's "helpers on PATH is not a defect"
  verdict rests on exactly this premise. `TestBinHelpers` parametrizes off the §Packaging
  layout, not `ls plugin/bin`, so a deleted helper fails its own case instead of vanishing.
- `allowed-tools: Bash` (unscoped shell) or a grant naming kubectl/helm →
  `TestSkillToolGrantsRespectScope`, anchored on §Scope → "Out of scope".
- a 7th skill dir with no §Skills row, or a skill renamed consistently while the spec table
  goes stale → set-equality against the parsed §Skills table replaced the count floor, so
  the module is now bidirectional like its `spec_conformance` siblings.

**Trap when mutation-testing directory renames on macOS**: `skills` → `Skills` does NOT
break `glob("*/SKILL.md")` (case-insensitive APFS). Use a genuinely different name.
