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

# IMAGE_TAG flows unvalidated into several `--set`/`--set-string` tokens below
# (e.g. `api.image.tag=${IMAGE_TAG}`) and into image references passed to
# build-image.sh / resolve_image_digest. helm treats `,` as an assignment
# separator within a single --set token, so an unvalidated tag could inject an
# arbitrary values path (e.g. `v1,api.image.repository=evil/img`), and a
# newline would desync the one-token-per-line heredoc streams read via `while
# IFS= read -r` throughout this script.
if [[ ! "$IMAGE_TAG" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  error "Invalid --image-tag '${IMAGE_TAG}'. Must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ (alphanumeric, '.', '_', '-' only — no comma, no whitespace, no newline)."
fi

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
# unvalidated value could inject an arbitrary extra manifest. Kubernetes
# namespaces are DNS-1123 labels: lowercase alphanumeric or '-', starting and
# ending alphanumeric, max 63 chars. Checked once here rather than per call
# site, mirroring ingress_class()/ingress_tls_secret() in lib/helpers.sh.
_validate_namespace_var() {
  local var_name="$1"
  local val="${!var_name:-}"
  [[ -z "$val" ]] && return 0
  if [[ ! "$val" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ || "${#val}" -gt 63 ]]; then
    error "Invalid ${var_name} '${val}'. Must be a valid Kubernetes namespace (DNS-1123 label: lowercase alphanumeric and '-', max 63 chars)."
  fi
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
  local failed=0
  for i in "${!PIDS[@]}"; do
    local pid="${PIDS[$i]}"
    local label="${LABELS[$i]}"
    if wait "$pid"; then
      info "  [OK] $label"
    else
      warn "  [FAIL] $label (exit $?)"
      cat "${INSTALL_TMPDIR}/${label//\//-}.log" >&2 || true
      (( failed++ ))
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

# _resolve_fernet_secret_name <namespace>
# Resolves airflow.fernetKeySecretName from the deployed release's
# user-supplied values (no `-a` — the chart default is already the second
# candidate in _fernet_key_candidates below). Never `error`s: no release, a
# `helm` failure, a malformed values blob, or a name failing the
# Kubernetes-name grammar all resolve to empty, so the caller falls through
# to the next candidate. `json.load(...) or {}` guards against `helm get
# values` printing bare `null` for a release with no overrides — `d.get(...)`
# on `None` would otherwise raise `AttributeError`.
_resolve_fernet_secret_name() {
  local ns="$1"

  if ! helm status dataspoke --namespace "${ns}" >/dev/null 2>&1; then
    echo ""
    return 0
  fi

  local name
  name="$(helm get values dataspoke --namespace "${ns}" -o json 2>/dev/null | python3 -c '
import json, sys

try:
    d = json.load(sys.stdin) or {}
except Exception:
    d = {}
print((d.get("airflow") or {}).get("fernetKeySecretName") or "")
' 2>/dev/null || echo "")"

  # Validated before it ever reaches kubectl argv or the operator-facing
  # recovery text in _check_airflow_credentials_prod's Fernet error — the
  # same grammar SECRET_TO_CHECK is checked against below, applied here to a
  # less-trusted source (a release value, not a --values overlay this
  # operator authored).
  if [[ ! "${name}" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]]; then
    echo ""
    return 0
  fi
  echo "${name}"
}

# _fernet_key_candidates <namespace>
# Prints the de-duplicated, ordered union of every Secret name that could be
# carrying the live Fernet key: the resolved release's
# airflow.fernetKeySecretName first (when non-empty), then the chart's own
# projection name, then the legacy pre-hook Secret from a release installed
# before fernetKeySecretName was pinned. An unresolved release value (fresh
# install, no live release, a `helm` failure) simply drops the first
# candidate — the two-literal search still runs. One name per line; callers
# read via `while IFS= read -r`.
_fernet_key_candidates() {
  local ns="$1"

  local resolved
  resolved="$(_resolve_fernet_secret_name "${ns}")"

  local seen=() name dup s
  for name in "${resolved}" "dataspoke-airflow-metadata-encryption-key" "dataspoke-airflow-fernet-key"; do
    [[ -z "${name}" ]] && continue
    dup=false
    for s in ${seen[@]+"${seen[@]}"}; do
      if [[ "${s}" == "${name}" ]]; then
        dup=true
        break
      fi
    done
    if [[ "${dup}" == "false" ]]; then
      seen+=("${name}")
      echo "${name}"
    fi
  done
}

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
# _check_airflow_credentials_prod.
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
# pre-flight failure instead (_check_airflow_credentials_prod); install.sh
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
# <secret_name> defaults to "dataspoke-secrets".
_sync_env_from_secret() {
  local ns="$1"
  local secret_key="$2"
  local env_var_name="$3"
  local secret_name="${4:-dataspoke-secrets}"

  local value
  value="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath="{.data.${secret_key}}" | base64 --decode)"

  local prefix="${env_var_name}="
  local tmp_file
  tmp_file="$(mktemp)"

  if grep -q "^${env_var_name}=" "$ENV_FILE" 2>/dev/null; then
    awk -v prefix="${prefix}" -v val="${value}" \
      'index($0, prefix)==1 {print prefix val; next} {print}' \
      "$ENV_FILE" > "$tmp_file"
    mv "$tmp_file" "$ENV_FILE"
  else
    cp "$ENV_FILE" "$tmp_file"
    printf '%s=%s\n' "${env_var_name}" "${value}" >> "$tmp_file"
    mv "$tmp_file" "$ENV_FILE"
  fi
  chmod 600 "$ENV_FILE"
}

# _read_configmap_value <namespace> <key>
# Reads a single key out of the app ConfigMap (dataspoke-app-config) — the
# ConfigMap counterpart to reading a key out of the credentials Secret. Used
# for DATASPOKE_POSTGRES_{USER,DB}, which live in the ConfigMap rather than
# the Secret (see spec/feature/HELM_CHART.md §ConfigMap keys). Deliberately a
# thin reader with no env-file-writing body of its own — callers pipe the
# result into _write_env_var, so this does not become a third near-duplicate
# of _sync_env_from_secret / _write_env_var's own awk/mktemp rewrite logic.
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
# Writes/updates a plain (non-Secret) value in helm-charts/.env.<profile>. Idempotent.
_write_env_var() {
  local env_var_name="$1"
  local value="$2"

  local prefix="${env_var_name}="
  local tmp_file
  tmp_file="$(mktemp)"

  if grep -q "^${env_var_name}=" "$ENV_FILE" 2>/dev/null; then
    awk -v prefix="${prefix}" -v val="${value}" \
      'index($0, prefix)==1 {print prefix val; next} {print}' \
      "$ENV_FILE" > "$tmp_file"
    mv "$tmp_file" "$ENV_FILE"
  else
    cp "$ENV_FILE" "$tmp_file"
    printf '%s=%s\n' "${env_var_name}" "${value}" >> "$tmp_file"
    mv "$tmp_file" "$ENV_FILE"
  fi
  chmod 600 "$ENV_FILE"
}

# _check_airflow_credentials_prod <namespace> <secret_name>
# Validates ALL 11 required keys are present, non-empty, and not equal to known
# insecure defaults. Also hard-errors if the Secret still carries
# DATASPOKE_POSTGRES_USER or DATASPOKE_POSTGRES_DB — both relocated to the app
# ConfigMap (config.postgres.{user,db}), never rejected in favor of an
# install.sh-driven repair here: install.sh never mutates an operator-owned
# Secret in prod, so a lingering key is surfaced as a pre-flight failure with
# a copy-pasteable removal command instead. Prod profile only.
_check_airflow_credentials_prod() {
  local ns="$1"
  local secret_name="$2"

  local required_keys=(
    DATASPOKE_POSTGRES_PASSWORD
    DATASPOKE_REDIS_PASSWORD
    DATASPOKE_AIRFLOW_USER DATASPOKE_AIRFLOW_PASSWORD
    DATASPOKE_INTERNAL_TOKEN DATASPOKE_JWT_SECRET_KEY
    DATASPOKE_AIRFLOW_WEBSERVER_SECRET_KEY DATASPOKE_AIRFLOW_JWT_SECRET
    DATASPOKE_AIRFLOW_FERNET_KEY
    DATASPOKE_OAUTH_STATE_SECRET DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET
  )

  # DATASPOKE_POSTGRES_USER / DATASPOKE_POSTGRES_DB are not secrets and do not
  # belong in this Secret — they live in the app ConfigMap
  # (config.postgres.*) instead, where templates/configmap.yaml asserts they
  # agree with postgresql.auth.username/database. install.sh never patches an
  # operator-owned Secret in prod (contrast with the dev self-heal,
  # _ensure_postgres_identity_leaves_credentials_secret), so this rejects
  # rather than repairs it. Presence is tested with
  # --allow-missing-template-keys=false, not on the value being non-empty —
  # see that self-heal's own comment for why an empty-string value must still
  # be caught.
  local relocated_key
  for relocated_key in DATASPOKE_POSTGRES_USER DATASPOKE_POSTGRES_DB; do
    if kubectl get secret "${secret_name}" -n "${ns}" \
         -o jsonpath="{.data.${relocated_key}}" --allow-missing-template-keys=false >/dev/null 2>&1; then
      error "prod Secret '${secret_name}' still carries ${relocated_key}, which belongs to the app
ConfigMap (config.postgres.*) instead — it is not a secret and its presence here is a second,
silently divergent source of the Postgres identity. Remove it:
  kubectl patch secret ${secret_name} -n ${ns} --type=merge \\
    -p='{\"data\":{\"${relocated_key}\":null}}'
then re-run the install."
    fi
  done

  for key in "${required_keys[@]}"; do
    local val
    val="$(kubectl get secret "${secret_name}" -n "${ns}" \
      -o jsonpath="{.data.${key}}" 2>/dev/null | base64 --decode 2>/dev/null || true)"
    if [[ -z "${val}" ]]; then
      if [[ "${key}" == "DATASPOKE_AIRFLOW_FERNET_KEY" ]]; then
        local _fernet_msg
        _fernet_msg="prod Secret '${secret_name}' is missing required key: DATASPOKE_AIRFLOW_FERNET_KEY.
If this namespace already ran Airflow against a Postgres metadata DB you are keeping (for
example a PVC retained from a previous release), do NOT generate a new key — supply the exact
key that DB's connections and Variables were encrypted with, or they become permanently
undecryptable."

        # The missing irreversibility signal: a retained Postgres PVC proves
        # this is NOT a fresh install, independent of anything the recovery
        # search below finds. Without this, an operator who finds no
        # matching Secret in that search has nothing stopping them from
        # concluding "fresh install" anyway and generating a new key.
        if kubectl get pvc data-dataspoke-postgresql-0 -n "${ns}" >/dev/null 2>&1; then
          _fernet_msg+="
WARNING: PersistentVolumeClaim 'data-dataspoke-postgresql-0' survives in namespace '${ns}' — this
is NOT a fresh install. Generating a new key below would leave every connection and Variable
already encrypted in that PVC's Airflow metadata DB permanently undecryptable."
        fi

        _fernet_msg+="
Recover it from whatever this cluster last projected it into — try each of the following, in
order (the first is this release's own airflow.fernetKeySecretName, when resolvable — an operator
who pinned a self-chosen name here is exactly the case this ordering exists to cover):"
        local _fc
        while IFS= read -r _fc; do
          _fernet_msg+="
  kubectl get secret ${_fc} -n ${ns} -o jsonpath='{.data.fernet-key}' | base64 --decode"
        done < <(_fernet_key_candidates "${ns}")

        # Namespace-wide scan for ANY Secret carrying a fernet-key data key —
        # guidance text only, never the adoption search order above: silently
        # trusting an arbitrary discovered Secret would be unsafe. This finds
        # a pin that never reached this release's own recorded values at
        # all — e.g. a Secret an operator created and named directly in an
        # overlay whose release was never applied, or was applied by tooling
        # _resolve_fernet_secret_name cannot introspect — and needs no live
        # release to exist for that.
        local _fernet_scan
        _fernet_scan="$(kubectl get secret -n "${ns}" \
          -o jsonpath='{range .items[?(@.data.fernet-key)]}{.metadata.name} {end}' 2>/dev/null || true)"
        if [[ -n "${_fernet_scan}" ]]; then
          _fernet_msg+="
A namespace-wide scan for any Secret carrying a fernet-key also found: ${_fernet_scan}— check
whether one of these is the Secret your Airflow release actually mounts; its name may never have
reached the release's own recorded values."
        fi

        _fernet_msg+="
Add DATASPOKE_AIRFLOW_FERNET_KEY=<that value> to '${secret_name}' and re-run the install.
Only if this is a genuinely fresh install (no retained Postgres PVC to decrypt), generate one:
  python3 -c \"import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())\""
        error "${_fernet_msg}"
      else
        error "prod Secret '${secret_name}' is missing required key: ${key}"
      fi
    fi
  done

  # Shape check for the Fernet key: `openssl rand -hex 32` — the shape every
  # other high-entropy key above uses, and the one the README's own
  # generation block sits directly next to — decodes to 48 raw bytes, not the
  # 32 Fernet requires, so it passes pod startup and fails only the first
  # time Airflow tries to encrypt or decrypt a connection or Variable, long
  # after install reports success. Catch the shape mismatch here instead.
  local fernet_val
  fernet_val="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_AIRFLOW_FERNET_KEY}' | base64 --decode)"
  if [[ ! "${fernet_val}" =~ ^[A-Za-z0-9_-]{43}=$ ]]; then
    error "DATASPOKE_AIRFLOW_FERNET_KEY in Secret '${secret_name}' is not shaped like a Fernet key
(must be URL-safe base64 of exactly 32 raw bytes: 43 base64 characters followed by '='). Generate
one with:
  python3 -c \"import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())\"
— but only for a genuinely fresh install; see the missing-key error above if a Postgres PVC with
existing Airflow connections/Variables survives this namespace."
  fi

  local jwt_val
  jwt_val="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_JWT_SECRET_KEY}' | base64 --decode)"
  if [[ "${jwt_val}" == "changeme-dev-secret-do-not-use-in-prod" ]]; then
    error "DATASPOKE_JWT_SECRET_KEY is the dev default — operator must set a unique secret."
  fi

  local airflow_user
  airflow_user="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_AIRFLOW_USER}' | base64 --decode)"
  if [[ "${airflow_user}" == "admin" ]]; then
    error "DATASPOKE_AIRFLOW_USER must not be 'admin' — rename to reduce brute-force exposure."
  fi

  local airflow_password
  airflow_password="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_AIRFLOW_PASSWORD}' | base64 --decode)"
  if [[ -z "${airflow_password}" || "${airflow_password}" == "admin" ]]; then
    error "DATASPOKE_AIRFLOW_PASSWORD in Secret '${secret_name}' must not be empty or 'admin'."
  fi

  local google_oauth_secret_val
  google_oauth_secret_val="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath='{.data.DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET}' | base64 --decode)"
  if [[ "${google_oauth_secret_val}" == placeholder-* ]]; then
    error "DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET is the dev placeholder — operator must set a real Google OAuth client secret."
  fi
}

# _build_airflow_extra_env_file <secret_name>
# Writes the Airflow extraEnv YAML block (with the resolved secret name) to a
# temp file and prints its path. Caller is responsible for cleanup.
_build_airflow_extra_env_file() {
  local secret_name="$1"
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
  printf '%s' "${tmp_env_file}"
}

# _resolve_existing_secret_name [<overlay_file>]
# Extracts secrets.existingSecret from an operator overlay using python3+yaml.
# Prints the resolved name, or empty string if absent/unset. On a malformed
# overlay (invalid YAML, or YAML that doesn't parse to a mapping — a list or
# scalar document), prints one stderr line and exits non-zero; the caller
# assigns via `$(...) || error "..."` so the failure gets the same [ERROR]
# voice as every other pre-flight abort instead of a bare Python traceback.
_resolve_existing_secret_name() {
  local overlay_file="${1:-}"
  if [[ -z "${overlay_file}" || ! -f "${overlay_file}" ]]; then
    echo ""
    return 0
  fi
  if ! python3 -c "import yaml" 2>/dev/null; then
    error "python3 with PyYAML is required to parse the operator overlay for secrets.existingSecret. Install: pip install pyyaml"
  fi
  python3 - "${overlay_file}" <<'PYEOF'
import sys, yaml

PATH = sys.argv[1]


def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def dig(node, *keys):
    # A node that is PRESENT but not a mapping is an operator error, not an
    # absent key. Coercing it to {} would resolve the whole path to "unset"
    # and let the caller fall through to a default — fail loudly instead.
    walked = []
    for key in keys:
        if node is None:
            return None
        if not isinstance(node, dict):
            fail(f"Overlay file '{PATH}': '{'.'.join(walked)}' must be a mapping, got {type(node).__name__}.")
        walked.append(key)
        node = node.get(key)
    return node


with open(PATH) as f:
    try:
        data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        fail(f"Invalid YAML in overlay file '{PATH}': {' '.join(str(exc).split())}")

if data is not None and not isinstance(data, dict):
    fail(f"Overlay file '{PATH}' does not parse to a YAML mapping at its top level.")

print(dig(data, "secrets", "existingSecret") or "")
PYEOF
}

# _resolve_storage_classes [<overlay_file>]
# Extracts every StorageClass name the operator's overlay pins from the keys
# the postgresql/redis/airflow subcharts honour, using the same python3+yaml
# pattern as _resolve_existing_secret_name above:
#   postgresql.primary.persistence.storageClass
#   redis.master.persistence.storageClass
#   redis.replica.persistence.storageClass
#   global.defaultStorageClass, global.storageClass (Bitnami-wide fallbacks)
#   postgresql.global.{defaultStorageClass,storageClass}
#   redis.global.{defaultStorageClass,storageClass} — a `global:` block nested
#     inside a Bitnami subchart still reaches that child as .Values.global,
#     and common.storage.class ranks it AHEAD of the component's own
#     persistence.storageClass, so a pin here shadows the three above.
#   airflow.{logs,dags,triggerer,workers,workers.celery,redis}.persistence.
#     storageClassName — NOTE the different spelling (`storageClassName`, not
#     `storageClass`) inherited from the upstream apache-airflow chart. This
#     is a copy-paste trap: pasting a Bitnami-shaped key here silently pins
#     nothing. values-prod.example.yaml §Airflow log persistence actively
#     tells operators to uncomment two of these (workers.celery and
#     triggerer) for post-mortem log retention.
# Out of scope because the shipped architecture cannot reach them:
# postgresql.readReplicas.persistence, postgresql.backup.cronjob.storage, and
# redis.sentinel.persistence (standalone, no backup CronJob, no sentinel).
#
# A literal "-" is the upstream Bitnami convention (charts/common/templates/
# _storage.tpl, documented at redis/values.yaml:543,1035 and mirrored by
# postgresql) for "disable dynamic provisioning, bind a pre-provisioned PV"
# — it renders as storageClassName: "". The apache-airflow chart reproduces
# that exact mapping ONLY on the `logs` and `dags` persistence blocks
# (logs-persistent-volume-claim.yaml:46, dags-persistent-volume-claim.yaml:46);
# `triggerer`, `workers`, `workers.celery`, and `redis` pass the value
# straight into `storageClassName` with no such branch, so a "-" there
# renders literally and Kubernetes rejects it as an invalid class name. Each
# printed line therefore carries a provenance tag ahead of the name —
# "bitnami" and "airflow-sentinel" honour "-"; "airflow-literal" does not —
# and de-duplication is keyed on the (tag, name) pair, not the bare name, so
# a caller can tell a Bitnami "-" from an Airflow "-" apart even after two
# different keys pin the identical string.
# Prints one `<tag>\t<name>` line per resolved, de-duplicated, non-empty pin;
# nothing at all when the overlay pins none (the cluster default then
# applies and the pre-flight check skips cleanly). On a malformed overlay,
# behaves like _resolve_existing_secret_name above.
_resolve_storage_classes() {
  local overlay_file="${1:-}"
  if [[ -z "${overlay_file}" || ! -f "${overlay_file}" ]]; then
    return 0
  fi
  if ! python3 -c "import yaml" 2>/dev/null; then
    error "python3 with PyYAML is required to parse the operator overlay for pinned StorageClasses. Install: pip install pyyaml"
  fi
  python3 - "${overlay_file}" <<'PYEOF'
import sys, yaml

PATH = sys.argv[1]


def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def dig(node, *keys):
    # A node that is PRESENT but not a mapping is an operator error, not an
    # absent key. Coercing it to {} would make the whole gate silently
    # resolve zero pins and no-op — fail loudly instead.
    walked = []
    for key in keys:
        if node is None:
            return None
        if not isinstance(node, dict):
            fail(f"Overlay file '{PATH}': '{'.'.join(walked)}' must be a mapping, got {type(node).__name__}.")
        walked.append(key)
        node = node.get(key)
    return node


with open(PATH) as f:
    try:
        data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        fail(f"Invalid YAML in overlay file '{PATH}': {' '.join(str(exc).split())}")

if data is not None and not isinstance(data, dict):
    fail(f"Overlay file '{PATH}' does not parse to a YAML mapping at its top level.")

airflow = dig(data, "airflow")

# (provenance tag, resolved value) — tag decides whether "-" is a valid
# pre-provisioned-PV sentinel (bitnami / airflow-sentinel) or an unsupported
# literal that will reach the API server verbatim (airflow-literal). See the
# docstring above for which upstream template each tag corresponds to.
pins = [
    ("bitnami", dig(data, "postgresql", "primary", "persistence", "storageClass")),
    ("bitnami", dig(data, "redis", "master", "persistence", "storageClass")),
    ("bitnami", dig(data, "redis", "replica", "persistence", "storageClass")),
    ("bitnami", dig(data, "global", "defaultStorageClass")),
    ("bitnami", dig(data, "global", "storageClass")),
    # A `global:` block nested INSIDE a Bitnami subchart reaches that child as
    # .Values.global too, and common.storage.class gives it precedence over
    # the component's own persistence.storageClass — so a pin here shadows
    # the three above and must be gated with them.
    ("bitnami", dig(data, "postgresql", "global", "defaultStorageClass")),
    ("bitnami", dig(data, "postgresql", "global", "storageClass")),
    ("bitnami", dig(data, "redis", "global", "defaultStorageClass")),
    ("bitnami", dig(data, "redis", "global", "storageClass")),
    ("airflow-sentinel", dig(airflow, "logs", "persistence", "storageClassName")),
    ("airflow-sentinel", dig(airflow, "dags", "persistence", "storageClassName")),
    ("airflow-literal", dig(airflow, "triggerer", "persistence", "storageClassName")),
    ("airflow-literal", dig(airflow, "workers", "persistence", "storageClassName")),
    ("airflow-literal", dig(airflow, "workers", "celery", "persistence", "storageClassName")),
    ("airflow-literal", dig(airflow, "redis", "persistence", "storageClassName")),
]
seen = set()
for tag, name in pins:
    if isinstance(name, str) and name and (tag, name) not in seen:
        seen.add((tag, name))
        print(f"{tag}\t{name}")
PYEOF
}

# _assert_no_internal_ingress_exposure <chart_values_file> [<overlay_file>]
# Prod-only guard: aborts when the effective api.ingress.hosts[*].paths[*]
# would publish /internal/* on the public API ingress — the residual half of
# issue #130. /internal/* includes POST /internal/admin/bootstrap, which seeds
# a default admin whose credentials are published in this repository (see
# helm-charts/README.md §Prod profile). Narrowing the chart default itself is
# not an option: the api-wired integration tests drive /internal/* over the
# dev ingress, which shares this same default (values-dev.yaml), so this
# check runs only on the prod pre-flight, never on dev.
#
# Resolves the effective hosts the same way Helm does — the chart default
# from <chart_values_file>, wholesale-replaced by the overlay's
# api.ingress.hosts when the overlay sets that key at all (Helm's
# list-replace semantics for lists — see values-prod.example.yaml's header
# comment) — using the same python3+PyYAML pattern as
# _resolve_existing_secret_name/_resolve_storage_classes above.
#
# A path admits /internal/* when it is the catch-all "/" or is, after
# stripping a trailing slash, exactly "/internal" — the only two
# path-element-wise prefixes of "/internal", since it has a single path
# segment. On a malformed chart/overlay file, behaves like
# _resolve_existing_secret_name above.
_assert_no_internal_ingress_exposure() {
  local chart_values_file="$1" overlay_file="${2:-}"
  if ! python3 -c "import yaml" 2>/dev/null; then
    error "python3 with PyYAML is required to check the API ingress paths for /internal exposure. Install: pip install pyyaml"
  fi
  # Written directly to a temp file rather than captured via `x="$(... <<EOF)"`
  # — a heredoc nested inside a command substitution confuses bash's own
  # paren-matching the moment the heredoc body carries an odd number of
  # literal `'` characters (an apostrophe in a comment is enough), even
  # though the quoted `<<'PYEOF'` terminator makes those characters fully
  # inert to expansion. Redirecting to a file sidesteps the nesting.
  local offenders_file
  offenders_file="$(mktemp)"
  if ! python3 - "${chart_values_file}" "${overlay_file}" > "${offenders_file}" <<'PYEOF'
import sys, yaml

CHART_FILE = sys.argv[1]
OVERLAY_FILE = sys.argv[2] if len(sys.argv) > 2 else ""


def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def load_mapping(path, label):
    with open(path) as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            fail(f"Invalid YAML in {label} '{path}': {' '.join(str(exc).split())}")
    if data is not None and not isinstance(data, dict):
        fail(f"{label} '{path}' does not parse to a YAML mapping at its top level.")
    return data or {}


def dig(node, *keys):
    walked = []
    for key in keys:
        if node is None:
            return None
        if not isinstance(node, dict):
            fail(f"'{'.'.join(walked)}' must be a mapping, got {type(node).__name__}.")
        walked.append(key)
        node = node.get(key)
    return node


chart_data = load_mapping(CHART_FILE, "chart values file")
hosts = dig(chart_data, "api", "ingress", "hosts") or []

if OVERLAY_FILE:
    overlay_data = load_mapping(OVERLAY_FILE, "overlay file")
    overlay_hosts = dig(overlay_data, "api", "ingress", "hosts")
    # Helm list-replace semantics: an overlay hosts key replaces the chart
    # default hosts list wholesale, not merged into it.
    if overlay_hosts is not None:
        hosts = overlay_hosts

for host in hosts:
    if not isinstance(host, dict):
        continue
    host_name = host.get("host", "<unknown host>")
    for p in host.get("paths") or []:
        if not isinstance(p, dict):
            continue
        path = p.get("path")
        if not isinstance(path, str):
            continue
        normalized = path.rstrip("/") or "/"
        if normalized == "/" or normalized == "/internal":
            print(f"{host_name}\t{path}")
PYEOF
  then
    rm -f "${offenders_file}"
    error "Could not parse the API ingress paths for /internal exposure (see above)."
  fi

  if [[ -s "${offenders_file}" ]]; then
    local offenders offender_host offender_path
    offenders="$(cat "${offenders_file}")"
    rm -f "${offenders_file}"
    while IFS=$'\t' read -r offender_host offender_path; do
      [[ -z "$offender_path" ]] && continue
      error "The API ingress host '${offender_host}' publishes path '${offender_path}', which admits /internal/* — including POST /internal/admin/bootstrap, which seeds a default admin whose credentials are published in this repository. Narrow api.ingress.hosts[].paths in your --values overlay to the public API surface (/api/v1, /health, /ready, and optionally /redoc, /openapi.json) — see helm-charts/values-prod.example.yaml for the correct path list."
    done <<< "${offenders}"
  fi
  rm -f "${offenders_file}"
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
  extra_env_file="$(_build_airflow_extra_env_file "dataspoke-secrets")"
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
    local_extra_env_file="$(_build_airflow_extra_env_file "dataspoke-secrets")"
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
    if [[ -n "${DATASPOKE_TEST_DATAHUB_TOKEN:-}" ]]; then
      info "Applying dataspoke-datahub-secret (DataHub PAT)..."
      kubectl create secret generic dataspoke-datahub-secret \
        --namespace "${NS}" \
        --from-literal=token="${DATASPOKE_TEST_DATAHUB_TOKEN}" \
        --dry-run=client -o yaml | kubectl apply -f -
    else
      info "DATASPOKE_TEST_DATAHUB_TOKEN is unset — dataspoke-datahub-secret not created."
    fi

    # Langfuse secret key (out-of-band secret)
    if [[ -n "${DATASPOKE_TEST_LANGFUSE_SECRET_KEY:-}" ]]; then
      info "Applying dataspoke-langfuse-secret (Langfuse secret key)..."
      kubectl create secret generic dataspoke-langfuse-secret \
        --namespace "${NS}" \
        --from-literal=secret_key="${DATASPOKE_TEST_LANGFUSE_SECRET_KEY}" \
        --dry-run=client -o yaml | kubectl apply -f -
    else
      info "DATASPOKE_TEST_LANGFUSE_SECRET_KEY is unset — dataspoke-langfuse-secret not created."
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

    # Populate DATASPOKE_TEST_* block in .env for laptop-side test access
    info "Writing DATASPOKE_TEST_* values to .env..."
    # DATASPOKE_POSTGRES_{USER,DB} come from the app ConfigMap, not the
    # credentials Secret — they are non-secret and live there instead (see
    # spec/feature/HELM_CHART.md §ConfigMap keys).
    _write_env_var        "DATASPOKE_TEST_POSTGRES_USER" "$(_read_configmap_value "${NS}" "DATASPOKE_POSTGRES_USER")"
    _sync_env_from_secret "${NS}" "DATASPOKE_POSTGRES_PASSWORD" "DATASPOKE_TEST_POSTGRES_PASSWORD"
    _write_env_var        "DATASPOKE_TEST_POSTGRES_DB"   "$(_read_configmap_value "${NS}" "DATASPOKE_POSTGRES_DB")"
    _sync_env_from_secret "${NS}" "DATASPOKE_REDIS_PASSWORD"    "DATASPOKE_TEST_REDIS_PASSWORD"
    _sync_env_from_secret "${NS}" "DATASPOKE_AIRFLOW_USER"      "DATASPOKE_TEST_AIRFLOW_USER"
    _sync_env_from_secret "${NS}" "DATASPOKE_AIRFLOW_PASSWORD"  "DATASPOKE_TEST_AIRFLOW_PASSWORD"
    _sync_env_from_secret "${NS}" "DATASPOKE_INTERNAL_TOKEN"    "DATASPOKE_TEST_INTERNAL_TOKEN"
    _sync_env_from_secret "${NS}" "DATASPOKE_JWT_SECRET_KEY"    "DATASPOKE_TEST_JWT_SECRET_KEY"

    # Laptop-side host/port for direct DB/cache access. In managed mode this is
    # the ingress LoadBalancer IP; in shared mode it is 127.0.0.1, reached via
    # `kubectl port-forward` (bin/port-forward.sh) on the same canonical ports.
    TCP_HOST="$(tcp_access_host)"
    _write_env_var "DATASPOKE_TEST_POSTGRES_HOST" "${TCP_HOST}"
    _write_env_var "DATASPOKE_TEST_POSTGRES_PORT" "9201"
    _write_env_var "DATASPOKE_TEST_REDIS_HOST"    "${TCP_HOST}"
    _write_env_var "DATASPOKE_TEST_REDIS_PORT"    "9202"
    _write_env_var "DATASPOKE_TEST_AIRFLOW_URL"   "$(ingress_scheme)://airflow.${DATASPOKE_KUBE_INGRESS_DOMAIN:-dev.dataspoke.example.com}"

    # Dummy-data source access. In shared mode TCP_HOST is 127.0.0.1 (port-forward);
    # in managed mode it is the LoadBalancer IP (nginx TCP passthrough).
    # _POSTGRES_HOST_PORT is the in-cluster cluster-DNS address used by the
    # DataSpoke API pod when building ingestion source recipes — it is the same
    # in both modes because the API always runs in-cluster.
    _write_env_var "DATASPOKE_TEST_DUMMY_DATA_POSTGRES_HOST"      "${TCP_HOST}"
    _write_env_var "DATASPOKE_TEST_DUMMY_DATA_KAFKA_BROKERS"      "${TCP_HOST}:9104"
    _write_env_var "DATASPOKE_TEST_DUMMY_DATA_POSTGRES_HOST_PORT" \
      "example-postgres.${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE}.svc.cluster.local:5432"

    # Dev-lock URL — same pattern: 127.0.0.1 in shared mode, LoadBalancer IP in managed.
    _write_env_var "DATASPOKE_TEST_LOCK_URL" "http://${TCP_HOST}:9221"

    info ".env updated with DATASPOKE_TEST_* values."
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
  echo "  Langfuse UI:   ${DATASPOKE_TEST_LANGFUSE_HOST:-${SCHEME}://langfuse.<not set>}/"
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
  echo "  Credentials (auto-generated): see DATASPOKE_TEST_AIRFLOW_{USER,PASSWORD} in ${ENV_FILE}"
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
  #
  # Required explicitly in prod — no `nginx` default. The --set below overrides
  # whatever class the operator's --values overlay pins, so defaulting would
  # silently republish the API, frontend, and Airflow UI on any IngressClass
  # that happens to be named `nginx` (often another team's internet-facing
  # controller). The existence check that follows proves the class is real, not
  # that it is the one the operator meant.
  : "${DATASPOKE_KUBE_INGRESS_CLASS:?must be set explicitly in the prod .env — install.sh --sets this class onto every DataSpoke Ingress, overriding the className in your --values overlay}"
  INGRESS_CLASS="$(ingress_class)"
  if ! kubectl get ingressclass "${INGRESS_CLASS}" >/dev/null 2>&1; then
    error "IngressClass '${INGRESS_CLASS}' not found in the cluster. Install a controller or set DATASPOKE_KUBE_INGRESS_CLASS."
  fi
  info "IngressClass '${INGRESS_CLASS}' is present."

  # Refuse to publish /internal/* on the public API ingress (fail fast). See
  # _assert_no_internal_ingress_exposure's docstring above for why this is a
  # prod-only pre-flight gate rather than a narrower chart default.
  _assert_no_internal_ingress_exposure "$CHART_DIR/values.yaml" "${EXTRA_VALUES:-}"
  info "API ingress paths do not publish /internal/*."

  # Verify every StorageClass the operator's overlay pins exists (fail fast).
  #
  # A namespace-scoped Helm release cannot own a cluster-scoped StorageClass —
  # see helm-charts/prod-prereq/ for the cluster-admin prerequisite this
  # check assumes was applied first. Resolved from eleven overlay keys across
  # the postgresql/redis Bitnami subcharts and the Airflow subchart's
  # persistence blocks — see _resolve_storage_classes's docstring above for
  # the full list, the `storageClass` (Bitnami) vs `storageClassName`
  # (Airflow) spelling difference, and which of the two honour a literal
  # `-`. An overlay that pins none of them skips this check cleanly — the
  # cluster default StorageClass then applies.
  #
  # Failing here, rather than later, is the point: a missing class otherwise
  # leaves the PVC Pending, so PostgreSQL/Redis/Airflow never start, the
  # API's wait-for-postgres init container loops, and the install dies on a
  # rollout timeout whose symptom names PostgreSQL rather than storage.
  # Recovery then needs the stuck PVCs deleted by hand, because
  # storageClassName is immutable once bound.
  #
  # Resolved into a variable FIRST, then iterated — not
  # `done < <(_resolve_storage_classes ...)`. A process substitution's exit
  # status is invisible to `set -e`: the helper's own `error()` (or an
  # uncaught parse failure) would terminate only the subshell, the
  # while-loop would read zero lines, and this fail-fast gate would silently
  # no-op instead of aborting. Assigning via `$( ... )` makes a non-zero
  # status trip `set -e` as intended; `|| error ...` gives the parse-failure
  # path the same [ERROR] voice as every other pre-flight abort here.
  if [[ -n "${EXTRA_VALUES:-}" && -f "${EXTRA_VALUES}" ]]; then
    PINNED_STORAGE_CLASSES="$(_resolve_storage_classes "${EXTRA_VALUES}")" \
      || error "Could not parse the --values overlay for pinned StorageClasses (see above)."
    while IFS=$'\t' read -r sc_class sc_name; do
      [[ -z "$sc_name" ]] && continue
      # A literal "-" is the upstream Bitnami convention (see the docstring
      # on _resolve_storage_classes) for "disable dynamic provisioning, bind
      # a pre-provisioned PV" (renders as storageClassName: ""). The Airflow
      # chart reproduces that mapping ONLY on the `logs`/`dags` persistence
      # blocks (sc_class "airflow-sentinel"); on `triggerer`/`workers`/
      # `workers.celery`/`redis` (sc_class "airflow-literal") it passes the
      # value straight into storageClassName with no such branch, so a "-"
      # there would render literally and Kubernetes would reject it as an
      # invalid class name — reject it here instead, before any resource is
      # created.
      if [[ "$sc_name" == "-" ]]; then
        if [[ "$sc_class" == "bitnami" || "$sc_class" == "airflow-sentinel" ]]; then
          info "StorageClass pin '-' (${sc_class}) — dynamic provisioning disabled (pre-provisioned PV expected); skipping existence check."
          continue
        fi
        error "StorageClass pin '-' on an Airflow persistence key (triggerer/workers/workers.celery/redis) is not honoured by the apache-airflow chart — it would render storageClassName: '-' literally, which Kubernetes rejects as an invalid class name. Remove the '-' and name an explicit StorageClass. There is no pre-provisioned-PV path on these keys: triggerer/workers/workers.celery expose no existingClaim, and persistence.enabled: false renders an emptyDir, not a bound PV (airflow.redis.persistence does accept existingClaim)."
      fi
      # Validate against the Kubernetes DNS-subdomain grammar before using it
      # as a kubectl argument — an overlay value beginning with `-` would
      # otherwise be parsed as a kubectl flag (e.g. a name of `-A`), letting a
      # malformed overlay pass the gate for a class that was never checked.
      if ! [[ "$sc_name" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]]; then
        error "StorageClass name '${sc_name}' pinned in your --values overlay is not a valid Kubernetes name."
      fi
      if ! kubectl get storageclass "${sc_name}" >/dev/null 2>&1; then
        error "StorageClass '${sc_name}' pinned in your --values overlay was not found in the cluster. Apply the cluster-scoped prerequisites first — see helm-charts/prod-prereq/."
      fi
      info "StorageClass '${sc_name}' is present."
    done <<< "${PINNED_STORAGE_CLASSES}"
  fi

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
  # assignment.
  if ! [[ "$SECRET_TO_CHECK" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]]; then
    error "secrets.existingSecret '${SECRET_TO_CHECK}' in your --values overlay is not a valid Kubernetes name."
  fi

  # Verify operator-pre-created Secret (fail fast; never auto-generate in prod)
  _ensure_dataspoke_secrets "${NS}" "prod" "${SECRET_TO_CHECK}"

  # Validate ALL required keys are present and not insecure defaults
  _check_airflow_credentials_prod "${NS}" "${SECRET_TO_CHECK}"

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

  # Build resolved extraEnv referencing the operator secret name
  local_extra_env_file="$(_build_airflow_extra_env_file "${SECRET_TO_CHECK}")"

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
  if [[ "$SKIP_SEED" == "false" ]]; then
    # The seed script's api_internal_request helper (bin/lib/helpers.sh)
    # kubectl execs into the API pod and calls its own loopback port
    # directly — no ingress or DNS involved. The unconditional
    # dataspoke-api rollout-status wait above already guarantees the pod
    # is Ready to accept the call.
    info "Seeding default admin user..."
    bash "$SCRIPT_DIR/post-install/seed-admin-user.sh"
  else
    info "Skipping admin user seed (--skip-seed)."
  fi

  echo ""
  echo "=== Installation complete (profile: prod) ==="
  echo ""
  echo "  Helm release: dataspoke  namespace: ${NS}"
  echo ""
  if [[ "$FRONTEND_MODE" == "cluster" ]]; then
    # Best-effort: resolve the deployed frontend ingress host
    _frontend_host="$(kubectl get ingress -n "${NS}" \
      -o jsonpath='{.items[?(@.metadata.name=="dataspoke-frontend")].spec.rules[0].host}' 2>/dev/null || true)"
    if [[ -n "${_frontend_host}" ]]; then
      echo "  Web UI: http://${_frontend_host}/"
    else
      echo "  Web UI: served at your configured frontend.ingress host (see your operator overlay)."
    fi
    if [[ "$SKIP_SEED" == "true" ]]; then
      echo "  Login:  (admin not seeded — --skip-seed)"
    else
      echo "  Login:  dataspoke@dataspoke.local / dataspoke  (rotate via PATCH /auth/me)"
    fi
  else
    echo "  Frontend: disabled (--frontend none)."
  fi
  echo ""
  echo "  Post-install: configure peripherals and runtime settings via:"
  echo "    /api/v1/admin/peripherals/{datahub,langfuse}"
  echo "    /api/v1/admin/conf"
  echo ""
  info "Total elapsed: $((SECONDS - START_TIME))s"
  echo ""
fi
