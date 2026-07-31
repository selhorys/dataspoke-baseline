# registry-cleanup — Image Retention Policy

Retention policies for the container registry named by `DATASPOKE_KUBE_IMAGE_REGISTRY`. Like
[`prod-prereq/`](../prod-prereq/README.md), they are applied once by an operator and live outside
the Helm release: the registry is a dependency DataSpoke pushes to, not an object the umbrella
chart owns, so no install or uninstall cycle should touch it.

One file per registry vendor, because the retention mechanism is vendor-specific rather than a
DataSpoke abstraction — `bin/build-image.sh` dispatches on `DATASPOKE_KUBE_CLOUD_VENDOR` and pushes
to whichever registry that selects.

| File | Vendor | Mechanism |
|---|---|---|
| `gcp-artifact-registry.json` | `GCP` | Artifact Registry cleanup policies (`gcloud artifacts repositories set-cleanup-policies`) |
| — | `AWS` | ECR lifecycle policies (`aws ecr put-lifecycle-policy`); a different schema, not yet authored |

The rules below describe the Artifact Registry policy. An ECR equivalent expresses the same intent
— keep tagged images, keep a fixed number of recent versions, expire old untagged ones — through
`selection` rules with `countType` / `countNumber`.

Every `bin/build-image.sh` run pushes a new digest under a mutable tag (`dev`, or the operator's
`--image-tag` in prod). The previous digest keeps its storage but loses its tag, so an unmanaged
repository accumulates one orphaned version per build, indefinitely.

## The three rules

Rules are evaluated with Keep winning over Delete, so the two Keep rules bound what the Delete rule
can reach.

| Rule | Effect |
|---|---|
| `keep-tagged` | Never delete a tagged version — covers `dev` and every prod release tag |
| `keep-recent-untagged` | Always retain the 20 newest versions per image, whatever their tag state |
| `delete-stale-untagged` | Delete untagged versions older than 30 days |

## Why `keepCount` is the rule that matters

The installer pins each workload to a content digest (`<repository>@sha256:…`, see
[`spec/feature/HELM_CHART.md`](../../spec/feature/HELM_CHART.md) §Digest stamping). A pinned
Deployment therefore references a version by digest, not by tag — and that version is untagged the
moment the next build moves the tag forward.

The consequence is that `helm rollback` restores a pod template pinned to an older digest. If
retention has already collected that digest, the rollback renders an image reference that no longer
resolves and the pods land in `ImagePullBackOff`. `keepCount` is what buys rollback depth: at 20, a
repository retains roughly twenty builds of history per image regardless of age. Raise it where
build churn is high or where a longer rollback window matters more than storage.

## Applying it

```bash
# Attach in dry-run: the policy is evaluated and logged, nothing is deleted.
gcloud artifacts repositories set-cleanup-policies <repo> \
  --location=<region> --policy=gcp-artifact-registry.json --dry-run

# Inspect a cycle's worth of would-be deletions.
gcloud logging read \
  'resource.type="artifactregistry.googleapis.com/Repository"' --limit=50

# Enforce. Re-set the same policy with the flag negated — `repositories update`
# carries no dry-run flag of its own.
gcloud artifacts repositories set-cleanup-policies <repo> \
  --location=<region> --policy=gcp-artifact-registry.json --no-dry-run
```

`gcloud artifacts repositories describe <repo> --location=<region>` reports both the attached
policies and `cleanupPolicyDryRun`, which is the field that says whether deletion is live.

Deletion is not reversible: a collected version is gone, and rebuilding the same source does not
reproduce its digest. Attach in dry-run and read a cycle before enforcing.
