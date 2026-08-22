---
name: vendor-cli-error-string-match
description: A fix that greps a vendor CLI's stderr for a sentinel ("NOT_FOUND") is only as good as the real CLI's wording — verify against the installed SDK source, never against the mock the generator wrote to test it
metadata:
  type: feedback
---

When a change short-circuits, branches, or classifies on a **string found in a
third-party CLI's stderr**, do not accept a mocked-harness demonstration. The
mock is authored by the same agent that authored the matcher, so it emits
exactly the sentinel the matcher looks for — a self-fulfilling test that proves
nothing about the real tool.

**Why:** `resolve_image_digest` in `helm-charts/bin/lib/helpers.sh` skipped its
3× retry loop only when gcloud's stderr contained `NOT_FOUND`. The installed SDK
disproved it for the case the fix targeted: in
`google-cloud-sdk/lib/googlecloudsdk/command_lib/artifacts/docker_util.py`,
`_ValidateAndGetDockerVersion` catches `HttpNotFoundError` and re-raises
`InvalidInputValueError(_DOCKER_IMAGE_NOT_FOUND)` whose text is
`"Image not found."` (constant at ~line 89, raised at ~line 445) — no
`NOT_FOUND` substring. Only a missing *repository* (the uncaught
`GetRepository` 404) renders `NOT_FOUND: ...`.

**Status:** code fixed — the matcher is now
`grep -qiE "NOT_FOUND|Image not found"`, and the AWS branch gained the
symmetric `grep -qE "RepositoryNotFoundException|ImageNotFoundException"`
alongside its pre-existing exit-0 `"None"` case. Verified with mocks emitting
the *real* SDK/botocore text: 1 attempt each, no 2s sleep.
`spec/feature/HELM_CHART.md` §Digest stamping still describes the narrower
`NOT_FOUND` / `"None"` pair — check whether that lagged text was ever synced
before re-flagging the code.

**How to apply:** the vendor SDKs are usually on the box. `readlink -f $(command
-v gcloud)` → `<sdk>/lib/googlecloudsdk/command_lib/<surface>/`, then grep the
message constants and trace which one the subcommand's `Run` reaches. Same move
for `aws` (botocore error shapes). And when the code matcher is widened, grep
the spec for the old sentinel — a doc that still names only the original string
is the other half of the same finding. Related:
[[preserve-on-failure-pins-stale]], [[verify-branch-reachability-rationales]].
