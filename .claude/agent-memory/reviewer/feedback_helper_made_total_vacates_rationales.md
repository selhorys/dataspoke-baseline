---
name: helper-made-total-vacates-rationales
description: When a change makes a helper total (it can no longer raise), hunt every comment/docstring/test that justified a placement by that raise — they silently become vacuous
metadata:
  type: feedback
---

A change that wraps a helper's body in `try/except Exception` makes it *total*: it can no longer
raise. Every rationale elsewhere that was phrased as "we call it **here** so the raise lands in
the guarded scope" is then justifying a distinction with no behavioural consequence. Grep for the
call sites and read the surrounding prose, not just the code.

**Why:** #140 guarded the `db.bind` read inside `independent_sessionmaker`. That silently vacated
(a) the comment in `IngestionService._report_api_health` explaining why the factory is resolved
per call inside the `try`, and (b) the kill power of
`test_reading_the_injected_sessions_bind_cannot_break_the_sweep`. Applying the mutation the test
was written to kill (hoisting `factory = independent_sessionmaker(self._db)` above the `try`)
produced **0 kills** across the whole `tests/unit/` suite afterwards.

**How to apply:** verify a generator's "this mutation now survives / still dies" claim by
*applying* it, never by reasoning — `cp file scratchpad/file.bak`, patch, `uv run pytest <file>`,
restore from the backup (the working tree is dirty, so `git checkout` is not a safe undo). Also
check for sibling rationales that get *weaker* rather than false: a comment that is still literally
true but no longer distinguishes the alternative is the usual residue of this class of fix. Related:
[[verify-branch-reachability-rationales]], [[isolate-failures-concurrent-edit]],
[[asyncsession-bind-seam]].
