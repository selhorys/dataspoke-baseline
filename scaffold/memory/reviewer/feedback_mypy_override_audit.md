---
name: mypy-override-audit
description: Auditing [[tool.mypy.overrides]] — --warn-unused-configs does NOT catch over-granted error codes; re-run mypy with overrides stripped to get the true suppressed set
metadata:
  type: feedback
---

When a diff stages typing debt behind per-module `[[tool.mypy.overrides]]`, never accept
"mypy is 0 errors and `--warn-unused-configs` is clean" as evidence the overrides are minimal.

**Why:** `--warn-unused-configs` only reports override *sections* that match no module at all.
It says nothing about a section that matches a real module but disables error codes that module
never trips. In the issue-#72 review, 5 of 18 module entries carried unused codes
(`validation.assertions` was granted `arg-type` + `no-any-return` + `attr-defined` but only trips
`attr-defined`). Root cause was grouping two modules into one `module = [...]` list and taking the
**union** of their codes — the generator's own policy comment forbade exactly that.

**How to apply:** Reconstruct the true suppressed set by writing a scratch `mypy.ini` that copies
`[tool.mypy]` globals but omits every override, run `uv run mypy --config-file <scratch> src/`,
then diff each module's actually-tripped codes against its `disable_error_code` list
programmatically (parse `pyproject.toml` with `tomllib`). That list is also the only reliable way
to test whether each override's justifying *comment* names the real cause — comments frequently
describe one error while the disable silently covers a second, unrelated one (e.g. a variable-
shadowing `arg-type` defect hidden under an "optional deps are non-None" rationale).

Two related gotchas verified the same way:
- `untyped_calls_exclude = ["datahub"]` matches at dot boundaries: `datahub.sub` is exempt,
  `datahubx` and `mypkg.datahub` are not. Our `src.shared.datahub` is therefore unaffected.
- Ruff `per-file-ignores` has no unused-config warning at all; check each listed file actually
  trips the ignored rule via `ruff check --isolated --select <rule> <file>`.

See [[verify-branch-reachability-rationales]] for the sibling rule on prose rationales.
