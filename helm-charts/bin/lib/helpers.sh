# Shared shell helpers for helm-charts/bin scripts.
# Source this file — do not execute directly.
# Usage: source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../../lib/helpers.sh"
#   (adjust the relative path depending on script depth)

info()  { echo -e "\033[0;32m[INFO]\033[0m  $*"; }
warn()  { echo -e "\033[0;33m[WARN]\033[0m  $*"; }
error() { echo -e "\033[0;31m[ERROR]\033[0m $*" >&2; exit 1; }

# print_usage [script_path]
# Print the script's leading comment block (after the shebang), stripped of
# the `# ` prefix. Stops at the first non-comment line. Default: caller's $0.
print_usage() {
  local script="${1:-$0}"
  awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$script"
}

# step <n> <total> <name>
# Print a green [INFO] step-boundary header with elapsed time.
# Reads START_TIME from the environment (exported by the parent script).
step() {
  local n="$1" total="$2" name="$3"
  local elapsed=$(( SECONDS - ${START_TIME:-0} ))
  info "==> [${n}/${total}] ${name} (t+${elapsed}s)"
}

# require_tools <cmd> [<cmd>...]
# Verify each command is on PATH; error if any are missing.
require_tools() {
  for cmd in "$@"; do
    command -v "$cmd" >/dev/null 2>&1 || error "'${cmd}' is not installed or not in PATH."
  done
}

# use_context <cluster>
# Switch the active kubectl context.
use_context() {
  local cluster="$1"
  info "Switching to Kubernetes context: ${cluster}"
  kubectl config use-context "${cluster}"
}

# ingress_mode
# Echo the ingress deployment mode from DATASPOKE_KUBE_INGRESS_MODE:
#   managed — this project installs and owns an nginx-ingress controller plus a
#             LoadBalancer that assigns an external IP, and derives the
#             <IP>.nip.io domain (GKE Autopilot / minikube default).
#   shared  — reuse a pre-existing cluster ingress controller. No controller
#             install and no LoadBalancer IP; virtual hosts ride a pre-set
#             domain (DATASPOKE_KUBE_INGRESS_DOMAIN) over http or https per
#             DATASPOKE_KUBE_INGRESS_SCHEME, and TCP services are reached via
#             `kubectl port-forward` (bin/port-forward.sh), not the ingress.
# Default: managed (preserves existing behavior when the var is unset).
ingress_mode() { echo "${DATASPOKE_KUBE_INGRESS_MODE:-managed}"; }

# ingress_scheme
# Echo the URL scheme for ingress-domain-based URLs from
# DATASPOKE_KUBE_INGRESS_SCHEME: `http` (default, both modes) or `https` (set
# when a shared controller terminates TLS + HSTS in front of the virtual
# hosts). Errors out on any other value. IP:port TCP endpoints (dev-lock,
# Kafka, Postgres) bypass the ingress and never take this scheme.
ingress_scheme() {
  local scheme="${DATASPOKE_KUBE_INGRESS_SCHEME:-http}"
  if [[ "$scheme" != "http" && "$scheme" != "https" ]]; then
    error "Invalid DATASPOKE_KUBE_INGRESS_SCHEME '${scheme}'. Must be 'http' or 'https'."
  fi
  echo "$scheme"
}

# ingress_class
# Echo the validated IngressClass name every DataSpoke Ingress binds to, from
# DATASPOKE_KUBE_INGRESS_CLASS (default `nginx`). One source for all three
# paths that create Ingresses — the umbrella chart's API/frontend/Airflow
# ingresses, the peripheral charts (DataHub frontend, Langfuse), and the GMS
# kubectl manifest — each supplying it by `--set` or substitution so it
# outranks any values file. In managed mode it is also the class name the
# owned nginx-ingress controller registers as its ingressClassResource.
# Errors out if the value is not a valid DNS-1123 subdomain (Kubernetes
# object-name rules) — it is interpolated into `helm --set` tokens, where a
# comma starts the next assignment and a newline becomes a standalone helm
# flag, and into a `sed` substitution whose output is piped to `kubectl apply`.
ingress_class() {
  local class="${DATASPOKE_KUBE_INGRESS_CLASS:-nginx}"
  if [[ ! "$class" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]]; then
    error "Invalid DATASPOKE_KUBE_INGRESS_CLASS '${class}'. Must be a valid DNS-1123 subdomain (lowercase alphanumeric, '-', '.')."
  fi
  echo "$class"
}

