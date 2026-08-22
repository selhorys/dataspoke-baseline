---
name: override-flag-downstream-cannot-express
description: In a two-command sequence, any flag on command 1 that command 2 has no counterpart for silently validates a different target than the one that gets installed
metadata:
  type: feedback
---

When a feature is sold as "run A, then run B, and a pass in A means B passes",
diff A's flag list against B's. Every flag A accepts that B cannot express is a
way to validate one target and mutate another, with no warning on either side.

**Why:** `helm-charts/bin/install-prod-preflight.sh` takes `--namespace` and
`--secret-name`; `helm-charts/bin/install.sh` has **neither** — it always
resolves the namespace from `DATASPOKE_KUBE_DATASPOKE_NAMESPACE` and the Secret
from the overlay's `secrets.existingSecret` (default `dataspoke-secrets`). So
the pre-flight can adopt/create/verify a Secret in a namespace the install
never looks at, then print an install command with no way to say so. The same
diff also catches grammar drift: the pre-flight's `assert_k8s_name` allows
DNS-1123 *subdomains* while `install.sh:_validate_namespace_var` requires
*labels* (no dots, ≤63 chars).

**How to apply:** grep both argument parsers, not the docs. Also compare
default resolution, not just flag names — the pre-flight defaults `--values` to
`helm-charts/values-prod.yaml` when present; `install.sh` has no overlay
default at all. A hand-copied predicate is the same class of break: check for
literal regexes/bounds duplicated across the pair (image-tag allowlist lives at
`install.sh:141` and `install-prod-preflight.sh:747`; the admin-password
10–128 + not-`dataspoke` rule lives in the pre-flight *and* in
`post-install/seed-admin-user.sh`).
