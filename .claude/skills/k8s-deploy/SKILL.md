---
name: k8s-deploy
description: Drive the helm-charts/bin/ install/uninstall/build/health scripts for both dev and prod profiles — configure, install, reinstall, uninstall, health-check, and rebuild-the-API / rebuild-the-frontend. The dev profile is one command: install.sh installs umbrella chart + peripherals (nginx-ingress, DataHub, Langfuse, dummy data, dev-lock) and auto-seeds peripheral connection config via the admin API. The prod profile is a TWO-command sequence driven by one operator file (helm-charts/.env.prod): install-prod-preflight.sh validates the env file, the values overlay and the cluster, resolves the eleven credentials and creates the credentials Secret, and must pass BEFORE install.sh --profile prod, which installs the umbrella chart only and aborts on a missing Secret. Peripherals and LLM settings are seeded afterwards from the same file.
argument-hint: "[configure|install|reinstall|uninstall|health-check|run-api] [--profile dev|prod] [--components <csv>, dev only] [other options...]"
allowed-tools: Bash(*), Read, Edit, Write, Glob, Grep, Skill(k8s-work), AskUserQuestion
---

## Routing

Parse `$ARGUMENTS` and the user's request to determine the action. If ambiguous or no arguments given, ask the user which action they want:

| Action | Trigger keywords |
|--------|-----------------|
| **configure** | `configure`, `config`, `setup`, `env` |
| **install** | `install`, `up`, `create` |
| **health-check** | `health-check`, `health`, `check`, `status` |
| **run-api** | `run-api`, `run`, `start`, `deploy`, `test-mode`, `iterate` |
| **reinstall** | `reinstall`, `reset` |
| **uninstall** | `uninstall`, `teardown`, `down`, `remove`, `destroy` |

### Profile

Every action requires a profile. Parse `--profile dev` or `--profile prod`
from `$ARGUMENTS`. If absent, ask the user — do not default. The dev
profile installs peripherals and seeds; the prod profile is umbrella-chart
only.

**Prod is a two-command sequence**, and the order is not negotiable:

```bash
./helm-charts/bin/install-prod-preflight.sh --values <overlay.yaml>   # validates + populates
./helm-charts/bin/install.sh --profile prod --image-tag <tag> --values <overlay.yaml>
```

**Never run a prod `install.sh` without the pre-flight having passed.**
`install.sh --profile prod` aborts when the credentials Secret is absent and
never creates one, so a prod install attempted first fails partway through the
operator's checks with nothing to show for it. The pre-flight is where every one
of those checks costs nothing — it never installs, upgrades, deletes or builds,
and its only three mutations are writing resolved credentials into the env file,
creating the namespace (behind `--create-namespace`), and creating the Secret.

Dev has no pre-flight script; `install.sh --profile dev` is the whole sequence.

### Component names

When the user specifies components, match against these names (the
`--components` flag accepts a comma-separated subset). If no components
are specified, operate on **all-for-profile**.

`--components` and `--from-component` are dev-only — `install.sh` rejects both
under `--profile prod`, which always runs its full phase sequence. A prod
request naming components is a request for something the installer does not
offer: say so rather than dropping the flag and running a full install.

| Component | Profiles | Aliases |
|---|---|---|
| `nginx-ingress` | dev | `ingress` |
| `datahub` | dev | — |
| `langfuse` | dev | `lf`, `observability` |
| `dataspoke-infra` | dev | `infra`, `infrastructure`, `chart`, `umbrella` |
| `api` | dev | (rebuild + helm-upgrade the API only — iteration path) |
| `frontend` | dev | `ui`, `web` (rebuild + helm-upgrade the Next.js UI only — iteration path) |
| `dummy-data` | dev | `example`, `dummy` |
| `dev-lock` | dev | `lock` |
| `seed` | dev | (post-install admin-API seeding only) |

---

## Action: configure

### Two rules that apply everywhere in this skill

**1. Never print a credential value from a prod env file** — not in a
confirmation dump, not in a summary, not in a diff. Everything printed here
lands in a terminal, a transcript, and possibly a log. Show names with a
set/blank verdict instead:

```bash
awk -F= '/^DATASPOKE_/ {printf "%-58s %s\n", $1, (length($2)>0 ? "SET" : "blank")}' helm-charts/.env.prod
```

This overrides step 5's "show the final env file content" for the prod profile:
show the *names and verdicts*, never the values. The same rule governs how
populate results are reported — provenance per key (`from the env file` /
`adopted from the cluster` / `generated`), never the value. The pre-flight
already reports exactly that; relay it verbatim rather than reading the file.

**2. Collect every operator-supplied input in one pass.** Ask for the OAuth
client secret, the admin password, and the DataHub, LLM **and** Langfuse blocks
together — one `AskUserQuestion`, each item labelled with what happens if it is
left blank:

