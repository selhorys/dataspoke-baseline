---
name: cited-precedent-is-itself-violation
description: Generators justify layer-rule deviations with "the repo already does this" — check whether the cited precedents themselves violate the normative table; nothing lints src/backend -> src/api
metadata:
  type: feedback
---

When a generator's completion report defends a deviation with "the repo already has that
edge", verify the cited call sites against the normative rule before accepting them.

**Why:** `spec/feature/BACKEND.md` §Layer Rules is a table with a *Must not import from*
column — `src/backend/` must not import `src/api/`, and the same for `src/workflows/` and
`src/shared/`. Two files already break it (`src/backend/admin/dag_control_service.py` imports
`src.api.schemas.admin`; `src/backend/auth/privilege.py` imports `src.api.dependencies`), so a
generator can always find "precedent". Nothing enforces the rule mechanically: `pyproject.toml`
configures no flake8-tidy-imports / banned-api / import-linter, ruff and mypy both pass on a
fresh violation, and no unit test asserts the layering. The reviewer is the only gate.

**How to apply:** on any diff that adds a `from src.api...` import under `src/backend/` (or
`src/workflows/`), re-read BACKEND.md §Layer Rules and treat the pre-existing offenders as
debt, not authorization. The sanctioned bridge for a rule two layers share is a `src/shared/`
module with thin wrappers on each side — `src/shared/dataset_filter.py` ←
`src/api/schemas/_dataset_filter.py` + `src/backend/_dataset_filter.py` is the worked example,
and it keeps the "one copy of the rule" property the generator is usually optimising for.
Second-order cost worth naming in the finding: importing `src.api.schemas.<mod>` executes
`src/api/schemas/__init__.py`, which imports the whole schema barrel into the backend's import
graph, so one later `from src.backend...` inside any schema module turns it into a cycle.
Related: [[verify-generator-dead-code-claims]].
