---
name: frontend-prettier-not-a-gate
description: Neither prettier (src/frontend, 129 files warn at HEAD) nor `ruff format` (src/, migrations/ dirty at HEAD) is a gate — only tsc --noEmit / Vitest and `ruff check` / mypy are
metadata:
  type: feedback
---

Never raise a formatter warning as a finding without first running the same check
on the HEAD version of that file. This holds on **both** sides of the repo.

**Why (frontend):** `.prettierrc` sets `printWidth: 100`, but `package.json` has no
`format` or `format:check` script and nothing in CI enforces it. A repo-wide
`npx prettier --check "components/**/*.tsx" "lib/**/*.ts"` reports **"Code style
issues found in 129 files"** — the checked-in tree is broadly non-conformant,
including files no one has touched in months. On the governance dashboard
title-search/sort review, `prettier --check` flagged the reviewed `page.tsx`;
`git show HEAD:<file>` through the same check produced an identical warning set,
proving the drift was pre-existing.

**Why (Python):** same shape. On the req3 passive-observation review,
`uv run ruff format --check src/ migrations/` reported 5 files "would be
reformatted" — every one of them a file the run had touched, which looks damning.
Piping the HEAD blobs through the same check
(`git show HEAD:src/shared/db/models.py > /tmp/f.py && uv run ruff format --check /tmp/f.py`)
reformatted them too. `ruff check` (the configured lint gate) passed cleanly on the
same tree.

**How to apply:** the real gates are `pnpm -C src/frontend typecheck` + Vitest on the
frontend, and `uv run ruff check` + `uv run mypy` + `uv run pytest tests/unit/` on
Python. For any suspected formatting finding, isolate against HEAD first — e.g.
`git show HEAD:<path> > /tmp/f.py && uv run ruff format --check /tmp/f.py` — and
report only the delta the change itself introduced. Formatting is a linter concern
the review rubric already excludes; this memory exists so the check does not get
re-litigated. See [[frontend-probe-silent-noop]] for the sibling trap in Vitest
probes and [[isolate-failures-concurrent-edit]] for the HEAD-isolation recipe.
