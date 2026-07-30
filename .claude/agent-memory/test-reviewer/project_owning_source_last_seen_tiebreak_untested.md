---
name: owning-source-last-seen-tiebreak-untested
description: The owning-source rule's third component (most recent last_seen_at) is unexercised at every tier, and the batch/single equivalence test is structurally blind to it
metadata:
  type: project
---

The owning-source priority rule in `src/backend/ingestion/service.py`
(`_reverse_lookup_rank`) has three components. Two are covered; the third is not.

Inverting the sign of the `last_seen_at` term — `-last_seen_at.timestamp()` →
`+…`, i.e. the *oldest* covering mapping wins — leaves **all 2809 unit tests green**
(verified 2026-07-30). No spot test covers it either: the closest,
`test_a_regular_parent_beats_its_wrapper_at_equal_derivation_rank`, pits a parent against
its wrapper, so the wrapper term decides before `last_seen_at` is ever consulted. A
covering fixture needs **two regular sources** (both `parent_source_id IS NULL`) at
**equal derivation** with different `last_seen_at`.

The pre-existing `TestReverseLookupPrecedence` docstring asserts the rule in prose and
sets both mappings to `datetime.now(tz=UTC)`.

**Why it matters more since #103.** `_reverse_lookup_rank` is now shared by
`reverse_lookup` and the batched `reverse_lookup_batch` the freshness measurer calls, and
the new spot module `test_ingestion_owning_source.py` quotes "remaining ties go to the
most recent `last_seen_at`" as its spec anchor.

**How to apply:** `test_batch_agrees_with_reverse_lookup_on_a_single_urn` is an
*equivalence* test between two callers of one shared rank function — it cannot catch any
mutation inside that function, because both sides move together. Never accept an
equivalence test as coverage of the rule the two implementations share; each component of
the shared rule needs its own discriminating fixture. Related: [[feedback-review-method]].
