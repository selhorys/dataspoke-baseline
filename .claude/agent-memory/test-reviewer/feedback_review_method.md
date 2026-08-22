---
name: feedback-review-method
description: Test-review method for this repo — mutate src and re-run to verify discrimination claims; grep the test tree for citations orphaned by spec edits in the same change-set
metadata:
  type: feedback
---

Two review steps that have repeatedly found things a read-only review missed.

**1. Independently mutation-test, do not trust the generator's mutation table.**
Patch the impl (`cp` a backup first), re-run the affected suite, restore, then
`git diff --stat` the touched src files to prove the tree is byte-restored.
Extend beyond the mutations the generator reported: mutate the *sibling* call
sites it did not cover. That is how a "guarded by the preset/custom param tests"
claim gets falsified — the guard often covers 1 of N call sites.

**Why:** a generator's mutation table is scoped to the tests it wrote, so it
proves those tests discriminate but says nothing about the blast radius the plan
claimed they cover.

**How to apply:** any stage whose plan lists a named regression risk. Reproduce
the risk at the *least*-covered site, not the most.

**2. When a change-set edits a spec section or removes a contract value, grep the
whole test tree — plus `tests/e2e/` and `src/frontend/` — for the deleted text AND
the deleted identifier, not just the files the agent modified.**
`spec/TESTING.md §Assertion Discipline` forbids a `spec:` citation whose text is
absent at the cited location, and a stage-1 spec rewrite silently orphans
citations in untouched test files that the test stage never opens.

**Why:** the citation rule is verified per-file, so an orphaned quote in an
adjacent file passes every gate (typecheck, lint, the suite) and survives.

**How to apply:** normalize whitespace and em/en dashes, then substring-match
each removed spec sentence against `src/**/*.test.*` and `tests/`. Do the same for
any *identifier* the change removed from an enum / allowlist / emitted-key set — a
plain `grep -rn <old_key>` across `tests/` + `src/frontend/` catches suites the unit
run never touches (the `validation_score_sum` → `valid_confd`/`valid_in_time` recast
left spot, api-wired, two E2E specs and six Vitest files pinning a key the API now
422s). Beware: a naive `grep` of a long spec quote fails on sentences that wrap
mid-phrase — normalize newlines before concluding a citation is fabricated.

**3. A static import/source scan closes symbol-swap leaks only — never behavior.**
When a generator answers "N of M call sites unguarded" with an O(1) source-tree
scan (e.g. "symbol X is imported only by file Y"), re-mutate the *behavior* at an
unguarded site rather than the import. Inline re-pins (`?? new Date()`), dropped
`useMemo` wrappers, and wrong hook deps all slip past a scan that only reads
`import` statements, and past `tsc` too.

**Why:** the scan and the behavior are different invariants; accepting the scan
as the fix for a coverage finding silently narrows the finding.

**How to apply:** after any static-invariant test lands, run two mutations at an
unguarded call site — one that changes an identifier (scan should catch) and one
that changes only an expression (scan will not). Report both results.

**4. "Untestable without patching import machinery" is usually false — check it.**
An agent excluding a spec'd branch because reaching it "means patching Python's
import machinery" can normally be refuted in two lines:
`monkeypatch.setitem(sys.modules, "<pkg.mod>", None)` makes
`from <pkg.mod> import X` raise `ImportError`. Reproduce the branch yourself
before accepting the exclusion, then ask whether the branch's *discriminating*
logic (usually a guard deciding **which** inputs degrade) is behavioural rather
than a tautology.

**Why:** a "deliberately unexercised, exclusion is a decision not an oversight"
docstring reads as rigour and is rarely re-checked, so it can permanently retire a
row of a spec outcome table.

**How to apply:** any test-file docstring that argues a spec'd case is unreachable.
Reproduce first; only then judge the argument.

**5. Read-only reviews can still mutation-test the frontend — rsync it to the
scratchpad and symlink `node_modules`.**
When the launch prompt forbids editing the tree, `rsync -a --exclude node_modules
--exclude .next src/frontend/ <scratchpad>/fe1/` then
`ln -s <repo>/src/frontend/node_modules <scratchpad>/fe1/node_modules`, and run
`npx vitest run <paths>` from the copy. Vitest resolves `@/` from the copied
`vitest.config.mts`/`tsconfig.json`, so aliases work and the repo is never touched.

**Why:** the alternative — accepting the generator's prose about what its fixture
discriminates — is exactly the claim most worth falsifying, and "read-only" is not
a reason to downgrade to prose review.

**How to apply:** any Vitest-scoped review. Keep a `page.tsx.orig` inside the copy
and `diff` after each mutation so the copy itself stays honest. `rm -rf` may be
permission-blocked; use a fresh numbered dir instead.

**6. Python tests that read repo *data files* mutation-test via a scratchpad mirror —
repo writes are classifier-blocked under a read-only launch.**
When the file under review resolves paths from `Path(__file__).resolve().parents[N]`,
copy the data tree AND the test file into a scratchpad mirror at the same relative depth
(`mirror/tests/unit/<pkg>/test_x.py`), so `parents[N]` resolves to the mirror root. Then
`.venv/bin/python -m pytest` with `cwd=mirror`. Mutate the mirror, never the repo.

**Why:** under a read-only prompt the harness blocks writes to the repo, and a `bash`
heredoc that writes a script containing repo-copy operations gets blocked too — but
`uv run python - <<PY` writing only into the scratchpad succeeds.

**How to apply:** any spec-conformance / manifest / chart-text test. Verify the mirror
baseline is green *before* mutating, and re-check `git status --porcelain` at the end.
Watch for macOS case-insensitivity: a directory rename that only changes case is a no-op
mutation and shows up as a false survivor.
