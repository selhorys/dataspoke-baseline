---
name: dataset-filter-verdict-test-seams
description: SQL dataset_filter + metric verdict store — cycle-1 and cycle-2 mutation tables; the predicate-polarity blind spot (== vs !=, is_(True) vs is_(False)) that survives the whole unit suite
metadata:
  type: project
---

Branch `feat/governance-sql-dataset-filter`: `dataset_filter` became a SQL `WHERE`-clause
string over `dataset_registry`; per-dataset verdicts land in `metric_dataset_results`.
Measured mutation results on the **unit** suite (3399 tests, ~28s).

**Cycle-2 (after fix pass 1) — closed:** all five cycle-1 survivors are now killed —
`_replace_dataset_verdicts` DELETE scoping + bound id, `list_metric_datasets`
`verdict_join` metric_id, `met=true`/`met=false` predicate swap, `on_conflict_do_update`
→ `do_nothing` / SET missing `tag_urns` (unit AND the spot `>` watermark), and the
`dataset_urn_asc` default order. Also killed: tri-state map swap, `last_check_at`
fallback swap, watermark `max`→`min` / unscoped / paged, scope dropping
`datahub_registered` or the clause entirely, all-three adding a predicate,
breakdown listing passers, registry `datahub_registered` insert/SET mutants.

**Cycle-2 survivors — the polarity blind spot.** Every SQL-shape assertion in this change
set asserts the **column name**, never the operator, so these all pass 3399/3399:

| Mutation | Site | Test that should have caught it |
|---|---|---|
| `column == sa.literal(v)` → `!=` | `src/shared/dataset_filter.py` `_compile` Equals | `test_equality_compiles_against_the_named_registry_column` |
| `column == sa.any_(…)` → `!= sa.all_(…)` | same, InList | `test_in_list_binds_the_values_it_names` |
| `column.contains(…)` (`@>`) → `== sa.any_(…)` | same, ArrayContains — defeats the GIN index BACKEND_SCHEMA.md L595 ties to this predicate | `test_membership_compiles_against_the_named_registry_column` |
| `datahub_registered.is_(True)` → `is_(False)` | `_dataset_filter.py:85` **and** `metrics/service.py:809` | `test_only_registered_datasets_are_in_scope`; `assert "datahub_registered" in page_sql` |

Fix shape: assert the rendered predicate, e.g. `"dataset_registry.origin = %(param_1)s"`,
`"dataset_registry.tag_urns @> "`, `"datahub_registered is true"`.

**Cycle-2 survivor — the sibling DELETE.** `delete_metric_config` (service.py:509/512)
tolerates BOTH `delete(MetricResult)` and `delete(MetricDatasetResult)` losing their
`.where(metric_id == …)`, and a wrong bound id.
`test_delete_metric_config_clears_results_and_verdicts` only substring-checks the two
table names among issued DELETEs; the spot cascade test only checks the deleted metric's
own rows vanish. Beware: `replace(old, new, 1)` on `delete(MetricDatasetResult).where(…)`
hits line 512 (delete_metric_config), NOT line 651 (`_replace_dataset_verdicts`) — mutate
by line number here.

**Still uncovered spec line** (unchanged from cycle 1): BACKEND_SCHEMA.md L176/595-596's
four `dataset_registry` indexes exist in `models.py:240-247` but none is in
`test_indexes_exist`'s `expected_indexes`.

**Citation trap:** `TestFormatFilter` anchors to FRONTEND_BASIC.md §Shared Component
Notes, which says the Auto-indent button "never rejects, rewrites, or silently repairs" —
but `format_filter` uppercases keywords, lowercases columns, drops redundant parens and
raises. The right anchor for those cases is BACKEND.md:1374 ("a canonical formatter").

**Vacuity watch:** spot equivalence tests scope on `origin = 'DEV'` where the seeded dev
estate is entirely DEV, so a clause compiled to `TRUE` satisfies them; they need a
negative control. `test_metrics.py:2766` `!= ["unknown"]` passes on an empty list.

See [[run-id-filter-then-assert-tautology]] for the sibling "filter then assert the filter"
shape; [[populate-existing-alias-fake-trap]] for the fake-session pitfall this suite avoids.
