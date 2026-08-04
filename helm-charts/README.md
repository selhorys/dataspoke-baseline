# DataSpoke Helm Charts

Single deployment subsystem for DataSpoke — both production and development.
Scripts live in `helm-charts/bin/`, chart values in `helm-charts/dataspoke/`, and
dev-only peripheral manifests in `helm-charts/dev-peripherals/`.

## Prerequisites

- `kubectl` installed and configured
- `helm` v3 installed
- `python3` (Fernet key generation, values parsing — required by every profile of `bin/install.sh` and by the prod path of `bin/uninstall.sh`)
- A Kubernetes cluster with **8+ CPUs / 24 GB RAM** (dev) or, for prod, node capacity for the umbrella chart's steady-state resource **requests** — **~4.35 CPU / 7.7 GiB RAM** across all replicas, per `spec/feature/HELM_CHART.md §Resource Sizing`'s Total row (limits burst to ~10.15 CPU / 18.1 GiB; add 66 GiB persistent volume across Postgres and Redis) — see [§Prod profile](#prod-profile), step 1, below for the per-component breakdown

## Quick Start

### Configure

```bash
cp helm-charts/.env.dev.example helm-charts/.env.dev
# Edit helm-charts/.env.dev — set DATASPOKE_KUBE_CLUSTER, DATASPOKE_KUBE_IMAGE_REGISTRY, etc.
```

### Dev profile (full install)

```bash
./helm-charts/bin/install.sh --profile dev
# Defaults to helm-charts/.env.dev; override with --env-file <path>
```

The `--frontend` flag controls how the Next.js UI is handled:

| Flag | Behavior |
|------|----------|
| `--frontend none` | Do not deploy the frontend (dev default). |
| `--frontend local` | Write `src/frontend/.env.local` pointing at the in-cluster API, then run `pnpm dev` on the host. |
| `--frontend cluster` | Build the frontend image and deploy it in-cluster. |

`--frontend local` and `--frontend cluster` are dev-only modes. In prod the default is `--frontend cluster` (always deployed).

### Health check

```bash
./helm-charts/bin/health-check.sh --profile dev
./helm-charts/bin/health-check.sh --profile prod --quick
```

`--profile {dev|prod}` resolves `helm-charts/.env.<profile>` by the same rule
`install.sh` uses; `--env-file <path>` overrides it. Either way the script
echoes the resolved env file and ingress domain before its first probe, so a
confident verdict is never read off a deployment other than the one you meant.
A bare invocation still falls back to `.env.dev`. Other flags: `--quick`
(TCP-only), `--keep-lock`, `--force-release`.

**Under `--profile prod`, read the `DataSpoke Infra` section and nothing else,
and expect a non-zero exit.** The `DataHub`, `Dummy Data` and `Dev
Coordination` sections probe dev-only peripherals the prod profile never
installs, and they run unconditionally — against a healthy prod deployment they
report unhealthy, so the summary ends with `N service(s) unhealthy`, a
`Reinstall hint:` naming the dev-only `install.sh --profile dev --components
<name>`, and exit 1. Neither the count nor the hint is a verdict on your
deployment.

Prefer `--quick` on prod: the deep (non-`--quick`) Langfuse probe reads
`DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE`, which a prod env file does not carry,
and the script runs under `set -u`, so a full prod run stops there — after the
DataSpoke Infra lines it has already printed, and before the summary.

### Uninstall

```bash
./helm-charts/bin/uninstall.sh --profile dev                       # Full teardown
./helm-charts/bin/uninstall.sh --profile dev --components frontend  # Remove only the frontend (helm upgrade frontend.enabled=false)
```

### Prod profile

Prod is a two-command sequence: `bin/install-prod-preflight.sh` validates and
populates, then `bin/install.sh --profile prod` mutates the release — see
[§The short path](#the-short-path-fill-in-the-env-file-run-the-pre-flight) below.
`install.sh` itself then runs three phases — its own pre-flight, image build,
umbrella chart —
followed by an automatic post-install step: it seeds the default admin user
(`dataspoke@dataspoke.local` / `dataspoke`) unless `--skip-seed` is passed.
`SKIP_SEED` defaults to `false`, so **this runs on every prod install unless
you explicitly opt out**. (The `--components seed` gate you may have seen
elsewhere is dev-only — it does not guard the prod seed step.)

That default credential is published in this repository, so a login as
`dataspoke@dataspoke.local` / `dataspoke` succeeds against your API the
moment install returns, for anyone who can reach it. How reachable that is
depends entirely on your ingress controller and cluster network posture —
DataSpoke's chart applies no source-range or auth gating on the API ingress,
and the prod profile does not install or configure an ingress controller
(`DATASPOKE_KUBE_INGRESS_MODE=shared` by default — you bring your own).
The same seed rotates that account to `DATASPOKE_PROD_ADMIN_PASSWORD` when
you set it in the env file, closing the window inside the install itself; with
that line blank the published credential stays live and §5 is a required manual
step. Treat it as urgent if your controller is shared or internet-reachable.
Follow this runbook in order.

#### The short path: fill in the env file, run the pre-flight

Prod is a **two-command sequence**, and every *variable* you supply lives in one
file. The values overlay (§3) is the other operator-authored input, and it has
to exist before the pre-flight runs: the pre-flight validates it, and takes the
Secret name it verifies from the overlay's `secrets.existingSecret`. So write
the overlay first — `helm-charts/values-prod.example.yaml` is the starting
point — then:

```bash
cp helm-charts/.env.prod.example helm-charts/.env.prod
# Edit helm-charts/.env.prod: the deployment shape, the Google OAuth client
# secret, and whichever other DATASPOKE_PROD_* inputs are yours to know.
# LEAVE THE CREDENTIAL LINES BLANK — a blank line is a request for the
# pre-flight to resolve that key, not an omission.

./helm-charts/bin/install-prod-preflight.sh --values /path/to/your-overlay.yaml
```

That single command performs the validation and the Secret creation §2 below
describes — it checks the env file, the values overlay and the cluster
prerequisites, resolves the eleven credentials into the env file, creates the
credentials Secret, and derives an image tag from `git HEAD` — then prints the
exact §4 install command, tag included, for you to paste. Add
`--create-namespace` on a first install, since the credentials Secret is
namespace-scoped and is created before `install.sh` ever runs.

Two of the numbered steps are **inputs it consumes rather than work it does**.
It validates §1's cluster prerequisites without providing them: the
IngressClass, the StorageClasses your overlay pins and their CSI drivers, DNS,
registry auth and node capacity are still yours to have in place. And it
validates the §3 overlay without producing it — the overlay is an input, so §3
is a step you complete before this command rather than one it replaces. (With
no overlay to read, stage 2 judges the chart defaults instead, and those
publish the whole API surface, so it stops there.) The pre-flight's contribution
is telling you which piece is missing before a release is mutated rather than
after.

```bash
./helm-charts/bin/install-prod-preflight.sh --values /path/to/your-overlay.yaml --verify-only
```

`--verify-only` is a read-only audit: it runs every check and performs none of
the script's three mutations (writing the env file, creating the namespace,
creating the Secret), which makes it safe to run against a live deployment.

The pre-flight applies its gates through `bin/lib/helpers.sh`, the same
functions `install.sh`'s own prod pre-flight calls on the same inputs, **so a
pass here means `install.sh`'s pre-flight will pass**. That shared-gate
invariant is what makes the split into two commands worth the extra step rather
than a second place for the rules to drift. It is also non-interactive — no
prompt, and no credential in any flag — so the identical command works in a
terminal and in CI.

Its flags:

| Flag | Effect |
|------|--------|
| `--env-file <path>` | Operator env file (default `helm-charts/.env.prod`) |
| `--values <path>` | Values overlay; single-use, like `install.sh`'s. Defaults to `helm-charts/values-prod.yaml` when that file exists, and warns loudly when there is no overlay at all |
| `--namespace <ns>` | Inspect a namespace other than `DATASPOKE_KUBE_DATASPOKE_NAMESPACE`. `install.sh` has no counterpart, so never use it to produce the install command |
| `--secret-name <name>` | Inspect a Secret other than the overlay's `secrets.existingSecret`. Same caveat |
| `--image-tag <tag>` / `--allow-dirty` | Use an explicit tag instead of `git rev-parse --short HEAD`, or accept a dirty work tree for the derived one |
| `--create-namespace` | Create the namespace when absent (the Secret is namespace-scoped, so it must exist by stage 5) |
| `--skip-secret` | Do not create the Secret — for operators delivering the eleven keys via ExternalSecrets, Vault or SealedSecrets. Every other stage still runs, and blank credential lines are left alone rather than resolved: minting eleven values the deployment will never use would put fresh prod credentials on disk for nothing, and would make stage 5 report all eleven as drift the moment the external system materialises the Secret |
| `--skip-postinstall-check` | Do not require the `DATASPOKE_PROD_PERIPHERAL_*` / `DATASPOKE_PROD_LLM_*` blocks to be complete (see §7) |
| `--verify-only` | Read-only audit, as above |

The numbered steps below remain valid and stay authoritative for *what* is
checked. Work through them if you prefer the manual route, if you deliver the
credentials through a secrets manager, or when you need to know exactly what
the script produces.

#### 1. Prerequisites

- Cluster-scoped prerequisites the Helm release cannot own itself — at
  minimum, any non-default `StorageClass` your operator overlay pins for
  Postgres, Redis, or Airflow persistence. Apply these first; see
  [`helm-charts/prod-prereq/`](prod-prereq/) for what belongs here and why,
  and the exact overlay keys the pre-flight checks.
- An IngressClass already installed in the cluster, named by
  `DATASPOKE_KUBE_INGRESS_CLASS` — **required in prod, with no default**,
  because a default would silently republish the API, frontend, and Airflow UI
  on any class that happens to be named `nginx`, often another team's
  internet-facing controller. The preflight checks the class exists and fails
  fast if absent (existence proves the class is real, not that it is the one
  you meant), and every Ingress the install creates (API, frontend, Airflow)
  binds to it. The install passes it by `--set`, which
  outranks the `className` in a `--values` overlay, so the env var is the one
  place to change it. The name does not identify the controller behind it —
  read that with
  `kubectl get ingressclass <class> -o jsonpath='{.spec.controller}'`.
  `k8s.io/ingress-nginx` is the community controller and honours the
  `nginx.ingress.kubernetes.io/*` annotations; `nginx.org/ingress-controller`
  is NGINX Inc./F5 and honours `nginx.org/*` instead. DataSpoke's ingresses
  carry the 50 MB body-size limit in both spellings, so it holds on either
  controller; the HTTPS-redirect annotation is community-spelled only, so on
  an NGINX Inc. controller a TLS-terminating host redirects on that
  controller's own default instead. Any annotation you add in an overlay must
  be spelled for the controller you actually run.
- DNS (or your own resolution mechanism) pointing the `app.`, `api.`, and
  `airflow.` subdomains of your chosen domain at that ingress controller.
- Registry auth for pulling the built images — either a public registry or an
  `imagePullSecret`. Today only the API workload threads one
  (`api.imagePullSecrets`, see `values-prod.example.yaml`); Postgres and
  Airflow pull from the same registry but take their pull secret via their
  own subchart-native keys (`postgresql.global.imagePullSecrets`,
  `airflow.imagePullSecrets`) — set those too if your registry requires auth.
  The frontend subchart does not support a pull secret at all yet, so a
  private-registry operator running `--frontend cluster` will see
  `ImagePullBackOff` on that one workload regardless.
- Cluster capacity for the prod resource budget in `dataspoke/values.yaml` — 2
  API + 2 frontend replicas (API: 500m/1 CPU, 512Mi/1024Mi mem each; frontend:
  250m/500m CPU, 256Mi/512Mi mem each), Postgres 1-2 CPU / 2-6Gi mem, Redis
  master **and** replica sized identically at 250m/500m CPU, 256Mi/512Mi mem
  each (a smaller replica OOMKills under a full resync while the master stays
  healthy — see `spec/feature/HELM_CHART.md §Redis memory policy`), and
  Airflow's five components (api-server, scheduler, triggerer, dag-processor,
  statsd) plus the transient `db-migrate` hook Job. Total steady-state
  request/limit: **4350m/10150m CPU, 7.7Gi/18.1Gi memory, 66Gi PV** (excludes
  event-consumer, disabled by default, and the `db-migrate` hook) — see
  `spec/feature/HELM_CHART.md §Resource Sizing → Production defaults` for the
  full per-component table. Size nodes accordingly.