# datahub_gms_host
# Echo the validated virtual host serving DataHub GMS to laptop-side callers
# (tests, tooling, the install's own PAT mint). GMS gets its own hostname
# rather than a path on the DataHub frontend host so its rule is a plain
# host-root route that needs no rewrite annotation and no second Ingress on a
# claimed host. In-cluster callers use cluster DNS and are unaffected.
# Errors out if DATASPOKE_KUBE_INGRESS_DOMAIN is unset or the derived host is
# not a valid DNS-1123 subdomain — the host reaches the same `helm --set` and
# `sed`-into-`kubectl apply` sinks as ingress_class(), and additionally becomes
# the origin that tooling sends the DataHub PAT to.
datahub_gms_host() {
  local host="datahub-gms.${DATASPOKE_KUBE_INGRESS_DOMAIN:-}"
  if [[ ! "$host" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]]; then
    error "Invalid DATASPOKE_KUBE_INGRESS_DOMAIN '${DATASPOKE_KUBE_INGRESS_DOMAIN:-}'. The derived GMS host '${host}' must be a valid DNS-1123 subdomain (lowercase alphanumeric, '-', '.')."
  fi
  echo "$host"
}

# ingress_tls_secret
# Echo the validated TLS Secret name from DATASPOKE_KUBE_INGRESS_TLS_SECRET,
# or an empty string when unset (no per-Ingress TLS). Errors out if the value
# is non-empty and not a valid DNS-1123 subdomain (Kubernetes object-name
# rules) — it is interpolated into `helm --set` tokens, so an unvalidated
# value could inject extra flags via a comma or newline.
ingress_tls_secret() {
  local secret="${DATASPOKE_KUBE_INGRESS_TLS_SECRET:-}"
  if [[ -n "$secret" && ! "$secret" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]]; then
    error "Invalid DATASPOKE_KUBE_INGRESS_TLS_SECRET '${secret}'. Must be a valid DNS-1123 subdomain (lowercase alphanumeric, '-', '.')."
  fi
  echo "$secret"
}

# tcp_access_host
# Echo the host that laptop/test clients use to reach TCP services (Postgres,
# Redis, Kafka, dev-lock). In shared mode these are not published on the shared
# controller, so access is via `kubectl port-forward` to 127.0.0.1. In managed
# mode they ride the owned ingress LoadBalancer IP.
tcp_access_host() {
  if [[ "$(ingress_mode)" == "shared" ]]; then
    echo "127.0.0.1"
  else
    echo "${DATASPOKE_KUBE_INGRESS_IP:-}"
  fi
}

# ensure_namespace <ns>
# Get-or-create a Kubernetes namespace, idempotent.
ensure_namespace() {
  local ns="$1"
  if kubectl get namespace "${ns}" >/dev/null 2>&1; then
    info "Namespace '${ns}' already exists."
  else
    info "Creating namespace '${ns}'..."
    kubectl create namespace "${ns}"
  fi
}

# helm_repo_add_if_missing <name> <url>
# Idempotent helm repo add. Does NOT run helm repo update — callers manage
# that themselves (some update a specific repo, some update all).
helm_repo_add_if_missing() {
  local name="$1" url="$2"
  if helm repo list 2>/dev/null | grep -q "^${name}"; then
    info "Helm repo '${name}' already added."
  else
    info "Adding Helm repo '${name}' (${url})..."
    helm repo add "${name}" "${url}"
  fi
}

# upsert_env_var <key> <value> [env_file]
# Portable .env upsert: update existing KEY= line or append if absent.
# Note: uses '|' as sed delimiter — safe for hex secrets, URLs, and hostnames
# that do not contain literal pipe characters.
upsert_env_var() {
  local key="$1" value="$2"
  # Default: walk up from the sourcing script's dir to find helm-charts/.env.dev
  local file="${3:-${ENV_FILE:-$(cd "$(dirname "${BASH_SOURCE[1]}")" && pwd)/../../.env.dev}}"
  if grep -q "^${key}=" "${file}" 2>/dev/null; then
    sed -i.bak "s|^${key}=.*|${key}=${value}|" "${file}" && rm -f "${file}.bak"
  else
    # Ensure file ends in a newline before appending so the new line isn't
    # concatenated onto the last existing line, and no blank line is inserted.
    [[ -s "${file}" && "$(tail -c1 "${file}" | wc -l)" -eq 0 ]] && printf '\n' >> "${file}"
    printf '%s=%s\n' "${key}" "${value}" >> "${file}"
  fi
  # Harden permissions after every write — the file contains secrets (postgres
  # password, redis password, internal token, LLM API key, DataHub PAT).
  chmod 600 "${file}" 2>/dev/null || true
}

