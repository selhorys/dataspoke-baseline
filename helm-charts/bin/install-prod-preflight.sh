#!/usr/bin/env bash
# DataSpoke prod pre-flight — validate the operator's configuration, resolve the
# eleven credentials into the env file, create the credentials Secret, and print
# the install command. It never installs, upgrades, deletes, or builds.
#
# Usage: install-prod-preflight.sh [OPTIONS]
#
# OPTIONS
#   --env-file <path>          Operator env file (default: helm-charts/.env.prod).
#   --values <path>            Values overlay (default: helm-charts/values-prod.yaml
#                              when it exists). Single use, like install.sh's.
#   --namespace <ns>           Override DATASPOKE_KUBE_DATASPOKE_NAMESPACE.
#   --secret-name <name>       Override the overlay's secrets.existingSecret
#                              (which itself defaults to dataspoke-secrets).
#   --image-tag <tag>          Use this image tag instead of deriving one from git HEAD.
#   --allow-dirty              Derive the tag from git HEAD even with uncommitted or
#                              untracked files present.
#   --create-namespace         Create DATASPOKE_KUBE_DATASPOKE_NAMESPACE when absent.
#   --skip-secret              Do not create the credentials Secret — for operators
#                              delivering the eleven keys through ExternalSecrets,
#                              Vault or SealedSecrets. Every other stage still runs.
#   --skip-postinstall-check   Do not require the DATASPOKE_PROD_PERIPHERAL_* /
#                              DATASPOKE_PROD_LLM_* blocks to be complete.
#   --verify-only              Read-only audit: never write the env file, create a
#                              namespace, or create a Secret. Every check still runs.
#   --help, -h                 Print this usage message.
#
# Seven stages, announced <n>/7, with credential populate third. Stage 1's
# kubectl-context check and stage 2's overlay resolution both precede populate,
# because populate reads the cluster to adopt and the Secret it adopts from is
# the one the overlay names.
#
# See spec/feature/HELM_CHART.md §Prod operator workflow for the normative
# contract and helm-charts/README.md for the surrounding runbook.
set -euo pipefail

# ---------------------------------------------------------------------------
# What this script is, in one paragraph
# ---------------------------------------------------------------------------
# Its only three mutations are writing resolved credentials into the env file,
# creating the namespace (behind --create-namespace), and creating the
# credentials Secret. `--verify-only` removes all three, which is what makes it
# safe to run against a live prod deployment as an audit.
#
# Every gate install.sh also applies is a SHARED function from
# bin/lib/helpers.sh, called by both — the overlay assertions, the IngressClass
# probe, the pinned StorageClasses, the credentials-Secret content contract,
# the image-tag grammar, the admin-password rules, and the namespace / Secret
# name grammar. That sharing is the invariant the two-command sequence is sold
# on: a pass here means install.sh's own pre-flight passes. A predicate both
# scripts need therefore belongs in lib/helpers.sh with both callers on it, not
# copied into whichever file noticed it first.
#
# The checks that live only here are the ones install.sh has no counterpart
# for, and each is about an input install.sh never reads: the env-file NAME
# scan (install.sh sources the file and cannot un-see a stale name), the
# deployment-shape presence checks, the newline rejection on a value about to
# be written back or handed to `--from-env-file`, the post-install readiness
# blocks (applied after the release exists, by the seed scripts), and the
# mutable-tag refusal. None of them can make install.sh fail on input this
# script passed, which is what the invariant above actually requires.
#
# No credential ever reaches argv and there is no interactive prompt, so the
# same command works in a terminal and in CI. The Secret is created from a
# mode-0600 mktemp env file via `kubectl create secret --from-env-file`, never
# `--from-literal`, which would leak every value into shell history and into
# `ps auxww` / /proc/<pid>/cmdline for the process's lifetime. Resolved values
# reach the env file through env_file_set_var, which passes both the key and the
# value to awk through the environment rather than argv for the same reason.
# Nothing prints a credential value: populate reports provenance per key, and
# the Secret audit reports byte lengths.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELM_CHARTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$HELM_CHARTS_DIR/.." && pwd)"
CHART_DIR="$HELM_CHARTS_DIR/dataspoke"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
# shellcheck source=lib/helpers.sh
source "$SCRIPT_DIR/lib/helpers.sh"

START_TIME=$SECONDS
export START_TIME
TOTAL_STAGES=7

# git is required only on the branch that derives a tag from HEAD, so it is
# checked in stage 7 rather than here.
require_tools kubectl python3 openssl awk base64

# ---------------------------------------------------------------------------
# Temp-file reclamation
# ---------------------------------------------------------------------------
# The stage-5 env file handed to `kubectl create secret --from-env-file` holds
# all eleven credentials in plaintext. It is removed the moment kubectl returns,
# on the success and the failure path alike, and the EXIT trap is the backstop
# for every other way this script can end — an `error()` abort from a helper, a
# failed kubectl inside `set -e`, or an operator's Ctrl-C.
#
# EXIT alone covers the signals: bash runs the EXIT trap on its way out of a
# fatal SIGINT/SIGTERM too. The INT and TERM traps below therefore exist only
# to preserve the death, not to clean up. A handler installed on INT REPLACES
# bash's die-on-SIGINT, so trapping the reclaim function on INT directly would
# make Ctrl-C a no-op: the handler would run, the script would continue, and
# the run that mints and writes prod credentials would go on to write them
# after the operator asked it to stop. `exit` from the handler restores that,
# and the EXIT trap still fires on the way out.
_SECRET_ENV_FILE=""
_reclaim_secret_env_file() {
  if [[ -n "${_SECRET_ENV_FILE}" ]]; then
    rm -f "${_SECRET_ENV_FILE}"
    _SECRET_ENV_FILE=""
  fi
}
trap '_reclaim_secret_env_file' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
ENV_FILE_ARG=""
VALUES_ARG=""
NAMESPACE_ARG=""
SECRET_NAME_ARG=""
IMAGE_TAG_ARG=""
ALLOW_DIRTY=false
CREATE_NAMESPACE=false
SKIP_SECRET=false
SKIP_POSTINSTALL_CHECK=false
VERIFY_ONLY=false