| Operator input | Verdict when blank |
|---|---|
| `DATASPOKE_PROD_GOOGLE_OAUTH_CLIENT_SECRET` | **hard stop** — the one value nothing can generate (the Google Cloud Console issues it) |
| `DATASPOKE_PROD_ADMIN_PASSWORD` | **warns**; the install seeds the repo-published `dataspoke@dataspoke.local / dataspoke` and leaves it live |
| `DATASPOKE_PROD_PERIPHERAL_DATAHUB_{GMS_URL,FRONTEND_URL,TOKEN}`, `DATASPOKE_PROD_LLM_{PROVIDER,MODEL,API_KEY}` | **stops the readiness stage** unless `--skip-postinstall-check` |
| `DATASPOKE_PROD_PERIPHERAL_DATAHUB_KAFKA_*` | **stops the readiness stage, but only when `event-consumer.enabled` resolves true** — and then only the fields that posture needs (brokers always; the SASL mechanism under `SASL_SSL`/`SASL_PLAINTEXT`; username and password only for a typed mechanism, never under `AWS_MSK_IAM`) |
| `DATASPOKE_PROD_PERIPHERAL_DATAHUB_{SERVICE_CORPUSER_URN,DEFAULT_ENV}` | **never blocks** — the readiness stage does not check them; still ask, since the seed sends whatever is there and an absent value is "leave unchanged" |
| `DATASPOKE_PROD_PERIPHERAL_LANGFUSE_*` | **warns only, never blocks** — which is exactly why it gets forgotten |
| the other ten credentials | adopted from the cluster, else generated — **never ask for these** |

Asking only for what blocks the stage you happen to have reached guarantees a
second round of questions after the operator believes they are done, and
silently drops whatever merely warns.

Present the peripheral and LLM blocks as **deferrable**, and say what deferral
costs until the seeds run: DataHub-backed features report the peripheral's
absence rather than working, generation features (ontology generation, metadata
generation, LLM validation) fail at first use, and tracing stays off. Presenting
a deferrable block as mandatory is what pushes an operator into inventing
placeholder values — the failure mode the pre-flight's `placeholder-` rejection
exists to catch.

### Prod configuration planes

**Prod splits configuration across three planes.** Put each input in the plane that owns it and never mix them:

