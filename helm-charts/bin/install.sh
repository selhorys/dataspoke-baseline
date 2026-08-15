#!/usr/bin/env bash
# DataSpoke installation entry point.
#
# Usage: install.sh --profile {dev|prod} [OPTIONS]
#
# OPTIONS
#   --env-file <path>           Path to the env file (default: helm-charts/.env.<PROFILE>).
#   --components <csv>          (dev only) Subset of components to install (default: all-for-profile).
#                               Names: nginx-ingress, datahub, langfuse, dataspoke-infra,
#                                      api, frontend, dummy-data, dev-lock, seed
#   --from-component <n>        (dev only) Resume an interrupted full install at <n>.
#   --frontend none|local|cluster
#                               Frontend deployment mode. Controls whether the Next.js
#                               frontend is deployed and how developers access it.
#                               none    — do not deploy; no image build. (dev default)
#                               local   — do not deploy; write src/frontend/.env.local
#                                         so `pnpm dev` points at the in-cluster API.
#                                         (dev only)
#                               cluster — build image and deploy in-cluster via Helm.
#                                         (prod default; also available in dev)
#   --skip-build                Skip Docker image rebuilds (api/airflow/postgres/frontend).
#   --skip-seed                 Skip post-install admin-API seeding (both profiles).
#   --values <path>             Extra values file for the umbrella chart (prod, single use).
#   --image-tag <tag>           Override image tag (default: dev).
#   --no-digest-pin             Skip image-digest resolution entirely for the three
#                               DataSpoke-owned workloads (api, event-consumer,
#                               frontend): render each one's image as the mutable
#                               `repo:tag` reference, force its image.pullPolicy to
#                               Always (so the substituted rollout restart below
#                               actually re-pulls instead of reusing a cached tag),
#                               and, after the umbrella helm upgrade, unconditionally
#                               issue an explicit rollout restart of dataspoke-api,
#                               dataspoke-event-consumer, and dataspoke-frontend (the
#                               last only when it is actually deployed). postgresql
#                               and airflow are never digest-stamped regardless of
#                               this flag. The explicit, operator-chosen escape hatch
#                               from digest pinning — see
#                               spec/feature/HELM_CHART.md §Digest stamping.
#   --help, -h                  Print this usage message.
#
# The --components api path rebuilds the API image, runs helm upgrade, and
# waits for rollout.
# The --components frontend path builds the frontend image, runs helm upgrade
# with frontend.enabled=true, and waits for rollout. In dev the default install
# keeps frontend.enabled=false (host pnpm dev); --components frontend explicitly
# deploys the containerised frontend in-cluster for verification.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELM_CHARTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$HELM_CHARTS_DIR/.." && pwd)"
CHART_DIR="$HELM_CHARTS_DIR/dataspoke"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
# shellcheck source=lib/helpers.sh
source "$SCRIPT_DIR/lib/helpers.sh"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
PROFILE=""
ENV_FILE_ARG=""
COMPONENTS_CSV=""
FROM_COMPONENT=""
FRONTEND_MODE=""
SKIP_BUILD=false
SKIP_SEED=false
EXTRA_VALUES=""
IMAGE_TAG="dev"
IMAGE_TAG_EXPLICIT=false
NO_DIGEST_PIN=false

# Resolved once per install (see resolve_image_digest in lib/helpers.sh) and
# read by _api_image_helm_set_args / _frontend_helm_set_args to pin the image
# reference itself to `<repository>@sha256:...` (via the `dataspoke.imageRef`
# named template, api.image.digest / frontend.image.digest /
# event-consumer.image.digest) and to stamp the identical value as the
# dataspoke.io/image-digest pod annotation (provenance only — nothing in this
# script reads it back) on every workload running that image. A non-empty
# digest means the pod-template hash changes exactly when the digest changes,
# so Helm rolls the workload by construction. `--no-digest-pin` skips
# resolution entirely, leaving both variables empty; every call site then
# also forces that workload's image.pullPolicy to Always (see
# _api_image_helm_set_args / the prod frontend --set args below) — every
# chart's pullPolicy defaults to IfNotPresent, and a bare `rollout restart`
# does not force a re-pull, so without this a node with the reused tag
# already cached would keep serving stale layers while every readiness wait
# still reports success — and issues an explicit rollout restart after the
# umbrella helm upgrade instead (see _rollout_restart_workload's call sites
# below). `set -u` safety: both start empty so an unresolved digest reads as
# "" rather than an unbound-variable error.
API_IMAGE_DIGEST=""
FRONTEND_IMAGE_DIGEST=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)         PROFILE="${2:-}"; shift 2 ;;
    --env-file)        ENV_FILE_ARG="${2:-}"; shift 2 ;;
    --components)      COMPONENTS_CSV="${2:-}"; shift 2 ;;
    --from-component)  FROM_COMPONENT="${2:-}"; shift 2 ;;
    --frontend)        FRONTEND_MODE="${2:-}"; shift 2 ;;
    --skip-build)      SKIP_BUILD=true; shift ;;
    --skip-seed)       SKIP_SEED=true; shift ;;
    --no-digest-pin)   NO_DIGEST_PIN=true; shift ;;
    --values)
      if [[ -n "${EXTRA_VALUES}" ]]; then
        error "--values may only be given once; it takes exactly one overlay file (unlike helm's repeatable -f). Merge multiple overlays into one file first."
      fi
      EXTRA_VALUES="${2:-}"
      # Checked here, not deferred to Phase 3: the Phase 1 pre-flight (prod)
      # reads this overlay for pinned StorageClasses and secrets.existingSecret
      # via `-f "${EXTRA_VALUES}"`-guarded calls. A typo'd path would silently
      # skip both — falling back to the unchecked cluster default StorageClass
      # and the literal `dataspoke-secrets` name — and only fail later, deep
      # into resource creation. The downstream `-f` guards become belt-and-
      # braces once this fires first.
      [[ -f "${EXTRA_VALUES}" ]] || error "Extra values file not found: ${EXTRA_VALUES}"
      shift 2 ;;
    --image-tag)       IMAGE_TAG="${2:-dev}"; IMAGE_TAG_EXPLICIT=true; shift 2 ;;
    --help|-h) print_usage; exit 0 ;;
    *) error "Unknown option: $1 (use --help)" ;;
  esac
done

if [[ -z "$PROFILE" ]]; then
  error "--profile {dev|prod} is required. Use --help for usage."
fi
if [[ "$PROFILE" != "dev" && "$PROFILE" != "prod" ]]; then
  error "Invalid profile '${PROFILE}'. Must be 'dev' or 'prod'."
fi

# IMAGE_TAG flows into several `--set`/`--set-string` tokens below (e.g.
# `api.image.tag=${IMAGE_TAG}`) and into image references passed to
# build-image.sh / resolve_image_digest. The grammar is assert_image_tag's in
# lib/helpers.sh — shared with the standalone pre-flight, which derives a tag
# and prints the install command carrying it, so it cannot hand over one this
# line then rejects.
assert_image_tag "$IMAGE_TAG"

# --components / --from-component (single/subset-component reinstall and
# resume) are dev-only fast paths — the prod branch below has no per-component
# dispatch and always runs a full install regardless of either flag. Hard
# error rather than warn-and-proceed: a caller passing either flag on
# --profile prod out of dev habit expects a narrow, single-component action
# (e.g. rebuild just the API) and silently running the COMPLETE prod install
# instead — full Secret derivation, every Ingress's class override, the whole
# umbrella upgrade, the admin seed — is "do more than the operator asked",
# the same failure direction the repeated-`--values` check above already
# hard-errors on.
if [[ "$PROFILE" == "prod" && -n "$COMPONENTS_CSV" ]]; then
  error "--components is dev-only; --profile prod always runs a full install. Re-run without --components '${COMPONENTS_CSV}', or use --profile dev if that is what you meant."
fi
if [[ "$PROFILE" == "prod" && -n "$FROM_COMPONENT" ]]; then
  error "--from-component is dev-only; --profile prod always runs a full install. Re-run without --from-component '${FROM_COMPONENT}', or use --profile dev if that is what you meant."
fi

# Apply per-profile defaults for FRONTEND_MODE
if [[ -z "$FRONTEND_MODE" ]]; then
  if [[ "$PROFILE" == "dev" ]]; then
    FRONTEND_MODE="none"
  else
    FRONTEND_MODE="cluster"
  fi
fi
# Validate FRONTEND_MODE
if [[ "$FRONTEND_MODE" != "none" && "$FRONTEND_MODE" != "local" && "$FRONTEND_MODE" != "cluster" ]]; then
  error "Invalid --frontend '${FRONTEND_MODE}'. Must be none|local|cluster."
fi
if [[ "$FRONTEND_MODE" == "local" && "$PROFILE" == "prod" ]]; then
  error "--frontend local is dev-only (no localhost story for prod)."
fi

# Resolve env file: explicit --env-file wins; otherwise profile-aware default.
ENV_FILE="${ENV_FILE_ARG:-$HELM_CHARTS_DIR/.env.$PROFILE}"
export ENV_FILE

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  error "Env file not found at $ENV_FILE — copy helm-charts/.env.${PROFILE}.example (or .env.dev.example for dev) and edit it."
fi
source "$ENV_FILE"
# Harden permissions immediately — the file may have been created via cp or
# an editor that inherits a permissive umask.
chmod 600 "$ENV_FILE" 2>/dev/null || true

# Every *_NAMESPACE var below is interpolated into `kubectl apply -f -` YAML
# documents throughout this script (metadata.name / metadata.namespace), so an
# unvalidated value could inject an arbitrary extra manifest. The grammar is
# assert_k8s_namespace's in lib/helpers.sh, shared with the standalone
# pre-flight's --namespace so the two entry points cannot disagree about which
# names are legal. Checked once here rather than per call site, mirroring
# ingress_class()/ingress_tls_secret() in lib/helpers.sh. An unset variable is
# not this wrapper's business — the consumers below decide which of them are
# required for the profile in play.
_validate_namespace_var() {
  local var_name="$1"
  local val="${!var_name:-}"
  [[ -z "$val" ]] && return 0
  assert_k8s_namespace "${var_name}" "$val" || exit 1
}
_validate_namespace_var DATASPOKE_KUBE_DATASPOKE_NAMESPACE
_validate_namespace_var DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE
_validate_namespace_var DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE
_validate_namespace_var DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE

START_TIME=$SECONDS
export START_TIME

echo ""
echo "=== DataSpoke installation (profile: ${PROFILE}) ==="
echo ""

# ---------------------------------------------------------------------------
# Pre-flight: required tools
# ---------------------------------------------------------------------------
info "Checking required tools..."
require_tools kubectl helm python3
info "kubectl, helm, and python3 are available."

# ---------------------------------------------------------------------------
# Per-install tempdir for background task logs (0700, cleaned on exit)
# ---------------------------------------------------------------------------
INSTALL_TMPDIR="$(mktemp -d -t dataspoke-install.XXXX)"
chmod 700 "$INSTALL_TMPDIR"
trap 'rm -rf "${INSTALL_TMPDIR}"' EXIT

# ---------------------------------------------------------------------------
# Airflow SimpleAuthManager passwords-file path (prod only)
# ---------------------------------------------------------------------------
# Materialised by the prod-only init container
# (_build_airflow_simple_auth_init_container_file) and read by Airflow itself
# via AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE
# (_build_airflow_extra_env_file). Kept as one constant so the writer and the
# reader of "the same path" cannot drift apart.
AIRFLOW_SIMPLE_AUTH_PASSWORDS_DIR="/opt/airflow/simple-auth-manager"
AIRFLOW_SIMPLE_AUTH_PASSWORDS_FILE="${AIRFLOW_SIMPLE_AUTH_PASSWORDS_DIR}/passwords.json"

# ---------------------------------------------------------------------------
# Shared helpers (used by both profile branches)
# ---------------------------------------------------------------------------
PIDS=()
LABELS=()

_run_bg() {
  local label="$1"; shift
  ( "$@" > "${INSTALL_TMPDIR}/${label//\//-}.log" 2>&1 ) &
  PIDS+=($!)
  LABELS+=("$label")
  info "  Started background task: $label (pid $!)"
}

