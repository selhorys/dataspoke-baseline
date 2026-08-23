---
name: residual-diff-for-move-claims
description: Proving a "pure move / no behaviour change" claim — normalise HEAD for foreign edits, strip the moved blocks, diff the residual; a raw git diff misattributes hunks
metadata:
  type: feedback
---

To audit a "this is a verbatim move, no behaviour change" claim, do not read the raw
`git diff` — build a **residual diff**:

1. Copy `git show HEAD:<file>` into the scratchpad.
2. Normalise HEAD for any *other* phase's edits already sitting in the working tree
   (in the #144 run, `install.sh` already carried commit-3's `DATASPOKE_TEST_*` →
   `DATASPOKE_DEV_*` rename — 28 sites — so every one of those lines showed up as a
   move-stage hunk until it was `sed`-normalised away).
3. Programmatically strip each moved function *and its contiguous leading `#` docstring*
   from the normalised HEAD copy.
4. `diff -u` the stripped HEAD against the working-tree file. What remains is exactly the
   set of changes the stage actually made to the origin file, and every line of it must
   map to a declared deviation.

Then diff each moved body **and its docstring separately** against the destination file
(a body can be character-identical while the docstring above it was silently reworded),
and drive the moved predicate **side by side against the HEAD copy** — `source` the new
lib, then `source` a file holding HEAD's function definition so both are callable in one
shell, and compare stdout+status case by case under a stubbed `kubectl`.

**Why:** the multi-agent workflow does not keep stages in plan order, so the tree under
review routinely mixes phases; and generators reliably declare body changes while missing
comment-block edits. Character-identity of bodies is necessary but not sufficient.

**How to apply:** any stage whose contract is "extract / relocate / rename, behaviour
unchanged". Pair it with [[no-references-remain-brace-grep]] — a rename's stale
references land in chart `values.yaml`/template comments and spec prose that no later
stage in the plan owns.
