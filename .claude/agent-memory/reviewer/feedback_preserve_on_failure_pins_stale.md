---
name: preserve-on-failure-pins-stale
description: RESOLVED — install.sh's preserve-the-old-digest fallback was replaced by a two-outcome resolve-or-abort model; keep the lesson that a "preserved" value which also SELECTS content deploys the old build, and prove restart claims by counting mocked kubectl calls
metadata:
  type: feedback
---

When a fix answers "omitting the `--set` flag DELETES the value" (see
[[conditional-helm-set-clears-value]]) by reading the live value off the cluster
and re-emitting it, check what that value now *selects*. Preserving is correct
only for a passive marker. Once the same value also pins the image reference
(`repo@sha256:X` instead of `repo:tag`), preserving it on a run that just
rebuilt and pushed means the new build is **never deployed** — and, because the
Deployment is pinned to the old digest, a manual `kubectl rollout restart`
re-pulls the old content too. The run still prints its success banner.

**Why:** `helm-charts/bin/install.sh` once had `_resolve_digest_or_preserve` +
`_ensure_rolled` + a `dataspoke.io/image-tag` comparison, which shipped stale
code silently on a transient `gcloud artifacts docker images describe` failure.
That whole layer is gone: resolution is now `_resolve_digest_or_abort`
(resolve, or `error` before the umbrella `helm upgrade`), with `--no-digest-pin`
as the only, operator-chosen fallback. Do not expect those function names to
exist.

**How to apply:** still the transferable rule — never accept "the fallback
issues an explicit `kubectl rollout restart`" from a comment or spec paragraph.
Count the mocked `kubectl rollout restart` invocations. And when a resolve step
can abort, check the abort actually propagates: `VAR="$(fn)"` runs `fn` in a
subshell, so an `error`/`exit 1` inside it only kills the script because
`set -e` re-raises the assignment's status — verify with a harness rather than
assuming. Related: [[install-sh-tool-baseline]].