_wait_all() {
  # Report each task the moment it finishes, and print a heartbeat naming what is
  # still outstanding while we wait.
  #
  # Waiting on the PIDs in array order and printing only at the end made this
  # phase silent for its whole duration — and it is the longest phase in the
  # install (image builds plus DataHub plus Langfuse, each of which can wait
  # minutes on cluster scheduling). Each task's output goes to its own log and was
  # shown only on failure, so a healthy slow run and a hung one produced identical
  # output: nothing. In-order waiting made it worse, since a task that finished
  # early stayed unreported until every task ahead of it in the array completed.
  local failed=0 remaining=${#PIDS[@]} waited=0
  local -a done_flags=()
  local i pid label rc heartbeat=0
  for i in "${!PIDS[@]}"; do done_flags[i]=0; done

  while (( remaining > 0 )); do
    for i in "${!PIDS[@]}"; do
      (( done_flags[i] )) && continue
      pid="${PIDS[$i]}"
      label="${LABELS[$i]}"
      # Non-blocking reap: kill -0 fails once the process is gone, and only then
      # does `wait` return its real exit status instead of blocking.
      if ! kill -0 "$pid" 2>/dev/null; then
        rc=0
        wait "$pid" || rc=$?
        done_flags[i]=1
        remaining=$(( remaining - 1 ))
        if (( rc == 0 )); then
          info "  [OK] $label (t+$((SECONDS - START_TIME))s)"
        else
          warn "  [FAIL] $label (exit $rc, t+$((SECONDS - START_TIME))s)"
          cat "${INSTALL_TMPDIR}/${label//\//-}.log" >&2 || true
          failed=$(( failed + 1 ))
        fi
      fi
    done
    (( remaining == 0 )) && break
    sleep 5
    waited=$(( waited + 5 ))
    # Heartbeat every 30s: which tasks are still running, and the last line each
    # one logged, so a stuck task is distinguishable from a slow one.
    if (( waited - heartbeat >= 30 )); then
      heartbeat=$waited
      local still=""
      for i in "${!PIDS[@]}"; do
        (( done_flags[i] )) || still+=" ${LABELS[$i]}"
      done
      info "  ... still running:${still} (${waited}s elapsed in this phase)"
      for i in "${!PIDS[@]}"; do
        (( done_flags[i] )) && continue
        local logf="${INSTALL_TMPDIR}/${LABELS[$i]//\//-}.log"
        [[ -s "$logf" ]] && info "      [${LABELS[$i]}] $(tail -1 "$logf" | tr -d '\r' | cut -c1-120)"
      done
    fi
  done

  PIDS=()
  LABELS=()
  if (( failed > 0 )); then
    error "${failed} background task(s) failed — see output above."
  fi
}

# _build_chart_deps <chart_dir>
# `helm dependency build` does not re-package an unchanged-version file://
# dependency, so edits to a local subchart's templates would otherwise ship a
# stale package. Drop the local subchart archives first so the build re-packages
# them from the current source. This forces a full re-resolve, so the remote OCI
# deps (bitnami postgresql/redis) get re-pulled from Docker's CDN — a fetch that
# intermittently resets the connection. Retry the build to ride out those
# transient resets rather than failing the whole install on one bad pull.
_build_chart_deps() {
  local chart_dir="$1"
  rm -f "${chart_dir}"/charts/frontend-*.tgz "${chart_dir}"/charts/event-consumer-*.tgz
  local attempt
  for attempt in 1 2 3 4 5; do
    if helm dependency build "${chart_dir}"; then
      return 0
    fi
    warn "  helm dependency build failed (attempt ${attempt}/5) — retrying in 5s..."
    sleep 5
  done
  error "helm dependency build for '${chart_dir}' failed after 5 attempts."
}

# ---------------------------------------------------------------------------
# Secret management helpers
# ---------------------------------------------------------------------------

# _ensure_fernet_key_joins_credentials_secret <namespace> <secret_name>
# Dev-only self-heal for a credentials Secret that predates
# DATASPOKE_AIRFLOW_FERNET_KEY joining the credentials contract: patches the
# key in rather than leaving the Secret on its old shape, which would
# otherwise hard-error later in _ensure_airflow_fernet_secret with no
# remediation. Adopts the first non-empty `fernet-key` found by walking
# _fernet_key_candidates in order — the deployed release's own
# airflow.fernetKeySecretName (when resolvable), then the chart's projection
# name, then the legacy pre-hook Secret from a release installed before
# fernetKeySecretName was pinned — so a cluster that already ran Airflow
# keeps its stored connections and Variables decryptable regardless of which
# Secret name it actually mounts. Generation is the last resort, only when no
# candidate carries a key. No-op once the key is present. Prod never calls
# this — the operator owns the pre-created Secret's shape; see
# verify_credential_secret in lib/helpers.sh.
_ensure_fernet_key_joins_credentials_secret() {
  local ns="$1"
  local secret_name="$2"

  local existing
  existing="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_AIRFLOW_FERNET_KEY}' 2>/dev/null | base64 --decode 2>/dev/null || true)"
  [[ -n "${existing}" ]] && return 0

  info "'${secret_name}' predates DATASPOKE_AIRFLOW_FERNET_KEY joining the credentials contract — adding it."

  local fernet_key="" adopted_from="" candidate
  while IFS= read -r candidate; do
    [[ -n "${fernet_key}" ]] && break
    if kubectl get secret "${candidate}" -n "${ns}" >/dev/null 2>&1; then
      fernet_key="$(kubectl get secret "${candidate}" -n "${ns}" \
        -o jsonpath='{.data.fernet-key}' | base64 --decode)"
      [[ -n "${fernet_key}" ]] && adopted_from="${candidate}"
    fi
  done < <(_fernet_key_candidates "${ns}")

  if [[ -n "${fernet_key}" ]]; then
    info "  Adopting the Fernet key already live in this cluster (${adopted_from})."
  else
    info "  No live Fernet key found on this cluster — generating a new one."
    fernet_key="$(python3 -c 'import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')"
  fi

  # --patch-file /dev/stdin (not -p with the value inlined) so the key
  # material never lands in this process's argv — matching every other
  # Secret write in this file and the ps-visibility guidance in README.md.
  kubectl patch secret "${secret_name}" -n "${ns}" --type=merge --patch-file /dev/stdin <<EOF
{"data":{"DATASPOKE_AIRFLOW_FERNET_KEY":"$(printf '%s' "${fernet_key}" | base64 | tr -d '\n')"}}
EOF

  # dataspoke-airflow-fernet-key is left in place even when adopted from —
  # it is not deleted here so a mid-upgrade pod restart never finds Airflow's
  # secretKeyRef pointing at a Secret that no longer exists (the umbrella
  # helm upgrade that re-points every Airflow component onto
  # dataspoke-airflow-metadata-encryption-key has not run yet at this point
  # in the install). helm-charts/bin/uninstall.sh deletes it alongside the
  # other chart-derived Airflow Secrets on both profiles.
}

# _ensure_postgres_identity_leaves_credentials_secret <namespace> <secret_name>
# Dev-only self-heal for a credentials Secret still carrying
# DATASPOKE_POSTGRES_USER or DATASPOKE_POSTGRES_DB — both relocated out of this
# Secret and into the app ConfigMap (config.postgres.{user,db}), so the
# ConfigMap stays the single source of the Postgres identity and the two
# cannot silently drift apart. `envFrom` lists the ConfigMap ahead of the
# Secret on every workload that mounts both (api-deployment.yaml), so a
# lingering key here would otherwise keep shadowing the ConfigMap's value —
# this self-heal is what actually closes that gap on an existing install, not
# the values-file/ConfigMap-template change alone.
# A strategic-merge patch setting each key to `null` removes it when present
# and is a clean no-op when absent (a JSON-patch `remove` op would error
# instead on whichever of the two keys is already missing), so the patch
# itself runs unconditionally — only the log line below is gated on presence.
# Presence is tested with --allow-missing-template-keys=false (exit non-zero
# when the field is absent), not on the value being non-empty: a key present
# with an empty string ("" from a blank line in --from-env-file) must still
# be caught, since `envFrom.secretRef` still shadows the ConfigMap's value in
# that case. Prod never calls this — a Secret still carrying either key is a
# pre-flight failure instead (verify_credential_secret); install.sh
# never mutates an operator-owned Secret.
_ensure_postgres_identity_leaves_credentials_secret() {
  local ns="$1"
  local secret_name="$2"

  local key
  for key in DATASPOKE_POSTGRES_USER DATASPOKE_POSTGRES_DB; do
    if kubectl get secret "${secret_name}" -n "${ns}" \
         -o jsonpath="{.data.${key}}" --allow-missing-template-keys=false >/dev/null 2>&1; then
      info "'${secret_name}' still carries DATASPOKE_POSTGRES_{USER,DB} — removing them (the app ConfigMap is the single source of the Postgres identity)."
      break
    fi
  done

  # --patch-file /dev/stdin with a fixed literal (not -p with a value
  # interpolated) so nothing but this constant JSON lands in this process's
  # argv — matching every other Secret write in this file.
  kubectl patch secret "${secret_name}" -n "${ns}" --type=merge --patch-file /dev/stdin <<'EOF'
{"data":{"DATASPOKE_POSTGRES_USER":null,"DATASPOKE_POSTGRES_DB":null}}
EOF
}

# _ensure_dataspoke_secrets <namespace> <profile> [<secret_name>]
# Idempotent: creates the consolidated credential Secret in dev with
# auto-generated values (including Airflow webserver/jwt secrets).
# In prod: fails fast unless the Secret already exists.
# <secret_name> defaults to "dataspoke-secrets".
_ensure_dataspoke_secrets() {
  local ns="$1"
  local profile="$2"
  local secret_name="${3:-dataspoke-secrets}"

  if kubectl get secret "${secret_name}" -n "${ns}" >/dev/null 2>&1; then
    if [[ "$profile" == "dev" ]]; then
      # Dev may still self-heal a pre-existing Secret that predates the
      # current credentials contract (see
      # _ensure_fernet_key_joins_credentials_secret), so "leaving untouched"
      # would be inaccurate here — that function logs its own message on the
      # patch path.
      info "'${secret_name}' already exists in '${ns}'."
      _ensure_fernet_key_joins_credentials_secret "${ns}" "${secret_name}"
      _ensure_postgres_identity_leaves_credentials_secret "${ns}" "${secret_name}"
    else
      info "'${secret_name}' already exists in '${ns}' — leaving untouched."
    fi
    return 0
  fi

  if [[ "$profile" == "prod" ]]; then
    error "prod install requires a pre-created K8s Secret named '${secret_name}'. Create it with
--from-env-file (never --from-literal, which leaks every value into shell history and process
argv) — see helm-charts/README.md §2 for the full eleven-key env-file recipe:
  kubectl create secret generic ${secret_name} \\
    --from-env-file=/tmp/dataspoke-secrets.env \\
    -n ${ns}
or pass --values <overlay.yaml> with secrets.existingSecret: <name>
(DATASPOKE_POSTGRES_USER and DATASPOKE_POSTGRES_DB are NOT part of this Secret
— they are non-secret and are set via config.postgres.{user,db} chart values
instead, default \"dataspoke\"/\"dataspoke\")"
  fi

  local pg_password redis_password
  pg_password="$(openssl rand -hex 32)"
  redis_password="$(openssl rand -hex 32)"

  local airflow_password internal_token jwt_secret airflow_webserver_secret airflow_jwt_secret
  airflow_password="$(openssl rand -hex 32)"
  internal_token="$(openssl rand -hex 32)"
  jwt_secret="$(openssl rand -hex 32)"
  airflow_webserver_secret="$(openssl rand -hex 16)"
  airflow_jwt_secret="$(openssl rand -hex 16)"

  # OAuth state secret: auto-generated per install (random HMAC key).
  local oauth_state_secret
  oauth_state_secret="$(openssl rand -hex 32)"

  # Airflow Fernet key: encrypts connection secrets and Variables in Airflow's
  # metadata DB (the Postgres PVC), so it must not silently change while that
  # PVC survives — a fresh value would leave every stored connection and
  # Variable permanently undecryptable. `openssl rand -hex 32` (used for every
  # other key above) is the wrong shape: it decodes to 48 raw bytes, and
  # Fernet requires exactly 32. Generation is the last resort — first walk
  # _fernet_key_candidates (the deployed release's own
  # airflow.fernetKeySecretName when resolvable, then the chart's projection
  # name, then the legacy pre-hook Secret) and adopt the first non-empty
  # `fernet-key` found, so a credentials Secret re-created by hand while the
  # release is live keeps the Postgres PVC's Airflow connections decryptable
  # regardless of which Secret name it actually mounts. This path — a fresh
  # `_ensure_dataspoke_secrets` create — does not cover a `bin/uninstall.sh`
  # dev teardown, which deletes the credentials Secret and this projection
  # together; see spec/feature/HELM_CHART.md §Dev — install-time provisioning.
  local airflow_fernet_key="" candidate
  while IFS= read -r candidate; do
    [[ -n "${airflow_fernet_key}" ]] && break
    if kubectl get secret "${candidate}" -n "${ns}" >/dev/null 2>&1; then
      airflow_fernet_key="$(kubectl get secret "${candidate}" -n "${ns}" \
        -o jsonpath='{.data.fernet-key}' | base64 --decode)"
      [[ -n "${airflow_fernet_key}" ]] && info "  Adopting the Fernet key already projected into ${candidate}."
    fi
  done < <(_fernet_key_candidates "${ns}")
  if [[ -z "${airflow_fernet_key}" ]]; then
    airflow_fernet_key="$(python3 -c 'import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')"
  fi

  # Google OAuth client secret: sourced from DATASPOKE_DEV_GOOGLE_OAUTH_CLIENT_SECRET
  # in .env. Falls back to a placeholder if absent — the OAuth callback will fail
  # gracefully until the operator supplies a real value.
  local google_oauth_client_secret
  google_oauth_client_secret="${DATASPOKE_DEV_GOOGLE_OAUTH_CLIENT_SECRET:-placeholder-set-google-oauth-secret-via-env}"

  info "Creating '${secret_name}' in namespace '${ns}'..."
  cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: ${secret_name}
  namespace: ${ns}
type: Opaque
data:
  DATASPOKE_POSTGRES_PASSWORD: $(printf '%s' "${pg_password}" | base64 | tr -d '\n')
  DATASPOKE_REDIS_PASSWORD: $(printf '%s' "${redis_password}" | base64 | tr -d '\n')
  DATASPOKE_AIRFLOW_USER: $(printf '%s' "dataspoke-admin" | base64 | tr -d '\n')
  DATASPOKE_AIRFLOW_PASSWORD: $(printf '%s' "${airflow_password}" | base64 | tr -d '\n')
  DATASPOKE_INTERNAL_TOKEN: $(printf '%s' "${internal_token}" | base64 | tr -d '\n')
  DATASPOKE_JWT_SECRET_KEY: $(printf '%s' "${jwt_secret}" | base64 | tr -d '\n')
  DATASPOKE_AIRFLOW_WEBSERVER_SECRET_KEY: $(printf '%s' "${airflow_webserver_secret}" | base64 | tr -d '\n')
  DATASPOKE_AIRFLOW_JWT_SECRET: $(printf '%s' "${airflow_jwt_secret}" | base64 | tr -d '\n')
  DATASPOKE_AIRFLOW_FERNET_KEY: $(printf '%s' "${airflow_fernet_key}" | base64 | tr -d '\n')
  DATASPOKE_OAUTH_STATE_SECRET: $(printf '%s' "${oauth_state_secret}" | base64 | tr -d '\n')
  DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET: $(printf '%s' "${google_oauth_client_secret}" | base64 | tr -d '\n')
EOF
}

# _url_encode <value>
# Percent-encodes a value for use inside a URI userinfo component. SQLAlchemy's
# parser unquotes both halves of `user:password`, so both must be encoded for a
# credential holding `@`, `/`, `%`, or any other delimiter to survive the trip.
_url_encode() {
  printf '%s' "$1" | python3 -c \
    'import urllib.parse,sys; print(urllib.parse.quote(sys.stdin.read(),safe=""))'
}

# _derive_airflow_metadata_secret <namespace> [<secret_name>]
# Reads DATASPOKE_POSTGRES_PASSWORD from the consolidated Secret, builds the
# Airflow metadata connection URI against the fixed role 'dataspoke' (see
# below), and applies dataspoke-airflow-metadata-db (key: connection).
# Compare-and-rotate, mirroring _ensure_airflow_key_secrets below: the URI is
# re-derived on every run and the Secret rewritten only when the derived
# value differs from what is already live, so a rotated
# DATASPOKE_POSTGRES_PASSWORD reaches Airflow on every run.
# <secret_name> defaults to "dataspoke-secrets".
_derive_airflow_metadata_secret() {
  local ns="$1"
  local secret_name="${2:-dataspoke-secrets}"

  AIRFLOW_METADATA_DSN_ROTATED=false

  local pg_password
  pg_password="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_POSTGRES_PASSWORD}' | base64 --decode)"

  # The role is the literal 'dataspoke', not a value read from this Secret or
  # the app ConfigMap: this DSN's 'airflow' database is created
  # `OWNER dataspoke` by the bundled subchart's initdb (create-airflow-db.sql
  # in dataspoke/values.yaml / values-dev.yaml — see config.postgres.user's
  # comment there for why the role name is chart-pinned), so the DSN's role
  # must be that owner. Consistent with the host (dataspoke-postgresql) and
  # database (airflow) this function already hardcodes for the same reason —
  # none of the three is operator-choosable without also editing the initdb
  # SQL that creates them.
  local enc_user enc_pwd
  enc_user="$(_url_encode "dataspoke")"
  enc_pwd="$(_url_encode "${pg_password}")"

  local conn_uri="postgresql://${enc_user}:${enc_pwd}@dataspoke-postgresql:5432/airflow?sslmode=disable"

  local existing_conn_uri=""
  if kubectl get secret dataspoke-airflow-metadata-db -n "${ns}" >/dev/null 2>&1; then
    existing_conn_uri="$(kubectl get secret dataspoke-airflow-metadata-db -n "${ns}" \
      -o jsonpath='{.data.connection}' | base64 --decode)"
  fi

  if [[ "${existing_conn_uri}" == "${conn_uri}" ]]; then
    info "  dataspoke-airflow-metadata-db already up to date — skipping."
    return 0
  fi

  # A fresh create (existing_conn_uri empty) must not trigger a restart — only
  # a genuine rotation of an already-live connection string does.
  if [[ -n "${existing_conn_uri}" ]]; then
    AIRFLOW_METADATA_DSN_ROTATED=true
  fi

  info "Applying dataspoke-airflow-metadata-db..."
  cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: dataspoke-airflow-metadata-db
  namespace: ${ns}
type: Opaque
data:
  connection: $(printf '%s' "${conn_uri}" | base64 | tr -d '\n')
EOF

  if [[ "${AIRFLOW_METADATA_DSN_ROTATED}" == "true" ]]; then
    warn "Airflow metadata DB connection rotated (derived connection string changed) — the consuming pods will be restarted after the upgrade."
  fi
}

# Set by _ensure_airflow_key_secrets when a derived Airflow key Secret was
# *updated* (source key rotated), as opposed to created on a fresh install.
# Read after the umbrella helm upgrade to decide whether to roll the pods that
# hold the old key in memory.
AIRFLOW_KEYS_ROTATED=false

# Set by _derive_airflow_metadata_secret when the derived Airflow metadata
# connection string was *updated* (DATASPOKE_POSTGRES_PASSWORD rotated), as
# opposed to created on a fresh install. A dedicated flag rather than sharing
# AIRFLOW_KEYS_ROTATED above: _ensure_airflow_key_secrets resets that flag at
# its own entry (below), so a shared flag would be clobbered wherever the
# key-secrets call follows this one — as it does in prod's Phase 3. Read after
# the umbrella helm upgrade,
# alongside AIRFLOW_KEYS_ROTATED, to decide whether to restart the pods that
# hold the old connection string in memory.
AIRFLOW_METADATA_DSN_ROTATED=false

# _ensure_airflow_key_secrets <namespace> <secret_name>
# Derives Airflow webserver/jwt secrets from the consolidated Secret and
# creates the two Airflow-chart-compatible Secrets. Idempotent.
_ensure_airflow_key_secrets() {
  local ns="$1"
  local secret_name="$2"

  AIRFLOW_KEYS_ROTATED=false

  local webserver_key jwt_key
  webserver_key="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_AIRFLOW_WEBSERVER_SECRET_KEY}' | base64 --decode)"
  jwt_key="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_AIRFLOW_JWT_SECRET}' | base64 --decode)"

  if [[ -z "${webserver_key}" || -z "${jwt_key}" ]]; then
    error "Secret '${secret_name}' is missing DATASPOKE_AIRFLOW_WEBSERVER_SECRET_KEY or DATASPOKE_AIRFLOW_JWT_SECRET."
  fi

  local existing_api_secret_key=""
  if kubectl get secret dataspoke-airflow-api-secret-key -n "${ns}" >/dev/null 2>&1; then
    existing_api_secret_key="$(kubectl get secret dataspoke-airflow-api-secret-key -n "${ns}" \
      -o jsonpath='{.data.api-secret-key}' | base64 --decode)"
  fi
  if [[ "${existing_api_secret_key}" != "${webserver_key}" ]]; then
    if [[ -n "${existing_api_secret_key}" ]]; then
      AIRFLOW_KEYS_ROTATED=true
    fi
    info "Creating/updating dataspoke-airflow-api-secret-key..."
    cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: dataspoke-airflow-api-secret-key
  namespace: ${ns}
type: Opaque
data:
  api-secret-key: $(printf '%s' "${webserver_key}" | base64 | tr -d '\n')
EOF
  else
    info "  dataspoke-airflow-api-secret-key already up to date — skipping."
  fi

  local existing_jwt_secret=""
  if kubectl get secret dataspoke-airflow-jwt-secret -n "${ns}" >/dev/null 2>&1; then
    existing_jwt_secret="$(kubectl get secret dataspoke-airflow-jwt-secret -n "${ns}" \
      -o jsonpath='{.data.jwt-secret}' | base64 --decode)"
  fi
  if [[ "${existing_jwt_secret}" != "${jwt_key}" ]]; then
    if [[ -n "${existing_jwt_secret}" ]]; then
      AIRFLOW_KEYS_ROTATED=true
    fi
    info "Creating/updating dataspoke-airflow-jwt-secret..."
    cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: dataspoke-airflow-jwt-secret
  namespace: ${ns}
type: Opaque
data:
  jwt-secret: $(printf '%s' "${jwt_key}" | base64 | tr -d '\n')
EOF
  else
    info "  dataspoke-airflow-jwt-secret already up to date — skipping."
  fi

  if [[ "${AIRFLOW_KEYS_ROTATED}" == "true" ]]; then
    warn "Airflow signing key rotated — the consuming pods will be restarted after the upgrade."
  fi
}

# _ensure_airflow_fernet_secret <namespace> <secret_name>
# Projects DATASPOKE_AIRFLOW_FERNET_KEY from the consolidated Secret into
# dataspoke-airflow-metadata-encryption-key (key: fernet-key), the single-key
# shape the Airflow chart's fernetKeySecretName expects. Idempotent, but —
# unlike _ensure_airflow_key_secrets — deliberately has no rotation branch:
# the two signing keys tolerate rotation (a mismatch is re-projected and the
# affected pods restarted, costing only live Airflow sessions), but the
# Fernet key encrypts Airflow's stored connections and Variables in the
# metadata DB, so re-projecting a changed value would leave that data
# permanently undecryptable with no recovery path. A live projection that
# disagrees with the source key therefore aborts the install with the
# recovery command instead of silently overwriting it.
#
# The comparison source walks _fernet_key_candidates in order — the deployed
# release's own airflow.fernetKeySecretName (when resolvable), then the
# chart's projection name, then the legacy pre-hook Secret — taking the
# first candidate with a non-empty fernet-key. This covers a release whose
# Airflow chart actually mounts a self-chosen Secret name (installed by a
# means other than this script's own forced --set, e.g. a direct `helm
# upgrade` or GitOps tool applying an overlay unmodified), not just the two
# literals every install.sh invocation itself pins to. Gated on the read
# VALUE at each candidate, not on Secret existence, so a cluster installed
# before any projection existed — where the candidate Secret is absent, or
# present but never populated — still gets a real comparison instead of an
# unchecked create. Mirrors _ensure_fernet_key_joins_credentials_secret's
# emptiness-gated fallback above.
_ensure_airflow_fernet_secret() {
  local ns="$1"
  local secret_name="$2"

  local fernet_key
  fernet_key="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_AIRFLOW_FERNET_KEY}' | base64 --decode)"

  if [[ -z "${fernet_key}" ]]; then
    error "Secret '${secret_name}' is missing DATASPOKE_AIRFLOW_FERNET_KEY."
  fi

  local existing_fernet_key="" compared_against="" candidate
  while IFS= read -r candidate; do
    [[ -n "${existing_fernet_key}" ]] && break
    if kubectl get secret "${candidate}" -n "${ns}" >/dev/null 2>&1; then
      existing_fernet_key="$(kubectl get secret "${candidate}" -n "${ns}" \
        -o jsonpath='{.data.fernet-key}' | base64 --decode)"
      [[ -n "${existing_fernet_key}" ]] && compared_against="${candidate}"
    fi
  done < <(_fernet_key_candidates "${ns}")

  if [[ -n "${existing_fernet_key}" && "${existing_fernet_key}" != "${fernet_key}" ]]; then
    error "DATASPOKE_AIRFLOW_FERNET_KEY in Secret '${secret_name}' disagrees with the key already
projected into '${compared_against}' on this cluster. Re-projecting it
would leave Airflow's stored connections and Variables in the metadata DB permanently
undecryptable, so this aborts instead. To recover, restore the source key to match the live
projection:
  kubectl get secret ${compared_against} -n ${ns} \\
    -o jsonpath='{.data.fernet-key}' | base64 --decode
then set DATASPOKE_AIRFLOW_FERNET_KEY in '${secret_name}' to that value. Alternatively, drop the
Postgres PVC together with the credentials Secret for a clean reset (dev: --delete-pvcs; prod:
--delete-namespaces) — a freshly generated Fernet key is correct only once the PVC it would have
disagreed with is also gone."
  fi

  # Gate the skip branch on the *value* actually live in the WRITE TARGET
  # (dataspoke-airflow-metadata-encryption-key — hardcoded below, see that
  # comment for why it must stay hardcoded even though the read above is
  # now candidate-driven) matching the contract key — not merely on some
  # candidate agreeing. A value found via a different candidate (a
  # self-chosen fernetKeySecretName the deployed release does not actually
  # use for THIS Secret, or the legacy hook) always falls through to the
  # idempotent kubectl apply below, even when it agrees with the contract
  # key, because the one Secret this function writes has not itself been
  # confirmed up to date.
  if [[ -n "${existing_fernet_key}" \
        && "${existing_fernet_key}" == "${fernet_key}" \
        && "${compared_against}" == "dataspoke-airflow-metadata-encryption-key" ]]; then
    info "  dataspoke-airflow-metadata-encryption-key already up to date — skipping."
  else
    info "Creating dataspoke-airflow-metadata-encryption-key..."
    # The write target stays hardcoded — NOT resolved via
    # _fernet_key_candidates / _resolve_fernet_secret_name. All three helm
    # invocations in this script pin airflow.fernetKeySecretName to this
    # exact name, so a dynamic write target would fork the release from this
    # projection: Airflow would still mount
    # dataspoke-airflow-metadata-encryption-key regardless of what got
    # written here.
    cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: dataspoke-airflow-metadata-encryption-key
  namespace: ${ns}
type: Opaque
data:
  fernet-key: $(printf '%s' "${fernet_key}" | base64 | tr -d '\n')
EOF
  fi
}

# _rollout_restart_workload <namespace> <name>
# Restarts a workload without hardcoding its kind: the Airflow chart renders
# scheduler and triggerer as either a Deployment or a StatefulSet depending on
# log/triggerer persistence. A restart failure is a genuine problem — RBAC
# denial, API-server error — and is left unguarded so `set -euo pipefail`
# aborts the install on it.
_rollout_restart_workload() {
  local ns="$1"
  local name="$2"

  local kind
  for kind in deployment statefulset; do
    if kubectl get "${kind}/${name}" -n "${ns}" >/dev/null 2>&1; then
      kubectl rollout restart "${kind}/${name}" -n "${ns}"
      return 0
    fi
  done

  info "  ${name} not found in ${ns} — skipping restart."
}

# _resolve_digest_or_abort <image_ref>
# Two-outcome digest resolution — the only two outcomes this installer
# supports:
#   resolve_image_digest succeeds -> print the resolved `sha256:...` digest.
#   resolve_image_digest fails    -> abort the install (exit 1), strictly
#                                     BEFORE the umbrella `helm upgrade` runs.
# resolve_image_digest (lib/helpers.sh) already `warn`s the underlying cause
# (registry error, missing CLI, network failure) to stderr immediately above
# this function's own abort message. The explicit, operator-chosen escape
# hatch is `--no-digest-pin`: every call site below skips this function
# entirely when NO_DIGEST_PIN=true, leaving the corresponding *_IMAGE_DIGEST
# variable empty so the image renders as the mutable `<repository>:<tag>`,
# and issues an explicit rollout restart after the upgrade instead (see each
# call site's own restart block). This installer never reads cluster state
# (a Deployment's live image, a pod annotation) to decide what to deploy —
# every input to the deployed image reference comes from this run's own
# build/registry, not from a prior run's outcome.
_resolve_digest_or_abort() {
  local image_ref="$1"

  local digest
  digest="$(resolve_image_digest "${image_ref}")"
  if [[ -z "${digest}" ]]; then
    error "Could not resolve an image digest for '${image_ref}' (see the resolution failure reported above). Fix the underlying cause — registry credentials, CLI availability, network — and re-run, or re-run with --no-digest-pin to deploy '${image_ref}' by its mutable tag instead."
  fi
  echo "${digest}"
}

# _restart_airflow_key_consumers <namespace>
# Rolls every Airflow component that holds a signing key or the metadata-DB
# connection string in memory. A Secret content update does not roll pods
# that reference it via secretKeyRef, and the chart's own checksum/jwt-secret
# annotation is suppressed when jwtSecretName is set (which install.sh always
# sets) — and, for the metadata-DB connection, the chart renders no
# checksum/metadata-secret annotation at all once data.metadataSecretName is
# set (also always, see §Rotation tolerance of the Airflow projections in
# spec/feature/HELM_CHART.md) — so the restart must be explicit either way.
# AIRFLOW__API__SECRET_KEY reaches all four components; the JWT secret
# reaches api-server and scheduler only; the metadata-DB connection string
# reaches every Airflow workload (the shared standard_airflow_environment
# helper includes it for all four components plus the db-migrate job). This
# helper restarts all four uniformly rather than branch per caller on which
# of the three rotated. Called whenever AIRFLOW_KEYS_ROTATED or
# AIRFLOW_METADATA_DSN_ROTATED is set — a DSN-only rotation fires this
# identically to a signing-key rotation.
_restart_airflow_key_consumers() {
  local ns="$1"

  info "Restarting Airflow components to pick up the rotated signing key(s) and/or metadata-DB connection..."
  local component
  for component in api-server scheduler dag-processor triggerer; do
    _rollout_restart_workload "${ns}" "dataspoke-airflow-${component}"
  done
}

# _sync_env_from_secret <namespace> <secret_key> <env_var_name> [<secret_name>]
# Extracts <secret_key> from the consolidated Secret and writes/updates
# <env_var_name>=<value> in helm-charts/.env.<profile>. Idempotent.
# <secret_name> defaults to "dataspoke-secrets". The read is this function's
# own; the write is env_file_set_var's (lib/helpers.sh), which is the single
# rewriter every env-file writer in this project shares.
_sync_env_from_secret() {
  local ns="$1"
  local secret_key="$2"
  local env_var_name="$3"
  local secret_name="${4:-dataspoke-secrets}"

  local value
  value="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath="{.data.${secret_key}}" | base64 --decode)"

  env_file_set_var "${env_var_name}" "${value}" "$ENV_FILE"
}

# _read_configmap_value <namespace> <key>
# Reads a single key out of the app ConfigMap (dataspoke-app-config) — the
# ConfigMap counterpart to reading a key out of the credentials Secret. Used
# for DATASPOKE_POSTGRES_{USER,DB}, which live in the ConfigMap rather than
# the Secret (see spec/feature/HELM_CHART.md §ConfigMap keys). Deliberately a
# thin reader with no env-file-writing body of its own — callers pipe the
# result into _write_env_var, so every env-file write in this script still
# lands in the one rewriter, env_file_set_var in lib/helpers.sh.
_read_configmap_value() {
  local ns="$1"
  local key="$2"

  local value
  if ! value="$(kubectl get configmap dataspoke-app-config -n "${ns}" \
       -o jsonpath="{.data.${key}}" 2>/dev/null)"; then
    error "Could not read '${key}' from ConfigMap 'dataspoke-app-config' in namespace '${ns}' — is config.createConfigMap set to false?"
  fi
  echo "${value}"
}

# _write_env_var <env_var_name> <value>
# Writes/updates a plain (non-Secret) value in helm-charts/.env.<profile>.
# Idempotent. A named wrapper over env_file_set_var (lib/helpers.sh) bound to
# this install's resolved $ENV_FILE, so the dev post-install block below reads
# as one column of assignments rather than repeating the file argument.
_write_env_var() {
  env_file_set_var "$1" "$2" "$ENV_FILE"
}

# _build_airflow_extra_env_file <namespace> <secret_name> [<emit_simple_auth_vars>]
# Writes the Airflow extraEnv YAML block (with the resolved secret name) to a
# temp file and prints its path. Caller is responsible for cleanup.
#
# <emit_simple_auth_vars> ("true"/"false", default "false") additionally
# emits AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS — the literal
# "<DATASPOKE_AIRFLOW_USER>:ADMIN", read from the Secret and composed here
# rather than a bare secretKeyRef, since the ":ADMIN" role suffix must be
# appended and Airflow parses the whole value as a comma-separated
# username:role list — and AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE,
# the fixed path the prod-only init container materialises
# (_build_airflow_simple_auth_init_container_file; $AIRFLOW_SIMPLE_AUTH_PASSWORDS_FILE).
#
# The composed value is re-validated against $AIRFLOW_SIMPLE_AUTH_USERNAME_REGEX
# here too, not just in verify_credential_secret's pre-flight: this
# function is the one place that actually renders the username into
# airflow.extraEnv, which the vendored Airflow chart evaluates through Go
# template `tpl` (a defense-in-depth re-assertion at the point of use, in
# case a future caller ever invokes this helper without having gone through
# that pre-flight first).
#
# The prod call site passes "true"; both dev call sites pass "false" (or omit
# the argument) — dev's simple_auth_manager_all_admins: "True" never reads
# either var, and gating them here (rather than unconditionally) keeps the
# dev chart render byte-identical: this function's caller is the one place
# all three call sites share, so a profile-blind emission here would leak
# into dev even though the profile-specific pieces of §Airflow authentication
# otherwise live entirely in the prod-only helm-upgrade call (see
# _build_airflow_simple_auth_init_container_file's own docstring for why that
# init container/volume similarly cannot live in values.yaml).
_build_airflow_extra_env_file() {
  local ns="$1"
  local secret_name="$2"
  local emit_simple_auth_vars="${3:-false}"
  local tmp_env_file
  tmp_env_file="$(mktemp "${INSTALL_TMPDIR}/airflow-extra-env.XXXX.yaml")"
  cat > "${tmp_env_file}" <<EOF
- name: AIRFLOW_CONN_DATASPOKE_API
  value: "http://dataspoke-api:8002"
- name: DATASPOKE_INTERNAL_TOKEN
  valueFrom:
    secretKeyRef:
      name: ${secret_name}
      key: DATASPOKE_INTERNAL_TOKEN
EOF
  if [[ "${emit_simple_auth_vars}" == "true" ]]; then
    # 'X' sentinel: preserve trailing bytes through `$(...)` so the allowlist
    # judges the raw secret value, not a newline-stripped copy of it.
    local airflow_user
    airflow_user="$(kubectl get secret "${secret_name}" -n "${ns}" \
      -o jsonpath='{.data.DATASPOKE_AIRFLOW_USER}' | base64 --decode; printf 'X')"
    airflow_user="${airflow_user%X}"
    if [[ ! "${airflow_user}" =~ ${AIRFLOW_SIMPLE_AUTH_USERNAME_REGEX} ]]; then
      error "DATASPOKE_AIRFLOW_USER '${airflow_user}' in Secret '${secret_name}' does not match
${AIRFLOW_SIMPLE_AUTH_USERNAME_REGEX} — re-asserted here because this function is the shared path
that composes it into airflow.extraEnv, a Helm \`tpl\` injection sink (see
\$AIRFLOW_SIMPLE_AUTH_USERNAME_REGEX's own comment). verify_credential_secret should have
caught this in pre-flight; re-run the install after fixing the Secret."
    fi
    cat >> "${tmp_env_file}" <<EOF
- name: AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS
  value: "${airflow_user}:ADMIN"
- name: AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE
  value: "${AIRFLOW_SIMPLE_AUTH_PASSWORDS_FILE}"
EOF
  fi
  printf '%s' "${tmp_env_file}"
}

# _build_airflow_simple_auth_init_container_file <namespace> <secret_name>
# Writes a prod-only values fragment
# (airflow.apiServer.{podAnnotations,extraInitContainers,extraVolumes,extraVolumeMounts})
# to a temp file and prints its path. Caller is responsible for cleanup.
#
# Kept out of values.yaml deliberately. Helm always loads a chart's own
# values.yaml as the base layer, even when only values-dev.yaml is passed via
# -f — and values-dev.yaml's own `airflow.apiServer:` map does not reset
# extraInitContainers/extraVolumes/extraVolumeMounts (it only overrides
# `resources`), so a static default under those keys in values.yaml would be
# inherited by the dev release too via Helm's map-deep-merge (the same
# mechanism already carries apiServer.podAnnotations/podDisruptionBudget into
# dev's render today). That would add an (inert, since dev's
# simple_auth_manager_all_admins: "True" never opens the passwords file) init
# container and emptyDir to dev's Airflow api-server Deployment — moving
# dev's rendered output for no behavioural gain. Passing this as an
# additional -f on the prod-only helm-upgrade call keeps dev's chart render
# untouched.
#
# **This -f layer's own hazard**: it sits ahead of the operator's --values
# overlay in VALUES_ARGS specifically so an overlay CAN extend these same
# three list-typed fields for an unrelated reason (a sidecar, a debug
# volume). But Helm deep-merges MAPS and REPLACES LISTS wholesale — an
# overlay that sets ANY of airflow.apiServer.{extraInitContainers,
# extraVolumes,extraVolumeMounts} at all silently drops this entire init
# container/volume/mount instead of appending to them, and neither `helm
# template` nor `helm lint` catches it (both still exit 0 against the
# resulting broken pod template). _assert_no_airflow_simple_auth_overlay_conflict
# (pre-flight, called before this function) is the guard against that —
# aborting instead of letting an overlay silently disable this issue's own
# fix. See also helm-charts/README.md's operator-facing note on this hazard.
#
# The init container materialises the single-entry mapping
# `{"<user>": "<password>"}` into a memory-backed emptyDir mounted at
# $AIRFLOW_SIMPLE_AUTH_PASSWORDS_DIR — SimpleAuthManager.init() opens
# core.simple_auth_manager_passwords_file with mode a+ and catches only
# BlockingIOError, so a read-only Secret mount would raise an uncaught
# OSError and crash api-server startup; an emptyDir mounted writable avoids
# that. `medium: Memory` keeps the plaintext password off the node
# filesystem — this volume holds nothing but that one file. The password
# arrives via a secretKeyRef env var, never in argv (a command-line password
# is visible in `ps auxww` and the pod spec) — the `python3 -c` argument is
# only the script source, which reads the value from os.environ at run
# time. json.dumps over os.environ (rather than a hand-built string) is what
# lets an arbitrary BYO password escape correctly. The script opens the file
# via os.open with an explicit 0o600 mode rather than the builtin open(): the
# `command:` override replaces the image's own ENTRYPOINT, so nothing sets a
# restrictive umask first, and a bare open(path, "w") would land on the
# runtime default (0o644, e.g. via umask 0o022) — world- and group-readable
# in an emptyDir mounted into every container of this pod, including
# wait-for-airflow-migrations (the chart's own apiServer.extraVolumeMounts
# hook applies to it too). Pre-seeding this way — rather than letting Airflow
# generate one — is what pins the password to the operator's value:
# SimpleAuthManager generates a random password only for a username absent
# from the file and preserves an entry already present.
#
# Resources/securityContext are set explicitly to match this pod's other two
# containers (wait-for-airflow-migrations, api-server) rather than inheriting
# nothing: an unsized init container is exactly the invariant this chart's
# own render-walk enforces everywhere else (see values.yaml's resource
# comments), and on GKE Autopilot an unsized container gets its own injected
# defaults (0.5 vCPU / 2Gi memory at the time of writing) folded into the
# pod's max(largest-init, sum-of-app) sizing — multiples of what
# airflow.apiServer.resources itself asks for. allowPrivilegeEscalation:
# false / capabilities.drop: [ALL] / readOnlyRootFilesystem: true (this
# script only ever writes to the mounted volume, never the root fs) mirrors
# the chart's own containerSecurityContext default, so a
# pod-security.kubernetes.io/enforce: restricted namespace admits this
# container the same as its siblings.
#
# A hash of the effective {user,password} pair is stamped as a pod
# annotation (podAnnotations, deep-merged with the chart's own
# cluster-autoscaler safe-to-evict annotation already at that map key) so a
# credential rotation rolls the api-server pod template naturally through
# Helm's own mechanism. Without this, rotating ONLY the password leaves the
# pod template byte-identical (the username, unlike the password, IS baked
# into the manifest today as a literal in AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS,
# so rotating it already rolls the pod) — `helm upgrade` creates no new
# ReplicaSet, the api-server keeps running against its OLD passwords file,
# and the API's own Airflow client (which DOES pick up the new password
# immediately, since it reads the Secret at request time) presents a
# credential Airflow's stale file no longer recognises: a silent, permanent
# 401 on every workflow trigger. This was a no-op under all_admins: "True"
# (no credential was ever checked), so it is a newly load-bearing rotation
# path this issue introduces.
_build_airflow_simple_auth_init_container_file() {
  local ns="$1"
  local secret_name="$2"
  local tmp_values_file
  tmp_values_file="$(mktemp "${INSTALL_TMPDIR}/airflow-simple-auth-init.XXXX.yaml")"

  # 'X' sentinel on both reads: the annotation must hash the same bytes the
  # init container receives through its secretKeyRefs. `$(...)` strips trailing
  # newlines, so hashing the stripped copy would leave the annotation unchanged
  # across a rotation that only altered trailing whitespace — and the pod would
  # not roll onto the new credential.
  local airflow_user airflow_password credentials_hash
  airflow_user="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_AIRFLOW_USER}' | base64 --decode; printf 'X')"
  airflow_user="${airflow_user%X}"
  airflow_password="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_AIRFLOW_PASSWORD}' | base64 --decode; printf 'X')"
  airflow_password="${airflow_password%X}"
  credentials_hash="$(printf '%s:%s' "${airflow_user}" "${airflow_password}" | openssl dgst -sha256 -r | awk '{print $1}')"

  cat > "${tmp_values_file}" <<EOF
airflow:
  apiServer:
    podAnnotations:
      dataspoke.io/simple-auth-credentials-hash: "${credentials_hash}"
    extraInitContainers:
      - name: simple-auth-manager-passwords
        image: "${DATASPOKE_KUBE_IMAGE_REGISTRY}/airflow:${IMAGE_TAG}"
        imagePullPolicy: "{{ .Values.images.airflow.pullPolicy }}"
        securityContext:
          allowPrivilegeEscalation: false
          readOnlyRootFilesystem: true
          capabilities:
            drop:
              - ALL
        resources:
          requests:
            cpu: 50m
            memory: 64Mi
            ephemeral-storage: 64Mi
          limits:
            cpu: 50m
            memory: 64Mi
            ephemeral-storage: 64Mi
        env:
          - name: DATASPOKE_AIRFLOW_USER
            valueFrom:
              secretKeyRef:
                name: ${secret_name}
                key: DATASPOKE_AIRFLOW_USER
          - name: DATASPOKE_AIRFLOW_PASSWORD
            valueFrom:
              secretKeyRef:
                name: ${secret_name}
                key: DATASPOKE_AIRFLOW_PASSWORD
        command: ["python3", "-c"]
        args:
          - |
            import json, os

            path = "${AIRFLOW_SIMPLE_AUTH_PASSWORDS_FILE}"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(
                    {os.environ["DATASPOKE_AIRFLOW_USER"]: os.environ["DATASPOKE_AIRFLOW_PASSWORD"]},
                    f,
                )
        volumeMounts:
          - name: simple-auth-manager-passwords
            mountPath: ${AIRFLOW_SIMPLE_AUTH_PASSWORDS_DIR}
    extraVolumes:
      - name: simple-auth-manager-passwords
        emptyDir:
          medium: Memory
          sizeLimit: 1Mi
    extraVolumeMounts:
      - name: simple-auth-manager-passwords
        mountPath: ${AIRFLOW_SIMPLE_AUTH_PASSWORDS_DIR}
EOF
  printf '%s' "${tmp_values_file}"
}

# _api_image_helm_set_args
# Prints the --set flags pinning every workload that runs the API image: the API
# itself, and the event-consumer, which runs the same image under a `command:`
# override and builds nothing of its own. They are emitted from one place so a
# new call site cannot pin one and forget the other — a consumer left on the
# chart default resolves to `dataspoke/api:latest`, which no build produces.
# Also pins both workloads' image reference to `<repository>@<digest>` (instead
# of the mutable `<repository>:<tag>`) and stamps the same value as the
# dataspoke.io/image-digest pod annotation (provenance only — nothing reads it
# back), from the global $API_IMAGE_DIGEST (see resolve_image_digest /
# _resolve_digest_or_abort), when non-empty — a rebuild under a mutable tag
# pushes a new digest under the same tag string, which by itself renders a
# byte-identical pod template and rolls nothing; pinning by digest makes the
# image reference itself content-addressed, so `helm upgrade` creates a new
# ReplicaSet correctly by construction and `imagePullPolicy: IfNotPresent`
# remains safe (a cached `repo@sha256:X` can only ever be content X). Emitted
# only when the digest resolved — empty under `--no-digest-pin`, where the
# chart's `<repository>:<tag>` default renders instead and every call site
# issues an explicit rollout restart after the upgrade. Also, under
# `--no-digest-pin`, forces `image.pullPolicy=Always` on both workloads: the
# chart default is `IfNotPresent`, safe only when the image reference itself
# is content-addressed (the digest-pinned path above); on a reused mutable
# tag, the substituted `kubectl rollout restart` does not force a re-pull, so
# without this a node with that tag already cached would keep the stale
# content while `rollout status` still reports success. `--set` treats `.` as
# a path separator, so the dot in the annotation key must be escaped as `\.`
# — same idiom as the nginx annotation in _frontend_helm_set_args below.
# Output is one token per line; callers read into an array via a while-read loop.
_api_image_helm_set_args() {
  cat <<EOF
--set
api.image.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/api
--set
api.image.tag=${IMAGE_TAG}
--set
event-consumer.image.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/api
--set
event-consumer.image.tag=${IMAGE_TAG}
EOF
  if [[ -n "${API_IMAGE_DIGEST:-}" ]]; then
    cat <<EOF
--set-string
api.image.digest=${API_IMAGE_DIGEST}
--set-string
event-consumer.image.digest=${API_IMAGE_DIGEST}
--set-string
api.podAnnotations.dataspoke\.io/image-digest=${API_IMAGE_DIGEST}
--set-string
event-consumer.podAnnotations.dataspoke\.io/image-digest=${API_IMAGE_DIGEST}
EOF
  fi
  if [[ "${NO_DIGEST_PIN:-false}" == "true" ]]; then
    # Clears any api.image.digest / event-consumer.image.digest a values.yaml
    # default or the operator's --values overlay may have set: omitting a
    # --set flag only removes what a PREVIOUS --set supplied, it does not
    # touch a value that came from a -f file, so without this an overlay-
    # pinned digest would survive --no-digest-pin and `dataspoke.imageRef`
    # would keep rendering `<repository>@sha256:<stale>` instead of the
    # mutable `<repository>:<tag>` this flag is supposed to produce.
    cat <<EOF
--set-string
api.image.digest=
--set-string
event-consumer.image.digest=
--set
api.image.pullPolicy=Always
--set
event-consumer.image.pullPolicy=Always
EOF
  fi
}

# _frontend_helm_set_args <domain>
# Prints the --set flags required to enable and wire the frontend subchart.
# Output is one token per line; callers read into an array via a while-read loop.
# The nginx annotation key contains a dot that helm interprets as a path
# separator, so it must be escaped as \\.
_frontend_helm_set_args() {
  local domain="$1"
  local scheme
  scheme="$(ingress_scheme)"
  # Assigned, not inlined into the heredoc: a validation failure inside a
  # command substitution that only produces heredoc text does not trip `set -e`,
  # so an invalid class would be emitted as an empty token instead of aborting.
  local ingress_cls
  ingress_cls="$(ingress_class)"
  cat <<EOF
--set
frontend.enabled=true
--set
frontend.image.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/frontend
--set
frontend.image.tag=${IMAGE_TAG}
--set
frontend.image.pullPolicy=Always
--set
frontend.ingress.enabled=true
--set
frontend.ingress.className=${ingress_cls}
--set-string
frontend.ingress.annotations.nginx\.ingress\.kubernetes\.io/ssl-redirect=false
--set
frontend.ingress.hosts[0].host=app.${domain}
--set
frontend.ingress.hosts[0].paths[0].path=/
--set
frontend.ingress.hosts[0].paths[0].pathType=Prefix
--set
frontend.config.apiBaseUrl=${scheme}://api.${domain}
--set
frontend.config.airflowUrl=${scheme}://airflow.${domain}
EOF
  # Pins the frontend image reference to `<repository>@<digest>` and stamps
  # the dataspoke.io/image-digest pod annotation (provenance only), both from
  # the global $FRONTEND_IMAGE_DIGEST (see resolve_image_digest /
  # _resolve_digest_or_abort in lib/helpers.sh and install.sh) — same
  # reasoning and escaping idiom as _api_image_helm_set_args above. Emitted
  # only when the digest resolved — empty under `--no-digest-pin`.
  if [[ -n "${FRONTEND_IMAGE_DIGEST:-}" ]]; then
    cat <<EOF
--set-string
frontend.image.digest=${FRONTEND_IMAGE_DIGEST}
--set-string
frontend.podAnnotations.dataspoke\.io/image-digest=${FRONTEND_IMAGE_DIGEST}
EOF
  elif [[ "${NO_DIGEST_PIN:-false}" == "true" ]]; then
    # Clears any frontend.image.digest a values file may have set — same
    # reasoning as the clearing block in _api_image_helm_set_args above:
    # omitting a --set only removes what a previous --set supplied, not a
    # value set via -f.
    cat <<EOF
--set-string
frontend.image.digest=
EOF
  fi
  local tls_secret
  tls_secret="$(ingress_tls_secret)"
  if [[ -n "$tls_secret" ]]; then
    cat <<EOF
--set
frontend.ingress.tls[0].secretName=${tls_secret}
--set
frontend.ingress.tls[0].hosts[0]=app.${domain}
EOF
  fi
}

# _write_frontend_env_local <domain>
# Overwrites src/frontend/.env.local with NEXT_PUBLIC_* vars pointing at the
# in-cluster API and Airflow. Always overwrites — no backup.
_write_frontend_env_local() {
  local domain="$1"
  local scheme
  scheme="$(ingress_scheme)"
  local env_local_path="${REPO_ROOT}/src/frontend/.env.local"
  cat > "${env_local_path}" <<EOF
# Auto-generated by helm-charts/bin/install.sh --frontend local — safe to edit or delete.
NEXT_PUBLIC_API_BASE_URL=${scheme}://api.${domain}
NEXT_PUBLIC_AIRFLOW_URL=${scheme}://airflow.${domain}
EOF
  info "Wrote ${env_local_path} (API: ${scheme}://api.${domain}, Airflow: ${scheme}://airflow.${domain})"
}

# _api_airflow_tls_helm_set_args <domain>
# Prints the --set flags for per-Ingress TLS on the API and Airflow (chart-
# native) ingresses when DATASPOKE_KUBE_INGRESS_TLS_SECRET is set. Empty
# output when unset. Output is one token per line, same convention as
# _frontend_helm_set_args — callers read into an array via a while-read loop.
_api_airflow_tls_helm_set_args() {
  local domain="$1"
  local tls_secret
  tls_secret="$(ingress_tls_secret)"
  [[ -z "$tls_secret" ]] && return 0
  cat <<EOF
--set
api.ingress.tls[0].secretName=${tls_secret}
--set
api.ingress.tls[0].hosts[0]=api.${domain}
--set
airflow.ingress.apiServer.hosts[0].tls.enabled=true
--set
airflow.ingress.apiServer.hosts[0].tls.secretName=${tls_secret}
EOF
}

# helm upgrade --install for the dataspoke umbrella chart (dev overlay).
# Used by both the full dev install (phase 3) and the --components api fast path.
# Reads the global $FRONTEND_MODE to decide whether to append frontend --set flags.
_helm_upgrade_dataspoke_dev() {
  local ns="$1"

  # This upgrade pins airflow.{apiSecretKeySecretName,jwtSecretName,
  # fernetKeySecretName} below, so every call site of this function — the
  # full install's phase 3 and the `--components api` fast path — must
  # guarantee those projected Secrets (and the credentials Secret they derive
  # from) exist first. The `--components frontend` fast path renders its own
  # separate helm upgrade with the same pins and runs the identical sequence
  # inline rather than through this function. Idempotent: a no-op once
  # everything is already in sync.
  # Fernet first, before either rotating write, matching the prod branch:
  # _ensure_airflow_fernet_secret hard-errors on a source/projection
  # mismatch and writes nothing when it does. _ensure_airflow_key_secrets
  # runs next, ahead of _derive_airflow_metadata_secret, because it
  # VALIDATES the credentials Secret carries
  # DATASPOKE_AIRFLOW_WEBSERVER_SECRET_KEY / DATASPOKE_AIRFLOW_JWT_SECRET
  # before writing anything — a dev Secret still missing those aborts here
  # with nothing yet written. Only _restart_airflow_key_consumers (further
  # down, after the upgrade) repairs a rotated write's split from what the
  # running pods hold, so _derive_airflow_metadata_secret runs last: if IT
  # aborts, no other call in this sequence has a write stranded without it.
  _ensure_dataspoke_secrets "${ns}" "dev" "dataspoke-secrets"
  _ensure_airflow_fernet_secret "${ns}" "dataspoke-secrets"
  _ensure_airflow_key_secrets "${ns}" "dataspoke-secrets"
  _derive_airflow_metadata_secret "${ns}" "dataspoke-secrets"

  local extra_env_file
  extra_env_file="$(_build_airflow_extra_env_file "${ns}" "dataspoke-secrets" "false")"
  local dev_domain="${DATASPOKE_KUBE_INGRESS_DOMAIN:-dev.dataspoke.example.com}"
  local scheme
  scheme="$(ingress_scheme)"
  # OIDC post-login redirect = where the UI is served for this frontend mode
  # (host pnpm dev on localhost vs in-cluster app.<domain>).
  local oauth_redirect="${scheme}://app.${dev_domain}/"
  [[ "$FRONTEND_MODE" == "local" ]] && oauth_redirect="http://localhost:3000/"
  # One class for every Ingress this release renders. Note the key names differ:
  # the API and frontend templates read `ingress.className`, the Airflow chart
  # reads `ingress.apiServer.ingressClassName` — the wrong spelling is silently
  # dropped and GKE falls back to provisioning a GCE LoadBalancer.
  local ingress_cls
  ingress_cls="$(ingress_class)"

  local args=(
    upgrade --install dataspoke "$CHART_DIR"
    -f "$CHART_DIR/values-dev.yaml"
    -n "${ns}"
    --set-string global.imageRegistry=""
    --set-string postgresql.image.registry=""
    --set-string "postgresql.image.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/postgres"
    --set-string "postgresql.image.tag=${IMAGE_TAG}"
    --set-string "airflow.images.airflow.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/airflow"
    --set-string "airflow.images.airflow.tag=${IMAGE_TAG}"
    --set airflow.images.airflow.pullPolicy=Always
    --set-string "config.airflow.callbackBaseUrl=http://dataspoke-api:8002"
    --set "api.ingress.className=${ingress_cls}"
    --set "api.ingress.hosts[0].host=api.${dev_domain}"
    --set "api.ingress.hosts[0].paths[0].path=/"
    --set "api.ingress.hosts[0].paths[0].pathType=Prefix"
    --set-string "config.corsOrigins=http://localhost:3000\,http://app.${dev_domain}\,https://app.${dev_domain}"
    --set-string "config.oauthPostLoginRedirect=${oauth_redirect}"
    --set "airflow.ingress.apiServer.ingressClassName=${ingress_cls}"
    --set "airflow.ingress.apiServer.hosts[0].name=airflow.${dev_domain}"
    --set-file "airflow.extraEnv=${extra_env_file}"
    --set "airflow.apiSecretKeySecretName=dataspoke-airflow-api-secret-key"
    --set "airflow.jwtSecretName=dataspoke-airflow-jwt-secret"
    --set "airflow.fernetKeySecretName=dataspoke-airflow-metadata-encryption-key"
    --set-string "auth.googleClientId=${DATASPOKE_DEV_GOOGLE_OAUTH_CLIENT_ID:-}"
    --timeout 10m
  )

  while IFS= read -r _iarg; do
    args+=("${_iarg}")
  done < <(_api_image_helm_set_args)

  if [[ "${FRONTEND_MODE:-none}" == "cluster" ]]; then
    while IFS= read -r _farg; do
      args+=("${_farg}")
    done < <(_frontend_helm_set_args "${dev_domain}")
  fi

  while IFS= read -r _tlsarg; do
    args+=("${_tlsarg}")
  done < <(_api_airflow_tls_helm_set_args "${dev_domain}")

  helm "${args[@]}"

  # Covers both call sites of this function: the full dev install (phase 3)
  # and the `--components api` fast path. Under a digest pin, the pod-template
  # hash changed exactly when the digest changed, so Helm already rolled
  # whatever it needed to — nothing further to do. Under `--no-digest-pin`
  # there is no digest to change the pod template, so restart explicitly. The
  # frontend restart is additionally gated on FRONTEND_MODE=="cluster" — on
  # the `--components api` fast path with a prior cluster-deployed frontend,
  # this same helm upgrade disables and deletes the frontend, so restarting it
  # here would race that deletion.
  if [[ "${NO_DIGEST_PIN}" == "true" ]]; then
    info "Restarting api/event-consumer workloads (--no-digest-pin: no digest pin to change the pod template)..."
    _rollout_restart_workload "${ns}" "dataspoke-api"
    _rollout_restart_workload "${ns}" "dataspoke-event-consumer"
    if [[ "${FRONTEND_MODE:-none}" == "cluster" ]]; then
      _rollout_restart_workload "${ns}" "dataspoke-frontend"
    fi
  fi
}

# ---------------------------------------------------------------------------
# DEV PROFILE
# ---------------------------------------------------------------------------
if [[ "$PROFILE" == "dev" ]]; then

  # Default component set for dev
  DEV_ALL=(nginx-ingress datahub langfuse dataspoke-infra dummy-data dev-lock seed)

  # Parse user-supplied --components into an array
  if [[ -n "$COMPONENTS_CSV" ]]; then
    IFS=',' read -ra SELECTED <<< "$COMPONENTS_CSV"
    # Trim whitespace
    COMPONENTS=()
    for c in "${SELECTED[@]}"; do
      COMPONENTS+=("$(echo "$c" | tr -d ' ')")
    done
  else
    COMPONENTS=("${DEV_ALL[@]}")
  fi

  # Resolve start index for --from-component
  START_INDEX=0
  if [[ -n "$FROM_COMPONENT" && -z "$COMPONENTS_CSV" ]]; then
    found=false
    for i in "${!DEV_ALL[@]}"; do
      if [[ "${DEV_ALL[$i]}" == "$FROM_COMPONENT" ]]; then
        START_INDEX=$i
        found=true
        break
      fi
    done
    if [[ "$found" != "true" ]]; then
      error "Unknown component '${FROM_COMPONENT}'. Valid names: ${DEV_ALL[*]}"
    fi
    COMPONENTS=("${DEV_ALL[@]:$START_INDEX}")
    info "Resuming from component '${FROM_COMPONENT}'."
  fi

  # Helpers
  _has_component() { local needle="$1"; for c in "${COMPONENTS[@]}"; do [[ "$c" == "$needle" ]] && return 0; done; return 1; }

  # -------------------------------------------------------------------------
  # Handle --components api (code-iteration fast path)
  # -------------------------------------------------------------------------
  if [[ "${#COMPONENTS[@]}" -eq 1 && "${COMPONENTS[0]}" == "api" ]]; then
    NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"
    SCHEME="$(ingress_scheme)"
    info "==> Fast path: rebuild API image + helm upgrade (digest-pinned roll) + rollout wait"

    use_context "${DATASPOKE_KUBE_CLUSTER}"

    if [[ "$SKIP_BUILD" == "false" ]]; then
      info "Building API image (tag: ${IMAGE_TAG})..."
      bash "$SCRIPT_DIR/build-image.sh" api "${IMAGE_TAG}"
    else
      info "--skip-build: skipping API image build."
    fi

    # Resolved once here and read by _api_image_helm_set_args (invoked inside
    # _helm_upgrade_dataspoke_dev below) to pin the dataspoke.io/image-digest
    # pod annotation and image reference onto the api and event-consumer
    # workloads. --no-digest-pin skips resolution entirely (both variables
    # stay empty) and _helm_upgrade_dataspoke_dev issues an explicit rollout
    # restart after the upgrade instead.
    API_IMAGE_DIGEST=""
    if [[ "${NO_DIGEST_PIN}" == "false" ]]; then
      API_IMAGE_DIGEST="$(_resolve_digest_or_abort "${DATASPOKE_KUBE_IMAGE_REGISTRY}/api:${IMAGE_TAG}")"
    fi
    # This is a full-release helm upgrade (below, via _helm_upgrade_dataspoke_dev)
    # that also renders the frontend's pod template whenever FRONTEND_MODE is
    # "cluster" — resolve its digest too so the pin is not dropped from that
    # template on every API-only iteration.
    FRONTEND_IMAGE_DIGEST=""
    if [[ "$FRONTEND_MODE" == "cluster" && "${NO_DIGEST_PIN}" == "false" ]]; then
      FRONTEND_IMAGE_DIGEST="$(_resolve_digest_or_abort "${DATASPOKE_KUBE_IMAGE_REGISTRY}/frontend:${IMAGE_TAG}")"
    fi

    info "Running helm upgrade for dataspoke umbrella chart..."

    # Re-package local subcharts so event-consumer template edits ship instead
    # of the stale packaged subchart in charts/. This upgrade is a full-release
    # one, so it deploys the event-consumer too — and the consumer runs this
    # same API image.
    _build_chart_deps "$CHART_DIR"

    _helm_upgrade_dataspoke_dev "${NS}"

    # Roll the Airflow pods still holding a superseded signing key or a
    # superseded metadata-DB connection string, if _helm_upgrade_dataspoke_dev
    # found either had drifted from what these pods are currently using.
    if [[ "${AIRFLOW_KEYS_ROTATED}" == "true" || "${AIRFLOW_METADATA_DSN_ROTATED}" == "true" ]]; then
      _restart_airflow_key_consumers "${NS}"
    fi

    # _helm_upgrade_dataspoke_dev already rolled dataspoke-api and
    # dataspoke-event-consumer (via the digest pin, or an explicit rollout
    # restart under --no-digest-pin) — just wait for the API to report ready.
    kubectl rollout status deployment/dataspoke-api -n "${NS}" --timeout=5m \
      && info "dataspoke-api is ready." \
      || error "dataspoke-api did not become ready in time — check pod logs."

    # Verify Airflow DAGs — best effort; never aborts this fast path. Routed
    # through api_internal_request (bin/lib/helpers.sh) — the same
    # kubectl-exec-into-the-pod call shape the post-install seed scripts use —
    # rather than a second, ingress-routed request. 70s timeout (well above
    # the helper's 10s default): the endpoint calls AirflowClient.list_dags(),
    # whose own httpx client carries a 60s timeout after authenticating
    # first, and a slow-but-working Airflow right after the umbrella upgrade
    # is exactly the state this check runs in — the tighter default would
    # misread that as a connection failure and retry it 5x for no reason.
    # API_INTERNAL_REQUEST_QUIET=1 downgrades a kubectl-exec failure to a
    # `warn` (helpers.sh) instead of a red [ERROR] — this step's own failure
    # already only warns and continues, so its plumbing should not print a
    # line that reads like an aborted install. Any failure here — a non-2xx
    # response, or api_internal_request itself failing — only warns.
    info "Verifying Airflow DAGs..."
    if DAGS_VERIFY_RESPONSE="$(API_INTERNAL_REQUEST_QUIET=1 api_internal_request "${NS}" POST "/internal/admin/dags/verify" '{}' 70)"; then
      DAGS_VERIFY_CODE="$(printf '%s\n' "$DAGS_VERIFY_RESPONSE" | head -n1)"
      if [[ "$DAGS_VERIFY_CODE" == "200" || "$DAGS_VERIFY_CODE" == "204" ]]; then
        info "Airflow DAGs verified."
      else
        warn "Failed to verify Airflow DAGs — retry after Airflow is ready."
      fi
    else
      warn "Failed to verify Airflow DAGs — retry after Airflow is ready."
    fi

    echo ""
    info "API iteration deploy complete (t+$((SECONDS - START_TIME))s)."
    echo ""
    if [[ -n "${DATASPOKE_KUBE_INGRESS_DOMAIN:-}" ]]; then
      echo "  API:   ${SCHEME}://api.${DATASPOKE_KUBE_INGRESS_DOMAIN}/api"
      echo "  ReDoc: ${SCHEME}://api.${DATASPOKE_KUBE_INGRESS_DOMAIN}/redoc"
    fi
    echo ""
    echo "  To run integration tests:"
    echo "    set -a && source ${ENV_FILE} && set +a && uv run pytest tests/integration/api_wired/ -v"
    echo ""
    echo "  To stop the API:"
    echo "    kubectl scale deployment/dataspoke-api --replicas=0 -n '${NS}'"
    echo ""
    exit 0
  fi

  # -------------------------------------------------------------------------
  # Handle --components frontend (containerised frontend fast path)
  # The default dev install keeps frontend.enabled=false so host `pnpm dev`
  # remains the standard dev workflow. This path explicitly enables and deploys
  # the containerised frontend in-cluster for verification or prod-parity testing.
  # -------------------------------------------------------------------------
  if [[ "${#COMPONENTS[@]}" -eq 1 && "${COMPONENTS[0]}" == "frontend" ]]; then
    NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"
    DOMAIN="${DATASPOKE_KUBE_INGRESS_DOMAIN:-dev.dataspoke.example.com}"
    info "==> Fast path: rebuild frontend image + helm upgrade + rollout"
    info "    Note: deploys the containerised frontend in-cluster (overrides frontend.enabled=false)."

    use_context "${DATASPOKE_KUBE_CLUSTER}"

    if [[ "$SKIP_BUILD" == "false" ]]; then
      info "Building frontend image (tag: ${IMAGE_TAG})..."
      bash "$SCRIPT_DIR/build-image.sh" frontend "${IMAGE_TAG}"
    else
      info "--skip-build: skipping frontend image build."
    fi

    # Resolved once here and read by _frontend_helm_set_args /
    # _api_image_helm_set_args below to pin the dataspoke.io/image-digest pod
    # annotation and image reference onto every workload this upgrade renders.
    # --no-digest-pin skips resolution entirely and the restart below after
    # the upgrade covers all three workloads unconditionally instead.
    FRONTEND_IMAGE_DIGEST=""
    if [[ "${NO_DIGEST_PIN}" == "false" ]]; then
      FRONTEND_IMAGE_DIGEST="$(_resolve_digest_or_abort "${DATASPOKE_KUBE_IMAGE_REGISTRY}/frontend:${IMAGE_TAG}")"
    fi
    API_IMAGE_DIGEST=""
    if [[ "${NO_DIGEST_PIN}" == "false" ]]; then
      API_IMAGE_DIGEST="$(_resolve_digest_or_abort "${DATASPOKE_KUBE_IMAGE_REGISTRY}/api:${IMAGE_TAG}")"
    fi

    info "Running helm upgrade for dataspoke umbrella chart (frontend.enabled=true)..."

    # Re-package local subcharts so frontend template/config edits ship instead
    # of the stale packaged subchart in charts/.
    _build_chart_deps "$CHART_DIR"

    # This is a full-release helm upgrade (below) that pins
    # airflow.{apiSecretKeySecretName,jwtSecretName,fernetKeySecretName}, so it
    # must guarantee those projected Secrets — and the credentials Secret they
    # derive from — exist first. Idempotent: a no-op once already in sync.
    # Fernet, then keys, then derive last — see _helm_upgrade_dataspoke_dev
    # for why _ensure_airflow_key_secrets must precede
    # _derive_airflow_metadata_secret (it validates before writing; derive
    # does not).
    _ensure_dataspoke_secrets "${NS}" "dev" "dataspoke-secrets"
    _ensure_airflow_fernet_secret "${NS}" "dataspoke-secrets"
    _ensure_airflow_key_secrets "${NS}" "dataspoke-secrets"
    _derive_airflow_metadata_secret "${NS}" "dataspoke-secrets"

    SCHEME="$(ingress_scheme)"
    # One class for every Ingress this release renders. The API and frontend
    # templates read `ingress.className`, the Airflow chart reads
    # `ingress.apiServer.ingressClassName` — the wrong spelling is silently
    # dropped and GKE falls back to provisioning a GCE LoadBalancer.
    INGRESS_CLS="$(ingress_class)"
    local_extra_env_file="$(_build_airflow_extra_env_file "${NS}" "dataspoke-secrets" "false")"
    frontend_fast_args=()
    while IFS= read -r _farg; do
      frontend_fast_args+=("${_farg}")
    done < <(_frontend_helm_set_args "${DOMAIN}")
    tls_fast_args=()
    while IFS= read -r _tlsarg; do
      tls_fast_args+=("${_tlsarg}")
    done < <(_api_airflow_tls_helm_set_args "${DOMAIN}")
    api_image_fast_args=()
    while IFS= read -r _iarg; do
      api_image_fast_args+=("${_iarg}")
    done < <(_api_image_helm_set_args)
    helm upgrade --install dataspoke "$CHART_DIR" \
      -f "$CHART_DIR/values-dev.yaml" \
      -n "${NS}" \
      --set-string global.imageRegistry="" \
      --set-string postgresql.image.registry="" \
      --set-string "postgresql.image.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/postgres" \
      --set-string postgresql.image.tag="${IMAGE_TAG}" \
      "${api_image_fast_args[@]}" \
      --set-string "airflow.images.airflow.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/airflow" \
      --set-string "airflow.images.airflow.tag=${IMAGE_TAG}" \
      --set airflow.images.airflow.pullPolicy=Always \
      --set-string "config.airflow.callbackBaseUrl=http://dataspoke-api:8002" \
      --set "api.ingress.className=${INGRESS_CLS}" \
      --set "api.ingress.hosts[0].host=api.${DOMAIN}" \
      --set "api.ingress.hosts[0].paths[0].path=/" \
      --set "api.ingress.hosts[0].paths[0].pathType=Prefix" \
      --set-string "config.corsOrigins=http://localhost:3000\,http://app.${DOMAIN}\,https://app.${DOMAIN}" \
      --set-string "config.oauthPostLoginRedirect=${SCHEME}://app.${DOMAIN}/" \
      --set "airflow.ingress.apiServer.ingressClassName=${INGRESS_CLS}" \
      --set "airflow.ingress.apiServer.hosts[0].name=airflow.${DOMAIN}" \
      --set-file "airflow.extraEnv=${local_extra_env_file}" \
      --set "airflow.apiSecretKeySecretName=dataspoke-airflow-api-secret-key" \
      --set "airflow.jwtSecretName=dataspoke-airflow-jwt-secret" \
      --set "airflow.fernetKeySecretName=dataspoke-airflow-metadata-encryption-key" \
      --set-string "auth.googleClientId=${DATASPOKE_DEV_GOOGLE_OAUTH_CLIENT_ID:-}" \
      "${frontend_fast_args[@]}" \
      ${tls_fast_args[@]+"${tls_fast_args[@]}"} \
      --timeout 10m

    # Under a digest pin, the pod-template hash changed exactly when the
    # digest changed, so Helm already rolled whatever it needed to. Under
    # --no-digest-pin there is no digest to change the pod template, so
    # restart all three workloads this upgrade renders explicitly.
    if [[ "${NO_DIGEST_PIN}" == "true" ]]; then
      info "Restarting api/event-consumer/frontend workloads (--no-digest-pin: no digest pin to change the pod template)..."
      _rollout_restart_workload "${NS}" "dataspoke-api"
      _rollout_restart_workload "${NS}" "dataspoke-event-consumer"
      _rollout_restart_workload "${NS}" "dataspoke-frontend"
    fi

    # Roll the Airflow pods still holding a superseded signing key or a
    # superseded metadata-DB connection string, if the ensure-secrets steps
    # above found either had drifted from what these pods are currently
    # using. Ordered ahead of the rollout wait below: the writes above have
    # already landed in the projected Secrets, so an abort between the two
    # leaves the Airflow pods running on a superseded value — and the next
    # run compares the credentials Secret against those same projections,
    # finds them equal, and skips the restart, stranding it permanently.
    if [[ "${AIRFLOW_KEYS_ROTATED}" == "true" || "${AIRFLOW_METADATA_DSN_ROTATED}" == "true" ]]; then
      _restart_airflow_key_consumers "${NS}"
    fi

    info "Waiting for frontend deployment to become ready..."
    kubectl rollout status deployment/dataspoke-frontend -n "${NS}" --timeout=5m \
      && info "dataspoke-frontend is ready." \
      || error "dataspoke-frontend did not become ready in time — check pod logs."

    echo ""
    info "Frontend deploy complete (t+$((SECONDS - START_TIME))s)."
    echo ""
    echo "  Frontend: ${SCHEME}://app.${DOMAIN}/"
    echo "  API:      ${SCHEME}://api.${DOMAIN}/api/v1/"
    echo ""
    echo "  To stop the frontend pod:"
    echo "    kubectl scale deployment/dataspoke-frontend --replicas=0 -n '${NS}'"
    echo ""
    exit 0
  fi

  # -------------------------------------------------------------------------
  # Full dev install — phased
  # -------------------------------------------------------------------------

  # Ensure dev namespaces
  use_context "${DATASPOKE_KUBE_CLUSTER}"
  ensure_namespace "${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE}"
  ensure_namespace "${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"
  ensure_namespace "${DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE}"
  ensure_namespace "${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE}"

  # -----------------------------------------------------------------------
  # Phase 1: nginx-ingress (sequential — must complete before parallel phase)
  # -----------------------------------------------------------------------
  if _has_component nginx-ingress; then
    step 1 5 "nginx-ingress"
    bash "$SCRIPT_DIR/dev-peripherals/nginx-ingress.sh"
    # Re-source .env so DATASPOKE_KUBE_INGRESS_IP/_DOMAIN are available
    source "$ENV_FILE"
  fi

  NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"

  # -----------------------------------------------------------------------
  # Phase 2: Parallel bootstrap
  # Build images ‖ install DataHub ‖ install Langfuse
  # -----------------------------------------------------------------------
  step 2 5 "parallel bootstrap (image builds + DataHub + Langfuse)"

  if [[ "$SKIP_BUILD" == "false" ]]; then
    _run_bg "build-api"      bash "$SCRIPT_DIR/build-image.sh" api      "${IMAGE_TAG}"
    _run_bg "build-airflow"  bash "$SCRIPT_DIR/build-image.sh" airflow  "${IMAGE_TAG}"
    _run_bg "build-postgres" bash "$SCRIPT_DIR/build-image.sh" postgres "${IMAGE_TAG}"
    if [[ "$FRONTEND_MODE" == "cluster" ]]; then
      _run_bg "build-frontend" bash "$SCRIPT_DIR/build-image.sh" frontend "${IMAGE_TAG}"
    fi
  else
    info "  --skip-build: skipping image builds."
  fi

  if _has_component datahub; then
    _run_bg "datahub" bash "$SCRIPT_DIR/dev-peripherals/datahub.sh"
  fi
  if _has_component langfuse; then
    _run_bg "langfuse" bash "$SCRIPT_DIR/dev-peripherals/langfuse.sh"
  fi

  _wait_all
  # Re-source .env to pick up any new values written by parallel tasks
  source "$ENV_FILE"

  # -----------------------------------------------------------------------
  # Phase 3: Umbrella chart (dataspoke-infra)
  # -----------------------------------------------------------------------
  if _has_component dataspoke-infra; then
    step 3 5 "dataspoke-infra (umbrella chart)"

    # The consolidated credential Secret and its Airflow projections
    # (metadata-db connection, webserver/jwt keys, Fernet key) are ensured
    # inside _helm_upgrade_dataspoke_dev below, ahead of the helm upgrade that
    # pins their Secret names — see that function's header comment.

    # LLM API key (out-of-band secret)
    if [[ -n "${DATASPOKE_DEV_LLM_API_KEY:-}" ]]; then
      info "Applying dataspoke-llm-secret (LLM API key)..."
      kubectl create secret generic dataspoke-llm-secret \
        --namespace "${NS}" \
        --from-literal=api_key="${DATASPOKE_DEV_LLM_API_KEY}" \
        --dry-run=client -o yaml | kubectl apply -f -
    else
      info "DATASPOKE_DEV_LLM_API_KEY is unset — dataspoke-llm-secret not created."
    fi

    # DataHub token (out-of-band secret)
    if [[ -n "${DATASPOKE_DEV_DATAHUB_TOKEN:-}" ]]; then
      info "Applying dataspoke-datahub-secret (DataHub PAT)..."
      kubectl create secret generic dataspoke-datahub-secret \
        --namespace "${NS}" \
        --from-literal=token="${DATASPOKE_DEV_DATAHUB_TOKEN}" \
        --dry-run=client -o yaml | kubectl apply -f -
    else
      info "DATASPOKE_DEV_DATAHUB_TOKEN is unset — dataspoke-datahub-secret not created."
    fi

    # Langfuse secret key (out-of-band secret)
    if [[ -n "${DATASPOKE_DEV_LANGFUSE_SECRET_KEY:-}" ]]; then
      info "Applying dataspoke-langfuse-secret (Langfuse secret key)..."
      kubectl create secret generic dataspoke-langfuse-secret \
        --namespace "${NS}" \
        --from-literal=secret_key="${DATASPOKE_DEV_LANGFUSE_SECRET_KEY}" \
        --dry-run=client -o yaml | kubectl apply -f -
    else
      info "DATASPOKE_DEV_LANGFUSE_SECRET_KEY is unset — dataspoke-langfuse-secret not created."
    fi

    # Source-credential Secret: dummy-data Postgres (dev only)
    # Allows ACTIVE_CUSTOM_MANAGED ingestion recipes that reference ${dummy-data-pg__password}
    # to resolve at run time. Must live in the DataSpoke API namespace so secret_resolver.py
    # (which reads /var/run/secrets/kubernetes.io/serviceaccount/namespace) can reach it.
    info "Applying dataspoke-source-cred-dummy-data-pg (dev source credential)..."
    kubectl create secret generic dataspoke-source-cred-dummy-data-pg \
      --namespace "${NS}" \
      --from-literal=password="${DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD:-ExampleDev2024!}" \
      --dry-run=client -o yaml | kubectl apply -f -

    # Helm repo setup
    info "Adding/updating Helm repositories..."
    helm_repo_add_if_missing bitnami        "https://charts.bitnami.com/bitnami"
    helm_repo_add_if_missing apache-airflow "https://airflow.apache.org"
    helm repo update

    # Build chart dependencies
    info "Building Helm chart dependencies..."
    _build_chart_deps "$CHART_DIR"

    # Resolved here — immediately ahead of the helm upgrade below, and only
    # when dataspoke-infra is actually part of this run — and read by
    # _api_image_helm_set_args / _frontend_helm_set_args inside
    # _helm_upgrade_dataspoke_dev to pin the dataspoke.io/image-digest pod
    # annotation and image reference onto every workload running that image.
    # --no-digest-pin skips resolution entirely; _helm_upgrade_dataspoke_dev
    # restarts explicitly after the upgrade instead. Scoped inside this
    # `_has_component dataspoke-infra` gate — a `--components
    # nginx-ingress|datahub|langfuse|dummy-data|dev-lock|seed` run never
    # touches the umbrella chart, so it must not perform a cloud lookup (or
    # dereference DATASPOKE_KUBE_IMAGE_REGISTRY under `set -u`) for it.
    API_IMAGE_DIGEST=""
    FRONTEND_IMAGE_DIGEST=""
    if [[ "${NO_DIGEST_PIN}" == "false" ]]; then
      API_IMAGE_DIGEST="$(_resolve_digest_or_abort "${DATASPOKE_KUBE_IMAGE_REGISTRY}/api:${IMAGE_TAG}")"
      if [[ "$FRONTEND_MODE" == "cluster" ]]; then
        FRONTEND_IMAGE_DIGEST="$(_resolve_digest_or_abort "${DATASPOKE_KUBE_IMAGE_REGISTRY}/frontend:${IMAGE_TAG}")"
      fi
    fi

    # Helm upgrade --install
    info "Installing DataSpoke umbrella chart..."
    _helm_upgrade_dataspoke_dev "${NS}"

    # Roll the Airflow pods still holding a superseded signing key or a
    # superseded metadata-DB connection string
    if [[ "${AIRFLOW_KEYS_ROTATED}" == "true" || "${AIRFLOW_METADATA_DSN_ROTATED}" == "true" ]]; then
      _restart_airflow_key_consumers "${NS}"
    fi

    # Ensure pgvector + AGE extensions
    info "Ensuring pgvector + age extensions in the dataspoke database..."

    # DATASPOKE_POSTGRES_{USER,DB} come from the app ConfigMap, not the
    # credentials Secret — they are non-secret and live there instead (see
    # spec/feature/HELM_CHART.md §ConfigMap keys). Only the password is a
    # secret, read from the consolidated Secret (never from .env).
    DS_POSTGRES_USER="$(_read_configmap_value "${NS}" "DATASPOKE_POSTGRES_USER")"
    DS_POSTGRES_PASSWORD="$(kubectl get secret dataspoke-secrets -n "${NS}" \
      -o jsonpath='{.data.DATASPOKE_POSTGRES_PASSWORD}' | base64 --decode)"
    DS_POSTGRES_DB="$(_read_configmap_value "${NS}" "DATASPOKE_POSTGRES_DB")"

    # Validate the postgres username before interpolating it into the GRANT
    # statements below.
    if [[ ! "${DS_POSTGRES_USER}" =~ ^[a-zA-Z_][a-zA-Z0-9_]{0,62}$ ]]; then
      error "DATASPOKE_POSTGRES_USER '${DS_POSTGRES_USER}' is not a valid SQL identifier."
    fi
    # Same shape check on the database name — it reaches `psql -d` below as
    # an argv element (no shell re-parses it), so a leading '-' would be
    # taken as a flag rather than a database name.
    if [[ ! "${DS_POSTGRES_DB}" =~ ^[a-zA-Z_][a-zA-Z0-9_]{0,62}$ ]]; then
      error "DATASPOKE_POSTGRES_DB '${DS_POSTGRES_DB}' is not a valid SQL identifier."
    fi

    kubectl rollout status statefulset/dataspoke-postgresql -n "${NS}" --timeout=5m >/dev/null 2>&1 || true
    kubectl exec -n "${NS}" dataspoke-postgresql-0 -- \
      env PGPASSWORD="${DS_POSTGRES_PASSWORD}" \
      psql -U postgres -d "${DS_POSTGRES_DB}" -c "
        CREATE EXTENSION IF NOT EXISTS vector;
        CREATE EXTENSION IF NOT EXISTS age;
        GRANT USAGE ON SCHEMA ag_catalog TO ${DS_POSTGRES_USER};
        GRANT SELECT ON ALL TABLES IN SCHEMA ag_catalog TO ${DS_POSTGRES_USER};
      " >/dev/null 2>&1 \
      && info "  Extensions ready (vector + age)." \
      || warn "  Could not create extensions — run manually via kubectl exec."

    # Wait for Airflow api-server
    info "Waiting for Airflow api-server to become ready..."
    kubectl rollout status deployment/dataspoke-airflow-api-server -n "${NS}" --timeout=5m \
      && info "Airflow api-server is ready." \
      || error "Airflow api-server did not become ready in time — check pod logs."

    # Wait for DataSpoke API
    info "Waiting for DataSpoke API to become ready..."
    kubectl rollout status deployment/dataspoke-api -n "${NS}" --timeout=5m \
      && info "DataSpoke API is ready." \
      || warn "DataSpoke API did not become ready in time."

    # Wait for the event-consumer. Warn rather than error: the consumer is not
    # on the path of any UC1–UC5 flow, so a failure here should not abort an
    # otherwise-good install — but it must be visible, since nothing else in
    # the dev loop looks at this pod.
    info "Waiting for DataSpoke event-consumer to become ready..."
    kubectl rollout status deployment/dataspoke-event-consumer -n "${NS}" --timeout=5m \
      && info "DataSpoke event-consumer is ready." \
      || warn "DataSpoke event-consumer did not become ready in time — check pod logs."

    # Wait for frontend (only when deployed in-cluster)
    if [[ "$FRONTEND_MODE" == "cluster" ]]; then
      info "Waiting for DataSpoke frontend to become ready..."
      kubectl rollout status deployment/dataspoke-frontend -n "${NS}" --timeout=5m \
        && info "DataSpoke frontend is ready." \
        || warn "DataSpoke frontend did not become ready in time — check pod logs."
    fi

    # Populate the auto-populated DATASPOKE_DEV_* block in .env for laptop-side dev access
    info "Writing DATASPOKE_DEV_* values to .env..."
    # DATASPOKE_POSTGRES_{USER,DB} come from the app ConfigMap, not the
    # credentials Secret — they are non-secret and live there instead (see
    # spec/feature/HELM_CHART.md §ConfigMap keys).
    _write_env_var        "DATASPOKE_DEV_POSTGRES_USER" "$(_read_configmap_value "${NS}" "DATASPOKE_POSTGRES_USER")"
    _sync_env_from_secret "${NS}" "DATASPOKE_POSTGRES_PASSWORD" "DATASPOKE_DEV_POSTGRES_PASSWORD"
    _write_env_var        "DATASPOKE_DEV_POSTGRES_DB"   "$(_read_configmap_value "${NS}" "DATASPOKE_POSTGRES_DB")"
    _sync_env_from_secret "${NS}" "DATASPOKE_REDIS_PASSWORD"    "DATASPOKE_DEV_REDIS_PASSWORD"
    _sync_env_from_secret "${NS}" "DATASPOKE_AIRFLOW_USER"      "DATASPOKE_DEV_AIRFLOW_USER"
    _sync_env_from_secret "${NS}" "DATASPOKE_AIRFLOW_PASSWORD"  "DATASPOKE_DEV_AIRFLOW_PASSWORD"
    _sync_env_from_secret "${NS}" "DATASPOKE_INTERNAL_TOKEN"    "DATASPOKE_DEV_INTERNAL_TOKEN"
    _sync_env_from_secret "${NS}" "DATASPOKE_JWT_SECRET_KEY"    "DATASPOKE_DEV_JWT_SECRET_KEY"

    # Laptop-side host/port for direct DB/cache access. In managed mode this is
    # the ingress LoadBalancer IP; in shared mode it is 127.0.0.1, reached via
    # `kubectl port-forward` (bin/port-forward.sh) on the same canonical ports.
    TCP_HOST="$(tcp_access_host)"
    _write_env_var "DATASPOKE_DEV_POSTGRES_HOST" "${TCP_HOST}"
    _write_env_var "DATASPOKE_DEV_POSTGRES_PORT" "9201"
    _write_env_var "DATASPOKE_DEV_REDIS_HOST"    "${TCP_HOST}"
    _write_env_var "DATASPOKE_DEV_REDIS_PORT"    "9202"
    _write_env_var "DATASPOKE_DEV_AIRFLOW_URL"   "$(ingress_scheme)://airflow.${DATASPOKE_KUBE_INGRESS_DOMAIN:-dev.dataspoke.example.com}"

    # Dummy-data source access. In shared mode TCP_HOST is 127.0.0.1 (port-forward);
    # in managed mode it is the LoadBalancer IP (nginx TCP passthrough).
    # _POSTGRES_HOST_PORT is the in-cluster cluster-DNS address used by the
    # DataSpoke API pod when building ingestion source recipes — it is the same
    # in both modes because the API always runs in-cluster.
    _write_env_var "DATASPOKE_DEV_DUMMY_DATA_POSTGRES_HOST"      "${TCP_HOST}"
    _write_env_var "DATASPOKE_DEV_DUMMY_DATA_KAFKA_BROKERS"      "${TCP_HOST}:9104"
    _write_env_var "DATASPOKE_DEV_DUMMY_DATA_POSTGRES_HOST_PORT" \
      "example-postgres.${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE}.svc.cluster.local:5432"

    # Dev-lock URL — same pattern: 127.0.0.1 in shared mode, LoadBalancer IP in managed.
    _write_env_var "DATASPOKE_DEV_LOCK_URL" "http://${TCP_HOST}:9221"

    info ".env updated with DATASPOKE_DEV_* values."
  fi

  # -----------------------------------------------------------------------
  # Phase 4: Parallel post-bootstrap
  # -----------------------------------------------------------------------
  step 4 5 "parallel post-bootstrap (dummy-data + dev-lock)"

  if _has_component dummy-data; then
    _run_bg "dummy-data" bash "$SCRIPT_DIR/dev-peripherals/dummy-data.sh"
  fi
  if _has_component dev-lock; then
    _run_bg "dev-lock" bash "$SCRIPT_DIR/dev-peripherals/dev-lock.sh"
  fi

  _wait_all

  # -----------------------------------------------------------------------
  # Phase 5: Post-install seeding
  # -----------------------------------------------------------------------
  if _has_component seed && [[ "$SKIP_SEED" == "false" ]]; then
    step 5 5 "post-install seeding"
    bash "$SCRIPT_DIR/post-install/seed-peripheral-config.sh"
    bash "$SCRIPT_DIR/post-install/seed-runtime-config.sh"
    bash "$SCRIPT_DIR/post-install/seed-admin-user.sh"
  else
    info "Skipping seeding (--skip-seed or 'seed' not in components)."
  fi

  # Write src/frontend/.env.local when local mode is requested
  if [[ "$FRONTEND_MODE" == "local" ]]; then
    _write_frontend_env_local "${DATASPOKE_KUBE_INGRESS_DOMAIN:-dev.dataspoke.example.com}"
  fi

  # -----------------------------------------------------------------------
  # Re-read .env for summary
  # -----------------------------------------------------------------------
  source "$ENV_FILE"
  SCHEME="$(ingress_scheme)"

  echo ""
  echo "=== Installation complete (profile: dev) ==="
  echo ""
  echo "Namespaces:"
  kubectl get namespaces \
    "${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE}" \
    "${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}" \
    "${DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE}" \
    "${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE}" 2>/dev/null || true
  echo ""
  if [[ "$(ingress_mode)" == "shared" ]]; then
    echo "Ingress endpoints (via shared cluster ingress; domain ${DATASPOKE_KUBE_INGRESS_DOMAIN:-<not set>}):"
  else
    echo "Ingress endpoints (via nginx-ingress at ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}):"
  fi
  echo ""
  echo "  DataHub UI:    ${SCHEME}://datahub.${DATASPOKE_KUBE_INGRESS_DOMAIN:-<not set>}/"
  echo "  DataHub GMS:   ${SCHEME}://datahub-gms.${DATASPOKE_KUBE_INGRESS_DOMAIN:-<not set>}/"
  echo "  DataSpoke API: ${SCHEME}://api.${DATASPOKE_KUBE_INGRESS_DOMAIN:-<not set>}/api/v1/"
  echo "  Airflow UI:    ${SCHEME}://airflow.${DATASPOKE_KUBE_INGRESS_DOMAIN:-<not set>}/"
  echo "  Langfuse UI:   ${DATASPOKE_DEV_LANGFUSE_HOST:-${SCHEME}://langfuse.<not set>}/"
  echo ""
  if [[ "$(ingress_mode)" == "shared" ]]; then
    echo "  TCP services (Postgres/Redis/Kafka/lock) are not on the shared ingress."
    echo "  Open them on 127.0.0.1 with: ./helm-charts/bin/port-forward.sh"
    echo "    PostgreSQL 127.0.0.1:9201   Redis 127.0.0.1:9202   DataHub Kafka 127.0.0.1:9005"
    echo "    Example PG 127.0.0.1:9102   Example Kafka 127.0.0.1:9104   Lock API 127.0.0.1:9221"
  else
    echo "  PostgreSQL:    ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}:9201"
    echo "  Redis:         ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}:9202"
    echo "  DataHub Kafka: ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}:9005"
    echo "  Example PG:    ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}:9102"
    echo "  Example Kafka: ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}:9104"
    echo "  Lock API:      ${DATASPOKE_KUBE_INGRESS_IP:-<not set>}:9221"
  fi
  echo ""
  echo "  Credentials (auto-generated): see DATASPOKE_DEV_AIRFLOW_{USER,PASSWORD} in ${ENV_FILE}"
  echo "  Langfuse: ${DATASPOKE_DEV_LANGFUSE_INIT_USER_EMAIL:-dataspoke@dataspoke.local} / ${DATASPOKE_DEV_LANGFUSE_INIT_USER_PASSWORD:-<see .env>}"
  echo ""
  case "$FRONTEND_MODE" in
    none)
      echo "  Frontend:      not deployed (--frontend none). Use --frontend local | cluster to deploy."
      ;;
    local)
      echo "  Frontend (host dev):"
      echo "    src/frontend/.env.local written (API: ${SCHEME}://api.${DATASPOKE_KUBE_INGRESS_DOMAIN:-<not set>}, DataHub: ${SCHEME}://datahub.${DATASPOKE_KUBE_INGRESS_DOMAIN:-<not set>})"
      echo "    Run:   pnpm -C src/frontend install && pnpm -C src/frontend dev"
      echo "    Open:  http://localhost:3000"
      if [[ "$SKIP_SEED" == "true" ]]; then
        echo "    Login: (admin not seeded — --skip-seed)"
      else
        echo "    Login: dataspoke@dataspoke.local / dataspoke  (rotate via PATCH /auth/me)"
      fi
      ;;
    cluster)
      echo "  Frontend (in-cluster):"
      echo "    Web UI: ${SCHEME}://app.${DATASPOKE_KUBE_INGRESS_DOMAIN:-<not set>}/"
      if [[ "$SKIP_SEED" == "true" ]]; then
        echo "    Login:  (admin not seeded — --skip-seed)"
      else
        echo "    Login:  dataspoke@dataspoke.local / dataspoke  (rotate via PATCH /auth/me)"
      fi
      ;;
  esac
  echo ""
  echo "API iteration:"
  echo "  ./helm-charts/bin/install.sh --profile dev --components api"
  echo ""
  echo "Seed dummy data:"
  echo "  uv run python -m tests.integration.util --reset-seed"
  echo ""
  echo "Health check:"
  echo "  ./helm-charts/bin/health-check.sh"
  echo ""
  info "Total elapsed: $((SECONDS - START_TIME))s ($(printf '%dm%02ds' $(( (SECONDS - START_TIME) / 60 )) $(( (SECONDS - START_TIME) % 60 ))))"
  echo ""