# Every value-taking option asserts its value is actually there before the
# `shift 2`. Without it a flag given last (`--env-file` with nothing after it)
# makes `shift 2` fail, which under `set -e` ends the run at exit 1 with no
# output at all — the least diagnosable failure this script can produce, and an
# easy one to reach from a CI variable that expanded to nothing.
_require_option_value() {
  (( $2 >= 2 )) || error "$1 requires a value (use --help)."
  [[ -n "$3" ]] || error "$1 was given an empty value. An option that expanded to nothing in CI is
never what was meant, and every one of these selects what this script reads or writes."
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)               _require_option_value "$1" $# "${2:-}"; ENV_FILE_ARG="$2"; shift 2 ;;
    --namespace)              _require_option_value "$1" $# "${2:-}"; NAMESPACE_ARG="$2"; shift 2 ;;
    --secret-name)            _require_option_value "$1" $# "${2:-}"; SECRET_NAME_ARG="$2"; shift 2 ;;
    --image-tag)              _require_option_value "$1" $# "${2:-}"; IMAGE_TAG_ARG="$2"; shift 2 ;;
    --allow-dirty)            ALLOW_DIRTY=true; shift ;;
    --create-namespace)       CREATE_NAMESPACE=true; shift ;;
    --skip-secret)            SKIP_SECRET=true; shift ;;
    --skip-postinstall-check) SKIP_POSTINSTALL_CHECK=true; shift ;;
    --verify-only)            VERIFY_ONLY=true; shift ;;
    --values)
      # Single use, matching install.sh: helm's -f is repeatable but this
      # script hands the overlay to gates that read exactly one file, and two
      # overlays would leave the second one unchecked.
      if [[ -n "${VALUES_ARG}" ]]; then
        error "--values may only be given once; it takes exactly one overlay file. Merge multiple overlays into one file first."
      fi
      _require_option_value "$1" $# "${2:-}"
      VALUES_ARG="$2"
      shift 2 ;;
    --help|-h) print_usage; exit 0 ;;
    *) error "Unknown option: $1 (use --help)" ;;
  esac
done

ENV_FILE="${ENV_FILE_ARG:-$HELM_CHARTS_DIR/.env.prod}"

# The overlay resolves here so stages 2, 4 and 6 all read the same file. An
# absent default is not an error — the gates below take an empty overlay
# cleanly — but it is worth shouting about: without an overlay the release runs
# on chart defaults, whose api.ingress.hosts publishes the whole API surface and
# is rejected by stage 2 anyway.
EXTRA_VALUES=""
_default_overlay="$HELM_CHARTS_DIR/values-prod.yaml"
if [[ -n "${VALUES_ARG}" ]]; then
  [[ -f "${VALUES_ARG}" ]] || error "Values overlay not found: ${VALUES_ARG}"
  EXTRA_VALUES="${VALUES_ARG}"
elif [[ -f "${_default_overlay}" ]]; then
  EXTRA_VALUES="${_default_overlay}"
fi

echo ""
echo "DataSpoke prod pre-flight"
echo "========================="
echo "  Env file: ${ENV_FILE}"
echo "  Overlay:  ${EXTRA_VALUES:-<none>}"
if [[ "${VERIFY_ONLY}" == "true" ]]; then
  echo "  Mode:     --verify-only (the env file, the namespace and the Secret are left untouched)"
fi
echo ""

if [[ -z "${EXTRA_VALUES}" ]]; then
  warn "No --values overlay given and no ${_default_overlay} to fall back on. A prod release needs one:
it carries secrets.existingSecret, the ingress hosts and their published path list, TLS, the
registry, replica counts and the storage classes. Start from helm-charts/values-prod.example.yaml.
Stage 2 below judges the chart defaults instead, which publish the whole API surface."
fi

# ===========================================================================
# Stage 1 — env file
# ===========================================================================
step 1 "${TOTAL_STAGES}" "env file"

if [[ ! -f "${ENV_FILE}" ]]; then
  error "Env file not found at ${ENV_FILE} — copy helm-charts/.env.prod.example to it and fill in
the deployment shape plus whichever DATASPOKE_PROD_* inputs are yours to know. A blank credential
line is a request for this script to resolve it, not an omission."
fi

# Every DATASPOKE_PROD_* name anything reads out of this file: the eleven
# credential inputs from the shared map, then the post-install block, which is
# the union of what bin/post-install/seed-{runtime,peripheral}-config.sh read
# and what stage 6 below requires. Space-padded on both sides so a substring
# test matches whole names only.
_known_prod_names=" $(prod_credential_key_map | awk -F'\t' '{printf "%s ", $1}')\
DATASPOKE_PROD_ADMIN_PASSWORD DATASPOKE_PROD_GOOGLE_OAUTH_CLIENT_ID \
DATASPOKE_PROD_LLM_PROVIDER DATASPOKE_PROD_LLM_MODEL DATASPOKE_PROD_LLM_API_KEY \
DATASPOKE_PROD_PERIPHERAL_DATAHUB_GMS_URL DATASPOKE_PROD_PERIPHERAL_DATAHUB_FRONTEND_URL \
DATASPOKE_PROD_PERIPHERAL_DATAHUB_TOKEN DATASPOKE_PROD_PERIPHERAL_DATAHUB_SERVICE_CORPUSER_URN \
DATASPOKE_PROD_PERIPHERAL_DATAHUB_DEFAULT_ENV DATASPOKE_PROD_PERIPHERAL_DATAHUB_KAFKA_BROKERS \
DATASPOKE_PROD_PERIPHERAL_DATAHUB_KAFKA_SECURITY_PROTOCOL \
DATASPOKE_PROD_PERIPHERAL_DATAHUB_KAFKA_SASL_MECHANISM \
DATASPOKE_PROD_PERIPHERAL_DATAHUB_KAFKA_SASL_USERNAME \
DATASPOKE_PROD_PERIPHERAL_DATAHUB_KAFKA_SASL_PASSWORD \
DATASPOKE_PROD_PERIPHERAL_DATAHUB_KAFKA_AWS_REGION \
DATASPOKE_PROD_PERIPHERAL_LANGFUSE_HOST DATASPOKE_PROD_PERIPHERAL_LANGFUSE_PUBLIC_KEY \
DATASPOKE_PROD_PERIPHERAL_LANGFUSE_SECRET_KEY DATASPOKE_PROD_PERIPHERAL_LANGFUSE_PROJECT_ID \
DATASPOKE_PROD_PERIPHERAL_LANGFUSE_ENVIRONMENT_TAG "

# The variable NAMES are inspected before the file is sourced, because three of
# the four findings below are about a name existing at all. The scan parses
# assignments the way `source` would resolve them — commented lines ignored, an
# `export ` prefix stripped — and mirrors seed_profile's awk in lib/helpers.sh.
#
# Categories:
#   runtime — an unprefixed tier-1 app-runtime name. Those reach pods from the
#             ConfigMap and the credentials Secret; a stale copy here shadows
#             the Secret for anything that sources the file. The two build-
#             dispatch names below are the only unprefixed DATASPOKE_* names a
#             prod env file legitimately carries.
#   stub    — a stub_* dependency toggle. They are a dev mechanism stored in the
#             runtime_config DB row, and a production deployment answering 200
#             off a stub Redis, LLM, pgvector manager or notification service
#             fails invisibly.
#   dev     — a DATASPOKE_DEV_* name. Not fatal on its own, but seed_profile
#             calls a file declaring both prefixes `ambiguous`, which stops the
#             post-install seeds this same file feeds.
#   unknown — a DATASPOKE_PROD_* name nothing reads. Warned rather than
#             rejected, since the prefix is the operator's own namespace — but
#             warned rather than ignored, because the shape it catches is a
#             misspelling (DATASPOKE_PROD_POSTGRES_PASSWD): populate does not
#             see it, generates a different value, appends that, and the
#             operator's real credential sits in the file unused while the
#             deployment runs on the invented one.
#
# The known-name list reaches awk through the environment rather than argv,
# matching env_file_set_var's reasoning: `awk -v` processes escape sequences in
# its assignment, and the file being scanned is the operator's.
_env_name_findings="$(_PREFLIGHT_KNOWN_PROD_NAMES="${_known_prod_names}" awk '
  BEGIN { known = ENVIRON["_PREFLIGHT_KNOWN_PROD_NAMES"] }
  /^[[:space:]]*#/ { next }
  {
    line = $0
    sub(/^[[:space:]]*/, "", line)
    sub(/^export[[:space:]]+/, "", line)
    if (line !~ /^[A-Za-z_][A-Za-z0-9_]*=/) next
    name = substr(line, 1, index(line, "=") - 1)
    lower = tolower(name)
    if (lower ~ /(^|_)stub_/) { print "stub\t" name; next }
    if (name ~ /^DATASPOKE_DEV_/) { print "dev\t" name; next }
    if (name ~ /^DATASPOKE_KUBE_/) next
    if (name ~ /^DATASPOKE_PROD_/) {
      if (index(known, " " name " ") == 0) print "unknown\t" name
      next
    }
    if (name == "DATASPOKE_AWS_PROFILE" || name == "DATASPOKE_DOCKER_SUDO") next
    if (name ~ /^DATASPOKE_/ || name == "FORWARDED_ALLOW_IPS") { print "runtime\t" name; next }
  }