# resolve_image_digest <image_ref>
# Resolves the pushed content digest for <image_ref> ("<registry>/<name>:<tag>")
# and prints a bare `sha256:...` token on stdout. Vendor-aware, mirroring the
# three-way `case "${VENDOR}"` dispatch in build-image.sh exactly:
#   GCP|gcp -> `gcloud artifacts docker images describe --project <parsed>`.
#              Required for GCP: those builds go through Cloud Build
#              server-side, so no image ever lands in a local Docker daemon
#              and `docker inspect` has nothing to inspect. `--project` is
#              parsed out of the registry host the same way build-image.sh's
#              own GCP branch does — the active gcloud config project need
#              not match the registry project, and a mismatch here aborts the
#              install (bin/install.sh's _resolve_digest_or_abort) unless the
#              operator re-runs with --no-digest-pin.
#   AWS|aws -> `aws ecr describe-images`, parsing the ECR host/region/repo out
#              of <image_ref> with the identical sed/parameter-expansion this
#              function's caller uses to build it, matching build-image.sh's
#              own AWS branch. Reads the registry's current record for the
#              tag (not a local daemon), unlike the branch below.
#   empty/local (and any other value) -> `docker inspect` against the LOCAL
#              Docker daemon's recorded RepoDigests for <image_ref>, honouring
#              DATASPOKE_DOCKER_SUDO. This reports what THIS HOST pushed, not
#              what the registry's tag currently resolves to — a TOCTOU gap
#              relative to the GCP/AWS branches above that query the registry
#              directly. On a stale local `<repository>:<tag>` (a prior build
#              cached under the same tag, not re-pulled or re-built since),
#              this branch RESOLVES SUCCESSFULLY but to the OLD content's
#              digest — a successful resolve that is nonetheless wrong, since
#              nothing here compares the local daemon's cached image against
#              the registry's current tag. This is a real gap on a
#              --skip-build install run from a host whose local Docker cache
#              was not refreshed by this run's own build step. An image ID
#              that was also pushed/tagged into a second repository carries
#              multiple RepoDigests in unspecified order, so the match is
#              keyed on the repository part of <image_ref> rather than
#              blindly taking index 0.
# Never aborts ITSELF: a missing CLI or a non-zero/malformed result prints
# nothing on stdout and `warn`s instead of erroring here — its caller,
# _resolve_digest_or_abort (bin/install.sh), aborts the whole install on an
# empty result (the explicit escape hatch is --no-digest-pin, which skips
# this function entirely). Every caller captures this function's stdout via
# `$(resolve_image_digest ...)`, and `warn` (unlike `error`) writes to
# stdout, not stderr — so every `warn` call below is explicitly redirected to
# fd 2, or its text would leak into the captured digest variable instead of
# leaving it empty.
#
# The GCP and AWS branches retry their registry call up to 3 times (2s apart)
# before giving up — a bare CLI invocation with no retry would abort the
# whole install on one transient network blip, the same class of failure
# `_build_chart_deps` already rides out for `helm dependency build`. Both
# branches short-circuit that retry loop, without waiting out the full 3
# attempts, on the one response each knows is not transient: gcloud's
# NOT_FOUND (the image/tag genuinely does not exist in the registry) and
# AWS's literal "None" (the analogous nonexistent-tag response from
# `describe-images --query ... --output text`) — retrying either gains
# nothing, since the image will not exist on the next attempt either. The
# local/no-vendor branch reads a local Docker daemon, not the network, so it
# is not retried at all.
resolve_image_digest() {
  local image_ref="$1"
  local vendor="${DATASPOKE_KUBE_CLOUD_VENDOR:-}"
  local digest=""
  # `${image_ref%:*}` strips only the shortest trailing ":*" match — i.e. the
  # ":<tag>" suffix — even when the registry host itself carries a port
  # (host:port/name:tag), because bash's shortest-suffix rule for `%` anchors
  # on the LAST colon in the string. Shared by the AWS and local branches below.
  local repo="${image_ref%:*}"

  case "${vendor}" in
    GCP|gcp)
      if ! command -v gcloud >/dev/null 2>&1; then
        warn "gcloud not found — cannot resolve image digest for '${image_ref}'." >&2
        return 0
      fi
      # <region>-docker.pkg.dev/<project>/<repo> -> <project>. Same extraction
      # as build-image.sh's GCP branch; omitted entirely (rather than defaulted)
      # when the registry URL doesn't match, so a non-Artifact-Registry GCP
      # registry still gets a (project-less) lookup attempt instead of an error.
      local gcp_project
      gcp_project="$(echo "${image_ref}" | sed -n 's|^\([^/]*-docker\.pkg\.dev\)/\([^/]*\)/.*|\2|p')"
      local -a gcloud_args=(artifacts docker images describe "${image_ref}" --format='value(image_summary.digest)')
      [[ -n "${gcp_project}" ]] && gcloud_args+=(--project "${gcp_project}")
      local gcloud_attempt gcloud_err_file gcloud_status gcloud_not_found=false
      for gcloud_attempt in 1 2 3; do
        # stderr is captured to a temp file rather than merged in with `2>&1`
        # — merging would corrupt a SUCCESSFUL digest capture the moment
        # gcloud also emits anything on stderr (e.g. an unrelated deprecation/
        # config warning), silently turning a working lookup into a malformed
        # digest that fails the sha256 shape check below with no visible
        # cause. The `if ... ; then ... ; else` form (not `var=$(...) || true`
        # followed by a bare `$?` on the next line) is required: a plain `if`
        # with no matching command in its failed branch reports exit status 0
        # for the WHOLE compound statement, so `gcloud_status=$?` read AFTER
        # the `fi` would always read 0 regardless of gcloud's real exit code
        # — capturing it inside the `else` branch is the only place it is the
        # real gcloud exit status.
        gcloud_err_file="$(mktemp)"
        if digest="$(gcloud "${gcloud_args[@]}" 2>"${gcloud_err_file}")"; then
          gcloud_status=0
          rm -f "${gcloud_err_file}"
          break
        else
          gcloud_status=$?
        fi
        # A missing image/tag is not a transient failure, so a retry 2s later
        # gains nothing — same reasoning as the AWS branch's literal "None"
        # short-circuit below. Two distinct gcloud error shapes both mean
        # "does not exist" and must both short-circuit here:
        #   - a missing REPOSITORY surfaces the uncaught `GetRepository` 404
        #     verbatim: "NOT_FOUND: Requested entity was not found."
        #   - a missing image/tag inside an existing repository is caught by
        #     `_ValidateAndGetDockerVersion`
        #     (googlecloudsdk/command_lib/artifacts/docker_util.py) and
        #     re-raised as InvalidInputValueError(_DOCKER_IMAGE_NOT_FOUND),
        #     whose text is "Image not found.\n\nA valid container image ..."
        #     — it does NOT contain the substring "NOT_FOUND".
        # Matched case-insensitively against both phrasings so either shape
        # short-circuits instead of only the repository-missing one.
        # PERMISSION_DENIED is left on the general retry path below instead: an
        # IAM propagation delay or a token that gets refreshed mid-run can
        # plausibly resolve within 3 attempts, unlike a genuinely absent image.
        if grep -qiE "NOT_FOUND|Image not found" "${gcloud_err_file}" 2>/dev/null; then
          gcloud_not_found=true
          warn "Image '${image_ref}' not found in Artifact Registry — not retrying: $(cat "${gcloud_err_file}" 2>/dev/null)" >&2
          break
        fi
        if (( gcloud_attempt < 3 )); then
          warn "gcloud artifacts docker images describe failed for '${image_ref}' (attempt ${gcloud_attempt}/3, exit ${gcloud_status}) — retrying in 2s: $(cat "${gcloud_err_file}" 2>/dev/null)" >&2
          rm -f "${gcloud_err_file}"
          sleep 2
        fi
      done
      if [[ ${gcloud_status} -ne 0 ]]; then
        # The NOT_FOUND branch above already warned with the specific cause —
        # avoid a second, misleading "after 3 attempts" report for a lookup
        # that only ran once.
        if [[ "${gcloud_not_found}" == "false" ]]; then
          warn "gcloud artifacts docker images describe failed for '${image_ref}' after 3 attempts (exit ${gcloud_status}): $(cat "${gcloud_err_file}" 2>/dev/null)" >&2
        fi
        digest=""
      fi
      rm -f "${gcloud_err_file}"
      ;;
    AWS|aws)
      if ! command -v aws >/dev/null 2>&1; then
        warn "aws CLI not found — cannot resolve image digest for '${image_ref}'." >&2
        return 0
      fi
      # Mirrors build-image.sh's own ECR host/region/repo parsing exactly:
      # registry host = everything before the first '/' of the repo (tag
      # stripped), region parsed out of that host, repository name = the
      # remainder joined with the image name (already part of <image_ref>).
      local ecr_host="${repo%%/*}"
      local ecr_repo="${repo#*/}"
      local tag="${image_ref##*:}"
      local ecr_region
      ecr_region="$(echo "${ecr_host}" | sed -n 's|^[0-9]*\.dkr\.ecr\.\([a-z0-9-]\{1,\}\)\.amazonaws\.com$|\1|p')"
      if [[ -z "${ecr_region}" ]]; then
        warn "Could not parse the AWS region from '${image_ref}' — cannot resolve image digest." >&2
        return 0
      fi
      if [[ -z "${DATASPOKE_AWS_PROFILE:-}" ]]; then
        warn "DATASPOKE_AWS_PROFILE is unset — cannot resolve image digest for '${image_ref}'." >&2
        return 0
      fi
      local aws_attempt aws_err_file aws_status aws_not_found=false
      for aws_attempt in 1 2 3; do
        # stderr captured to a temp file (not merged with `2>&1`), and the
        # `if ... ; then ... ; else` form (not a bare `$?` read after the
        # `fi`) so aws_status carries the real aws exit code — same reasoning
        # as the GCP branch above (a plain `if` with no matching command in
        # its failed branch reports exit status 0 for the whole compound
        # statement, so a `$?` read after `fi` would always read 0).
        aws_err_file="$(mktemp)"
        if digest="$(aws ecr describe-images --region "${ecr_region}" --profile "${DATASPOKE_AWS_PROFILE}" \
          --repository-name "${ecr_repo}" --image-ids "imageTag=${tag}" \
          --query 'imageDetails[0].imageDigest' --output text 2>"${aws_err_file}")"; then
          aws_status=0
          rm -f "${aws_err_file}"
          break
        else
          aws_status=$?
        fi
        # A missing repository (RepositoryNotFoundException) or a missing tag
        # inside an existing repository (ImageNotFoundException) are both
        # non-zero-exit failures that mean "does not exist" — not transient,
        # so retrying gains nothing, same reasoning as the tag-only "None"
        # short-circuit below (which only fires on exit 0).
        if grep -qE "RepositoryNotFoundException|ImageNotFoundException" "${aws_err_file}" 2>/dev/null; then
          aws_not_found=true
          warn "'${image_ref}' not found in ECR (region ${ecr_region}) — not retrying: $(cat "${aws_err_file}" 2>/dev/null)" >&2
          break
        fi
        if (( aws_attempt < 3 )); then
          warn "aws ecr describe-images failed for '${image_ref}' (attempt ${aws_attempt}/3, exit ${aws_status}) — retrying in 2s: $(cat "${aws_err_file}" 2>/dev/null)" >&2
          rm -f "${aws_err_file}"
          sleep 2
        fi
      done
      if [[ ${aws_status} -ne 0 ]]; then
        # The RepositoryNotFoundException/ImageNotFoundException branch above
        # already warned with the specific cause — avoid a second, misleading
        # "after 3 attempts" report for a lookup that only ran once.
        if [[ "${aws_not_found}" == "false" ]]; then
          warn "aws ecr describe-images failed for '${image_ref}' after 3 attempts (exit ${aws_status}): $(cat "${aws_err_file}" 2>/dev/null)" >&2
        fi
        digest=""
      elif [[ "${digest}" == "None" ]]; then
        # `--query ... --output text` on a nonexistent tag returns the
        # literal string "None" with exit 0 — not a describe-images failure,
        # so it would otherwise fall through to the generic sha256-shape
        # warning below with no indication of the real cause. Not retried:
        # a nonexistent tag will not exist on the next attempt either.
        warn "Tag '${tag}' not found in ECR repository '${ecr_repo}' (region ${ecr_region})." >&2
        digest=""
      fi
      rm -f "${aws_err_file}"
      ;;
    *)
      if ! command -v docker >/dev/null 2>&1; then
        warn "docker not found — cannot resolve image digest for '${image_ref}'." >&2
        return 0
      fi
      local docker_cmd=(docker)
      [[ "${DATASPOKE_DOCKER_SUDO:-false}" == "true" ]] && docker_cmd=(sudo docker)
      local repo_digests
      repo_digests="$("${docker_cmd[@]}" inspect --format='{{range .RepoDigests}}{{println .}}{{end}}' "${image_ref}" 2>/dev/null || true)"
      # An image ID pushed to more than one repository carries multiple
      # RepoDigests in unspecified order — select the entry whose repository
      # (the part before '@') matches this image_ref's repository instead of
      # blindly taking index 0, which could silently attest another
      # repository's digest.
      digest="$(printf '%s\n' "${repo_digests}" | awk -F'@' -v repo="${repo}" '$1==repo{print $2; exit}')"
      ;;
  esac

  if [[ ! "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    warn "Could not resolve an image digest for '${image_ref}'. The caller (_resolve_digest_or_abort in bin/install.sh) aborts the install on this — re-run with --no-digest-pin to deploy '${image_ref}' by its mutable tag instead." >&2
    return 0
  fi

  echo "${digest}"
}

# wait_for_pod <name> <ns> <timeout_secs>
# Poll until the named pod reports Ready=True or timeout.
wait_for_pod() {
  local name="$1" ns="$2" timeout_secs="$3"
  info "  Waiting for pod $name to be Ready (up to ${timeout_secs}s)..."
  local elapsed=0
  while (( elapsed < timeout_secs )); do
    # kubectl wait fails instantly if pod is in CrashLoopBackOff, so we
    # poll manually to tolerate transient restarts during startup.
    local ready
    ready=$(kubectl get "pod/$name" -n "$ns" \
      -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "False")
    if [[ "$ready" == "True" ]]; then
      info "  Pod $name is Ready."
      return 0
    fi
    if (( elapsed % 30 == 0 && elapsed > 0 )); then
      local phase restarts
      phase=$(kubectl get "pod/$name" -n "$ns" -o jsonpath='{.status.phase}' 2>/dev/null || echo "Unknown")
      restarts=$(kubectl get "pod/$name" -n "$ns" -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null || echo "?")
      info "  [$name] ${elapsed}s — phase=$phase restarts=$restarts"
    fi
    sleep 10
    (( elapsed += 10 ))
  done
  error "Pod $name not ready after ${timeout_secs}s"
}

# wait_for_job <name> <ns> <timeout_secs>
# Poll until the job's pod phase is Succeeded or timeout.
wait_for_job() {
  local name="$1" ns="$2" timeout_secs="$3"
  info "  Waiting for job $name to complete (up to ${timeout_secs}s)..."
  local elapsed=0
  while (( elapsed < timeout_secs )); do
    local phase
    phase=$(kubectl get pod -l "job-name=$name" -n "$ns" \
      -o jsonpath='{.items[0].status.phase}' 2>/dev/null || echo "Pending")
    if [[ "$phase" == "Succeeded" ]]; then
      info "  Job $name completed."
      return 0
    elif [[ "$phase" == "Failed" ]]; then
      error "Job $name failed. Check logs: kubectl logs -l job-name=$name -n $ns"
    fi
    # Print progress every 30s
    if (( elapsed % 30 == 0 && elapsed > 0 )); then
      local tail
      tail=$(kubectl logs -l "job-name=$name" -n "$ns" --tail=1 2>/dev/null || echo "...")
      info "  [$name] ${elapsed}s elapsed — ${tail}"
    fi
    sleep 10
    (( elapsed += 10 ))
  done
  error "Job $name timed out after ${timeout_secs}s"
}

# wait_for_pod_by_label <label> <ns> <timeout_secs>
# Resolve the first pod matching <label>, then delegate to wait_for_pod.
wait_for_pod_by_label() {
  local label="$1" ns="$2" timeout_secs="$3"
  local name
  name=$(kubectl get pod -l "$label" -n "$ns" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
  if [[ -z "$name" ]]; then
    info "  No pod found with label $label yet, waiting..."
    local waited=0
    while (( waited < timeout_secs )); do
      sleep 10; (( waited += 10 ))
      name=$(kubectl get pod -l "$label" -n "$ns" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
      [[ -n "$name" ]] && break
    done
    [[ -z "$name" ]] && error "No pod found for label $label after ${timeout_secs}s"
  fi
  wait_for_pod "$name" "$ns" "$timeout_secs"
}
