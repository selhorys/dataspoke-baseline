---
name: pg-is-false-vs-partial-index
description: Verified on PG 17 — `col IS FALSE` never matches a `WHERE NOT col` partial index (not even with enable_seqscan=off), while `col = false` and `NOT col` do; recipe for the throwaway-container check
metadata:
  type: project
---

`sa_column.is_(False)` compiles to `col IS FALSE`, and PostgreSQL 17 will **not** use a
partial index declared `WHERE NOT col` for it — verified with `enable_seqscan = off`, so it
is an inapplicability, not a costing preference. `col = false` and bare `NOT col` both use
it (`eval_const_expressions`' `simplify_boolean_equality` folds `= false` into `NOT col`,
which then `equal()`-matches the stored index predicate; a `BooleanTest` node never does).
A bound parameter (`col = $1`) matches only under a *custom* plan and loses the index under
`plan_cache_mode = force_generic_plan`.

**Why:** `src/shared/dataset_filter.py` compiles `is_primary = false` through
`column.is_(False)` while `dataset_registry` carries
`ix_dataset_registry_not_primary … WHERE NOT is_primary` — the index the schema spec claims
serves exactly that predicate. Same trap applies to any future partial index over a boolean.

**How to apply:** whenever a review pairs a partial index with a compiler-emitted predicate,
prove the pairing instead of reading it. Recipe (no dev cluster needed, local brew postgres
is dyld-broken):

```
docker run -d --name pgidx -e POSTGRES_HOST_AUTH_METHOD=trust postgres:17-alpine
docker exec -i pgidx psql -U postgres   # create table, skewed insert, ANALYZE,
                                        # EXPLAIN (COSTS OFF) each candidate form
```

Test the *real* query shape too. RESOLVED in the `is_primary` fix pass: the compiler now
emits `bool_column == (sa.true() if node.value else sa.false())`, and the exact production
shape `datahub_registered IS true AND is_primary = false` re-verified on postgres:17-alpine
as `Index Scan using ix_dataset_registry_not_primary … Filter: (datahub_registered IS TRUE)`. Related: [[project-asyncpg-str-uuid-column]].