- `config.trustedProxyIps` (chart default: `"127.0.0.1"` — loopback only, no
  proxy trusted). The auth rate limiter (`POST /auth/token` 10/min, `POST
  /auth/register` 5/min — the only brute-force control, there is no account
  lockout) keys on the request's client address; with the loopback-only
  default every request arriving through your ingress controller shares one
  bucket. To get real per-client limits, set it to your ingress controller's
  pod CIDR in your `--values <overlay.yaml>`, e.g.
  `trustedProxyIps: "127.0.0.1,10.4.0.0/14"` — see the comment on
  `dataspoke/values.yaml`'s `config.trustedProxyIps` for why this must name
  the controller's actual pod CIDR and never widen to `"*"` or the full
  RFC1918 space. Widening it also flips `scope["scheme"]` from the trusted
  hop's `X-Forwarded-Proto`, which changes the Google OAuth `redirect_uri`
  your API generates from `http://` to `https://` — re-verify the redirect
  URI registered in the Google Cloud Console still matches before rolling
  this out.
- `config.rateLimitPerMinute` (chart default: `120`) — max requests per
  minute per rate-limit key (the same JWT-sub-or-client-IP key `trustedProxyIps`
  above attributes). The chart render fails fast on any value that is not a
  positive integer, so a bad overlay is caught by `helm template`/`install.sh`
  rather than reaching a pod.
- A CLI that can resolve an image digest, on the PATH of whatever host runs
  `install.sh`, matching what `DATASPOKE_KUBE_CLOUD_VENDOR` names: an
  authenticated `gcloud` for `GCP`, an authenticated `aws` for `AWS`, and
  `docker` when the variable is empty or names anything else — that branch
  reads the local daemon's recorded digests, so the host must also hold the
  image it is deploying. This applies to a deploy-only CI host invoked with
  `--skip-build` against images a separate build stage already pushed, which
  on the empty-vendor branch means such a host cannot resolve at all and must
  pass `--no-digest-pin`. `install.sh` uses it to resolve each
  in-scope workload's image digest before rendering the chart, so a rebuild
  pushed to the same mutable tag still rolls the workload (see
  `spec/feature/HELM_CHART.md §Digest stamping`). Pass `--no-digest-pin` to
  skip this requirement entirely — see step 4 below for what that trades
  away.

**The namespace needs no pre-creating — with one exception.** `install.sh`'s
prod pre-flight calls `ensure_namespace` ahead of every check that touches the
cluster's contents (the `--image-tag` refusal and context selection run first), so
`DATASPOKE_KUBE_DATASPOKE_NAMESPACE` is created automatically if it does not
already exist, and no separate operator step is required for it. The
credentials Secret in step 2 below is namespace-scoped, though, and is
created *before* `install.sh` ever runs — so the namespace has to exist by the
time the pre-flight reaches its stage 5. `install-prod-preflight.sh
--create-namespace` covers that without a separate operator step; an operator
building the Secret by hand creates the namespace themselves first. Only the
ordering is at stake: `ensure_namespace` is idempotent and adopts a namespace
the operator (or the pre-flight) already made.

#### 2. Create the namespace and the 11-key credential Secret

**Short path — the pre-flight does both:**

```bash
./helm-charts/bin/install-prod-preflight.sh \
  --values /path/to/your-overlay.yaml \
  --create-namespace
```

It resolves each of the eleven keys in a fixed order and writes the result back
into `helm-charts/.env.prod`:

1. **your own value**, when the `DATASPOKE_PROD_<X>` line is non-empty;
2. otherwise **the value this cluster's Secret is already using** (adopt);
3. otherwise a **freshly generated** value.

