# prod-prereq — Cluster-Scoped Prerequisites

This directory holds manifests a **cluster-admin applies before the prod Helm
release**, not manifests the release itself owns. The distinction is why they
live outside `dataspoke/`: a Helm release is namespace-scoped by convention —
`helm uninstall` only ever removes what it created, and a cluster-scoped
object created *by* the release would either leak on uninstall or, worse, get
deleted out from under a cluster that still needs it. Anything DataSpoke
*depends on* but does not *own* belongs here instead, applied once by
`kubectl apply -f`, and left in place across every install/uninstall cycle of
the umbrella chart.

See [`spec/feature/HELM_CHART.md` §Prod operator
workflow](../../spec/feature/HELM_CHART.md#prod-operator-workflow) for how
this fits into the full prod install sequence, including the pre-flight
check that enforces it.

## StorageClass — the first case

The umbrella chart's `postgresql`, `redis.master`, and `redis.replica`
components each provision a `PersistentVolumeClaim`, and so does every
Airflow component with `persistence.enabled: true` (`logs`, `dags`,
`triggerer`, `workers`/`workers.celery`, `redis`). If your operator overlay
pins a non-default class for any of them, that `StorageClass` must already
exist in the cluster before you run `install.sh`:

- Bitnami keys (spelled `storageClass`): `postgresql.primary.persistence.
  storageClass`, `redis.{master,replica}.persistence.storageClass`, or the
  `global.defaultStorageClass` / `global.storageClass` fallbacks the Bitnami
  subcharts also honour.
- Airflow keys (spelled `storageClassName` — **note the different key name**,
  a copy-paste trap when adapting a Bitnami-shaped snippet): `airflow.
  {logs,dags,triggerer,workers,workers.celery,redis}.persistence.
  storageClassName`.

`install.sh --profile prod` resolves all fifteen of those keys from your
`--values` overlay and fails the pre-flight fast on any name it cannot find
with `kubectl get storageclass`. An overlay that pins none of them skips the
check cleanly — the cluster's default `StorageClass` applies instead, and
there is nothing to pre-create.

Fifteen covers every PVC the shipped architecture provisions.
`postgresql.readReplicas.persistence`, `postgresql.backup.cronjob.storage`,
and `redis.sentinel.persistence` are deliberately out of scope — the chart
pins standalone PostgreSQL with no backup CronJob and Redis without Sentinel,
so none of them can render a PVC.

**The `-` sentinel is not honoured on every key.** A literal `-` is the
Bitnami/upstream convention for "skip dynamic provisioning, bind a
pre-provisioned PV" — the pre-flight accepts it without a lookup on the nine
Bitnami keys and on `airflow.{logs,dags}.persistence.storageClassName`,
because those templates map it to an empty `storageClassName`.
`airflow.{triggerer,workers,workers.celery,redis}.persistence.
storageClassName` pass the value straight through with no such mapping, so
the pre-flight rejects a `-` there instead of letting it reach Kubernetes as
a literal (and invalid) class name. Remove the `-` and name an explicit
class: there is no pre-provisioned-PV path on three of those four —
`triggerer`, `workers`, and `workers.celery` expose no `existingClaim`, and
`persistence.enabled: false` renders an `emptyDir` rather than binding a PV.
Only `airflow.redis.persistence` accepts an `existingClaim`.

**Why this matters more than a normal missing-resource error.** A missing
`StorageClass` does not fail cleanly at the PVC. It leaves every PVC that
names it `Pending` indefinitely, so the component behind it never starts —
on the PostgreSQL or Redis classes that means the API's `wait-for-postgres`
init container loops forever waiting for a database that will never come up;
on an Airflow persistence class it means that Airflow component alone hangs.
Either way the install dies on a rollout timeout whose symptom names the
stalled workload, not storage. Recovery then requires deleting
the stuck PVCs by hand, because `storageClassName` is immutable on a PVC once
it has bound — you cannot patch your way out of the wrong class after the
fact. The pre-flight check exists to turn that into an immediate, legible
error at the very start of the install instead.

**A class also needs its driver.** A `StorageClass` naming an out-of-tree CSI
provisioner whose driver is not installed passes the existence check and then
produces exactly the failure above — the PVC stays `Pending` because no driver
claims it. The pre-flight therefore reads the pinned class's `.provisioner`
and, wherever a `CSIDriver` could exist for it, looks one up:

```bash
kubectl get csidriver <provisioner>
```

What a missing driver costs depends on the provisioner's shape:

| Provisioner shape | Example | Driver not registered |
|---|---|---|
| Bare DNS-subdomain CSI driver name | `ebs.csi.aws.com`, `pd.csi.storage.gke.io` | **Aborts the install.** Install the driver (its own Helm chart or manifest bundle, per the vendor) before the release. |
| CSI-migrated in-tree name | `kubernetes.io/aws-ebs`, `kubernetes.io/gce-pd`, `kubernetes.io/azure-disk` | **Warns and proceeds.** Looked up under `ebs.csi.aws.com` / `pd.csi.storage.gke.io` / `disk.csi.azure.com`, because such a class commonly provisions through a separately installed addon (EKS's default `gp2` does). A cluster still running the in-tree plugin is equally legitimate, so this cannot be a hard gate — if yours is not one, install the addon or the PVC will stick `Pending`. |
| Any other in-tree name | `kubernetes.io/no-provisioner` (static/pre-provisioned volumes) | Not looked up. Compiled into kube-controller-manager; registers no `CSIDriver` object at all. |
| External non-CSI provisioner (`vendor/name`) | `rancher.io/local-path`, `openebs.io/local` | Not looked up — no `CSIDriver` will ever exist for it. Confirm its controller is actually running in-cluster yourself; a `StorageClass` object alone does not guarantee that. |

**The installer identity needs cluster-scoped read on CSIDrivers.** That lookup
is a read of a cluster-scoped resource, so whatever identity runs `install.sh`
needs `get` on `csidrivers.storage.k8s.io` on top of its namespace-scoped
rights — a separate grant from anything the Helm release itself requires, and
one an operator scoping the install identity to a single namespace will not
have given it. A `Forbidden` reply is reported distinctly from a missing
driver, so the pre-flight tells you to grant the read rather than to install a
driver that may already be there; it is fatal in the one row above that is
fatal on absence, and a warning in the others.

Example manifest for a cloud provider's dynamic provisioner (adjust
`provisioner` and `parameters` for your platform — this is illustrative, not
a manifest to apply as-is). These PVs hold the credential store (password
hashes, `api_tokens`/`password_reset_tokens`, Fernet-encrypted ingestion
secrets in PostgreSQL) and Redis's AOF (refresh-token revocation keys), so
the example below pins `reclaimPolicy: Retain` and enables platform disk
encryption rather than shipping the provisioner defaults:

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: dataspoke-ssd
provisioner: pd.csi.storage.gke.io   # GKE example; use your cluster's CSI driver
parameters:
  type: pd-ssd
  disk-encryption-kms-key: projects/<project>/locations/<region>/keyRings/<ring>/cryptoKeys/<key>
  # AWS EBS CSI equivalent: encrypted: "true", kmsKeyId: <key-arn>
volumeBindingMode: WaitForFirstConsumer
reclaimPolicy: Retain
```

`reclaimPolicy: Delete` (the provisioner default) destroys the underlying PV
— and the volume behind it — the moment its PVC is deleted; `helm uninstall`
never deletes a PVC, but a namespace delete does, so `Retain` is the
deliberate trade against losing this data to an operator mistake or a
cascading namespace teardown. A `Retain`-ed PV outlives its PVC and must be
reclaimed or deleted by hand.

Apply it once, then reference the class name in your operator overlay:

```yaml
postgresql:
  primary:
    persistence:
      storageClass: dataspoke-ssd
redis:
  master:
    persistence:
      storageClass: dataspoke-ssd
  replica:
    persistence:
      storageClass: dataspoke-ssd
```

## Adding a new prerequisite

Any future cluster-scoped dependency — a `ClusterRole` DataSpoke's
`ServiceAccount`s must bind to but not own, a `ValidatingWebhookConfiguration`,
a custom `PriorityClass` — follows the same convention: document it here,
apply it before the release, and (where practical) add a fail-fast check to
`install.sh`'s prod pre-flight rather than letting the failure surface deep
inside a rollout.