| Plane | Holds | Source |
|---|---|---|
| env file (`helm-charts/.env.prod`) | the **whole** operator input: `DATASPOKE_KUBE_*` deployment shape plus the `DATASPOKE_PROD_*` tier — the eleven credential inputs, the OAuth client id, the admin password, the LLM block, and the peripheral block | `.env.prod.example`, or a committed `.env.prod.<name>-no-credential.example` when the deployment publishes one; `--env-file <path>` overrides the default |
| `dataspoke-secrets` K8s Secret (or the overlay's `secrets.existingSecret`) | the 11 credential keys, as `DATASPOKE_<X>` | **`install-prod-preflight.sh` creates it** from the env file's `DATASPOKE_PROD_<X>` lines. `--skip-secret` for ExternalSecrets/Vault/SealedSecrets operators |
| operator overlay (`--values`) | ingress hosts + published path list, TLS secret names, CORS origins, OAuth redirect + client ID, `secrets.existingSecret`, replica counts, storage classes | copy of `values-prod.example.yaml` — `README.md §3` |

The env file is the single prod operator input file, credentials included, and
it is gitignored. A tracked `*.example` copy carries the same variable set with
placeholders and no real values — that is the rule, and it is about **which
copy**, not which variables.

**Prefer a committed `.env.prod.<name>-no-credential.example` as the copy
source** when the repo has one (`ls helm-charts/.env.prod.*-no-credential.example`),
falling back to `.env.prod.example`. It carries a real deployment's shape with
every credential line blank. It is a **copy source only, never an `--env-file`
argument** — copy it to `helm-charts/.env.prod` and point the scripts at that,
because passing an `.example` path to `--env-file` would have the pre-flight
write resolved credentials into a tracked file.

Steps 1–3 and 5 below apply to both profiles; step 4 is dev-only.

**For the prod profile, prefer the pre-flight over walking the planes by
hand**: once the env file and the overlay exist, run

```bash
./helm-charts/bin/install-prod-preflight.sh --values <overlay.yaml> --verify-only
```

and let it report what is missing. It is read-only in that mode, so it is safe
against a live deployment. The plane-by-plane walkthrough in `README.md §1–§3`
stays authoritative for *what* is checked, and is the path for `--skip-secret`
operators who deliver the eleven keys through a secrets manager.

1. Read `helm-charts/.env.dev` (dev profile) or `helm-charts/.env.prod` (prod profile). If it does not exist, create it from `helm-charts/.env.dev.example` (dev) or `helm-charts/.env.prod.example` (prod).
2. If it already exists, verify the canonical variables are present per spec `spec/feature/HELM_CHART.md §Configuration — Five-Tier Env Vars`:
   - **Kube deployment** (both profiles): `DATASPOKE_KUBE_CLUSTER`, `DATASPOKE_KUBE_DATASPOKE_NAMESPACE`, `DATASPOKE_KUBE_IMAGE_REGISTRY`, `DATASPOKE_KUBE_CLOUD_VENDOR` (`GCP` → Cloud Build; `AWS` → ECR via `DATASPOKE_AWS_PROFILE`, optional `DATASPOKE_DOCKER_SUDO`; empty → local Docker), `DATASPOKE_KUBE_INGRESS_MODE` (`managed` default — install & own nginx-ingress; `shared` — reuse a pre-existing controller), `DATASPOKE_KUBE_INGRESS_CLASS` (shared mode), `DATASPOKE_KUBE_INGRESS_IP` (managed: auto-populated in dev; shared: blank), `DATASPOKE_KUBE_INGRESS_DOMAIN` (managed: auto-populated `<IP>.nip.io`; shared: operator pre-set), `DATASPOKE_KUBE_INGRESS_SCHEME` (`http` default / `https` — scheme for every ingress-domain URL the dev install builds; set `https` when a shared controller terminates TLS), `DATASPOKE_KUBE_INGRESS_TLS_SECRET` (optional, default empty — when set, emits per-Ingress `tls:` blocks referencing this K8s TLS Secret).
   - **App runtime** (`DATASPOKE_*`, tier 1): not in `.env` — injected into pods from the `dataspoke-secrets` K8s Secret via `envFrom`. In dev, `install.sh` auto-generates the Secret; in prod, `install-prod-preflight.sh` creates it from the env file's `DATASPOKE_PROD_*` inputs. `DATASPOKE_CORS_ORIGINS` is rendered from chart values (`config.corsOrigins`). A bare tier-1 name written into a prod env file is **rejected by the pre-flight**: scripts `source` that file, so a stale copy shadows the Secret for every one of them.
   - **Prod-only inputs** (`DATASPOKE_PROD_*`, tier 5, prod profile): the eleven credential inputs (`DATASPOKE_PROD_<X>` supplies Secret key `DATASPOKE_<X>` for those eleven suffixes only — `DATASPOKE_PROD_PERIPHERAL_*`, `DATASPOKE_PROD_LLM_*` and `DATASPOKE_PROD_ADMIN_PASSWORD` are not Secret keys), plus `DATASPOKE_PROD_GOOGLE_OAUTH_CLIENT_ID`, `DATASPOKE_PROD_ADMIN_PASSWORD`, `DATASPOKE_PROD_LLM_{PROVIDER,MODEL,API_KEY}`, and the `DATASPOKE_PROD_PERIPHERAL_{DATAHUB,LANGFUSE}_*` block. Each peripheral suffix is the API contract field it carries, upper-cased. SMTP is deliberately absent — it is set against the running deployment via `PATCH /api/v1/admin/peripherals/smtp`.
   - **Dev only** (dev profile): `DATASPOKE_DEV_KUBE_{DATAHUB,LANGFUSE,DUMMY_DATA}_NAMESPACE`, `DATASPOKE_DEV_KUBE_DATAHUB_{,PREREQUISITES_}CHART_VERSION`, `DATASPOKE_DEV_DATAHUB_MYSQL_{ROOT_,}PASSWORD`, `DATASPOKE_DEV_DUMMY_DATA_{KAFKA_INSTANCE,POSTGRES_USER,POSTGRES_PASSWORD,POSTGRES_DB}`, `DATASPOKE_DEV_LLM_{PROVIDER,API_KEY,MODEL}`. The Langfuse internals and peripheral connection outputs are auto-populated by the peripheral install scripts.
   - **Dev access** (`DATASPOKE_DEV_*`, auto-populated): written by `install.sh` post-install via `_sync_env_from_secret`; never manually edited. Read by `tests/integration/`, `health-check.sh`, and `port-forward.sh` for laptop-side cluster access. Same prefix as the dev-only inputs above — the two tiers are separated by provenance and by which `.env.dev` section they sit in, not by name.
3. **Do NOT** add stub-mode toggles to any env file. The four dependency factories are toggled via the `runtime_config` DB row (`stub_redis_client`, `stub_llm_client`, `stub_pgvector_manager`, `stub_notification_service`) — flippable via `PATCH /api/v1/admin/conf`, not in the env file. A `stub_*` name in `.env.prod` is rejected outright by the pre-flight, and the prod seed path sets no `stub_*` flag at all.
4. **Dev only** — generate secure passwords (16+ chars, mixed case, at least one special character) for any missing password variables. **Prod credentials are never hand-generated. Leave the eleven `DATASPOKE_PROD_*` credential lines blank** so the pre-flight can adopt what the cluster is already using before it generates anything. A hand-typed value there is a value that can silently contradict a running deployment — and for `DATASPOKE_PROD_AIRFLOW_FERNET_KEY` that means the retained Postgres PVC's Airflow connections and Variables become permanently undecryptable. A blank line is a request, not an omission. The two exceptions are the two the pre-flight cannot resolve for the operator: `DATASPOKE_PROD_GOOGLE_OAUTH_CLIENT_SECRET` (externally issued) and `DATASPOKE_PROD_ADMIN_PASSWORD` (the operator's choice) — ask for both per rule 2 above, never invent them.
5. **Show the final env file content to the user and ask for confirmation before writing.** Do not proceed until the user approves. (Skip confirmation if the env file already has all required variables.) **For the prod profile, show names and set/blank verdicts only** — rule 1 above.

---

## Action: install

Run `configure` first if the profile env file (`helm-charts/.env.dev` or `helm-charts/.env.prod`) does not exist or is missing required variables.

### Pre-flight checks

