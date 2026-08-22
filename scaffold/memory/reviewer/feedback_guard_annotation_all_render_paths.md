---
name: guard-annotation-all-render-paths
description: A value the same system writes elsewhere is only present on the render paths that write it — prod hand-rolls its frontend --set flags instead of reusing _frontend_helm_set_args, so anything emitted only by that helper is missing there
metadata:
  type: feedback
---

When a fix depends on a value the same system writes elsewhere (a pod
annotation, a DB column, a header), do not stop at "the writer now writes it".
Enumerate **every** code path that renders the object, and confirm each one
writes the value.

**Why:** `helm-charts/bin/install.sh` has a structural asymmetry that keeps
producing this bug. `_api_image_helm_set_args` is called on all four paths that
render the api/event-consumer templates, but `_frontend_helm_set_args` is
called only by the dev paths — the **prod** branch hand-rolls
`--set frontend.image.{repository,tag}` plus a separate
`frontend_image_digest_args` array. An earlier round shipped a
`dataspoke.io/image-tag` mismatch guard that `_frontend_helm_set_args` stamped
and prod therefore never wrote, leaving the guard inert in the default prod
configuration (`--frontend cluster`). That guard has since been deleted, but
the split helper remains — anything new added to `_frontend_helm_set_args` must
be mirrored into the prod array by hand.

**How to apply:** grep the key across the whole script, not the shared helper —
`grep -n "podAnnotations\|frontend.image" install.sh` immediately shows which
workloads x which branches get it. Then exercise the consuming logic with the
value ABSENT, not just present-and-mismatched; an "absent ⇒ cannot disagree ⇒
allow" escape hatch is where a partially-armed guard hides. Related:
[[renamed-guard-comparison-target]], [[conditional-helm-set-clears-value]],
[[optin-workload-unconditional-wait]].