# ---------------------------------------------------------------------------
# PROD PROFILE
# ---------------------------------------------------------------------------
elif [[ "$PROFILE" == "prod" ]]; then

  if [[ "${IMAGE_TAG_EXPLICIT}" != true ]]; then
    error "--profile prod requires an explicit --image-tag <tag> to avoid deploying the mutable ':dev' tag onto a shared registry."
  fi

  NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"

  # Derive frontend.enabled from FRONTEND_MODE (true=cluster, false=none) up
  # front — resolved this early (rather than inside Phase 3, where the helm
  # upgrade itself lives) because the --skip-build early digest resolution
  # below, in Phase 1, needs to know whether a frontend digest is in scope
  # too.
  if [[ "$FRONTEND_MODE" == "cluster" ]]; then
    _prod_frontend_enabled="true"
  else
    _prod_frontend_enabled="false"
  fi

  # -----------------------------------------------------------------------
  # Phase 1: Pre-flight (no nginx-ingress — operator's controller)
  # -----------------------------------------------------------------------
  step 1 3 "pre-flight"
  use_context "${DATASPOKE_KUBE_CLUSTER}"
  ensure_namespace "${NS}"

  # Verify the operator's shared ingress controller is installed (fail fast).
  # Required explicitly in prod — no `nginx` default; see
  # assert_ingress_class_present in lib/helpers.sh, the shared probe the
  # standalone pre-flight runs too, for why.
  : "${DATASPOKE_KUBE_INGRESS_CLASS:?must be set explicitly in the prod .env — install.sh --sets this class onto every DataSpoke Ingress, overriding the className in your --values overlay}"
  INGRESS_CLASS="$(ingress_class)"
  assert_ingress_class_present "${INGRESS_CLASS}"
  info "IngressClass '${INGRESS_CLASS}' is present."

  # Refuse to publish /internal/* on the public API ingress (fail fast). See
  # _assert_no_internal_ingress_exposure's docstring in lib/helpers.sh for why
  # this is a prod-only pre-flight gate rather than a narrower chart default.
  _assert_no_internal_ingress_exposure "$CHART_DIR/values.yaml" "${EXTRA_VALUES:-}"
  info "API ingress paths do not publish /internal/*."

  # Refuse an overlay that would silently replace the Airflow SimpleAuthManager
  # passwords-file init container/volume/mount (fail fast). See
  # _assert_no_airflow_simple_auth_overlay_conflict's docstring in lib/helpers.sh.
  _assert_no_airflow_simple_auth_overlay_conflict "${EXTRA_VALUES:-}"
  info "--values overlay does not conflict with the Airflow SimpleAuthManager passwords-file mechanism."

  # Verify every StorageClass the operator's overlay pins exists AND, where
  # it names an out-of-tree CSI provisioner, that the matching CSIDriver is
  # actually registered (fail fast). An overlay that pins none skips cleanly.
  # See assert_pinned_storage_classes in lib/helpers.sh for the fifteen
  # overlay keys it resolves, the Bitnami/Airflow spelling split, and why a
  # missing class has to fail here rather than as a rollout timeout later.
  assert_pinned_storage_classes "${EXTRA_VALUES:-}"

  # --skip-build assumes the image was already pushed by a prior run (e.g. a
  # CI pipeline that built and pushed, then invoked this script only to
  # deploy) — the image already exists in the registry at this point, so its
  # digest can be resolved now, before any credential Secret below is created
  # or mutated. This matters for a deploy-only host that lacks gcloud/aws on
  # PATH (a supported shape — it is exactly what --skip-build is for):
  # without this early resolution, Phase 1 would create/update
  # dataspoke-airflow-metadata-encryption-key (the only Secret Phase 1
  # writes — dataspoke-airflow-metadata-db and the two signing-key
  # projections are Phase 3's, see that phase) and only then discover in
  # Phase 3 that no digest could be resolved and abort — mutating a cluster
  # Secret on a run that was never going to complete. When a build is about
  # to run (the default, no --skip-build), the image does not exist in the
  # registry yet at this point, so resolution stays where it can actually
  # succeed: immediately after Phase 2's push, in Phase 3 below.
  API_IMAGE_DIGEST=""
  FRONTEND_IMAGE_DIGEST=""
  if [[ "$SKIP_BUILD" == "true" && "${NO_DIGEST_PIN}" == "false" ]]; then
    API_IMAGE_DIGEST="$(_resolve_digest_or_abort "${DATASPOKE_KUBE_IMAGE_REGISTRY}/api:${IMAGE_TAG}")"
    if [[ "${_prod_frontend_enabled}" == "true" ]]; then
      FRONTEND_IMAGE_DIGEST="$(_resolve_digest_or_abort "${DATASPOKE_KUBE_IMAGE_REGISTRY}/frontend:${IMAGE_TAG}")"
    fi
  fi

  # Determine which Secret name is in play (default or BYO overlay)
  EXISTING_SECRET_NAME=""
  if [[ -n "${EXTRA_VALUES:-}" && -f "${EXTRA_VALUES}" ]]; then
    EXISTING_SECRET_NAME="$(_resolve_existing_secret_name "${EXTRA_VALUES}")" \
      || error "Could not parse the --values overlay for secrets.existingSecret (see above)."
  fi
  SECRET_TO_CHECK="${EXISTING_SECRET_NAME:-dataspoke-secrets}"
  # Grammar-check before it reaches kubectl argv or a `helm --set` token
  # below — the same reasoning as the StorageClass name check above: an
  # overlay value beginning with `-` would be parsed as a flag, and a comma
  # or `=` in the value would split a `--set` token into more than one
  # assignment. The shared gates in lib/helpers.sh assert the same grammar
  # themselves (assert_k8s_name), since they are reachable from the
  # standalone pre-flight's --secret-name too; this stays because the
  # `--set` tokens below are install.sh's own and are built here.
  if ! [[ "$SECRET_TO_CHECK" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]]; then
    error "secrets.existingSecret '${SECRET_TO_CHECK}' in your --values overlay is not a valid Kubernetes name."
  fi

  # Verify operator-pre-created Secret (fail fast; never auto-generate in prod)
  _ensure_dataspoke_secrets "${NS}" "prod" "${SECRET_TO_CHECK}"

  # Validate ALL required keys are present and not insecure defaults. Reads
  # the effective simple_auth_manager_all_admins from $CHART_DIR/values.yaml
  # merged with the operator's --values overlay (${EXTRA_VALUES}), same
  # inputs as _assert_no_internal_ingress_exposure above. The key-length
  # report is the standalone pre-flight's audit mode, not an install's, so
  # the trailing argument stays unset here.
  verify_credential_secret "${NS}" "${SECRET_TO_CHECK}" "$CHART_DIR/values.yaml" "${EXTRA_VALUES:-}"

  # Compare the Fernet key before any other Secret in this run is mutated: on
  # a mismatch it aborts non-mutating, so running it here keeps the
  # "pre-flight fails before any resources are created" promise (README.md
  # §2) true even for this check. _derive_airflow_metadata_secret and
  # _ensure_airflow_key_secrets — the two rotating Secret writes — both run
  # in Phase 3 (see that phase), so the ordering holds a fortiori. Phase 1's
  # only mutation is this Fernet projection's idempotent create.
  _ensure_airflow_fernet_secret "${NS}" "${SECRET_TO_CHECK}"

  # -----------------------------------------------------------------------
  # Phase 2: Image builds (skippable)
  # -----------------------------------------------------------------------
  if [[ "$SKIP_BUILD" == "false" ]]; then
    step 2 3 "image builds (parallel)"

    _run_bg "build-api"      bash "$SCRIPT_DIR/build-image.sh" api      "${IMAGE_TAG}"
    _run_bg "build-airflow"  bash "$SCRIPT_DIR/build-image.sh" airflow  "${IMAGE_TAG}"
    _run_bg "build-postgres" bash "$SCRIPT_DIR/build-image.sh" postgres "${IMAGE_TAG}"
    if [[ "$FRONTEND_MODE" == "cluster" ]]; then
      _run_bg "build-frontend" bash "$SCRIPT_DIR/build-image.sh" frontend "${IMAGE_TAG}"
    fi
    _wait_all
  else
    step 2 3 "image builds (skipped via --skip-build)"
    info "Using pre-built images tagged '${IMAGE_TAG}'."
  fi

  # -----------------------------------------------------------------------
  # Phase 3: Umbrella chart
  # -----------------------------------------------------------------------
  step 3 3 "umbrella chart (prod)"

  VALUES_ARGS=(-f "$CHART_DIR/values.yaml")

  # Prod-only Airflow SimpleAuthManager passwords-file materialisation (init
  # container + emptyDir + volumeMount) — see
  # _build_airflow_simple_auth_init_container_file's docstring for why this
  # rides on its own -f layer instead of a static values.yaml key. Placed
  # ahead of the operator's own --values overlay in VALUES_ARGS so an overlay
  # CAN extend airflow.apiServer.extraInitContainers/extraVolumes/
  # extraVolumeMounts for an unrelated reason — but Helm replaces LIST values
  # wholesale rather than merging them, so an overlay that sets any of the
  # three at all silently deletes this mechanism instead of extending it;
  # _assert_no_airflow_simple_auth_overlay_conflict (pre-flight, above) is
  # the guard against that, not this ordering. Rendered unconditionally in
  # prod — it is inert (never opened) when the effective
  # simple_auth_manager_all_admins is "true", so no branch on
  # _resolve_effective_all_admins is needed here.
  airflow_simple_auth_values_file="$(_build_airflow_simple_auth_init_container_file "${NS}" "${SECRET_TO_CHECK}")"
  VALUES_ARGS+=(-f "${airflow_simple_auth_values_file}")

  if [[ -n "$EXTRA_VALUES" ]]; then
    if [[ ! -f "$EXTRA_VALUES" ]]; then
      error "Extra values file not found: $EXTRA_VALUES"
    fi
    VALUES_ARGS+=(-f "$EXTRA_VALUES")
    info "Using extra values file: $EXTRA_VALUES"
  else
    info "No --values overlay provided. Using values.yaml defaults only."
    info "Production deployments typically require an operator overlay."
  fi

  info "Adding/updating Helm repositories..."
  helm_repo_add_if_missing bitnami        "https://charts.bitnami.com/bitnami"
  helm_repo_add_if_missing apache-airflow "https://airflow.apache.org"
  helm repo update

  info "Building Helm chart dependencies..."
  _build_chart_deps "$CHART_DIR"

  # Build resolved extraEnv referencing the operator secret name. "true":
  # prod also carries AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_{USERS,PASSWORDS_FILE}
  # (§Airflow authentication) — harmless when the effective all_admins is
  # "True" (SimpleAuthManager never reads either), load-bearing otherwise.
  local_extra_env_file="$(_build_airflow_extra_env_file "${NS}" "${SECRET_TO_CHECK}" "true")"

  info "Installing DataSpoke umbrella chart (prod)..."

  # Resolved here — read by _api_image_helm_set_args below (api_image_prod_args)
  # and used directly for the frontend --set-string flags further down — to
  # pin the dataspoke.io/image-digest pod annotation and image reference onto
  # every workload this upgrade renders, so `helm upgrade` creates a new
  # ReplicaSet correctly by construction instead of relying solely on an
  # explicit rollout restart. --no-digest-pin skips resolution entirely (both
  # variables stay empty) and the restart-floor block below restarts every
  # rendered workload unconditionally instead. The frontend digest is
  # resolved only when the frontend is actually being deployed. Under
  # --skip-build the image was already pushed by a prior run, so both
  # variables were already resolved in Phase 1, ahead of any credential
  # Secret mutation — see that block's comment. Only the default
  # build-then-deploy path (SKIP_BUILD == false) resolves here, since the
  # image does not exist in the registry until Phase 2's push, just above,
  # completes.
  if [[ "$SKIP_BUILD" == "false" && "${NO_DIGEST_PIN}" == "false" ]]; then
    API_IMAGE_DIGEST="$(_resolve_digest_or_abort "${DATASPOKE_KUBE_IMAGE_REGISTRY}/api:${IMAGE_TAG}")"
    if [[ "${_prod_frontend_enabled}" == "true" ]]; then
      FRONTEND_IMAGE_DIGEST="$(_resolve_digest_or_abort "${DATASPOKE_KUBE_IMAGE_REGISTRY}/frontend:${IMAGE_TAG}")"
    fi
  fi

  # Derive the Airflow metadata Secret from the operator Secret. Runs in
  # Phase 3, not Phase 1: this call rewrites dataspoke-airflow-metadata-db
  # whenever DATASPOKE_POSTGRES_PASSWORD has rotated, and the only thing that
  # repairs the resulting split between that write and what the running
  # Airflow pods hold is _restart_airflow_key_consumers further below.
  # Running it in Phase 1 would put the whole of Phase 2 (image builds)
  # between the write and the restart — the same gap
  # _ensure_airflow_key_secrets' own comment below describes for the
  # signing-key path — and a Phase-3 upgrade failure would leave a rewritten
  # DSN the pods only adopt on some later, unrelated restart. Phase 1's only
  # mutation stays the Fernet projection's idempotent create (README.md §2's
  # "pre-flight fails before any resources are created" promise).
  _derive_airflow_metadata_secret "${NS}" "${SECRET_TO_CHECK}"

  # Derive Airflow key secrets from the operator Secret. Ordered after digest
  # resolution rather than beside the other Phase-1 credential derivations:
  # this call writes a rotated key into the projected Secrets, and only
  # _restart_airflow_key_consumers further below repairs the resulting split
  # (same reasoning covers _derive_airflow_metadata_secret just above). What
  # remains between this write and the restart is the `helm upgrade` below —
  # a failed upgrade strands the key the same way; restarting the consumers
  # directly after this call, ahead of the upgrade, closes the rest.
  _ensure_airflow_key_secrets "${NS}" "${SECRET_TO_CHECK}"

  api_image_prod_args=()
  while IFS= read -r _iarg; do
    api_image_prod_args+=("${_iarg}")
  done < <(_api_image_helm_set_args)

  # --set treats `.` as a path separator, so the dot in the annotation key
  # must be escaped as `\.` — same idiom as _api_image_helm_set_args /
  # _frontend_helm_set_args above. Emitted only when the digest resolved
  # (skipped when the frontend is disabled or resolution failed).
  frontend_image_digest_args=()
  if [[ -n "${FRONTEND_IMAGE_DIGEST:-}" ]]; then
    frontend_image_digest_args=(
      --set-string "frontend.image.digest=${FRONTEND_IMAGE_DIGEST}"
      --set-string "frontend.podAnnotations.dataspoke\.io/image-digest=${FRONTEND_IMAGE_DIGEST}"
    )
  elif [[ "${NO_DIGEST_PIN}" == "true" && "${_prod_frontend_enabled}" == "true" ]]; then
    # Clears any frontend.image.digest the operator's --values overlay may
    # have set — omitting a --set only removes what a previous --set
    # supplied, not a value set via -f, so without this an overlay-pinned
    # digest would survive --no-digest-pin.
    frontend_image_digest_args=(--set-string "frontend.image.digest=")
  fi

  # Forces frontend.image.pullPolicy to Always under --no-digest-pin, same
  # reasoning as _api_image_helm_set_args's api/event-consumer pullPolicy
  # flags above — chart default is IfNotPresent, safe only when the image
  # reference itself is content-addressed. Unlike the dev fast paths (which
  # render the frontend via _frontend_helm_set_args, always Always
  # regardless of digest pin), prod sets the frontend's image fields
  # directly below, so this flag is the only place that pins it. Emitted
  # only when the frontend is actually being deployed.
  frontend_pull_policy_prod_args=()
  if [[ "${NO_DIGEST_PIN}" == "true" && "${_prod_frontend_enabled}" == "true" ]]; then
    frontend_pull_policy_prod_args=(--set "frontend.image.pullPolicy=Always")
  fi

  # One class for every Ingress this release renders, taken from
  # DATASPOKE_KUBE_INGRESS_CLASS (required and verified against the cluster in
  # pre-flight above). These --set flags outrank a class written into the
  # operator's --values overlay: the env var is the single place an operator on
  # `alb` or `traefik` changes it. The API and frontend templates read
  # `ingress.className`, the Airflow chart reads
  # `ingress.apiServer.ingressClassName` — the wrong spelling is silently
  # dropped and GKE falls back to provisioning a GCE LoadBalancer.
  INGRESS_CLS="$(ingress_class)"

  helm upgrade --install dataspoke "$CHART_DIR" \
    "${VALUES_ARGS[@]}" \
    -n "${NS}" \
    --set "api.ingress.className=${INGRESS_CLS}" \
    --set "frontend.ingress.className=${INGRESS_CLS}" \
    --set "airflow.ingress.apiServer.ingressClassName=${INGRESS_CLS}" \
    "${api_image_prod_args[@]}" \
    --set-string postgresql.image.registry="" \
    --set-string "postgresql.image.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/postgres" \
    --set-string "postgresql.image.tag=${IMAGE_TAG}" \
    --set-string "airflow.images.airflow.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/airflow" \
    --set-string "airflow.images.airflow.tag=${IMAGE_TAG}" \
    --set "api.enabled=true" \
    --set "frontend.enabled=${_prod_frontend_enabled}" \
    --set "frontend.image.repository=${DATASPOKE_KUBE_IMAGE_REGISTRY}/frontend" \
    --set "frontend.image.tag=${IMAGE_TAG}" \
    ${frontend_image_digest_args[@]+"${frontend_image_digest_args[@]}"} \
    ${frontend_pull_policy_prod_args[@]+"${frontend_pull_policy_prod_args[@]}"} \
    --set-file "airflow.extraEnv=${local_extra_env_file}" \
    --set "airflow.apiSecretKeySecretName=dataspoke-airflow-api-secret-key" \
    --set "airflow.jwtSecretName=dataspoke-airflow-jwt-secret" \
    --set "airflow.fernetKeySecretName=dataspoke-airflow-metadata-encryption-key" \
    --set-string "secrets.existingSecret=${SECRET_TO_CHECK}" \
    --set-string "postgresql.auth.existingSecret=${SECRET_TO_CHECK}" \
    --set-string "redis.auth.existingSecret=${SECRET_TO_CHECK}" \
    --set-string "event-consumer.existingSecretName=${SECRET_TO_CHECK}" \
    --timeout 15m

  # -----------------------------------------------------------------------
  # Roll the Airflow pods still holding a superseded signing key or a
  # superseded metadata-DB connection string. This runs immediately after the
  # helm upgrade and before any rollout-status wait below, so an abort on one
  # of those waits (a real risk — they're 5m timeouts against a fresh
  # rollout) never leaves the credentials Secret holding a rotated value that
  # the running Airflow pods were never restarted to pick up. Every call site
  # of this helper orders it the same way, for the same reason.
  # -----------------------------------------------------------------------
  if [[ "${AIRFLOW_KEYS_ROTATED}" == "true" || "${AIRFLOW_METADATA_DSN_ROTATED}" == "true" ]]; then
    _restart_airflow_key_consumers "${NS}"
  fi

  # -----------------------------------------------------------------------
  # Under a digest pin, the pod-template hash changed exactly when the
  # digest changed, so Helm already rolled whatever it needed to. Under
  # --no-digest-pin there is no digest to change the pod template, so
  # restart every workload this upgrade renders explicitly. `helm upgrade`
  # above has no `--wait`, so it can return before either a Helm-triggered
  # roll or an explicit restart has finished — `kubectl rollout status`
  # follows so the script does not exit mid-roll.
  # -----------------------------------------------------------------------
  if [[ "${NO_DIGEST_PIN}" == "true" ]]; then
    info "Restarting api/event-consumer workloads (--no-digest-pin: no digest pin to change the pod template)..."
    _rollout_restart_workload "${NS}" "dataspoke-api"
    _rollout_restart_workload "${NS}" "dataspoke-event-consumer"
  fi
  # Wait for the Airflow api-server BEFORE dataspoke-api. Without this wait
  # a broken simple-auth-manager-passwords init container — a
  # missing/rejected credential the pre-flight above should have already
  # caught, or a CreateContainerConfigError from a Secret race — would let
  # the script report install success over a permanently crash-looping
  # Airflow. This is the same wait dev runs (see "Waiting for Airflow
  # api-server to become ready" above).
  info "Waiting for Airflow api-server to become ready..."
  kubectl rollout status deployment/dataspoke-airflow-api-server -n "${NS}" --timeout=5m \
    || error "dataspoke-airflow-api-server did not become ready after the upgrade — check pod logs
