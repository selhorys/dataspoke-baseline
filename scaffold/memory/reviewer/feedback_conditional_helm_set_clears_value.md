---
name: conditional-helm-set-clears-value
description: A conditionally-emitted `--set` flag does not "leave the old value alone" — helm renders from scratch, so omitting it REMOVES the value and changes the pod template
metadata:
  type: feedback
---

When a script emits a `--set` / `--set-string` flag only under a condition
("emitted only when X resolved, so a failure degrades gracefully"), do not accept
that reasoning. `helm upgrade` re-renders values from files + flags on every run;
without `--reuse-values` the previous release's `--set` values are gone. Omitting
the flag therefore **deletes** the key from the render.

**Why:** `helm-charts/bin/install.sh` has no `--reuse-values` anywhere. When
`resolve_image_digest` fails, the `podAnnotations.dataspoke\.io/image-digest`
flag is skipped — which does not "preserve" the stamp on a release that already
carries one; the annotation disappears, the pod template changes, Helm rolls the
Deployment, and the script's own fallback `kubectl rollout restart` then rolls it
a second time. Both the code comment and the spec claimed the opposite.

**How to apply:** prove it with two renders rather than reading the comment —
build the arg array with the value set and unset and `grep` the output (see
`helm template ... -f values-dev.yaml` with the function bodies `sed`-extracted
from install.sh). Same trap for any "degrades gracefully by not passing the flag"
claim: annotations, `enabled`, image pins. Related:
[[helm-stale-local-subchart-tgz]], [[helm-null-and-replicas-gotchas]].

**Inverse case — the omission does NOT clear a values-FILE key.** `--set`
omission only deletes what a previous `--set` supplied; anything the chart's
`values.yaml` or the operator's `-f overlay.yaml` sets survives. So
`--no-digest-pin`, which emits no `image.digest` flag at all, still renders
`repo@sha256:...` if an overlay pins `api.image.digest` — the escape hatch
silently fails to escape. Clearing needs an explicit `--set-string
api.image.digest=`. Verify with `helm template ... -f overlay.yaml` and grep the
`image:` lines.
