---
name: feedback-exists-subquery-autocorrelate
description: EXISTS/scalar subquery referencing a table already in the outer FROM auto-correlates to nothing and raises InvalidRequestError; mocked unit tests never catch it
metadata:
  type: feedback
---

A correlated `select(X.col).where(...).exists()` subquery raises
`InvalidRequestError: returned no FROM clauses due to auto-correlation` at
query-BUILD time when `X` is **already present in the enclosing query's FROM**
(e.g. via `.outerjoin(X, ...)`). SQLAlchemy auto-correlates `X` to the outer
query, leaving the subquery with no FROM. The same EXISTS works fine in a
sibling method whose outer FROM does NOT already join `X` (e.g. metagen
`list_items` correlates `MetagenCandidate` against a `MetagenItem`-only FROM —
fine; `list_dataset_summaries` joins `MetagenCandidate` into the agg FROM —
broken once `conf_id` adds the membership EXISTS on the same table).

**Why:** Seen 2026-06 in metagen `/spoke/metagen/dataset` — the conf_id path
crashed 100% of the time, but the generator's mocked unit tests (282 passed)
never built real SQL, so it shipped green. Same class as
[[project_asyncpg_str_uuid_column]]: query-shape bugs hide behind mocks.

**How to apply:** When reviewing any new aggregation/list service method that
joins a table AND also filters on an EXISTS/scalar subquery over the same
table, compile the statement against the postgresql dialect yourself
(`stmt.compile(dialect=postgresql.dialect())`) before trusting unit-test green.
Fix is an aliased table in the subquery (`X.__table__.alias()`) or explicit
`.correlate(OuterTable)`. Do not rely on "tests pass" for raw-SQL correctness.