(kubectl logs -n '${NS}' deploy/dataspoke-airflow-api-server), in particular the
simple-auth-manager-passwords init container if airflow.config.core.simple_auth_manager_all_admins
is not a true-ish value: kubectl logs -n '${NS}' deploy/dataspoke-airflow-api-server -c simple-auth-manager-passwords"
  kubectl rollout status deployment/dataspoke-api -n "${NS}" --timeout=5m \
    || error "dataspoke-api did not become ready after the upgrade — check pod logs (kubectl logs -n '${NS}' deploy/dataspoke-api)."
  # event-consumer.enabled defaults to false in prod (dataspoke/values.yaml)
  # and ships commented out in values-prod.example.yaml — an operator overlay
  # is the only way to turn it on. Waiting unconditionally would abort every
  # default prod install on `kubectl rollout status` against a Deployment the
  # chart never rendered. Gate on whether the object actually exists
  # post-upgrade — the ground truth of what this release rendered.
  #
  # `--ignore-not-found -o name`, not a bare exit-code check: a plain
  # `kubectl get ... >/dev/null 2>&1` conflates "not found" with a transient
  # API-server error or an RBAC denial (both non-zero exit, or in some client
  # versions zero exit with an empty error body) — either would silently skip
  # this readiness gate. With --ignore-not-found, a genuine NotFound prints
  # nothing and still exits 0; any other failure exits non-zero and is
  # reported as what it is instead of being read as "not deployed".
  #
  # stderr goes to a file rather than being merged into the capture: the
  # emptiness test below is the "was it rendered" signal, and kubectl writes
  # non-fatal notices there on success — an exec-credential plugin notice, an
  # auth-plugin deprecation warning, a server-side Warning header. Merged with
  # 2>&1, any of those makes a genuine NotFound read as "deployed" and the
  # wait then aborts the install against an object the chart never created.
  # The stderr file lives in INSTALL_TMPDIR so the EXIT trap reclaims it on the
  # error path too.
  _ec_get_out=""
  _ec_get_err="$(mktemp "${INSTALL_TMPDIR}/event-consumer-get-err.XXXX")"
  if ! _ec_get_out="$(kubectl get deployment/dataspoke-event-consumer -n "${NS}" --ignore-not-found -o name 2>"${_ec_get_err}")"; then
    error "Could not check whether dataspoke-event-consumer is deployed: $(cat "${_ec_get_err}")"
  fi
  if [[ -n "${_ec_get_out}" ]]; then
    kubectl rollout status deployment/dataspoke-event-consumer -n "${NS}" --timeout=5m \
      || error "dataspoke-event-consumer did not become ready after the upgrade — check pod logs (kubectl logs -n '${NS}' deploy/dataspoke-event-consumer)."
  else
    info "dataspoke-event-consumer not deployed (event-consumer.enabled=false) — skipping rollout wait."
  fi
  if [[ "${_prod_frontend_enabled}" == "true" ]]; then
    if [[ "${NO_DIGEST_PIN}" == "true" ]]; then
      _rollout_restart_workload "${NS}" "dataspoke-frontend"
    fi
    kubectl rollout status deployment/dataspoke-frontend -n "${NS}" --timeout=5m \
      || error "dataspoke-frontend did not become ready after the upgrade — check pod logs (kubectl logs -n '${NS}' deploy/dataspoke-frontend)."
  fi

  # -----------------------------------------------------------------------
  # Seed default admin user (idempotent)
  # -----------------------------------------------------------------------
  # _ADMIN_SEED_STATUS: 0 (skipped or fully succeeded), 2 (seed-admin-user.sh's
  # ROTATION_FAILED_EXIT — the bootstrap succeeded but the rotation attempt
  # did not land, e.g. a transient 429 from the auth limiter), or anything
  # else (bootstrap itself failed — a broken deployment, not a rotation
  # hiccup). Only the last case aborts: `|| _ADMIN_SEED_STATUS=$?` catches the
  # exit under `set -e` rather than letting it end this script, because by
  # this point the helm upgrade above already succeeded and discarding a
  # completed install over a rotation retry is worse than reporting it loudly
  # in the summary below.
  _ADMIN_SEED_STATUS=0
  if [[ "$SKIP_SEED" == "false" ]]; then
    # The seed script's api_internal_request helper (bin/lib/helpers.sh)
    # kubectl execs into the API pod and calls its own loopback port
    # directly — no ingress or DNS involved. The unconditional
    # dataspoke-api rollout-status wait above already guarantees the pod
    # is Ready to accept the call.
    info "Seeding default admin user..."
    bash "$SCRIPT_DIR/post-install/seed-admin-user.sh" || _ADMIN_SEED_STATUS=$?
    if (( _ADMIN_SEED_STATUS != 0 && _ADMIN_SEED_STATUS != 2 )); then
      error "Seeding the default admin user failed (see the output above) after the helm upgrade