**Adoption precedes generation**, and the reason is the Fernet key ↔ Postgres
PVC coupling described in
[§Prod: what survives an uninstall](#prod-what-survives-an-uninstall).
`DATASPOKE_AIRFLOW_FERNET_KEY` encrypts Airflow's stored connections and
Variables inside a PVC that survives `helm uninstall`; a key regenerated while
that PVC survives leaves everything it encrypted permanently undecryptable, and
Airflow reports it at decrypt time rather than at install time. Resolving from
the running deployment first is what stops a re-install contradicting it. Only
`DATASPOKE_PROD_GOOGLE_OAUTH_CLIENT_SECRET` has no generator — the Google Cloud
Console issues it — so a blank line there stops the run.

The pre-flight reports **provenance per key** (`from the env file` / `adopted
from the cluster` / `generated`) and never a value; on an existing Secret it
verifies and never rewrites, reporting drift by key name.

**The walkthrough below is the manual equivalent**, and the Secret's content
contract is identical either way — same eleven keys, same rejection rules,
verified by the same `verify_credential_secret` in `bin/lib/helpers.sh` whether
the Secret was created by the script, by these commands, or by your secrets
manager. That is what keeps an ExternalSecrets/Vault/SealedSecrets operator
running `install-prod-preflight.sh --skip-secret` on exactly the same validated
path as everyone else, rather than guessing at what the release expects. Read
on if you are that operator, or if you need to know precisely what the script
produces.

Of the 11 required keys, only **one** — `DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET`
— must come from outside this shell (the Google Cloud Console OAuth client).
9 of the remaining 10 are generated as high-entropy secrets by the blocks
below; the tenth, `DATASPOKE_AIRFLOW_USER`, is a literal with a short random
suffix (not a secret). Nothing else needs sourcing for **this Secret** — §3
below still needs the same console's OAuth **client ID**, a separate,
non-secret value that goes in your operator overlay instead, not here.

For a real deployment, deliver these 11 keys via ExternalSecrets, Vault, or
SealedSecrets rather than plain `kubectl`. The blocks below are only the
floor for a one-off bootstrap: run them from a private/short-lived shell.
The `kubectl create secret` step uses `--from-env-file` rather than
`--from-literal=`, which would land every credential in your shell history
and in `ps auxww` / `/proc/<pid>/cmdline` for the process's lifetime,
visible to any co-tenant on the same bastion.

Every generated value uses `openssl rand -hex 32` (64 lowercase hex
characters, 256 bits of entropy) — a uniform class that is trivial to audit
and needs no escaping in a shell heredoc, an env-file, or a `kubectl`
argument. It is not what makes a password DSN-safe, though: `install.sh`
already percent-encodes any password before it reaches a connection-string
URI (`_url_encode`, used by `_derive_airflow_metadata_secret` for Airflow's
metadata-DB connection and by the API's rate limiter for its Redis URI), and
the app's own DB session carries credentials as discrete `sqlalchemy.URL`
fields rather than a DSN string in the first place (see
`src/shared/db/session.py`) — no path in this repo corrupts a non-hex
password. `DATASPOKE_AIRFLOW_FERNET_KEY` needs a different shape regardless
of any of that: Fernet requires URL-safe base64 of exactly 32 raw bytes, and
hex-encoded 32 bytes (48 raw bytes once decoded) passes pod startup but
fails Airflow's own decrypt at first use, long after install reports
success.

```bash
NS="<your-namespace>"
kubectl create namespace "$NS"
```

Check next whether this is a genuinely fresh install — the Fernet key
generated below must be replaced with the one already live on this cluster
if it is not:

```bash
kubectl get pvc data-dataspoke-postgresql-0 -n "$NS"
```

A hit means Airflow already ran against a retained metadata DB in this
namespace; generating a new `DATASPOKE_AIRFLOW_FERNET_KEY` instead of
supplying the one that DB was encrypted with leaves its stored connections
and Variables permanently undecryptable. See
[§Prod: what survives an uninstall](#prod-what-survives-an-uninstall) for
whether the key is still recoverable at all — a full uninstall/reinstall
cycle can delete the only remaining copy.

Read the one external value with your terminal's own echo-suppressing
input — not `export`, which would land it in shell history the same way
`--from-literal=` would, and not `read -p`, which is not portable (a
bracketed-paste-unaware terminal on bash < 5.1 can hand `read` the *next*
pasted line instead of console input, and zsh has no `-p` flag at all — it
exits 0 having read nothing, so the failure is silent):

```bash
printf 'Google OAuth client secret (from the Google Cloud Console OAuth client): '
read -rs DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET
echo
```

```bash
# mktemp, not a fixed /tmp path — a co-tenant on the same host cannot
# pre-create this filename with permissive permissions the way they could a
# guessable one, and mktemp's own default mode (0600) keeps it private from
# the first byte written, with no window where a later chmod would still be
# racing a reader. If you stop here (e.g. to substitute a recovered Fernet
# key per the PVC check above), remove the file by hand: rm "$SECRETS_ENV".
SECRETS_ENV="$(mktemp)"
cat > "$SECRETS_ENV" <<EOF
DATASPOKE_POSTGRES_PASSWORD=$(openssl rand -hex 32)
DATASPOKE_REDIS_PASSWORD=$(openssl rand -hex 32)
DATASPOKE_AIRFLOW_USER=dataspoke-admin-$(openssl rand -hex 4)
DATASPOKE_AIRFLOW_PASSWORD=$(openssl rand -hex 32)
DATASPOKE_INTERNAL_TOKEN=$(openssl rand -hex 32)
DATASPOKE_JWT_SECRET_KEY=$(openssl rand -hex 32)
DATASPOKE_AIRFLOW_WEBSERVER_SECRET_KEY=$(openssl rand -hex 32)
DATASPOKE_AIRFLOW_JWT_SECRET=$(openssl rand -hex 32)
DATASPOKE_AIRFLOW_FERNET_KEY=$(python3 -c "import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())")
DATASPOKE_OAUTH_STATE_SECRET=$(openssl rand -hex 32)
DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET=${DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET}
EOF
```

`DATASPOKE_AIRFLOW_USER`'s random suffix (unlike the published dev-install
literal `dataspoke-admin`) keeps it from being a fixed, guessable target —
it is not itself a secret, and the preflight only requires it not equal
`admin` (see the pre-flight table below). If you hand-edit this or the
Fernet line above, write the bare value: no surrounding quotes, no trailing
spaces. `kubectl`'s `--from-env-file` parser strips neither, so a quoted or
padded value is stored verbatim, and only the Fernet line's fixed length
would ever catch it.

```bash
kubectl create secret generic dataspoke-secrets-prod \
  --from-env-file="$SECRETS_ENV" \
  -n "$NS" \
  && rm "$SECRETS_ENV"
unset DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET
```

Key classification — the pre-flight table below rejects only the one
known-bad literal per key, not weak values in general; entropy and
uniqueness beyond that are on you:

| Key | Type | How the blocks above set it |
|-----|------|-------------------------------|
| `DATASPOKE_POSTGRES_PASSWORD`, `DATASPOKE_REDIS_PASSWORD`, `DATASPOKE_AIRFLOW_PASSWORD`, `DATASPOKE_JWT_SECRET_KEY`, `DATASPOKE_OAUTH_STATE_SECRET`, `DATASPOKE_INTERNAL_TOKEN`, `DATASPOKE_AIRFLOW_WEBSERVER_SECRET_KEY`, `DATASPOKE_AIRFLOW_JWT_SECRET` | high-entropy random | `openssl rand -hex 32` |
| `DATASPOKE_AIRFLOW_FERNET_KEY` | high-entropy random, fixed shape | URL-safe base64 of 32 raw bytes — Fernet rejects hex |
| `DATASPOKE_AIRFLOW_USER` | randomized literal, not a secret | `dataspoke-admin-<8 hex chars>`; see the pre-flight table below for the two rejection rules |
| `DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET` | externally issued | the one value you must supply — read via the prompt above |

**`DATASPOKE_AIRFLOW_PASSWORD` is genuinely consulted, under this chart's
default.** Airflow's built-in `SimpleAuthManager` checks it — alongside
`DATASPOKE_AIRFLOW_USER` — at every login, because
`airflow.config.core.simple_auth_manager_all_admins` defaults to `"False"`.
This key's presence is required unconditionally (like every other key in
this Secret) — the prod-only init container that materialises the
passwords file renders regardless of `all_admins`, and a missing key crashes
api-server startup rather than being skipped — but the pre-flight's rejection
of the literal `admin` value is conditional: it only fires when the
effective `simple_auth_manager_all_admins` is unset or a false-ish value
(this chart's default). An operator overlay that sets
`simple_auth_manager_all_admins` to a true-ish value (`t`/`true`/`1`,
case-insensitively — Airflow's own boolean parser, which this pre-flight
mirrors) instead makes both `DATASPOKE_AIRFLOW_{USER,PASSWORD}` dead weight
at login: `GET /auth/token` issues an anonymous ADMIN JWT to any caller with
no credential at all, and `GET /auth/token/login` sets that JWT as an
httponly cookie and redirects into the Airflow UI — reachability alone
grants Airflow admin. The chart ships no source-range restriction of its
own, so that overlay is only safe when the operator restricts
`airflow.<domain>` at the network layer instead. The pre-flight cannot fail
a deliberate operator choice, so it only warns in that case, naming the
exposure. Airflow's own `SimpleAuthManager` docstring states it "should not
be used in production" — under either posture it has no session revocation,
no password policy, no lockout, and (under the default) one file-backed
account; an operator wanting a real production IdP fronts the Airflow host
with an authenticating proxy, or points `airflow.config.core.auth_manager`
at a manager backed by their own IdP. See `spec/feature/HELM_CHART.md`
§Airflow authentication.

**The username and password arrive as environment variables, which outrank
`airflow.cfg`.** `install.sh` injects
`AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_{USERS,PASSWORDS_FILE}` directly as pod
env vars (Airflow's own configuration precedence places these above the
rendered `airflow.cfg`), so an overlay that instead sets
`airflow.config.core.simple_auth_manager_users` has that value silently
ignored in favor of the env var this chart already sets from
`DATASPOKE_AIRFLOW_USER`.

`DATASPOKE_POSTGRES_USER` and `DATASPOKE_POSTGRES_DB` are **not** in this
Secret. Both are non-secret and live in the app ConfigMap instead
(`config.postgres.user`/`config.postgres.db` chart values, default
`"dataspoke"`/`"dataspoke"`) — see `helm-charts/dataspoke/values.yaml`'s
`config.postgres` comment. The role (`DATASPOKE_POSTGRES_USER` /
`config.postgres.user`) is chart-pinned: it also appears as a bare literal in
the bundled subchart's `initdb` `GRANT`/`OWNER` statements
(`primary.initdb.scripts` in both `dataspoke/values.yaml` and
`dataspoke/values-dev.yaml`) and in `install.sh`'s
`_derive_airflow_metadata_secret` — changing it is unsupported. The database
(`DATASPOKE_POSTGRES_DB` / `config.postgres.db`) has no such third site:
`initdb` runs against `postgresql.auth.database` as its default connection
and never names it as a literal, so `config.postgres.db` plus
`postgresql.auth.database` is a clean, fully-guarded two-value pair you may
change together.

**A credentials Secret carrying `DATASPOKE_POSTGRES_USER` or
`DATASPOKE_POSTGRES_DB` fails the pre-flight** rather than being silently
ignored — remove either key before running `install.sh`:

```bash
kubectl patch secret dataspoke-secrets-prod -n <your-namespace> --type=merge \
  -p='{"data":{"DATASPOKE_POSTGRES_USER":null,"DATASPOKE_POSTGRES_DB":null}}'
```

**Rotating `DATASPOKE_POSTGRES_PASSWORD` needs a manual `ALTER ROLE` too.**
Bitnami's PostgreSQL image only sets the role's password from
`DATASPOKE_POSTGRES_PASSWORD` at first bootstrap (`initdb`) — writing a new
value into the credentials Secret and re-running `install.sh` re-derives
`dataspoke-airflow-metadata-db` with the new password and restarts the
Airflow pods that hold it (see §Secrets Management in
`spec/feature/HELM_CHART.md`), but the running PostgreSQL role's own
password is untouched. Without also running `ALTER ROLE dataspoke WITH
PASSWORD '<new-password>';` against the live database, the API's
alembic-migrate init container and every rotated consumer fail
authentication against the old password on the very next rollout.

The preflight (`verify_credential_secret` in `bin/lib/helpers.sh`, plus the
IngressClass / `assert_pinned_storage_classes` (StorageClass and its CSI
driver) / Secret-existence / `--image-tag` checks around it — all shared
functions, so `install-prod-preflight.sh` and `install.sh --profile prod` apply
the identical rules to the identical inputs) fails fast — before the chart is
installed, and before any
credential-derived Secret (e.g. `dataspoke-airflow-metadata-encryption-key`)
is created or modified — on any of (note the namespace itself may already
exist by this point: `ensure_namespace` runs ahead of these checks, per
[§1. Prerequisites](#1-prerequisites) above):

| Check | Rejected when |
|-------|---------------|
| IngressClass | not found in the cluster |
| StorageClass | any class pinned in the `--values` overlay is not found in the cluster (see [§1. Prerequisites](#1-prerequisites) and `helm-charts/prod-prereq/README.md`), or a literal `-` is pinned on an Airflow key that does not honour it |
| CSIDriver | a pinned StorageClass names a **bare** out-of-tree CSI provisioner (e.g. `ebs.csi.aws.com`) with no matching `CSIDriver` registered — a CSI-migrated in-tree name or an external non-CSI (`vendor/name`) provisioner only warns instead; see `helm-charts/prod-prereq/README.md` §StorageClass for the full provisioner-shape table |
| CSIDriver RBAC | the install identity lacks `get` on `csidrivers.storage.k8s.io` (a `Forbidden` reply) — fatal/warn split by the same provisioner shape as the row above; see the same section |
| Secret | missing entirely |
| All 11 keys | any of the 11 keys is absent, **including** `DATASPOKE_AIRFLOW_PASSWORD` — its presence is unconditional; only the separate `admin`-literal rejection below is conditional |
| `DATASPOKE_POSTGRES_USER` / `DATASPOKE_POSTGRES_DB` | either key is still present in the Secret — both moved to the app ConfigMap (`config.postgres.*`); see the migration note above |
| `DATASPOKE_JWT_SECRET_KEY` | equals the dev default `changeme-dev-secret-do-not-use-in-prod` |
| `DATASPOKE_AIRFLOW_USER` | equals `admin`, **or** does not match the allowlist `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` (the same convention as `SECRET_TO_CHECK`, namespaces, StorageClass names, and `--image-tag`). This username is composed into `airflow.extraEnv`, which the vendored Airflow chart renders through Go template `tpl` — an allowlist, not a denylist of `,`/`:`, because a denylist alone does not close a template-injection sink (a Go template escape, a YAML string escape, or a trailing newline all reach the same mis-parse by a different route) |
| `DATASPOKE_AIRFLOW_PASSWORD` | equals `admin` — **only when the effective `airflow.config.core.simple_auth_manager_all_admins` (chart default merged with your `--values` overlay, normalized the same way Airflow's own boolean parser does) is unset or a false-ish value** (this chart's default). An overlay value of `t`/`true`/`1` (case-insensitively) downgrades this to a warning instead — the credential pair is not consulted at all under that overlay; see the callout above. (Presence/non-emptiness is unconditional — see "All 11 keys" above.) |
| `DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET` | starts with `placeholder-` |
| `DATASPOKE_AIRFLOW_FERNET_KEY` shape | not URL-safe base64 of exactly 32 raw bytes (43 chars + `=`) — catches an `openssl rand -hex 32` value pasted in by mistake |
| `--image-tag` | not passed explicitly (prod refuses the mutable `:dev` tag) |

Note preflight validates known-bad literals and the Fernet key's shape only —
it does not measure entropy, so e.g. `DATASPOKE_JWT_SECRET_KEY=x` passes, and
a correctly-shaped but low-entropy Fernet key passes too. The shape check
only rejects a wrong-length value; it does not (and cannot) verify the key
actually decrypts an existing Postgres PVC's Airflow connections/Variables —
see the missing-key error's own warning against generating a new key when
one is retained.

Beyond the Secret checks above, a prod install also fails fast on
image-digest resolution (`_resolve_digest_or_abort` in `bin/install.sh`) for
the DataSpoke-owned workloads actually in scope — **api** (event-consumer
shares the api image and therefore the api digest — it has no resolution
step of its own), **frontend** (only when `--frontend cluster`) — before the
umbrella `helm upgrade` ever runs, using the vendor CLI required in [§1.
Prerequisites](#1-prerequisites) above. If that CLI is missing, or the
registry lookup itself fails (auth, network), the install aborts naming the
image reference it could not resolve — it does not silently fall back to
deploying the mutable tag. See step 4 below for `--no-digest-pin`, the
explicit escape hatch.

This walkthrough already uses a custom Secret name
(`dataspoke-secrets-prod`) via `secrets.existingSecret` in the overlay (§3) —
the same 11 keys and rejection rules apply to whatever Secret that name
resolves to. Set it to `dataspoke-secrets` instead (or omit
`secrets.existingSecret` from your overlay) to use the chart default name.

A disagreement between `DATASPOKE_AIRFLOW_FERNET_KEY` in this Secret and the
key already projected into `dataspoke-airflow-metadata-encryption-key` on a
live cluster aborts the install rather than re-projecting, and the aborting
install prints the recovery command — see
[§Prod: what survives an uninstall](#prod-what-survives-an-uninstall) for why
the guard exists and the one cycle it does not cover.

#### 3. Author your operator overlay

Copy `helm-charts/values-prod.example.yaml`, edit the placeholders (ingress
hosts, TLS secret names, CORS origins, OAuth post-login redirect, Google
client ID, `secrets.existingSecret`), and read its header comment on Helm's
map-merge / list-replace semantics before touching the
`cert-manager.io/cluster-issuer` annotation — the single biggest footgun for
operators not running cert-manager. The API ingress host is your own choice —
nothing in the install depends on it matching a fixed pattern, except
`bin/health-check.sh`: run as `--profile prod` it builds every probe URL from
`DATASPOKE_KUBE_INGRESS_DOMAIN` in `.env.prod` and assumes
`api.<DATASPOKE_KUBE_INGRESS_DOMAIN>` regardless of what your prod overlay
sets. The example overlay publishes the public API surface — `/api/v1`,
`/health`, `/ready`, `/redoc`, `/openapi.json` — and never `/internal/*`; a
prod pre-flight check in `install.sh` refuses to install if any configured
path would admit `/internal/*`. `/redoc` and `/openapi.json` disclose nothing
beyond the already-public surface (the internal routers are excluded from the
schema), but the frontend's "API docs" nav link points at
`${apiBaseUrl}/redoc` — removing either path is a valid hardening step, at
the cost of that link 404ing.

**The same list-replace semantics apply to `airflow.apiServer.{extraInitContainers,
extraVolumes,extraVolumeMounts}` — and here they can silently disable this
chart's own security fix.** `install.sh` injects the Airflow SimpleAuthManager
passwords-file mechanism (an init container, its memory-backed `emptyDir`,
and the matching volume mount — see the `DATASPOKE_AIRFLOW_PASSWORD` callout
above) onto exactly these three fields, on its own `-f` layer ahead of your
overlay. If your overlay also sets any of the three — to add an unrelated
sidecar or debug volume, say — Helm replaces the whole list rather than
merging it, deleting the passwords-file mechanism with **no error** from
either `helm template` or `helm lint` (both still exit 0 against the
resulting pod template). `install.sh` now refuses to install in that case
(a prod-only pre-flight check), so the fix is: re-declare the
`simple-auth-manager-passwords` init container/volume/volumeMount alongside
whatever else your overlay adds to these same three lists, rather than
setting them independently. See `spec/feature/HELM_CHART.md`
§Airflow authentication for the exact shape those three entries need.

#### 4. Install

**Run the command the pre-flight printed**, image tag included — it ends with
an `Install with:` line carrying the overlay path and the tag it resolved from
`git rev-parse --short HEAD`. Do not retype the tag from memory: the pre-flight
refuses a dirty work tree for the derived tag precisely so the tag names a
commit that contains what is being built, and `install.sh` requires an explicit
`--image-tag` in prod so a shared registry never receives the mutable `:dev`.

`--values` is single-use — unlike helm's repeatable `-f`, it takes exactly
one overlay file and the script errors if you pass it twice. Merge multiple
overlays into one file first.

```bash
./helm-charts/bin/install.sh --profile prod \
  --values /path/to/your-overlay.yaml \
  --image-tag <tag from the pre-flight>
```

`--env-file` is not passed: `--profile prod` resolves `helm-charts/.env.prod`
on its own, and naming it explicitly is one more place a second file can be
named by mistake. (The pre-flight adds `--env-file` to the command it prints
only when you ran it against a non-default env file.)

This resolves each in-scope workload's image digest before rendering the
chart (see [§1. Prerequisites](#1-prerequisites) above) — `gcloud`/`aws`
(per `DATASPOKE_KUBE_CLOUD_VENDOR`) must be on this host's PATH and
authenticated against the registry, including when this same command runs
with `--skip-build` on a deploy-only CI host against images a separate build
stage already pushed. Add `--no-digest-pin` if that host cannot reach the
registry API (only `docker`/local image handling is available, or none at
all) — the three DataSpoke-owned workloads (api, event-consumer, frontend)
then deploy by mutable `<repository>:<tag>` with `imagePullPolicy: Always`,
and an explicit `kubectl rollout restart` replaces the digest-triggered
roll. `postgresql` and `airflow` are never digest-stamped regardless of this
flag — see `spec/feature/HELM_CHART.md` §Digest stamping's "airflow and
postgres are not digest-stamped" note for the manual-restart requirement
those two still carry after an image update.

This automatically seeds the default admin user
(`dataspoke@dataspoke.local` / `dataspoke`) at the end unless `--skip-seed` is
passed, and rotates it to `DATASPOKE_PROD_ADMIN_PASSWORD` in the same step when
that input is set — see §5 for which of the two happened and what is left to do.

#### 5. Rotate the auto-seeded default admin (REQUIRED, unless step 4 did it)

**Already done when `DATASPOKE_PROD_ADMIN_PASSWORD` was set in
`helm-charts/.env.prod`.** Step 4's admin seed rotated
`dataspoke@dataspoke.local` to that password and verified the result by logging
in with it. What that attempt did is in the seed's own line in the install
output:

| The seed printed | State |
|---|---|
| `[INFO] Rotated 'dataspoke@dataspoke.local' to DATASPOKE_PROD_ADMIN_PASSWORD …` | the published default authenticated and the change took |
| `[INFO] 'dataspoke@dataspoke.local' already authenticates with DATASPOKE_PROD_ADMIN_PASSWORD …` | the target password authenticated on the first attempt — an earlier run had already done it |
| `[WARN] 'dataspoke@dataspoke.local' accepts neither DATASPOKE_PROD_ADMIN_PASSWORD nor the password published in this repository …` | someone rotated the account to a third value; the seed left it alone and the install **finished normally** |
| a red `[ERROR]` from the seed | **the install aborted there** — the seed exits non-zero and `install.sh` runs it under `set -euo pipefail`, so the `=== Installation complete (profile: prod) ===` summary never printed. The chart is installed; the install *command* failed |

On either `[INFO]`, confirm you can log in with your own password and skip the
rest of this section. On the `[WARN]`, reconcile `helm-charts/.env.prod` with
what that account actually holds, or reset it through `PATCH /api/v1/auth/me`
as its current owner. On an `[ERROR]`, read which of the three failures the
message names — the `PATCH` was rejected, the confirming login did not succeed,
or the in-pod exchange never completed — treat the published default as
possibly live, and re-run the install (or §6's standalone seed) once the cause
is fixed.

**Otherwise it is still live.** With that line blank, step 4's install seeded
`dataspoke@dataspoke.local` / `dataspoke` and rotated nothing. That credential
is published in this repository, so it authenticates against your API for
anyone who can reach it — see the exposure note in §Prod profile above for how
reachability depends on your ingress controller and cluster network posture.
Rotate it now, before treating the deployment as usable by anyone other than
the install operator. The account address is not configurable here:
`PATCH /auth/me` sets name and password only.

```bash
TOKEN="<token from logging in as the seeded admin>"
read -r -s -p "New password: " NEW_PASSWORD; echo
curl -s -X PATCH https://api.<your-domain>/api/v1/auth/me \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d @- <<EOF
{"password": "${NEW_PASSWORD}"}
EOF
```

(`read -s` plus piping the body through a heredoc into `-d @-` keeps the new
password out of both shell history and process argv — unlike embedding it
directly in a `-d '{"password": "<new-password>"}'` literal, which is visible
via `ps auxww` for the life of the curl process.)

#### 6. Re-run or skip the seed (optional)

The seed step in §4 is idempotent (safe to re-run) and only creates
`dataspoke@dataspoke.local` / `dataspoke` if no Admin exists yet — there is no
"wire DataHub before seeding" ordering constraint, user creation is
local-only. Use `--skip-seed` at install time if you provision the admin some
other way, then seed manually when ready:

```bash
ENV_FILE=helm-charts/.env.prod bash helm-charts/bin/post-install/seed-admin-user.sh
```

The `ENV_FILE=` prefix is **required** — the script defaults to `.env.dev`
when `ENV_FILE` is unset. When `DATASPOKE_PROD_ADMIN_PASSWORD` is set in that
file, this same run also rotates the account to it and prints one of the lines
§5 lists, exiting non-zero on the `[ERROR]` ones; it is idempotent because it
tries the target password first.

#### 7. Register peripherals and runtime config

Fill in sections 4 (LLM) and 5 (peripherals) of `helm-charts/.env.prod`, then
run the two seed scripts against it:

```bash
ENV_FILE=helm-charts/.env.prod bash helm-charts/bin/post-install/seed-peripheral-config.sh
ENV_FILE=helm-charts/.env.prod bash helm-charts/bin/post-install/seed-runtime-config.sh
```

The `ENV_FILE=` prefix is **required** — both scripts default to `.env.dev`,
so an unprefixed run against a prod cluster PATCHes dev addresses into prod's
config. Each script picks its profile from the prefix the named file declares
(`DATASPOKE_PROD_*` vs `DATASPOKE_DEV_*`), so there is no profile flag to get
wrong. Both are re-runnable at any time: an absent or empty value is read as
"leave unchanged", never as a clearing write.

**This is a correctness fix, not a convenience.** Hand-assembling four JSON
bodies from a schema that lives in another document is exactly where invented
placeholder values and malformed-PAT JSON come from — the two failure modes the
pre-flight's `placeholder-` rejection and the seeds' serialised payload
construction exist to catch (a quote or a backslash in a DataHub PAT otherwise
yields a `422` naming a field you never typed). The seed commands remove the
opportunity.

**The secret fields ride inside the PATCH.** The DataHub personal access token,
the Kafka SASL password and the Langfuse secret key are sent as part of the
peripheral payload, and the API writes them into `dataspoke-datahub-secret` /
`dataspoke-langfuse-secret` itself; the LLM API key goes the same way into
`dataspoke-llm-secret` via `PATCH /internal/admin/conf`. **You do not need to
pre-create those three Secrets** — the API creates each on first PATCH. Nor
does pre-creating one break the seed: the accessors are create-or-patch and the
patch is a strategic merge on `data`, so a Secret your own secrets manager
already owns is patched key-by-key rather than clobbered. Payloads carrying a
credential reach the in-pod caller through stdin rather than argv, so none of
these values appears in `ps auxww` on your machine or in the pod.

**The prod path sets no `stub_*` flag.** The four dependency toggles
(`stub_redis_client`, `stub_llm_client`, `stub_pgvector_manager`,
`stub_notification_service`) are a dev mechanism; a production deployment
running on a stub answers `200` and delivers none of it. A `stub_*` flag true
on a prod deployment means the dev path was run against it.

**SMTP stays a `curl`**, because it is deliberately not carried in the env file
— no install step needs it, and it is set against the running deployment:

```bash
curl -X PATCH https://api.<your-domain>/api/v1/admin/peripherals/smtp \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{...}'
```

See `spec/API.md` for the full request/response contracts.

### Prod: committing a credential-free copy source

`helm-charts/.env.prod` carries the whole of a prod deployment's operator
input, the eleven `DATASPOKE_PROD_*` credentials included, and it is
gitignored. A team that installs the same deployment repeatedly still wants the
non-credential half — cluster context, namespace, ingress class and domain,
registry, cloud vendor — captured somewhere rather than re-derived each time.
The supported way to do that is a committed **copy source**:

```
helm-charts/.env.prod.<name>-no-credential.example
```

**The rule is about which copy, not which variables.** A tracked `*.example`
carries the full variable set with placeholders and no real values; the
gitignored `.env.prod` carries the real ones. So this file has the same shape
as `.env.prod.example` — every section, every variable — with your deployment's
real shape filled in and every credential line left blank. Blank is the correct
state for those lines anyway: the pre-flight resolves each one by adopting from
the cluster or generating (see §2), so a copy source that leaves them empty is
a complete input, not a truncated one.

**It is a copy source only, never an `--env-file` argument.** Copy it to
`helm-charts/.env.prod` and run the pre-flight against that. Passing an
`.example` path to `--env-file` would have the pre-flight write resolved
credentials into a tracked file.

**Why the filename has to keep its leading dot.** `.gitignore` denies any file
whose basename starts with `.env` (`.env*`, unanchored — matching at any depth,
which covers `--env-file`'s arbitrary-path story on both `install.sh` and
`uninstall.sh`), and re-includes `*.example` templates and `.envrc`
(`!.env*.example`, `!.envrc`). A dot-less name like `env.prod-no-credential`
would also be committable — but it works by *evading* the rule rather than
satisfying it, and `.env*` is denied precisely because these files are
credential-bearing. Making the safety net depend on a filename convention
nobody enforces, on the single file type most likely to receive a pasted
secret, is backwards. `.env.prod.<name>-no-credential.example` needs no
exemption of its own, is caught by the deny rule the moment someone drops the
`.example` suffix, and is self-documenting as a copy source rather than live
config.

**Credential-free is not disclosure-free.** Such a file still names your
cluster context, cloud project, container registry and ingress hosts. Commit it
only to a **private** deployment repo. State that in its header:

```bash
# COPY SOURCE — copy to helm-charts/.env.prod, never pass as --env-file.
# Real deployment shape; every credential line left blank for the pre-flight
# to resolve (adopt from the cluster, else generate). Never paste a credential
# into this file: it is tracked.
# Credential-free is NOT disclosure-free — this file names the cluster
# context, cloud project, container registry, and ingress hosts. PRIVATE repo
# only.
DATASPOKE_KUBE_CLUSTER=...
DATASPOKE_KUBE_DATASPOKE_NAMESPACE=...
```

### Prod: what survives an uninstall

`./helm-charts/bin/uninstall.sh --profile prod` uninstalls the Helm release
and four chart-derived Secrets (`dataspoke-airflow-metadata-db`,
`dataspoke-airflow-api-secret-key`, `dataspoke-airflow-jwt-secret`,
`dataspoke-airflow-metadata-encryption-key`), but retains everything below by
design. These four are projections of keys held in the retained credentials
Secret, so deleting them is safe — the next install rebuilds them
byte-identically from that source.

`dataspoke-airflow-fernet-key` — the Airflow subchart's own pre-install-hook
Secret, which `helm uninstall` never removes (`hook-delete-policy:
before-hook-creation`) and which only exists on a cluster that ran a release
before `airflow.fernetKeySecretName` was pinned to
`dataspoke-airflow-metadata-encryption-key` — is deleted **conditionally**:
only when its `fernet-key` value agrees with `DATASPOKE_AIRFLOW_FERNET_KEY`
in the retained credentials Secret (a redundant copy, safe to drop). If it
disagrees, or the credentials Secret has no `DATASPOKE_AIRFLOW_FERNET_KEY` to
compare against (a Secret that predates the Fernet key joining the
credentials contract), the uninstaller
leaves it in place and warns — on such a cluster it may be the only live
carrier of the key that decrypts the retained Postgres PVC's Airflow
connections/Variables. There is no prod `--delete-pvcs` — that flag is
dev-only; namespace deletion is prod's only full wipe of the namespace's own
objects. **The uninstaller asks "delete the namespace?" first and only
prints this same summary afterward, when you answered no**
(`--delete-all` also implies `--delete-namespaces`, so it skips the summary
too) — the table below is a reference, not something you need to remember.
A namespace deletion does not by itself destroy the credential material on
these PVCs, though: under a `Retain`-reclaim-policy `StorageClass` (see
below) the underlying volumes survive the namespace and must be deleted by
hand — or their disk-encryption key destroyed — for a complete decommission.

**A custom-named Fernet Secret is untouched by `uninstall.sh`.** If this
release's Airflow chart was ever pointed at a self-chosen
`airflow.fernetKeySecretName` — for example by a direct `helm upgrade` or a
GitOps tool applying an overlay that `install.sh`'s own forced `--set` never
ran ahead of — the cleanup above only knows the two fixed names,
`dataspoke-airflow-metadata-encryption-key` (always deleted) and
`dataspoke-airflow-fernet-key` (conditionally deleted per the paragraph
above). A third, self-chosen Secret name is neither deleted nor
conditionally preserved; `uninstall.sh` does not know it exists and leaves
it exactly as it was. Locate and dispose of it by hand if you are
decommissioning the namespace for good.

| Resource | Kind | Size | Notes |
|----------|------|------|-------|
| `data-dataspoke-postgresql-0` | PVC | 50Gi | Postgres primary data |
| `redis-data-dataspoke-redis-master-0` | PVC | 8Gi | Redis primary data |
| `redis-data-dataspoke-redis-replicas-0` | PVC | 8Gi | Redis replica data (StatefulSet `dataspoke-redis-replicas`, plural — only the replica name pluralizes) |
| `dataspoke-secrets` (or your `secrets.existingSecret`) | Secret | -- | operator-owned; never deleted |
| `dataspoke-llm-secret`, `dataspoke-datahub-secret`, `dataspoke-langfuse-secret`, `dataspoke-smtp-secret` | Secret | -- | out-of-band, not managed by this script; survive silently if present |

Deleting one of these PVCs — by hand (see the manual commands below) or as a
side effect of a namespace deletion — does not necessarily destroy the
underlying volume: whether it does depends on the `StorageClass`'s
`reclaimPolicy`. A `Retain` reclaimPolicy leaves the volume behind, unbound
and unreclaimed, once its PVC is gone; `Delete` (the common provisioner
default) destroys it in the same action. That distinction matters here
because these volumes hold the credential store on the Postgres PVC
(password hashes, `api_tokens`/`password_reset_tokens`, Fernet-encrypted
ingestion secrets) and Redis's AOF (refresh-token revocation keys). See
[`helm-charts/prod-prereq/` §StorageClass](prod-prereq/#storageclass--the-first-case)
for the `Retain`-vs-`Delete` trade-off and an example manifest.

**Fernet key ↔ Postgres PVC coupling**: Airflow encrypts connection secrets
and Variables in its metadata DB with the Fernet key it reads from
`dataspoke-airflow-metadata-encryption-key`, projected from
`DATASPOKE_AIRFLOW_FERNET_KEY` in the retained credentials Secret. Because
that metadata lives in the retained Postgres PVC, the credentials Secret and
the PVC must be kept or dropped together.

Two different guarantees apply depending on what you do next:

- **Re-running `install.sh` against this still-live release** (no uninstall in
  between) compares `DATASPOKE_AIRFLOW_FERNET_KEY` in the credentials Secret
  against the live `dataspoke-airflow-metadata-encryption-key` projection and
  aborts on a disagreement instead of silently re-projecting it. Recover the
  live value with:

  ```bash
  kubectl get secret dataspoke-airflow-metadata-encryption-key -n <your-namespace> \
    -o jsonpath='{.data.fernet-key}' | base64 --decode
  ```

  then set `DATASPOKE_AIRFLOW_FERNET_KEY` in the credentials Secret to that
  value before retrying.
- **A full uninstall/reinstall cycle** carries a weaker version of this
  protection: this teardown always deletes `dataspoke-airflow-metadata-
  encryption-key` (it is safely regenerable, byte-identically, from the
  retained credentials Secret), removing the comparison point the guard above
  reads. Whether a comparison still happens on the next install depends on
  `dataspoke-airflow-fernet-key` (see above) — if it agreed with the
  credentials Secret at teardown time it was deleted too, and the next
  install trusts `DATASPOKE_AIRFLOW_FERNET_KEY` in the retained credentials
  Secret unchecked; if it disagreed (or the Secret had no key to compare) it
  was left in place, and the next install's `_ensure_airflow_fernet_secret`
  falls back to comparing against it, aborting on a mismatch exactly as in
  the still-live-release case above. Either way, do not edit that key by hand
  between teardown and reinstall while the Postgres PVC survives.

**Deleting the credential Secret**: deleting `dataspoke-secrets` (or your
`secrets.existingSecret`) destroys the only copy of all 11 credentials unless
they also live in an external secrets manager, and strands the Postgres PVC
above if you keep it — the running cluster still expects the old
`DATASPOKE_POSTGRES_PASSWORD` and `DATASPOKE_AIRFLOW_FERNET_KEY`. Delete the
Secret only together with the PVCs above, or not at all.

**A populated `.env.prod` makes a deleted Secret recoverable.** Once the env
file holds all eleven `DATASPOKE_PROD_*` credential inputs — which it does
after one pre-flight run — `bin/install-prod-preflight.sh` rebuilds the Secret
byte-identically, Fernet key included, so the env file, not the Secret, is the
surviving copy of what the retained PVCs depend on.

This **relocates** the risk rather than removing it. The env file becomes the
thing to protect, and the whole-Secret ↔ PVC coupling above reapplies in full
if it is lost, or if its Fernet line was never populated (an operator who has
only ever run `--verify-only`, or `--skip-secret`, has an env file that resolves
nothing). So the check that belongs before any teardown is on the env file, not
on the cluster — **confirm all eleven keys are set, by name with set/blank
verdicts only, never values**:

```bash
awk -F= '/^DATASPOKE_PROD_/ {printf "%-58s %s\n", $1, (length($2)>0 ? "SET" : "blank")}' \
  helm-charts/.env.prod
```

That prints the whole `DATASPOKE_PROD_*` block; the eleven that matter here are
the credential inputs — `..._POSTGRES_PASSWORD`, `..._REDIS_PASSWORD`,
`..._AIRFLOW_{USER,PASSWORD,WEBSERVER_SECRET_KEY,JWT_SECRET,FERNET_KEY}`,
`..._INTERNAL_TOKEN`, `..._JWT_SECRET_KEY`, `..._OAUTH_STATE_SECRET`,
`..._GOOGLE_OAUTH_CLIENT_SECRET`. Any of those reported `blank` exists only in
the cluster. Recover it before deleting anything — the pre-flight's own adopt
step does exactly that:

```bash
./helm-charts/bin/install-prod-preflight.sh --values /path/to/your-overlay.yaml
```

**Airflow log PVCs**: `logs-dataspoke-airflow-scheduler-0` and
`logs-dataspoke-airflow-triggerer-0` exist only if your overlay enables
Airflow log persistence (see `values-prod.example.yaml` §Airflow log
persistence) — disabled in the shipped chart default, so most installs never
create them. When present they hold real post-mortem task logs by design;
delete them only once you no longer need that retained history.

To delete manually (probe first — a PVC not present is normal for
log-persistence-off installs):

```bash
kubectl delete pvc data-dataspoke-postgresql-0 \
  redis-data-dataspoke-redis-master-0 \
  redis-data-dataspoke-redis-replicas-0 \
  -n <your-namespace>

# Only if present (see "Airflow log PVCs" above):
kubectl delete pvc logs-dataspoke-airflow-scheduler-0 \
  logs-dataspoke-airflow-triggerer-0 \
  -n <your-namespace>

kubectl delete secret dataspoke-secrets \
  -n <your-namespace>
```

Or delete the namespace for a full wipe — the sanctioned prod full teardown:

```bash
./helm-charts/bin/uninstall.sh --profile prod --delete-namespaces
```

---

## Ingress Endpoints

All HTTP services are accessed via virtual-host routing on the ingress
controller (`<SCHEME>://<service>.<INGRESS_DOMAIN>/`); TCP services (databases,
brokers) are exposed on dedicated ports and never take the scheme. How the
controller, `<INGRESS_DOMAIN>`, `<SCHEME>`, and `<TCP_HOST>` are provided
depends on `DATASPOKE_KUBE_INGRESS_MODE` in `helm-charts/.env.dev`:

- **`managed`** (default) — the install owns an nginx-ingress controller.
  `<INGRESS_DOMAIN>` auto-derives to `<LoadBalancer-IP>.nip.io` (wildcard DNS,
  no `/etc/hosts` entries) and `<TCP_HOST>` is that LoadBalancer IP (TCP
  passthrough on the controller). `<SCHEME>` is `http` (typical).
- **`shared`** — the install reuses the cluster's pre-existing ingress
  controller (e.g. AWS/EKS). The operator pre-sets
  `DATASPOKE_KUBE_INGRESS_DOMAIN` — wildcard DNS, or a record for each
  `<service>.` host in the table below (`app.`, `api.`, `airflow.`, `datahub.`,
  `datahub-gms.`, `langfuse.`) — and `<TCP_HOST>` is `127.0.0.1` — TCP
  services are forwarded to the same ports via `./helm-charts/bin/port-forward.sh`.
  `<SCHEME>` is `http` or `https` per `DATASPOKE_KUBE_INGRESS_SCHEME` — set
  `https` when the shared controller terminates TLS + HSTS in front of the
  virtual hosts, since an `http` page would break under browser
  mixed-content/auto-upgrade. `DATASPOKE_KUBE_INGRESS_TLS_SECRET` (optional)
  additionally puts a `tls:` block on the three dev ingresses DataSpoke owns
  (API, frontend, Airflow) — leave it empty when the controller terminates TLS
  with a controller-level or wildcard cert.

| Service | Address | Credentials |
|---------|---------|-------------|
| DataHub UI | `<SCHEME>://datahub.<INGRESS_DOMAIN>/` | `datahub` / `datahub` |
| DataHub GMS | `<SCHEME>://datahub-gms.<INGRESS_DOMAIN>/` | -- |
| DataSpoke Web UI (dev `--frontend cluster`) | `<SCHEME>://app.<INGRESS_DOMAIN>/` | `dataspoke` / `dataspoke` — rotate via `PATCH /api/v1/auth/me` before production (see §Prod profile §5) |
| DataSpoke Web UI (dev `--frontend local`) | `http://localhost:3000` | same as above |
| DataSpoke API | `<SCHEME>://api.<INGRESS_DOMAIN>/api/v1/` | per `.env` JWT |
| Airflow UI | `<SCHEME>://airflow.<INGRESS_DOMAIN>/` | `admin` / `admin` (see `.env`) |
| Langfuse UI | `<SCHEME>://langfuse.<INGRESS_DOMAIN>/` | `DATASPOKE_DEV_LANGFUSE_INIT_USER_{EMAIL,PASSWORD}` in `helm-charts/.env.dev` (auto-generated on first install) |
| DataSpoke PostgreSQL | `<TCP_HOST>:9201` | per `.env` |
| Redis | `<TCP_HOST>:9202` | per `.env` |
| DataHub Kafka | `<TCP_HOST>:9005` | -- |
| Example PostgreSQL | `<TCP_HOST>:9102` | `postgres` / `ExampleDev2024!` |
| Example Kafka | `<TCP_HOST>:9104` | -- |
| Lock API | `<TCP_HOST>:9221` | -- |

`datahub-gms.<INGRESS_DOMAIN>` is a credential-bearing origin: every call to it
carries the DataHub personal access token in an `Authorization: Bearer` header.
Give it the same treatment as the DataHub frontend host, not just a DNS record —
certificate SAN coverage (a wildcard cert covers it; an explicit-SAN cert must
list it) and whatever WAF or source-range allow-list your other DataSpoke hosts
sit behind. With `DATASPOKE_KUBE_INGRESS_SCHEME=https` the GMS Ingress refuses
the plaintext hop (`ssl-redirect: "true"`), so a missing SAN surfaces as a TLS
error rather than a silent downgrade.

Upgrading an existing dev environment: `DATASPOKE_DEV_DATAHUB_GMS_URL` in
`helm-charts/.env.dev` is only rewritten by the DataHub install step. Until you
re-run it, that variable still names the old origin and tooling keeps sending
the PAT there:

```bash
./helm-charts/bin/install.sh --profile dev --components datahub
```

The dev default (`--frontend none`) does not deploy the frontend pod. To run the UI on the host:

```bash
# 1. Write src/frontend/.env.local pointing at the in-cluster API
./helm-charts/bin/install.sh --profile dev --frontend local

# 2. Start the Next.js dev server
pnpm -C src/frontend install && pnpm -C src/frontend dev
# Open http://localhost:3000  —  login: dataspoke@dataspoke.local / dataspoke
```

To deploy the containerised frontend in-cluster instead:

```bash
./helm-charts/bin/install.sh --profile dev --frontend cluster
# Open <SCHEME>://app.<INGRESS_DOMAIN>/  —  login: dataspoke@dataspoke.local / dataspoke
```

The `--components frontend` fast path (rebuild + redeploy only the frontend pod) remains available as a code-iteration shortcut.

---

## Profile Differences

See `spec/feature/HELM_CHART.md §Profiles` for the canonical comparison table.
The short version: dev installs nginx-ingress, DataHub, Langfuse, dummy data,
and the dev-lock service; prod installs only the umbrella chart and assumes the
operator brings peripherals and an ingress controller.

---

## Stopping the API for Iteration

To stop the API without tearing down the full stack:

```bash
kubectl scale deployment/dataspoke-api --replicas=0 \
  -n "${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"
```

To rebuild and redeploy:

```bash
./helm-charts/bin/install.sh --profile dev --components api
```

This rebuilds the API image, runs `helm upgrade` (which pins the rebuilt image
by digest so it rolls the deployment by construction — see
`spec/feature/HELM_CHART.md` §Digest stamping), and waits for rollout.

---

## Selective Reinstall

Install (or reinstall) a subset of components:

```bash
# Reinstall DataHub only
./helm-charts/bin/install.sh --profile dev --components datahub

# Reinstall DataSpoke infra (Postgres, Redis, Airflow, API, event-consumer)
./helm-charts/bin/install.sh --profile dev --components dataspoke-infra

# Resume an interrupted full install starting at a specific component
./helm-charts/bin/install.sh --profile dev --from-component langfuse
```

Component names: `nginx-ingress`, `datahub`, `langfuse`, `dataspoke-infra`,
`api`, `frontend`, `dummy-data`, `dev-lock`, `seed`.

---

## Lock Service HTTP API

The dev-lock service provides an advisory mutex for coordinating multi-tester
access to shared dev-env resources.

`install.sh` auto-populates `DATASPOKE_DEV_LOCK_URL` in `helm-charts/.env.dev`
(`http://<INGRESS_IP>:9221` in managed mode, `http://127.0.0.1:9221` via
`bin/port-forward.sh` in shared mode), so the same command works in both:

```bash
LOCK_URL=$(grep DATASPOKE_DEV_LOCK_URL helm-charts/.env.dev | cut -d= -f2)
curl -s -X POST ${LOCK_URL}/lock/acquire \
  -H "Content-Type: application/json" \
  -d '{"owner": "alice", "message": "running ingestion test"}'
```

| Endpoint | Method | Response |
|----------|--------|----------|
| `/lock` | GET | Current lock status |
| `/lock/acquire` | POST | `200` acquired, `409` held by another, `400` missing owner |
| `/lock/release` | POST | `200` released, `403` wrong owner |
| `/lock` | DELETE | Force-release (admin) |

Lock state is in-memory and resets on pod restart.

---

## Troubleshooting

See `spec/feature/HELM_CHART.md §Troubleshooting` for the full list of known
issues and mitigations (pod eviction, OpenSearch OOM, MAE consumer stall, etc.).

Reinstall a failing component:

```bash
./helm-charts/bin/install.sh --profile dev --components <name>
```