1. Verify `kubectl` and `helm` are installed.
2. Verify the Kubernetes cluster specified in `DATASPOKE_KUBE_CLUSTER` is reachable (`kubectl cluster-info`).
3. Report cluster node resources (`kubectl get nodes`) so the user can confirm the cluster meets the budget for the profile: `spec/feature/HELM_CHART.md §Resource Sizing` (8+ CPU / 24 GB RAM for the full dev profile), or for prod the operator-sized budget in `helm-charts/README.md §1` (2 API + 2 frontend replicas, Postgres 1–2 CPU / 2–6Gi, Airflow's five components, Redis primary + replica).
4. **Prod only — run `install-prod-preflight.sh` and let it do the checking.** Do not re-implement its stages by hand; run it and relay what it reports.

   ```bash
   ./helm-charts/bin/install-prod-preflight.sh --values <overlay.yaml> [--create-namespace]
   ```

   That form mutates operator credential state — it writes the eleven resolved
   credentials into `helm-charts/.env.prod`, creates the namespace under
   `--create-namespace`, and creates the credentials Secret — so **get the
   user's approval before running it**, the same approval configure step 5
   requires before writing the env file. `--verify-only` performs none of the
   three and needs no approval: run it first, relay what it reports, then run
   the mutating form once the user agrees.

   Its seven stages, announced `<n>/7`, with **credential populate third**:

   | # | Stage | What must hold |
   |---|---|---|
   | 1 | env file | the deployment-shape vars are present; no bare tier-1 `DATASPOKE_*` name; no `stub_*` toggle; `DATASPOKE_PROD_ADMIN_PASSWORD`, when set, is 10–128 characters and not `dataspoke`; the current kubectl context equals `DATASPOKE_KUBE_CLUSTER` |
   | 2 | values overlay | the API ingress does not publish `/internal/*`; no Airflow SimpleAuthManager conflict; `secrets.existingSecret` resolves; `auth.googleClientId` agrees with `DATASPOKE_PROD_GOOGLE_OAUTH_CLIENT_ID` |
   | 3 | credential populate | the eleven keys resolve as operator value → adopt from this cluster's Secret → generate and write back. Only `DATASPOKE_PROD_GOOGLE_OAUTH_CLIENT_SECRET` can fail, and it stops the run |
   | 4 | cluster prerequisites | the namespace exists (or `--create-namespace`); the `DATASPOKE_KUBE_INGRESS_CLASS` IngressClass exists; every StorageClass the overlay pins exists with a usable CSI driver |
   | 5 | credentials Secret | created from a `0600` `mktemp` via `--from-env-file` when absent; when present, **verified and never rewritten** — drift reported by key name, then stop |
   | 6 | post-install readiness | DataHub and LLM blocks complete (each stops the run; `--skip-postinstall-check` overrides); the Kafka tuple only when `event-consumer.enabled` resolves true; Langfuse warns only |
   | 7 | image tag | explicit `--image-tag`, else `git rev-parse --short HEAD` on a clean tree (`--allow-dirty` overrides); never a mutable tag |

   Stages 1 and 2 precede populate on purpose: populate *reads* the cluster to adopt, so the wrong context would copy another deployment's credentials into the env file, and the Secret it adopts from is the one the overlay names.

   **The Secret's content contract** (`verify_credential_secret` in `bin/lib/helpers.sh`, applied by both scripts — authoritative table: `README.md §2`):
   - All 11 keys present and non-empty: `DATASPOKE_POSTGRES_PASSWORD`, `DATASPOKE_REDIS_PASSWORD`, `DATASPOKE_AIRFLOW_{USER,PASSWORD}`, `DATASPOKE_AIRFLOW_{WEBSERVER_SECRET_KEY,JWT_SECRET}`, `DATASPOKE_AIRFLOW_FERNET_KEY`, `DATASPOKE_INTERNAL_TOKEN`, `DATASPOKE_JWT_SECRET_KEY`, `DATASPOKE_OAUTH_STATE_SECRET`, `DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET`.
   - `DATASPOKE_AIRFLOW_FERNET_KEY` is URL-safe base64 of exactly 32 raw bytes (43 chars then `=`) — a hex value is rejected, because Fernet cannot decode it but nothing notices until first use.
   - No known-bad literal: `DATASPOKE_JWT_SECRET_KEY` equal to the dev default, `DATASPOKE_AIRFLOW_USER` equal to `admin` or outside `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`, `DATASPOKE_AIRFLOW_PASSWORD` equal to `admin` (conditional on the effective `simple_auth_manager_all_admins`), `DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET` starting with `placeholder-`.
   - No `DATASPOKE_POSTGRES_USER` / `DATASPOKE_POSTGRES_DB` — both belong to the app ConfigMap (`config.postgres.*`); their presence means stale instructions were followed.
   - Audits report key **lengths** and verdicts, never values.

   **`--skip-secret`** is for operators delivering the eleven keys through ExternalSecrets, Vault or SealedSecrets. Every other stage still runs, and the content contract is identical either way — which is what keeps them on the same validated path. Under it, blank credential lines are left alone rather than generated: minting eleven values the deployment will never use would put fresh production credentials on disk for nothing, and would make the stage-5 drift check report all eleven the moment the external system materialises the Secret.

   **Never hand-roll `kubectl create secret` on the operator's behalf, and never `--from-literal`** — the latter leaks every value into shell history and into `ps auxww` / `/proc/<pid>/cmdline` for the process's lifetime. The pre-flight builds it from a mode-0600 `mktemp` env file via `--from-env-file` and removes the file the moment `kubectl` returns. If the operator wants the manual equivalent, point at `README.md §2`; do not run it for them, and do not put Secret values anywhere but the env file the pre-flight owns.

   `--verify-only` runs every stage and performs none of the three mutations, so it is the safe way to inspect a live prod deployment.
5. If any check fails, report clearly and stop. Relay the pre-flight's own message — it names the stage, the key or class at fault, and the remedy.

### Full install (all components for the profile)

1. Execute the top-level install script **in the background**:
   - dev: `./helm-charts/bin/install.sh --profile dev`
   - prod: **run the exact command the pre-flight printed** after its `Install with:` line — `./helm-charts/bin/install.sh --profile prod --image-tag <tag> --values <operator-overlay>`. **Do not invent the image tag.** The pre-flight derived it from `git rev-parse --short HEAD` and refuses a dirty work tree for it, so the tag names a commit that actually contains what will be built; a tag typed from memory reads as provenance it does not have. Never `dev` or any other mutable tag — `install.sh` requires an explicit `--image-tag` in prod for that reason. **Leave `--env-file` off**: `--profile prod` resolves `helm-charts/.env.prod` on its own, and naming it explicitly is one more place a second file can be named by mistake (the pre-flight adds it to the printed command only when it ran against a non-default env file). **Never pass an `.example` path as `--env-file`** — those are tracked copy sources, not live config.
   - **Frontend** (`--frontend none|local|cluster`, default `none` in dev / `cluster` in prod): pass `--frontend local` for the host-`pnpm dev` workflow (writes `src/frontend/.env.local`), `--frontend cluster` to deploy the containerised UI in-cluster, or `--frontend none` for API-only. `local` is dev-only. The install summary prints the resulting Web UI URL + default `dataspoke@dataspoke.local / dataspoke` login. If the user hasn't said, ask which frontend mode they want (or default per profile).
   - Note the background task ID and output file path.
2. While the script runs, **alternate between two monitoring sources every ~30 seconds**:
   a. **Script output**: read the background task output file (e.g., `tail -20 <output-file>`) to report install progress messages.
   b. **Cluster state**: invoke the `/k8s-work` skill to get live pod/Helm status across all namespaces.
   - After each round, summarize what changed since the last check.
   - If a pod enters `CrashLoopBackOff`, `OOMKilled`, or `Error`, report it immediately and show recent logs.
3. Continue until the background script exits (exit code 0 = success, non-zero = failure) **and** all expected pods are `Running`/`Ready`.

### Partial install (specific components)

1. **Resuming an interrupted full install** (starting component plus every component after it in dependency order): prefer `./helm-charts/bin/install.sh --profile dev --from-component <name>` — it inherits the orchestrator's step markers, error handling, and final summary.
2. **Installing one or a few specific components**: `./helm-charts/bin/install.sh --profile dev --components <csv>`. Honors phase ordering automatically.
3. **Rebuild and redeploy the API only** (code-iteration path): `./helm-charts/bin/install.sh --profile dev --components api`. This rebuilds the API image, runs helm upgrade, and rolls the deployment.
4. **Rebuild and redeploy the frontend only** (code-iteration path): `./helm-charts/bin/install.sh --profile dev --components frontend`. The dev umbrella keeps `frontend.enabled=false` (developers run host `pnpm dev` at `src/frontend`); this fast path builds the frontend image and helm-upgrades with `frontend.enabled=true` to deploy the containerised UI in-cluster (verification / prod-parity). Stop it with `kubectl scale deployment/dataspoke-frontend --replicas=0 -n "${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"`.
5. Monitor with `/k8s-work` after each component completes.

### Post-install (dev)

1. Confirm all expected components are running.
2. Seed dummy data and register datasets in DataHub:
   ```bash
   uv run python -m tests.integration.util --reset-seed
   ```
3. Show access information (ingress endpoints table is in `helm-charts/README.md §Ingress Endpoints`; substitute `DATASPOKE_KUBE_INGRESS_IP` / `DATASPOKE_KUBE_INGRESS_DOMAIN` from `helm-charts/.env.dev`). In **shared** ingress mode `INGRESS_IP` is blank — HTTP services ride the operator-set `DATASPOKE_KUBE_INGRESS_DOMAIN`, and TCP services (Postgres/Redis/Kafka/lock) are reached on `127.0.0.1` by running `./helm-charts/bin/port-forward.sh` in a separate shell held open for the session.
4. Inform the user that `helm-charts/.env.dev` has been populated with runtime variables (hosts, URLs, ports — including `DATASPOKE_DEV_LOCK_URL` and `DATASPOKE_DEV_DUMMY_DATA_POSTGRES_HOST_PORT`) by `install.sh` (managed mode also derives ingress IP/domain in `dev-peripherals/nginx-ingress.sh`), and that they should run `source helm-charts/.env.dev` to load them into their shell.
5. The post-install seeding step (dev profile) has already wired DataHub + Langfuse + LLM provider/model into the API's runtime config via `/internal/admin/peripherals/*` and `/internal/admin/conf` — no manual admin-API calls needed unless `--skip-seed` was set.

### Post-install (prod)

None of the dev steps above apply — prod installs no peripherals, seeds no dummy data, and auto-wires no runtime config. Authoritative sequence: `helm-charts/README.md §5–§7`.

1. **Confirm the admin credential is not the published default.** Unless `--skip-seed` was passed, the install seeded `dataspoke@dataspoke.local` / `dataspoke` (`SKIP_SEED` defaults to `false`, so this runs on every prod install), and rotated it to `DATASPOKE_PROD_ADMIN_PASSWORD` when that input was set. **Read the admin seed's own outcome line** in the install output (the one-word verdict is internal to the script — it is translated into prose before it is printed, so do not grep for `ROTATED`). Accept only the two `[INFO]` lines: `Rotated 'dataspoke@dataspoke.local' to DATASPOKE_PROD_ADMIN_PASSWORD …` or `'dataspoke@dataspoke.local' already authenticates with DATASPOKE_PROD_ADMIN_PASSWORD …`. A `[WARN]` (the account accepts neither password — someone rotated it to a third value) or a blank `DATASPOKE_PROD_ADMIN_PASSWORD` leaves the install finishing normally with the published credential possibly still live. A red `[ERROR]` from the seed means **the install itself exited non-zero** — the seed's failure branches call `error()`, and `install.sh` runs the seed unguarded under `set -euo pipefail`, so the chart is installed but the completion summary never printed; report a failed install, not a completed one. The default credential is published in this repository and authenticates against the API for anyone who can reach it — how reachable depends on the operator's ingress controller and network posture, since the chart applies no source-range or auth gating on the API ingress. When it may still be live, walk the user through the `PATCH /api/v1/auth/me` recipe in `README.md §5`, which keeps the new password out of shell history and argv (`read -s` plus a heredoc into `-d @-`). Treat it as urgent if the controller is shared or internet-reachable. Do not report the install as complete until one of the two `[INFO]` lines is present, or the user explicitly declines to rotate.
2. If the install used `--skip-seed`, seed manually when ready: `ENV_FILE=helm-charts/.env.prod bash helm-charts/bin/post-install/seed-admin-user.sh`. The `ENV_FILE=` prefix is required (the script defaults to `.env.dev`). The same run rotates and verifies when `DATASPOKE_PROD_ADMIN_PASSWORD` is set, and is idempotent because it tries the target password first. The address is not configurable — `PATCH /auth/me` sets name and password only.
3. **Seed peripherals and runtime config from the same env file:**

   ```bash
   ENV_FILE=helm-charts/.env.prod bash helm-charts/bin/post-install/seed-peripheral-config.sh
   ENV_FILE=helm-charts/.env.prod bash helm-charts/bin/post-install/seed-runtime-config.sh
   ```

   **Both default to `.env.dev`.** An unprefixed run against a prod cluster PATCHes dev addresses into prod's config — never omit the `ENV_FILE=` prefix. Each script picks its profile from the prefix the named file declares (`DATASPOKE_PROD_*` vs `DATASPOKE_DEV_*`), so there is no profile flag; a file declaring both is ambiguous and aborts the seed.

   **`dataspoke-datahub-secret`, `dataspoke-langfuse-secret` and `dataspoke-llm-secret` need no pre-creating.** The DataHub PAT, the Kafka SASL password, the Langfuse secret key and the LLM API key ride inside these PATCHes and the API creates each Secret on the first one. An operator whose secrets manager already owns one keeps it: the accessors are create-or-patch with a strategic merge on `data`, so sibling keys survive. Both seeds are re-runnable — an absent or empty value is "leave unchanged", never a clearing write.

   **A `stub_*` flag true on a prod deployment means the dev path was run against it.** The prod seed path sets no `stub_*` flag at all. Check with `GET /api/v1/admin/conf` if a prod deployment behaves like a stub.

   SMTP is the one remaining `curl` (`PATCH /api/v1/admin/peripherals/smtp`) — it is deliberately not carried in the env file. Do not hand-assemble the other four JSON bodies: that is exactly where invented placeholder values and malformed-PAT JSON come from.
4. Confirm all expected components are running, and report access URLs from the overlay's ingress hosts (`app.`, `api.`, `airflow.`).

---

## Action: uninstall

### Show current state

1. Show what is currently deployed:
   - `helm list --all-namespaces`
   - `kubectl get pods -n <namespace>` for each namespace from `.env`
   - `kubectl get pvc -n <namespace>`
2. **Ask the user to confirm** they want to remove resources before proceeding.

### Full uninstall

1. **Ask the user** whether to also delete PVCs and namespaces (both default to preserved).
2. Execute the uninstall script with flags:
   - Always pass `--no-question` (user already confirmed).
   - dev — full wipe (PVCs + namespaces):
     `./helm-charts/bin/uninstall.sh --profile dev --no-question --delete-all`
   - dev — release-only (preserve PVCs and namespaces):
     `./helm-charts/bin/uninstall.sh --profile dev --no-question`
   - dev — mix-and-match with `--delete-pvcs` and/or `--delete-namespaces` for partial wipes.
   - **prod** — `--delete-pvcs` is dev-only and does not apply. Namespace deletion is prod's only full wipe:
     `./helm-charts/bin/uninstall.sh --profile prod --no-question --delete-namespaces`
     The flagless form uninstalls the release and chart-derived Secrets only. The operator-owned credential Secret is never deleted by the script. Before removing anything by hand, read `README.md §"Prod: what survives an uninstall"` — the coupling is whole-Secret, not per-key: `DATASPOKE_AIRFLOW_FERNET_KEY` decrypts the Airflow connections and Variables in the retained Postgres PVC, so the credentials Secret and the PVCs must be kept or dropped together, never one without the other.
3. **Prod pre-teardown check — the stranding risk is now conditional on the env file.** Once `helm-charts/.env.prod` holds all eleven `DATASPOKE_PROD_*` credential inputs, `install-prod-preflight.sh` rebuilds a deleted Secret byte-identically, Fernet key included, so it is recoverable. That **relocates** the risk rather than removing it: the env file becomes the thing to protect, and the old coupling reapplies in full if it is lost or if its Fernet line was never populated. So before agreeing to delete a Secret, a PVC or a namespace, **confirm all eleven keys are set — names and set/blank verdicts only, never values** (rule 1 under `Action: configure`):

   ```bash
   awk -F= '/^DATASPOKE_PROD_/ {printf "%-58s %s\n", $1, (length($2)>0 ? "SET" : "blank")}' helm-charts/.env.prod
   ```

   The eleven that matter are `..._POSTGRES_PASSWORD`, `..._REDIS_PASSWORD`, `..._AIRFLOW_{USER,PASSWORD,WEBSERVER_SECRET_KEY,JWT_SECRET,FERNET_KEY}`, `..._INTERNAL_TOKEN`, `..._JWT_SECRET_KEY`, `..._OAUTH_STATE_SECRET`, `..._GOOGLE_OAUTH_CLIENT_SECRET`. Any of those `blank` exists only in the cluster — recover it first by running the pre-flight, whose stage 3 adopts from the live Secret.
4. **Clean up orphaned PersistentVolumes, and report both layers.** A PVC deleted under a `Retain`-reclaimPolicy StorageClass leaves the PV behind in `Released` and the cloud volume behind it fully intact — a `Delete` policy destroys both in the same action. Report what is left at each layer, because deleting the PV object does not delete the disk:

   ```bash
   kubectl get pv -o custom-columns='NAME:.metadata.name,STATUS:.status.phase,RECLAIM:.spec.persistentVolumeReclaimPolicy,CLAIM:.spec.claimRef.name,VOLUME:.spec.csi.volumeHandle'
   ```

   `spec.csi.volumeHandle` is the cloud provider's own volume id — the thing that keeps costing money and keeps holding the credential store (Postgres password hashes, `api_tokens`, Fernet-encrypted ingestion secrets) and Redis's AOF after the cluster forgets about it. For a complete decommission it must be deleted in the cloud console/CLI, or its disk-encryption key destroyed. Never delete cloud volumes on the user's behalf; list them and let the user decide.

### Partial uninstall (specific components)

`uninstall.sh --components frontend` is the one supported targeted teardown:
it runs `helm upgrade --reuse-values --set frontend.enabled=false` on the
`dataspoke` release (removes the UI Deployment/Service/Ingress, leaves
everything else). `--components api` is rejected — the api subchart is the core
service; to stop it temporarily use `kubectl scale deployment/dataspoke-api
--replicas=0`. For any other single component, delete its Helm release (or
manifests) directly with `helm uninstall <release> -n <ns>` or `kubectl delete
-f ...`, then reinstall via `install.sh --components <csv>`. Do NOT delete
namespaces during partial uninstall.

### Post-uninstall

1. Confirm cleanup with `/k8s-work`.
2. Report the clean state — and for prod, report what deliberately survives: the retained PVCs, the operator-owned credentials Secret, the out-of-band `dataspoke-{llm,datahub,langfuse,smtp}-secret` objects, and any `Released` PV plus the cloud volume behind its `spec.csi.volumeHandle` (step 4 above). `README.md §"Prod: what survives an uninstall"` is the authoritative list.
3. For prod, restate the recovery position: with all eleven keys set in `helm-charts/.env.prod`, a re-install is `install-prod-preflight.sh` (which rebuilds the Secret byte-identically) then `install.sh --profile prod`. With any of them blank and the Secret gone, the retained PVCs are stranded.

---

## Action: reinstall

There is no dedicated `reinstall.sh`, and `uninstall.sh` does not support `--components`. Reinstall by deleting the target component's Helm release / manifests directly, then re-running `install.sh --components <name>` — `install.sh` is idempotent and `helm upgrade --install` handles re-creation.

**Prod has no per-component reinstall.** `--components` and `--from-component` are dev-only flags that `install.sh` rejects under `--profile prod`. A prod fix is a full cycle: `install-prod-preflight.sh --values <overlay.yaml>` then `install.sh --profile prod --image-tag <tag> --values <overlay.yaml>`. Both are idempotent, and the pre-flight verifies the existing Secret rather than rewriting it, so re-running is safe.

### Steps

1. Parse `$ARGUMENTS` to identify the target component (see the Component names table above). If no component is specified, ask the user which to reinstall.
2. Delete the component's existing release (look up the release/manifest set first — e.g. `helm list -A` or check the peripheral script for the manifest source), then re-run install:
   ```bash
   # Example: reinstall the dataspoke umbrella chart
   helm uninstall dataspoke -n "${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"
   ./helm-charts/bin/install.sh --profile dev --components dataspoke-infra
   ```
   For peripherals managed by plain manifests (dev-lock, dummy-data), `kubectl delete -f helm-charts/dev-peripherals/<name>/manifests/` before reinstalling.
3. Monitor output for errors. If teardown or rollout fails, report the error and suggest remediation.
4. On success, confirm the component is running and report access URLs.

---

## Action: health-check

1. Run `./helm-charts/bin/health-check.sh --profile <dev|prod>` and report the results. **Always pass the profile.** With no `--profile` and no `--env-file`, the script falls back to `.env.dev` — a prod operator who runs it bare gets a confident verdict for a different deployment. `--env-file helm-charts/.env.prod` is the equivalent. Either way the script echoes the resolved env file and ingress domain before its first probe; check that line matches the deployment you meant.
2. Supported flags:
   - `--profile {dev|prod}` — resolve `helm-charts/.env.<profile>` the way `install.sh` does
   - `--env-file <path>` — explicit env file; outranks `--profile`
   - `--quick` — TCP-only checks (skip deep application-layer probes)
   - `--keep-lock` — don't touch an existing dev-env lock
   - `--force-release` — release a held lock without prompting
3. The DataHub, dummy-data and dev-lock sections have no prod counterpart — they are dev-only peripherals, so they report unreachable against a prod deployment rather than being skipped. Say so rather than reporting them as failures. **On `--profile prod` this means the script can never exit 0**: those sections run unconditionally, so a healthy prod deployment still ends with `N service(s) unhealthy`, the dev-only reinstall hint below, and exit 1. Both the non-zero exit and that hint are expected on prod and are not a verdict on the deployment — judge it on the `DataSpoke Infra` section alone. **Pass `--quick` on prod**: the deep Langfuse probe reads `DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE`, absent from a prod env file, and the script runs under `set -u`, so a full prod run stops at that probe before the summary. If a prod run ends with `DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE: unbound variable`, that is this — read the DataSpoke Infra lines it printed first, and re-run with `--quick`.
4. If any service is unhealthy in **dev**, show the reinstall command:
   `./helm-charts/bin/install.sh --profile dev --components <name>` (component names per the table above). In **prod** there is no per-component reinstall — the fix is a full pre-flight-plus-install cycle (see `Action: reinstall`) — and the script's own hint names the dev command regardless of profile, so do not relay it under prod.
5. For deeper cluster-level diagnostics, invoke the `/k8s-work` skill.

---

## Action: run-api

Rebuild the DataSpoke API Docker image, redeploy it via `helm upgrade`, and roll the API deployment. This is the **code-iteration path** for the backend. (For the frontend equivalent, use `--components frontend` — see Partial install above; most frontend iteration happens via host `pnpm dev` and only needs an in-cluster deploy for prod-parity verification.) The API is accessible via nginx-ingress — no port-forward needed. Airflow callbacks reach it via cluster DNS (`http://dataspoke-api:8002`). Stub-mode toggles are in `runtime_config` (`stub_redis_client`, `stub_llm_client`, `stub_pgvector_manager`, `stub_notification_service`); the dev-profile install seeds them all to `true` post-install, flippable via `PATCH /api/v1/admin/conf`.

### Pre-flight

1. Verify `helm-charts/.env.dev` exists. If not, run **configure** first. This action is dev-only — its deploy command is `--components api`, which `install.sh` rejects under `--profile prod`.
2. If the user requests it (or `--health-check` flag), run `./helm-charts/bin/health-check.sh --profile dev --quick` to confirm infrastructure is reachable. If it fails, suggest `/k8s-deploy health-check` or `/k8s-deploy install` and stop.
3. Run `uv sync` to ensure Python dependencies are up to date.

### Option parsing

Parse `$ARGUMENTS` and the user's request for these options:

| Option | CLI flag | Default | Description |
|--------|----------|---------|-------------|
| `skip-build` | `--skip-build` | off | Skip Docker build, deploy existing image only |
| `image-tag` | `--image-tag <tag>` | `dev` | Override the image tag (CI-built images) |
| `health-check` | (runs `./helm-charts/bin/health-check.sh --profile dev --quick` first) | off | Pre-flight infrastructure check |
| `stop` | (no flag — see Stop section) | off | Scale down the API deployment |

### Deploy

1. Run `./helm-charts/bin/install.sh --profile dev --components api` with parsed flags in the foreground. The script builds the API image (via `helm-charts/bin/build-image.sh api`), runs `helm upgrade --install`, and rolls the API deployment to pick up the new `:dev` image. First run can take 5–10 minutes due to image builds; `--skip-build` rebuilds are 1–2 minutes.
2. Monitor the output for errors. If the build or rollout fails, report the error and suggest remediation.
3. On success, report the running state to the user:
   - API URL: `${DATASPOKE_KUBE_INGRESS_SCHEME:-http}://api.${DATASPOKE_KUBE_INGRESS_DOMAIN}/api/v1/`
   - ReDoc UI: `${DATASPOKE_KUBE_INGRESS_SCHEME:-http}://api.${DATASPOKE_KUBE_INGRESS_DOMAIN}/redoc`
   - Health: `${DATASPOKE_KUBE_INGRESS_SCHEME:-http}://api.${DATASPOKE_KUBE_INGRESS_DOMAIN}/health`
   - How to run tests: `set -a && source helm-charts/.env.dev && set +a && uv run pytest tests/integration/spot/` (spot) or `… tests/integration/api_wired/` (UC user stories) — run in separate invocations. The conftest `runtime_conf` fixture verifies the API has `stub_redis_client / stub_pgvector_manager / stub_notification_service` all `true` before the suite runs.
   - How to stop: `kubectl scale deployment/dataspoke-api --replicas=0 -n "${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"`

### Stop

If the user asks to stop:

1. Run `kubectl scale deployment/dataspoke-api --replicas=0 -n "${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"`.
2. Confirm the deployment has been scaled down.