already succeeded — the release is up, but the built-in admin account may not exist. Investigate
and re-run: bash ${SCRIPT_DIR}/post-install/seed-admin-user.sh"
    fi
  else
    info "Skipping admin user seed (--skip-seed)."
  fi

  echo ""
  echo "=== Installation complete (profile: prod) ==="
  echo ""
  echo "  Helm release: dataspoke  namespace: ${NS}"
  echo ""
  # Whether the admin seed above rotated the account, evaluated on exactly the
  # predicate seed-admin-user.sh rotates on — the env file naming the prod
  # profile through the shared seed_profile, plus a non-blank password — AND
  # requiring _ADMIN_SEED_STATUS == 0. Without that last condition a rotation
  # this run attempted and FAILED (status 2, reported above but not fatal to
  # this script — see the call site) would still print as "rotated": the
  # preconditions for attempting it were met, but attempting is not the same
  # as succeeding. Printing the published default as the login while it is no
  # longer live is worse than printing nothing: an operator reads it as the
  # credential to use and, failing that, as one still worth rotating.
  _admin_rotated=false
  if [[ "$SKIP_SEED" == "false" && -n "${DATASPOKE_PROD_ADMIN_PASSWORD:-}" \
        && "$(seed_profile "$ENV_FILE")" == "prod" && "${_ADMIN_SEED_STATUS}" == "0" ]]; then
    _admin_rotated=true
  fi
  if [[ "$FRONTEND_MODE" == "cluster" ]]; then
    # Best-effort: resolve the deployed frontend ingress host
    _frontend_host="$(kubectl get ingress -n "${NS}" \
      -o jsonpath='{.items[?(@.metadata.name=="dataspoke-frontend")].spec.rules[0].host}' 2>/dev/null || true)"
    if [[ -n "${_frontend_host}" ]]; then
      echo "  Web UI: http://${_frontend_host}/"
    else
      echo "  Web UI: served at your configured frontend.ingress host (see your operator overlay)."
    fi
  else
    echo "  Frontend: disabled (--frontend none)."
  fi
  # Outside the frontend conditional deliberately. The account exists and is
  # reachable through the API whether or not a UI was deployed, so tying the
  # only notice about the published default to `--frontend cluster` would let a
  # `--frontend none` prod install finish with no warning about it at all.
  if [[ "$SKIP_SEED" == "true" ]]; then
    echo "  Login:  dataspoke@dataspoke.local  (admin not seeded — --skip-seed)"
  elif [[ "${_admin_rotated}" == "true" ]]; then
    echo "  Login:  dataspoke@dataspoke.local  (password: DATASPOKE_PROD_ADMIN_PASSWORD in ${ENV_FILE};"
    echo "          read the admin seed's verdict above before treating it as the live one)"
  elif [[ "${_ADMIN_SEED_STATUS}" == "2" ]]; then
    # A rotation was attempted (the preconditions above were met) and did not
    # land — see seed-admin-user.sh's ROTATION_FAILED_EXIT. Loud, and distinct
    # from the "nothing attempted" branch below: an operator scanning past
    # the wall of text above should not have to notice a missing "Rotated"
    # line to learn this.
    echo "  Login:  dataspoke@dataspoke.local  (ROTATION TO DATASPOKE_PROD_ADMIN_PASSWORD FAILED THIS RUN"
    echo "          — see the seed-admin-user.sh output above. The password published in this"
    echo "          repository may still be live. Re-run: bash ${SCRIPT_DIR}/post-install/seed-admin-user.sh)"
  else
    # "may still be live", not "still live": with DATASPOKE_PROD_ADMIN_PASSWORD
    # blank this run rotated nothing, which says nothing about whether an
    # earlier one — or an operator with a curl — already did. Asserting the
    # published default is live against a deployment whose admin was rotated by
    # hand is a claim this script cannot check, and one that reads as a
    # completed step in reverse.
    echo "  Login:  dataspoke@dataspoke.local / dataspoke  (PUBLISHED DEFAULT — this run rotated"
    echo "          nothing, so it may still be live; rotate via PATCH /api/v1/auth/me, or set"
    echo "          DATASPOKE_PROD_ADMIN_PASSWORD and re-run bin/post-install/seed-admin-user.sh)"
  fi
  echo ""
  # The seed scripts read the same env file, so they are only worth naming when
  # it actually carries a block for them to send. Otherwise the admin routes are
  # the honest instruction — a seed command against blank blocks patches
  # nothing and reads as a step that was completed.
  if [[ -n "${DATASPOKE_PROD_PERIPHERAL_DATAHUB_GMS_URL:-}" || -n "${DATASPOKE_PROD_LLM_PROVIDER:-}" ]]; then
    echo "  Post-install: apply the peripheral and LLM blocks of ${ENV_FILE} with:"
    echo "    ENV_FILE=${ENV_FILE} bash ${SCRIPT_DIR}/post-install/seed-peripheral-config.sh"
    echo "    ENV_FILE=${ENV_FILE} bash ${SCRIPT_DIR}/post-install/seed-runtime-config.sh"
  else
    echo "  Post-install: configure peripherals and runtime settings via:"
    echo "    /api/v1/admin/peripherals/{datahub,langfuse}"
    echo "    /api/v1/admin/conf"
  fi
  echo ""
  info "Total elapsed: $((SECONDS - START_TIME))s"
  echo ""
fi
