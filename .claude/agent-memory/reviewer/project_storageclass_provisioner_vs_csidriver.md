---
name: storageclass-provisioner-vs-csidriver
description: StorageClass .provisioner is NOT a CSIDriver name — external non-CSI provisioners carry a slash and register no CSIDriver, and CSI-migrated kubernetes.io/* classes need a CSI driver the in-tree exemption skips
metadata:
  type: project
---

`helm-charts/bin/install.sh`'s prod pre-flight (~2530) reads
`kubectl get storageclass <name> -o jsonpath='{.provisioner}'`, exempts
`kubernetes.io/*`, then validates the rest with the DNS-1123 object-name regex
`^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$` and requires a registered `CSIDriver`.
Three facts that regex/exemption pair gets wrong:

1. **A provisioner is not an object name.** External (non-CSI) provisioners are
   `domain/name`: `rancher.io/local-path`, `k8s.io/minikube-hostpath`,
   `cluster.local/nfs-subdir-external-provisioner`, `openebs.io/local`,
   `docker.io/hostpath`. All contain `/`, all fail the object-name regex, all
   are legitimate. Only CSI provisioners are bare DNS subdomains
   (`pd.csi.storage.gke.io`, `ebs.csi.aws.com`).
2. **Out-of-tree ≠ CSI.** External provisioners register no `CSIDriver` object
   either, so "not `kubernetes.io/*` ⇒ must have a CSIDriver" is false.
3. **In-tree ≠ no driver needed.** Under CSI migration (GA), EKS's default
   `gp2` class still says `kubernetes.io/aws-ebs` but provisioning is handed to
   `ebs.csi.aws.com`, a separately-installed addon. Exempting `kubernetes.io/*`
   skips the single most common real instance of the Pending-PVC failure.

Measured on the live GKE Autopilot dev cluster (2026-08-02): `standard` →
`kubernetes.io/gce-pd` (in-tree name, exempt); every other class → a
`*.csi.storage.gke.io` provisioner with a matching registered CSIDriver. So the
GKE mainline passes the guard; the gaps are EKS-legacy and bare-metal.

**Why:** issue #123 asked for exactly the in-tree-exempt shape, so the
implementation is faithful to the issue text while still mis-firing — a design
question, not just a coding slip.

**How to apply:** when a guard validates a string with a regex, check the
string's own grammar, not the grammar of the object it resembles. Prefer
`kubectl get <res> -- "$value"` for the leading-dash/argument-injection concern
over a narrowing regex. Related:
[[feedback_verify_branch_reachability_rationales]],
[[project_helm_null_and_replicas_gotchas]].