' "${ENV_FILE}")"

# The eleven Secret keys, read from the shared map so this message cannot name
# a different set than the one populate and the audit use. Padded with spaces
# on both sides so a substring test matches whole names only.
_eleven_secret_keys=" $(prod_credential_key_map | awk -F'\t' '{printf "%s ", $2}')"

# Every finding is reported, then one abort follows the scan: an operator
# renaming one line per run is the failure mode a stop-on-first would create.
# `|| true` on each report is what buys that — error_no_exit returns 1 by
# design (so a resolver inside `$( ... )` can carry its own stop), and under
# `set -e` an unconsumed 1 would end the run at the first offending name.
_env_fatal=0
if [[ -n "${_env_name_findings}" ]]; then
  while IFS=$'\t' read -r _finding_kind _finding_name; do
    [[ -z "${_finding_name}" ]] && continue
    case "${_finding_kind}" in
      runtime)
        _env_fatal=1
        if [[ "${_eleven_secret_keys}" == *" ${_finding_name} "* ]]; then
          error_no_exit "${ENV_FILE} declares ${_finding_name}, one of the eleven credentials-Secret
keys. Pods read that key from the Secret, and a stale copy in a file that scripts \`source\` shadows
it for every one of them. Rename the line to DATASPOKE_PROD_${_finding_name#DATASPOKE_} — this
script maps that input into the Secret key itself (spec/feature/HELM_CHART.md §Tier 5)." || true
        else
          error_no_exit "${ENV_FILE} declares ${_finding_name}, an application-runtime name. Tier-1
names reach pods from the app ConfigMap and the credentials Secret, never from an env file, so a
line here is either shadowing one of them or has no reader at all. Remove it; chart values are the
place to set the ConfigMap-sourced ones (spec/feature/HELM_CHART.md §Tier 1)." || true
        fi
        ;;
      stub)
        _env_fatal=1
        error_no_exit "${ENV_FILE} declares ${_finding_name}. The four stub_* dependency toggles are
a dev mechanism carried in the runtime_config DB row, and a prod deployment running on a stub Redis,
LLM, pgvector manager or notification service answers 200 and delivers none of it. Remove the line —
the prod seed path sets no stub_* flag at all." || true
        ;;
      unknown)
        warn "${ENV_FILE} declares ${_finding_name}, which nothing reads — it is not one of the
eleven credential inputs, nor part of the admin/LLM/peripheral block the post-install seeds send.
Check it against helm-charts/.env.prod.example: a near-miss of a real name (a missing letter in a
credential input, say) leaves the value here unused while this script resolves the correctly spelled
line to something else entirely."
        ;;
      dev)
        warn "${ENV_FILE} declares ${_finding_name}. seed_profile reads a file carrying both
DATASPOKE_PROD_* and DATASPOKE_DEV_* names as ambiguous and the post-install seeds abort on it, so
this same file cannot seed peripherals or runtime config until the dev line is gone."
        ;;
    esac
  done <<< "${_env_name_findings}"
fi
if (( _env_fatal != 0 )); then
  error "Fix the env-file names reported above before re-running."
fi

# Sourced WITHOUT `set -a`: every kubectl call below forks a client-go exec
# credential plugin (gke-gcloud-auth-plugin, aws eks get-token, kubelogin), and
# an exported env file would hand each of them all eleven credentials plus the
# DataHub PAT and the LLM key. Shell variables are enough — this script reads
# them directly and passes the ones it needs by name.
# shellcheck disable=SC1090
source "${ENV_FILE}"
# The file gains a credential per blank line resolved below, so harden it before
# that happens, in case it arrived from a `cp` under a permissive umask.
# Suppressed under --verify-only along with the three content mutations: an
# audit that claims to leave the env file untouched must not change its mode
# either, and a live prod file's permissions are the operator's to set. It is
# still REPORTED there — a silent audit on the one file that holds all eleven
# credentials would leave a 0644 `.env.prod` as the single finding this mode
# is best placed to make and never makes.
if [[ "${VERIFY_ONLY}" != "true" ]]; then
  chmod 600 "${ENV_FILE}" 2>/dev/null || true
else
  # BSD stat (macOS) and GNU stat (Linux) spell the same question differently
  # and neither accepts the other's flag; an unreadable mode is reported as
  # unknown rather than as a pass.
  _env_file_mode="$(stat -f '%Lp' "${ENV_FILE}" 2>/dev/null || stat -c '%a' "${ENV_FILE}" 2>/dev/null || true)"
  if [[ -z "${_env_file_mode}" ]]; then
    warn "Could not read the permissions of ${ENV_FILE}. A run without --verify-only chmods it 600; check it by hand."
  elif [[ "${_env_file_mode}" != "600" ]]; then
    warn "${ENV_FILE} is mode ${_env_file_mode}, not 600 — it holds all eleven credentials, and every
local account can read it at anything group- or world-readable. --verify-only leaves the file
untouched by design, so fix it yourself:
  chmod 600 ${ENV_FILE}"
  else
    info "${ENV_FILE} is mode 600."
  fi
fi

# Deployment shape. Each of these has a consumer that cannot proceed without it:
# the context check just below, the namespace every object lands in, the class
# install.sh --sets onto every Ingress, and the registry every image reference
# is built from.
for _shape_var in DATASPOKE_KUBE_CLUSTER DATASPOKE_KUBE_INGRESS_CLASS DATASPOKE_KUBE_IMAGE_REGISTRY; do
  if [[ -z "${!_shape_var:-}" ]]; then
    error "${_shape_var} is not set in ${ENV_FILE}. See helm-charts/.env.prod.example for what each deployment-shape variable does."
  fi
done
# Required whether or not --namespace was given, and this is not redundant.
# install.sh has no --namespace: its prod branch takes
# DATASPOKE_KUBE_DATASPOKE_NAMESPACE straight out of this same file under
# `set -u`. Letting --namespace excuse a blank line would mean a pre-flight
# that passed and an install that dies on an unbound variable one command
# later — the exact failure the shared-gate invariant exists to rule out.
if [[ -z "${DATASPOKE_KUBE_DATASPOKE_NAMESPACE:-}" ]]; then
  error "DATASPOKE_KUBE_DATASPOKE_NAMESPACE is not set in ${ENV_FILE}. install.sh --profile prod reads
it from this file and has no --namespace flag to be told otherwise, so it has to be set here even
when this run overrides it with --namespace."
fi
if [[ -n "${NAMESPACE_ARG}" && "${NAMESPACE_ARG}" != "${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}" ]]; then
  warn "--namespace '${NAMESPACE_ARG}' disagrees with DATASPOKE_KUBE_DATASPOKE_NAMESPACE
('${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}') in ${ENV_FILE}. This run validates and populates
'${NAMESPACE_ARG}'; install.sh --profile prod will install into
'${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}', where nothing here has been checked and the credentials
Secret may not exist. Use --namespace only to inspect a second deployment, never as the input to
the install command printed at the end."
fi
if [[ -z "${DATASPOKE_KUBE_INGRESS_DOMAIN:-}" ]]; then
  warn "DATASPOKE_KUBE_INGRESS_DOMAIN is not set in ${ENV_FILE}. The release takes its hosts from the
overlay's ingress sections, so the install itself does not need it — but bin/health-check.sh builds
every probe URL from it, so a prod health check has nothing to probe until it is set."
fi

NS="${NAMESPACE_ARG:-${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}}"
# assert_k8s_namespace, not the broader assert_k8s_name: install.sh applies the
# DNS-1123 LABEL grammar to this same value (_validate_namespace_var), and a
# name accepted here but rejected there would break the invariant the
# two-command sequence rests on. Both call the one shared assertion.
assert_k8s_namespace "namespace" "${NS}" || exit 1
# The env file's own value too, even when --namespace overrode it for this run:
# install.sh reads that line and no other.
assert_k8s_namespace "DATASPOKE_KUBE_DATASPOKE_NAMESPACE" "${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}" || exit 1

# Validated for its own grammar here, checked against the cluster in stage 4.
INGRESS_CLASS="$(ingress_class)"

# assert_admin_password (lib/helpers.sh) is the same gate the post-install
# rotation applies to the same variable — reaching it there means this script
# was not run, and reaching it here means the rotation is refused before the
# install rather than after it, with the published default live in between. The
# value itself is never printed; its length is.
if [[ -n "${DATASPOKE_PROD_ADMIN_PASSWORD:-}" ]]; then
  assert_admin_password "${DATASPOKE_PROD_ADMIN_PASSWORD}" "${ENV_FILE}"
  info "DATASPOKE_PROD_ADMIN_PASSWORD is set (${#DATASPOKE_PROD_ADMIN_PASSWORD} characters) — the install's admin seed rotates dataspoke@dataspoke.local to it."
else
  warn "DATASPOKE_PROD_ADMIN_PASSWORD is blank. The install seeds the built-in admin
dataspoke@dataspoke.local with the password published in this repository and leaves it live; the
deployment is not production-ready until you rotate it by hand (PATCH /api/v1/auth/me)."
fi

# Before populate, not after: populate reads THIS cluster's Secret to adopt
# credentials, and adopting under the wrong context would write another
# deployment's credentials into this file. The context is only compared, never
# switched — changing an operator's active context is a side effect this script
# has no business having.
_current_context="$(kubectl config current-context 2>/dev/null || true)"
if [[ -z "${_current_context}" ]]; then
  error "kubectl has no current context. Select the one ${ENV_FILE} names:
  kubectl config use-context ${DATASPOKE_KUBE_CLUSTER}"
fi
if [[ "${_current_context}" != "${DATASPOKE_KUBE_CLUSTER}" ]]; then
  error "kubectl's current context is '${_current_context}' but ${ENV_FILE} names
'${DATASPOKE_KUBE_CLUSTER}'. Stage 3 adopts credentials from this cluster's Secret, so a mismatch
here would copy another deployment's credentials into the env file. Switch first:
  kubectl config use-context ${DATASPOKE_KUBE_CLUSTER}"
fi
info "kubectl context '${_current_context}' matches DATASPOKE_KUBE_CLUSTER."
info "Namespace: ${NS}"

# ===========================================================================
# Stage 2 — values overlay
# ===========================================================================
step 2 "${TOTAL_STAGES}" "values overlay"

# The same two gates install.sh's prod pre-flight runs, on the same inputs: the
# chart's own values merged with the operator's overlay.
_assert_no_internal_ingress_exposure "$CHART_DIR/values.yaml" "${EXTRA_VALUES}"
info "API ingress paths do not publish /internal/*."

_assert_no_airflow_simple_auth_overlay_conflict "${EXTRA_VALUES}"
info "Overlay does not conflict with the Airflow SimpleAuthManager passwords-file mechanism."

# Resolved before populate: the Secret adoption reads from is the one the
# overlay names, so a --secret-name or an overlay key that only took effect
# later would have populate adopting from a different object than the install
# mounts.
_overlay_secret_name="$(_resolve_existing_secret_name "${EXTRA_VALUES}")" \
  || error "Could not parse the --values overlay for secrets.existingSecret (see above)."
_install_secret_name="${_overlay_secret_name:-dataspoke-secrets}"
if [[ -n "${SECRET_NAME_ARG}" ]]; then
  SECRET_NAME="${SECRET_NAME_ARG}"
  info "Credentials Secret: '${SECRET_NAME}' (--secret-name)."
  # install.sh has no --secret-name: its prod branch always takes the overlay's
  # secrets.existingSecret, defaulting to dataspoke-secrets, and there is no
  # flag on the install command printed at the end that could express anything
  # else. A --secret-name that disagrees therefore validates, populates and
  # creates one object while the install mounts another.
  if [[ "${SECRET_NAME}" != "${_install_secret_name}" ]]; then
    warn "--secret-name '${SECRET_NAME}' disagrees with what install.sh will mount
('${_install_secret_name}', from secrets.existingSecret in the overlay, or the chart default). This
run adopts, verifies and may create '${SECRET_NAME}'; the release reads '${_install_secret_name}'.
Set secrets.existingSecret in the overlay instead — install.sh has no --secret-name to be told
otherwise. Use this flag only to inspect a Secret the release does not mount."
  fi
elif [[ -n "${_overlay_secret_name}" ]]; then
  SECRET_NAME="${_install_secret_name}"
  info "Credentials Secret: '${SECRET_NAME}' (secrets.existingSecret in ${EXTRA_VALUES})."
else
  SECRET_NAME="${_install_secret_name}"
  info "Credentials Secret: '${SECRET_NAME}' (chart default — the overlay sets no secrets.existingSecret)."
fi
# Grammar-checked here as well as inside every helper that takes it: this name
# reaches a `kubectl create secret` argv below, where a leading '-' is parsed
# as a flag.
assert_k8s_name "credentials Secret name" "${SECRET_NAME}" || exit 1

# The two halves of the Google OAuth pair live in different planes — the public
# client id in the overlay (rendered into the app ConfigMap), the client secret
# in the credentials Secret — so nothing but this comparison notices when they
# stop describing the same OAuth client. A mismatched pair installs, rolls out
# and passes every readiness probe, then fails at the callback with
# invalid_client. Neither value is printed: this output lands in terminals and
# CI logs, and the pair identifies the operator's OAuth client.
_overlay_client_id="$(overlay_string_value "${EXTRA_VALUES}" "auth.googleClientId")" \
  || error "Could not read auth.googleClientId from the --values overlay (see above)."
_env_client_id="${DATASPOKE_PROD_GOOGLE_OAUTH_CLIENT_ID:-}"
if [[ -n "${_overlay_client_id}" && -n "${_env_client_id}" ]]; then
  if [[ "${_overlay_client_id}" != "${_env_client_id}" ]]; then
    warn "auth.googleClientId in ${EXTRA_VALUES} and DATASPOKE_PROD_GOOGLE_OAUTH_CLIENT_ID in
${ENV_FILE} disagree (values withheld). The deployment uses the overlay's; the client secret in the
credentials Secret is paired with one of them. A mismatched pair installs cleanly and fails at the
OAuth callback with invalid_client."
  else
    info "auth.googleClientId agrees with DATASPOKE_PROD_GOOGLE_OAUTH_CLIENT_ID."
  fi
elif [[ -n "${_overlay_client_id}" ]]; then
  warn "The overlay sets auth.googleClientId but DATASPOKE_PROD_GOOGLE_OAUTH_CLIENT_ID is blank in
${ENV_FILE}. Nothing then records which OAuth client the client secret below belongs to."
elif [[ -n "${_env_client_id}" ]]; then
  warn "DATASPOKE_PROD_GOOGLE_OAUTH_CLIENT_ID is set but the overlay sets no auth.googleClientId, so
the deployment ships without a client id and Google sign-in is disabled."
else
  info "No Google OAuth client id on either side — Google sign-in is disabled in this deployment."
fi

# ===========================================================================
# Stage 3 — credential populate
# ===========================================================================
step 3 "${TOTAL_STAGES}" "credential populate"

# Per key: the operator's own value, else the value this cluster's Secret is
# already using, else a freshly generated one written back into the env file
# (spec/feature/HELM_CHART.md §Tier 5). A blank line is a request, not an
# omission, and adoption precedes generation because it is what stops a
# re-install contradicting a running deployment — the Airflow Fernet key above
# all, which a retained Postgres PVC's encrypted connections and Variables
# depend on.
#
# Under --skip-secret that request has no addressee. Those operators deliver
# the eleven keys through ExternalSecrets, Vault or SealedSecrets, and nothing
# in this run or the install will use a value minted here: generating anyway
# would put eleven fresh production credentials on the operator's disk (and in
# CI workspaces, backups and editor swapfiles) for a deployment that runs on
# eleven different ones — on precisely the path whose purpose is keeping them
# off that disk. Worse, once the external system materialises the Secret, the
# stage-5 drift check would report all eleven as differing and stop the
# pre-flight permanently, since it rewrites neither side. So a blank line is
# reported as delivered out of band and left alone; an operator value present
# in the file is still resolved and still compared.
#
# Only provenance is reported. The resolved values are kept in
# _RESOLVED_<SECRET_KEY> shell variables for stage 5's Secret creation and never
# echoed, never placed in argv.
_populate_generated=0
_populate_adopted=0
_populate_deferred=0
while IFS=$'\t' read -r _prod_name _secret_key; do
  [[ -z "${_secret_key}" ]] && continue

  _value="${!_prod_name:-}"
  _provenance=""

  if [[ -n "${_value}" ]]; then
    _provenance="from the env file"
  elif [[ "${SKIP_SECRET}" == "true" ]]; then
    _provenance="left blank — delivered out of band (--skip-secret)"
    _populate_deferred=$(( _populate_deferred + 1 ))
  else
    # `&& printf X` keeps a read that did not answer the question (RBAC denial,
    # unreachable API server) distinguishable from a genuine absence — the
    # sentinel is only appended on success, so the substitution's status still
    # carries the failure. Treating "could not read" as "absent" would fall
    # through to generation against a live deployment.
    if ! _adopted="$(adopt_credential_from_cluster "${NS}" "${SECRET_NAME}" "${_secret_key}" && printf 'X')"; then
      error "Could not resolve ${_prod_name}: reading ${_secret_key} from Secret '${SECRET_NAME}' in
'${NS}' failed (see the error above). Nothing has been written."
    fi
    _adopted="${_adopted%X}"
    if [[ -n "${_adopted}" ]]; then
      _value="${_adopted}"
      _provenance="adopted from the cluster"
      _populate_adopted=$(( _populate_adopted + 1 ))
    else
      # generate_credential_value reports its own reason through error_no_exit
      # — including the one key it refuses to invent — so the abort below adds
      # only what the operator does next.
      if ! _value="$(generate_credential_value "${_secret_key}")"; then
        error "Cannot resolve ${_prod_name} (see the error above). It is the one credential nothing
here can generate: the Google OAuth client secret is issued by the Google Cloud Console alongside
the client id, and a value invented for it would be a well-formed secret that authenticates
nothing. Create the OAuth client, put its secret in ${ENV_FILE}, and re-run."
      fi
      _provenance="generated"
      _populate_generated=$(( _populate_generated + 1 ))
    fi

    if [[ "${VERIFY_ONLY}" == "true" ]]; then
      _provenance="${_provenance} (not written — --verify-only)"
    else
      # env_file_set_var is the single env-file rewriter: it refuses a value
      # carrying a newline (this file is consumed with `source`), passes the key
      # and value to awk through the environment rather than argv, and chmods
      # the result 600.
      env_file_set_var "${_prod_name}" "${_value}" "${ENV_FILE}"
    fi
  fi

  printf -v "_RESOLVED_${_secret_key}" '%s' "${_value}"
  info "$(printf '  %-46s %s' "${_prod_name}" "${_provenance}")"
done <<< "$(prod_credential_key_map)"

# Checked once, over the resolved values, on every provenance branch — not just
# the ones this script writes. Both consumers refuse a line break, in different
# ways and with different consequences: env_file_set_var rejects it outright
# (the file is read with `source`, where a second physical line is executed as a
# command), while `kubectl create secret --from-env-file` parses the
# continuation as a KEY NAME and reports it back — printing a fragment of the
# credential into the terminal and into any CI log — or silently keeps only the
# part before the break. A value that came from the env file inside a quoted
# multi-line assignment reaches the second consumer without ever passing the
# first, which is why this stands on its own rather than being left to the
# writer. Refusing is the only honest answer: a credential that cannot
# round-trip through these two channels is a defect at its source.
while IFS=$'\t' read -r _prod_name _secret_key; do
  [[ -z "${_secret_key}" ]] && continue
  _resolved_var="_RESOLVED_${_secret_key}"
  if [[ "${!_resolved_var}" == *$'\n'* || "${!_resolved_var}" == *$'\r'* ]]; then
    error "${_prod_name} contains a newline or a carriage return. It cannot be written into
${ENV_FILE} (which is consumed with \`source\`) and cannot be carried by
\`kubectl create secret --from-env-file\`, whose parser would report the continuation line as an
invalid key name — echoing part of the credential into this terminal. Find where the value picked up
the break (a \`--from-file\` of a text editor's output and an external-secrets sync are the usual
sources) and strip it there. Nothing has been created."
  fi
done <<< "$(prod_credential_key_map)"

if (( _populate_deferred > 0 )); then
  info "${_populate_deferred} of the eleven are blank and were left alone (--skip-secret): they are yours to deliver through ExternalSecrets, Vault or SealedSecrets."
fi
if (( _populate_generated > 0 || _populate_adopted > 0 )); then
  if [[ "${VERIFY_ONLY}" == "true" ]]; then
    info "${_populate_adopted} adopted, ${_populate_generated} generated — none written back (--verify-only)."
  else
    info "${_populate_adopted} adopted, ${_populate_generated} generated and written back to ${ENV_FILE}, which is now the operator's copy of record."
  fi
elif (( _populate_deferred == 0 )); then
  info "All eleven credentials came from ${ENV_FILE} — nothing adopted, nothing generated."
fi

# ===========================================================================
# Stage 4 — cluster prerequisites
# ===========================================================================
step 4 "${TOTAL_STAGES}" "cluster prerequisites"

# The credentials Secret is namespace-scoped, so the namespace has to exist by
# stage 5. install.sh's own prod pre-flight calls ensure_namespace before
# anything else, which is why creating it here needs an explicit flag rather
# than being implied: a namespace this script invents on a typo'd --namespace
# is an object the operator did not ask for.
if kubectl get namespace "${NS}" >/dev/null 2>&1; then
  info "Namespace '${NS}' exists."
elif [[ "${CREATE_NAMESPACE}" != "true" ]]; then
  error "Namespace '${NS}' does not exist. Re-run with --create-namespace to have this script create
it, or create it yourself (kubectl create namespace ${NS})."
elif [[ "${VERIFY_ONLY}" == "true" ]]; then
  warn "Namespace '${NS}' does not exist. --create-namespace would create it; --verify-only
suppresses that, so stage 5 has nowhere to read or create the credentials Secret."
else
  ensure_namespace "${NS}"
fi

# The same probe install.sh's Phase 1 runs, through the one shared function —
# see assert_ingress_class_present in lib/helpers.sh for why prod has no
# default class and what existence does and does not prove.
assert_ingress_class_present "${INGRESS_CLASS}"
info "IngressClass '${INGRESS_CLASS}' is present."

# Every StorageClass the overlay pins, plus the CSI driver behind it. Failing
# here is the point: a missing class otherwise leaves the PVC Pending, the
# owning component never starts, and the install dies on a rollout timeout that
# names the workload rather than the storage.
assert_pinned_storage_classes "${EXTRA_VALUES}"

# ===========================================================================
# Stage 5 — credentials Secret
# ===========================================================================
step 5 "${TOTAL_STAGES}" "credentials Secret"

_secret_presence="$(secret_presence "${NS}" "${SECRET_NAME}")" \
  || error "Cannot determine whether Secret '${SECRET_NAME}' exists in '${NS}' (see the error above)."

if [[ "${_secret_presence}" == "present" ]]; then
  # Verified and never rewritten, drift included. That Secret may hold the only
  # surviving copy of material a retained PVC depends on, so which side is
  # authoritative is the operator's decision; this script's job is to name the
  # keys that differ and stop.
  if ! report_credential_secret_drift "${NS}" "${SECRET_NAME}" "${ENV_FILE}"; then
    error "The keys named above differ between ${ENV_FILE} and Secret '${SECRET_NAME}'. Nothing was
rewritten. Decide which side is authoritative — correct the env file, or replace the Secret
deliberately together with whatever PersistentVolumeClaims its old values encrypted — and re-run."
  fi
  # Length reporting on: this is the audit --verify-only exists for, and it
  # names a key that is present but empty, which the rejections below would
  # otherwise report one at a time.
  verify_credential_secret "${NS}" "${SECRET_NAME}" "$CHART_DIR/values.yaml" "${EXTRA_VALUES}" 1
  info "Secret '${SECRET_NAME}' satisfies the content contract and was left untouched."
elif [[ "${SKIP_SECRET}" == "true" ]]; then
  warn "Secret '${SECRET_NAME}' does not exist in '${NS}' and --skip-secret was given, so nothing
created it and stage 3 generated nothing to create it from. Materialise it through your
ExternalSecrets/Vault/SealedSecrets path with the same eleven keys before running install.sh, which
aborts on a missing credentials Secret and never creates one itself. The content contract is
identical either way — re-run this script once the Secret exists (with or without --skip-secret) to
have it verified against the same rules."
elif [[ "${VERIFY_ONLY}" == "true" ]]; then
  warn "Secret '${SECRET_NAME}' does not exist in '${NS}'. A run without --verify-only would create
it from the eleven resolved credentials; nothing was created here, so its content contract is
unverified."
else
  # The content contract runs against the RESOLVED VALUES first, before
  # anything is created. A value it rejects — a hex Fernet key, a
  # `placeholder-` OAuth client secret, the dev JWT default, an `admin` Airflow
  # username — must never be materialised: once it is in the cluster every
  # later run takes the "present" branch above, which verifies and never
  # rewrites, so recovering means deleting the Secret by hand. These are the
  # same predicates verify_credential_secret applies to the Secret afterwards,
  # through the one shared assert_credential_value_contract (lib/helpers.sh),
  # so nothing can be enforced on one side of the create and not the other.
  _effective_all_admins="$(_resolve_effective_all_admins "$CHART_DIR/values.yaml" "${EXTRA_VALUES}")"
  while IFS=$'\t' read -r _prod_name _secret_key; do
    [[ -z "${_secret_key}" ]] && continue
    _resolved_var="_RESOLVED_${_secret_key}"
    if [[ -z "${!_resolved_var}" ]]; then
      error "${_prod_name} resolved to an empty value, so Secret '${SECRET_NAME}' would carry an empty
${_secret_key}. Every one of the eleven is mounted by a pod that cannot start without it. Set the
line in ${ENV_FILE} and re-run. Nothing has been created."
    fi
  done <<< "$(prod_credential_key_map)"
  if ! assert_credential_value_contract "_RESOLVED_" \
       "the values resolved for Secret '${SECRET_NAME}'" "${_effective_all_admins}"; then
    error "The resolved credential named above does not satisfy the content contract, so Secret
'${SECRET_NAME}' was NOT created — a value rejected here would otherwise have to be removed from the
cluster by hand, because an existing Secret is verified and never rewritten. Correct the line in
${ENV_FILE} (or blank it, to have this script resolve it) and re-run."
  fi

  # --from-env-file off a mode-0600 mktemp, never --from-literal: the latter
  # puts every value in shell history and in `ps auxww` / /proc/<pid>/cmdline
  # for the process's lifetime. The file is written in the caller's TMPDIR by
  # mktemp's own 0600 contract — no window in which a later chmod would still
  # be racing a reader — and removed the moment kubectl returns, with the EXIT
  # trap above as the backstop.
  info "Creating Secret '${SECRET_NAME}' in '${NS}' from the eleven resolved credentials..."
  _SECRET_ENV_FILE="$(mktemp -t dataspoke-secret-env.XXXX)"
  while IFS=$'\t' read -r _prod_name _secret_key; do
    [[ -z "${_secret_key}" ]] && continue
    _resolved_var="_RESOLVED_${_secret_key}"
    printf '%s=%s\n' "${_secret_key}" "${!_resolved_var}" >> "${_SECRET_ENV_FILE}"
  done <<< "$(prod_credential_key_map)"

  _create_status=0
  kubectl create secret generic "${SECRET_NAME}" \
    --from-env-file="${_SECRET_ENV_FILE}" \
    -n "${NS}" || _create_status=$?
  _reclaim_secret_env_file
  if (( _create_status != 0 )); then
    error "kubectl create secret generic ${SECRET_NAME} -n ${NS} failed (exit ${_create_status}). The
eleven credentials are in ${ENV_FILE}; fix the reported cause and re-run — this script recreates the
Secret from that file byte-identically."
  fi
  # This SHOULD be unreachable: assert_credential_value_contract just judged
  # these exact values above, before creation. It is checked again here
  # anyway, because "should" is not "is" — kubectl's own env-file parser is a
  # second, independent implementation of "read KEY=VALUE lines", and a
  # residual disagreement between it and this script's read-back is precisely
  # the kind of defect that must never surface as a plain assert_credential_
  # value_contract message with no way out: the Secret already exists at this
  # point, and every later run takes the "present — verify and never rewrite"
  # branch above, so recovery without this message means finding the delete
  # command by hand.
  if ! verify_credential_secret "${NS}" "${SECRET_NAME}" "$CHART_DIR/values.yaml" "${EXTRA_VALUES}" 1; then
    error "Secret '${SECRET_NAME}' was created but failed verification against the identical content
contract already checked above, before creation — see the rejected key named in the error just
above. This should not happen; if it did, kubectl's own read of the Secret disagrees with what this
script resolved, and every later run will otherwise treat the Secret as present, verified, and
never rewritten. Remove it and re-run so it is recreated from ${ENV_FILE}:
  kubectl delete secret ${SECRET_NAME} -n ${NS}"
  fi
  info "Secret '${SECRET_NAME}' created and verified."
fi

# ===========================================================================
# Stage 6 — post-install readiness
# ===========================================================================
step 6 "${TOTAL_STAGES}" "post-install readiness"

# These blocks are not install inputs — they are applied after the release
# exists, by bin/post-install/seed-{peripheral,runtime}-config.sh reading this
# same env file. They are checked here because an operator who discovers them
# after the install has a running deployment whose features report a missing
# peripheral instead of working.
if [[ "${SKIP_POSTINSTALL_CHECK}" == "true" ]]; then
  warn "Post-install readiness skipped (--skip-postinstall-check). Until the DataHub block is seeded
every DataHub-backed feature reports the peripheral's absence rather than working; until the LLM
block is seeded every generation feature fails; until the Langfuse block is seeded tracing stays
off. All three are applied later with
  ENV_FILE=${ENV_FILE} bash helm-charts/bin/post-install/seed-peripheral-config.sh
  ENV_FILE=${ENV_FILE} bash helm-charts/bin/post-install/seed-runtime-config.sh"
else
  # --- DataHub ---
  _missing_datahub=""
  for _dh_var in DATASPOKE_PROD_PERIPHERAL_DATAHUB_GMS_URL \
                 DATASPOKE_PROD_PERIPHERAL_DATAHUB_FRONTEND_URL \
                 DATASPOKE_PROD_PERIPHERAL_DATAHUB_TOKEN; do
    [[ -z "${!_dh_var:-}" ]] && _missing_datahub="${_missing_datahub} ${_dh_var}"
  done

  # The Kafka tuple is the event consumer's connection, and the consumer is off
  # by default — so requiring brokers unconditionally would make a deployment
  # that never runs it either supply an address it never uses or defer this
  # whole stage and lose the DataHub and LLM checks with it. The overlay is
  # already resolved (stage 2), so the answer is available here: the overlay's
  # value when it sets one, else the chart's.
  _event_consumer_enabled="$(overlay_string_value "${EXTRA_VALUES}" "event-consumer.enabled")" \
    || error "Could not read event-consumer.enabled from the --values overlay (see above)."
  if [[ -z "${_event_consumer_enabled}" ]]; then
    _event_consumer_enabled="$(overlay_string_value "$CHART_DIR/values.yaml" "event-consumer.enabled")" \
      || error "Could not read event-consumer.enabled from the chart values (see above)."
  fi
  # YAML booleans arrive as Python's str() of them ("True"/"False"); an operator
  # may equally have written "true" or "1".
  case "$(printf '%s' "${_event_consumer_enabled}" | tr '[:upper:]' '[:lower:]')" in
    true|t|1|yes|on) _event_consumer_on=true ;;
    *)               _event_consumer_on=false ;;
  esac

  if [[ "${_event_consumer_on}" == "true" ]]; then
    # Completeness only — which fields a posture needs. The API cross-validates
    # the six as a set (validate_datahub_kafka_security, spec/API.md §DataHub
    # Kafka security) when the seed PATCHes them, and that check is the
    # authority; this one exists so a blank line is caught before the install
    # rather than at seed time.
    if [[ -z "${DATASPOKE_PROD_PERIPHERAL_DATAHUB_KAFKA_BROKERS:-}" ]]; then
      _missing_datahub="${_missing_datahub} DATASPOKE_PROD_PERIPHERAL_DATAHUB_KAFKA_BROKERS"
    fi
    _kafka_protocol="${DATASPOKE_PROD_PERIPHERAL_DATAHUB_KAFKA_SECURITY_PROTOCOL:-}"
    _kafka_mechanism="${DATASPOKE_PROD_PERIPHERAL_DATAHUB_KAFKA_SASL_MECHANISM:-}"
    case "${_kafka_protocol}" in
      SASL_SSL|SASL_PLAINTEXT)
        if [[ -z "${_kafka_mechanism}" ]]; then
          _missing_datahub="${_missing_datahub} DATASPOKE_PROD_PERIPHERAL_DATAHUB_KAFKA_SASL_MECHANISM"
        elif [[ "${_kafka_mechanism}" != "AWS_MSK_IAM" ]]; then
          # A typed mechanism authenticates with a username and password; under
          # AWS_MSK_IAM the consumer authenticates as its ServiceAccount's IAM
          # role and the API rejects both fields.
          for _kafka_var in DATASPOKE_PROD_PERIPHERAL_DATAHUB_KAFKA_SASL_USERNAME \
                            DATASPOKE_PROD_PERIPHERAL_DATAHUB_KAFKA_SASL_PASSWORD; do
            [[ -z "${!_kafka_var:-}" ]] && _missing_datahub="${_missing_datahub} ${_kafka_var}"
          done
        fi
        ;;
    esac
    info "event-consumer.enabled resolves true — the DataHub Kafka tuple is required."
  else
    info "event-consumer.enabled resolves false — the DataHub Kafka tuple is not required."
  fi

  # --- LLM ---
  _missing_llm=""
  for _llm_var in DATASPOKE_PROD_LLM_PROVIDER DATASPOKE_PROD_LLM_MODEL DATASPOKE_PROD_LLM_API_KEY; do
    [[ -z "${!_llm_var:-}" ]] && _missing_llm="${_missing_llm} ${_llm_var}"
  done

  # Both blocks are reported, then one abort — the same rule stage 1 follows,
  # for the same reason: an operator whose env file has both blocks blank is
  # editing one file, and a stop-on-first-block would make them run this twice
  # to learn what the first run already knew. `|| true` on each report is what
  # buys that; error_no_exit returns 1 by design and `set -e` would end the
  # stage on the first one.
  _postinstall_fatal=0
  if [[ -n "${_missing_datahub}" ]]; then
    _postinstall_fatal=1
    error_no_exit "The DataHub block of ${ENV_FILE} is incomplete — blank:${_missing_datahub}
DataHub is the metadata SSOT every DataSpoke feature reads, so until this is seeded the deployment
runs with every DataHub-backed feature reporting the peripheral's absence." || true
  else
    info "DataHub block is complete."
  fi
  if [[ -n "${_missing_llm}" ]]; then
    _postinstall_fatal=1
    error_no_exit "The LLM block of ${ENV_FILE} is incomplete — blank:${_missing_llm}
The inference loop needs a provider and a model together, and a missing key makes every LLM call
fail at its first use rather than here, so ontology generation, metadata generation and validation
would install healthy and then not work." || true
  else
    info "LLM block is complete."
  fi
  if (( _postinstall_fatal != 0 )); then
    error "Fill in the block(s) reported above, or re-run with --skip-postinstall-check to defer them
deliberately. They are applied after the release exists, by the two post-install seed scripts reading
this same file, so deferring costs only what each message states."
  fi

  # --- Langfuse ---
  # Warns, never blocks: an absent Langfuse block disables tracing and nothing
  # else. Which is exactly why it gets forgotten, so it is still reported.
  _missing_langfuse=""
  for _lf_var in DATASPOKE_PROD_PERIPHERAL_LANGFUSE_HOST \
                 DATASPOKE_PROD_PERIPHERAL_LANGFUSE_PUBLIC_KEY \
                 DATASPOKE_PROD_PERIPHERAL_LANGFUSE_SECRET_KEY; do
    [[ -z "${!_lf_var:-}" ]] && _missing_langfuse="${_missing_langfuse} ${_lf_var}"
  done
  if [[ -n "${_missing_langfuse}" ]]; then
    warn "The Langfuse block of ${ENV_FILE} is incomplete — blank:${_missing_langfuse}
LLM observability stays off; nothing else is affected, and it can be filled in and seeded at any
time against the running deployment."
  else
    info "Langfuse block is complete."
  fi
fi

# ===========================================================================
# Stage 7 — image tag
# ===========================================================================
step 7 "${TOTAL_STAGES}" "image tag"

# install.sh requires an explicit --image-tag in prod so a shared registry never
# receives the mutable `:dev`. Deriving it from HEAD is what makes that
# requirement cheap to satisfy correctly — and refusing a dirty tree is the same
# principle applied to images: a tag naming a commit that does not contain what
# is being deployed is worse than no tag at all, because it reads as provenance.
_mutable_tags=" dev latest main master stable edge "

if [[ -n "${IMAGE_TAG_ARG}" ]]; then
  IMAGE_TAG="${IMAGE_TAG_ARG}"
  _tag_source="--image-tag"
else
  require_tools git
  if ! git -C "${REPO_ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    error "${REPO_ROOT} is not a git work tree, so no tag can be derived from HEAD. Pass --image-tag <tag> explicitly."
  fi
  # --porcelain counts untracked files as changes deliberately: the Docker build
  # context is the work tree, not the commit, so an untracked file is content
  # the image would carry and the commit would not name.
  _git_dirty="$(git -C "${REPO_ROOT}" status --porcelain 2>/dev/null || true)"
  if [[ -n "${_git_dirty}" ]]; then
    if [[ "${ALLOW_DIRTY}" != "true" ]]; then
      error "The work tree at ${REPO_ROOT} has uncommitted or untracked changes, so a tag derived
from HEAD would name a commit that does not contain what would be built. Commit or stash them, pass
--image-tag <tag> explicitly, or re-run with --allow-dirty to accept the mismatch."
    fi
    warn "Deriving the tag from HEAD with a dirty work tree (--allow-dirty): the image will carry content the tagged commit does not."
  fi
  IMAGE_TAG="$(git -C "${REPO_ROOT}" rev-parse --short HEAD)"
  _tag_source="git rev-parse --short HEAD"
fi

# The one shared grammar (assert_image_tag, lib/helpers.sh) install.sh applies
# to its own --image-tag, so the tag printed in the install command below cannot
# be one install.sh then rejects.
assert_image_tag "${IMAGE_TAG}"
if [[ "${_mutable_tags}" == *" ${IMAGE_TAG} "* ]]; then
  error "Image tag '${IMAGE_TAG}' is a moving tag. A shared registry's '${IMAGE_TAG}' points at
whatever was pushed last, so the digest a rollout resolves is not the one this pre-flight validated
against. Use an immutable tag — the git short SHA this script derives by default."
fi
info "Image tag: ${IMAGE_TAG} (${_tag_source})."

# ===========================================================================
# Result
# ===========================================================================
echo ""
echo "=== Pre-flight passed ==="
echo ""
if [[ "${VERIFY_ONLY}" == "true" ]]; then
  echo "  --verify-only: the env file, the namespace and the credentials Secret were left untouched."
  echo ""
fi
echo "  Install with:"
printf '    %s/bin/install.sh --profile prod --image-tag %s' "${HELM_CHARTS_DIR}" "${IMAGE_TAG}"
if [[ -n "${EXTRA_VALUES}" ]]; then
  printf ' --values %s' "${EXTRA_VALUES}"
fi
# Only when it is not the default: install.sh resolves helm-charts/.env.prod on
# its own for --profile prod, and passing it explicitly is one more place a
# second file can be named by mistake.
if [[ "${ENV_FILE}" != "$HELM_CHARTS_DIR/.env.prod" ]]; then
  printf ' --env-file %s' "${ENV_FILE}"
fi
printf '\n'
echo ""
echo "  Then, once the release is up and the peripheral/LLM blocks of the env file are filled in:"
echo "    ENV_FILE=${ENV_FILE} bash ${HELM_CHARTS_DIR}/bin/post-install/seed-peripheral-config.sh"
echo "    ENV_FILE=${ENV_FILE} bash ${HELM_CHARTS_DIR}/bin/post-install/seed-runtime-config.sh"
echo ""
info "Total elapsed: $((SECONDS - START_TIME))s"
echo ""
