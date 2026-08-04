# Shared shell helpers for helm-charts/bin scripts.
# Source this file — do not execute directly.
# Usage: source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../../lib/helpers.sh"
#   (adjust the relative path depending on script depth)

info()  { echo -e "\033[0;32m[INFO]\033[0m  $*"; }
warn()  { echo -e "\033[0;33m[WARN]\033[0m  $*"; }
error() { echo -e "\033[0;31m[ERROR]\033[0m $*" >&2; exit 1; }

# error_no_exit <msg>
# Report a failure in error()'s voice — the same red [ERROR] line on stderr —
# but `return 1` instead of `exit 1`. For a resolver that has to be able to
# fail ONE item and hand the decision to stop back to its caller: a per-key
# credential resolver runs inside `$( ... )`, where error()'s exit would kill
# only the subshell and leave the caller reading an empty value as though the
# key had simply resolved to nothing.
error_no_exit() { echo -e "\033[0;31m[ERROR]\033[0m $*" >&2; return 1; }

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

# Static in-pod Python source for api_internal_request() below. METHOD,
# path, body, and the connect/read timeout reach it at runtime as argv
# (sys.argv[1..4]) — nothing here is ever assembled by interpolating caller
# data into this string. A body of `-` is the sentinel for the stdin body
# mode described on api_internal_request(): the payload is then read from
# stdin instead, so it never appears in argv.
read -r -d '' _API_INTERNAL_REQUEST_PY <<'PYEOF' || true
import sys, os, urllib.request, urllib.error

method = sys.argv[1]
path = sys.argv[2]
body = sys.argv[3] if len(sys.argv) > 3 else ""
if body == "-":
    body = sys.stdin.read()
timeout = float(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else 10

token = os.environ.get("DATASPOKE_INTERNAL_TOKEN", "")
url = "http://127.0.0.1:8002" + path
data = body.encode("utf-8") if body else None
headers = {"X-Internal-Token": token, "Content-Type": "application/json"}

req = urllib.request.Request(url, data=data, method=method, headers=headers)
try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        print(resp.status)
        sys.stdout.write(resp.read().decode("utf-8", errors="replace"))
except urllib.error.HTTPError as e:
    # A real HTTP response (4xx/5xx), not a connection failure — printed the
    # same way as a success, never treated as one here.
    print(e.code)
    sys.stdout.write(e.read().decode("utf-8", errors="replace"))
except Exception as e:
    print("000")
    sys.stderr.write("api_internal_request: connection failed: %s\n" % e)
PYEOF

# api_internal_request <namespace> <METHOD> <path> <json-body> [timeout]
# Calls the DataSpoke API's /internal/* surface from inside its own pod:
# `kubectl exec`s into deploy/dataspoke-api and runs the stdlib
# `urllib.request` script above against http://127.0.0.1:8002<path> — the
# API's own container port (docker-images/api/Dockerfile EXPOSE 8002,
# dataspoke-api Service targetPort 8002). No ingress host, no DNS, and no
# `curl` (absent from the python:3.13-slim API image) are involved. METHOD,
# path, timeout, and — unless the stdin body mode below is on — the body are
# passed as argv to `python3 -c`, not spliced into the Python source;
# `kubectl exec` runs the command array directly with no shell in the loop,
# so there is no quoting hazard either way.
#
# `timeout` (seconds) bounds the in-pod urlopen() call, default 10 — plenty
# for the storage-bounded seed-script PATCHes, but too tight for a slow-but-
# working handler: /internal/admin/dags/verify calls AirflowClient.list_dags(),
# whose own httpx client carries a 60s timeout after authenticating first.
# Callers whose endpoint can legitimately take longer than 10s pass a larger
# value explicitly rather than tripping this helper's `000`/retry path on a
# request that was never actually a connection failure.
#
# The in-pod script reads DATASPOKE_INTERNAL_TOKEN from its own environment
# (mounted via envFrom from dataspoke-secrets) and sends it as
# X-Internal-Token, so the token is never extracted to the caller's machine.
#
# Output contract: stdout's first line is the HTTP status code, the rest is
# the response body — the same shape callers used to branch on from curl's
# %{http_code}. `000` on the first line means the in-pod script could not
# connect at all (a completely empty response is also treated as `000` —
# there is otherwise no valid status line to read); an HTTPError (a real
# 4xx/5xx response) is printed the same way and is never treated as a
# connection failure.
#
# Only a `000` is retried — 5 attempts, 3s apart — to ride out a pod that is
# Ready but momentarily refusing connections right after startup. Any real
# HTTP response, 4xx/5xx included, returns immediately and is never retried.
#
# `kubectl exec` itself failing (pod missing, RBAC denied) is a distinct
# failure from a `000` connection failure and is not retried either. By
# default it aborts immediately via `error()` so it never gets folded into a
# confusing "connection failed after 5 attempts" message. Set
# API_INTERNAL_REQUEST_QUIET=1 to downgrade that one failure mode to `warn`
# plus `return 1` instead — for a caller whose own call site is deliberately
# best-effort and must never abort the surrounding script; the seed scripts
# leave this unset so an exec failure still aborts them.
#
# Set API_INTERNAL_REQUEST_BODY_STDIN=1 for any credential-bearing payload —
# a DataHub PAT, a Kafka SASL password, a Langfuse secret key, an LLM API key.
# Under it argv carries the literal `-` in the body position and the real body
# is fed to the in-pod script on stdin through `kubectl exec -i`, so the value
# never appears in `ps auxww` or /proc/<pid>/cmdline — on the operator's
# machine or inside the pod, for the life of the process — and never reaches
# the kube-apiserver audit log either, which records an exec's command array
# as the exec subresource's query string. Argv stays the default, because the
# non-secret call sites read more plainly with the body visible in the command
# and gain nothing from the indirection.
api_internal_request() {
  local ns="$1" method="$2" path="$3" body="${4:-}" timeout="${5:-10}"
  local quiet="${API_INTERNAL_REQUEST_QUIET:-0}"
  local body_stdin="${API_INTERNAL_REQUEST_BODY_STDIN:-0}"
  local attempt output exec_err exec_status exec_err_text status
  # `-i` attaches the pipe the body rides in on; `-t` is deliberately never
  # passed — a TTY would translate newlines and corrupt the payload, and a CI
  # run has no terminal to allocate one from.
  # An empty body stays on argv either way: there is nothing to disclose, and
  # the `-` sentinel would turn it into a one-newline body instead of none.
  local argv_body="${body}" stdin_body=""
  local -a kubectl_argv=(kubectl exec -n "${ns}")
  if [[ "${body_stdin}" == "1" && -n "${body}" ]]; then
    kubectl_argv+=(-i)
    argv_body="-"
    stdin_body="${body}"
  fi
  kubectl_argv+=(deploy/dataspoke-api -c api --
    python3 -c "${_API_INTERNAL_REQUEST_PY}" "${method}" "${path}" "${argv_body}" "${timeout}")
  for attempt in 1 2 3 4 5; do
    exec_err="$(mktemp)"
    # The body is re-fed from a variable through a here-string, which bash
    # re-expands on every iteration — a pipe or a redirected file handle would
    # already be at EOF from attempt 2 on, sending an empty body that PATCHes
    # nothing while still answering 200. In argv mode the here-string carries
    # only a newline, which no one reads: without `-i`, kubectl does not
    # attach its own stdin to the container.
    # Two here-string properties this rides on. It appends a newline, so stdin
    # mode transports `body + "\n"` where argv mode transports exactly `body`
    # — tolerated because every body on this path is JSON, whose parser treats
    # trailing whitespace as insignificant. And bash before 5.1 backs it with
    # a mode-0600 file under TMPDIR that it unlinks before the exec, so on
    # those shells (macOS /bin/bash 3.2 among them) a credential body is
    # briefly on disk, readable only by the invoking user.
    if output="$("${kubectl_argv[@]}" 2>"${exec_err}" <<<"${stdin_body}")"; then
      exec_status=0
    else
      exec_status=$?
    fi
    # Read and remove the temp file before any exit path below — the earlier
    # shape only removed it on success, leaking one file per kubectl-exec
    # failure (every attempt on a pod that never comes back).
    exec_err_text="$(cat "${exec_err}" 2>/dev/null)"
    rm -f "${exec_err}"
    if [[ ${exec_status} -ne 0 ]]; then
      if [[ "${quiet}" == "1" ]]; then
        warn "kubectl exec into dataspoke-api (-n ${ns}) failed (exit ${exec_status}): ${exec_err_text}"
        return 1
      fi
      error "kubectl exec into dataspoke-api (-n ${ns}) failed (exit ${exec_status}): ${exec_err_text}"
    fi
    if [[ -z "${output}" ]]; then
      status="000"
    else
      status="$(printf '%s\n' "${output}" | head -n1)"
    fi
    if [[ "${status}" != "000" ]]; then
      printf '%s\n' "${output}"
      return 0
    fi
    if (( attempt < 5 )); then
      sleep 3
    fi
  done
  if [[ -z "${output}" ]]; then
    printf '000\n'
  else
    printf '%s\n' "${output}"
  fi
}

# Static Python source for api_error_detail() below. Held in a variable and
# handed to `python3 -c` rather than piped in on stdin, because stdin is where
# the body being scrubbed arrives.
read -r -d '' _API_ERROR_DETAIL_PY <<'PYEOF' || true
import json, re, sys

raw = sys.stdin.read()
try:
    parsed = json.loads(raw)
except Exception:
    sys.stdout.write("<%d bytes of non-JSON body, withheld>" % len(raw))
    raise SystemExit(0)

_INPUT_VALUE = re.compile(r"input_value=.*?, input_type=", re.DOTALL)


def scrub(node):
    if isinstance(node, dict):
        return {k: scrub(v) for k, v in node.items() if k != "input"}
    if isinstance(node, list):
        return [scrub(v) for v in node]
    if isinstance(node, str):
        return _INPUT_VALUE.sub("input_value=<redacted>, input_type=", node)
    return node


sys.stdout.write(json.dumps(scrub(parsed)))
PYEOF

# api_error_detail
# Read a DataSpoke API error body on stdin and echo a printable summary of it
# with the rejected input stripped out. Callers that PATCH a credential-bearing
# payload print this instead of the raw body: the API's 422 envelope carries
# the offending value back in two shapes, and either one would otherwise put a
# PAT, a SASL password or an LLM key on the operator's terminal and into any
# CI log capturing stderr.
#   - a request-body rejection (src/api/main.py `_handle_request_validation`)
#     carries pydantic's per-error dicts under detail.errors, each with an
#     `input` key holding the value;
#   - a model rejection raised inside a handler is rendered with `str(exc)`,
#     whose pydantic v2 text embeds `input_value=...` in the message.
# Both are removed here. A body that is not JSON is reported by size only,
# since nothing then bounds what it might quote back.
api_error_detail() {
  python3 -c "${_API_ERROR_DETAIL_PY}"
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

# env_file_set_var <key> <value> [env_file]
# The single idempotent `.env` rewriter: replace every existing `<key>=` line
# with `<key>=<value>`, or append the assignment when the key is absent.
# Every writer of an operator env file goes through this one implementation —
# upsert_env_var below, and install.sh's _write_env_var / _sync_env_from_secret
# — so a value one writer round-trips safely cannot be corrupted by another.
# <env_file> defaults to $ENV_FILE.
#
# awk, not sed. `sed "s|^${key}=.*|${key}=${value}|"` re-reads the replacement
# text as part of the substitution: a value containing the `|` delimiter ends
# the replacement early and the remainder is parsed as sed flags, while `&`
# and `\1` expand to the matched text and to backreferences. All three
# characters are legal in a password, a DSN and a base64url key. awk's
# `print prefix value` emits the value uninterpreted.
#
# Neither the key nor the value reaches awk's argv — both are passed through
# the environment and read via ENVIRON, for two independent reasons. argv is
# world-readable through `ps auxww` and /proc/<pid>/cmdline for the life of
# the process, and this function is handed real credentials (the prod
# resolution path writes all eleven Secret keys through it); and `awk -v
# val=...` processes escape sequences inside its assignment, so a value
# carrying a literal backslash (`a\tb`) would be written out with a tab in
# it. A process's environment is readable only by its own uid.
#
# The rewrite lands in a mktemp file in the TARGET's own directory — mode
# 0600 by mktemp's contract, and same-directory so the `mv` is an atomic
# rename rather than a cross-device copy that would briefly expose a second
# file — and the result is chmod 600 after every write, because these files
# carry the Postgres and Redis passwords, the internal token, the LLM API key
# and the DataHub PAT.
#
# awk terminates every record it prints with a newline, so a file whose last
# line lacked one gains it here and an appended assignment can never be
# concatenated onto the preceding line.
#
# A value carrying a newline or a carriage return is REFUSED rather than
# written. These files are consumed by `set -a && source <file>`, so a second
# physical line is executed as a shell command by whoever sources it — with
# the operator's kubeconfig — while the variable silently keeps only the
# fragment before the break. Truncating instead would be just as wrong: the
# values arriving here are a DataHub PAT read out of a GraphQL response, a
# Langfuse key, and the eleven credentials read back out of a Kubernetes
# Secret, and a credential that cannot round-trip through this file is a
# defect at its source, not something to silently repair.
#
# Every OTHER shell-significant character is handled by SHELL-QUOTING the
# value on the way out, and this is load-bearing for the same reason the
# newline refusal is. The file is consumed with `source`, which subjects an
# unquoted assignment's right-hand side to command substitution, parameter
# expansion and metacharacter tokenization — so an unquoted
# `DATASPOKE_PROD_POSTGRES_PASSWORD=p$(id)w` written here EXECUTES `id` on
# the workstation of whoever sources the file next, and the variable ends up
# holding `pw`. That value is not hypothetical on the prod path: the eleven
# credentials are read verbatim out of a live Kubernetes Secret
# (adopt_credential_from_cluster) before being written here, so anyone able
# to write that Secret would otherwise be writing shell into an operator's
# env file. Non-adversarially the same gap silently corrupts any legitimate
# password containing `$`, a backtick, a space or `#`, and the corrupted
# readback is what a later run would push into a new Secret.
#
# Quoting is applied only when the value needs it — anything outside
# $_ENV_FILE_UNQUOTED_SAFE is wrapped in single quotes with embedded
# apostrophes escaped as '\'' — so hex, base64url, URLs, host:port pairs and
# broker lists (every value the dev peripherals write) stay byte-identical to
# what earlier releases produced and no reader of an existing `.env.dev`
# sees a changed line. report_credential_secret_drift's own reader reverses
# exactly this encoding, so the file→Secret comparison keeps comparing values
# rather than quoting.

# The alphabet an assignment's right-hand side can carry unquoted with the
# shell doing nothing to it: no whitespace, no `$`, backtick, backslash,
# quote, `#`, `~`, `!` or any metacharacter that ends the word (`;&|<>()`).
_ENV_FILE_UNQUOTED_SAFE='^[A-Za-z0-9_.:/@,%^+=-]+$'

env_file_set_var() {
  local key="$1" value="$2"
  local file="${3:-${ENV_FILE:-}}"
  [[ -n "${file}" ]] || error "env_file_set_var: no env file given and ENV_FILE is unset (key '${key}')."

  if [[ "${value}" == *$'\n'* || "${value}" == *$'\r'* ]]; then
    error "env_file_set_var: refusing to write ${key} into '${file}' — the value contains a newline
or carriage return. This file is consumed with \`source\`, which would run everything after the
break as a shell command and leave ${key} holding only the fragment before it. Find where the
value picked up the line break (a \`kubectl create secret --from-file\` of a text editor's output
and an external-secrets sync are the usual sources) and strip it there."
  fi

  # Shell-quote what needs it — see the docstring above for why an unquoted
  # value is executable by whoever `source`s this file. An empty value stays
  # `KEY=`, which is what every template's blank line already looks like and
  # what the pre-flight reads as "resolve this for me"; `KEY=''` would be the
  # same value spelled in a way an operator has to decode.
  local serialised="${value}"
  if [[ -n "${value}" && ! "${value}" =~ $_ENV_FILE_UNQUOTED_SAFE ]]; then
    local _sq="'" _sq_escaped="'\\''"
    serialised="${_sq}${value//${_sq}/${_sq_escaped}}${_sq}"
  fi

  # Created under a 077 umask rather than by a plain `> "${file}"` redirect:
  # a credential appended a moment later must never sit in a file the ambient
  # umask made group- or world-readable, however briefly.
  if [[ ! -e "${file}" ]]; then
    ( umask 077; : > "${file}" )
  fi

  # Serialise the read-modify-write. install.sh runs the dev-peripheral
  # scripts in parallel (_run_bg) and datahub.sh and langfuse.sh both write
  # this same file through upsert_env_var, so two interleaved
  # read-rewrite-rename cycles silently drop whichever key landed first. This
  # function is the only writer left, so one lock here covers every caller.
  # mkdir is the atomic test-and-set that exists everywhere — flock is absent
  # on macOS, which is where this runs. The wait is bounded and then proceeds
  # anyway: a lock directory left behind by a killed writer must never wedge
  # an install, and losing a concurrent write is a strictly smaller failure
  # than refusing to write at all.
  local lock_dir="${file}.lock"
  local lock_held="0" lock_waited=0
  while true; do
    if mkdir "${lock_dir}" 2>/dev/null; then
      lock_held="1"
      break
    fi
    if (( lock_waited >= 100 )); then
      warn "env_file_set_var: '${lock_dir}' has been held for 10s — writing ${key} without it (stale lock from a killed writer)."
      break
    fi
    sleep 0.1
    lock_waited=$(( lock_waited + 1 ))
  done

  local file_dir="${file%/*}"
  [[ "${file_dir}" == "${file}" ]] && file_dir="."
  local tmp_file
  tmp_file="$(mktemp "${file_dir}/.env-write.XXXXXX")"

  local rewrite_status=0
  if ! _ENV_FILE_SET_KEY="${key}" _ENV_FILE_SET_VALUE="${serialised}" awk '
      BEGIN {
        key = ENVIRON["_ENV_FILE_SET_KEY"]
        value = ENVIRON["_ENV_FILE_SET_VALUE"]
        prefix = key "="
      }
      index($0, prefix) == 1 { print prefix value; found = 1; next }
      { print }
      END { if (!found) print prefix value }
    ' "${file}" > "${tmp_file}"; then
    rewrite_status=1
  fi

  if (( rewrite_status == 0 )); then
    mv "${tmp_file}" "${file}"
    # Harden permissions after every write — see the docstring above for what
    # these files hold.
    chmod 600 "${file}" 2>/dev/null || true
  else
    rm -f "${tmp_file}"
  fi

  # Released before the error below, so a failed rewrite does not leave the
  # next writer waiting out the full stale-lock timeout.
  if [[ "${lock_held}" == "1" ]]; then
    rmdir "${lock_dir}" 2>/dev/null || true
  fi

  if (( rewrite_status != 0 )); then
    error "Could not rewrite '${file}' to set ${key}."
  fi
}

# upsert_env_var <key> <value> [env_file]
# .env upsert for the dev-peripheral scripts, which rely on the default env
# file being discovered rather than passed: walk up from the SOURCING script's
# directory to helm-charts/.env.dev. Resolving that default here rather than
# inside env_file_set_var is load-bearing — ${BASH_SOURCE[1]} is this
# function's own caller, and one more frame of indirection would resolve it to
# lib/helpers.sh instead. The write itself is env_file_set_var's above.
upsert_env_var() {
  local key="$1" value="$2"
  local file="${3:-${ENV_FILE:-$(cd "$(dirname "${BASH_SOURCE[1]}")" && pwd)/../../.env.dev}}"
  env_file_set_var "${key}" "${value}" "${file}"
}

# seed_profile <env_file>
# Echo the deployment profile an operator env file names — `prod`, `dev`,
# `ambiguous`, or `none` — so every post-install seed picks the same profile
# from the same file and `ENV_FILE=` alone selects it.
#
# The verdict is read from the file's own assignments, not from the process
# environment: a `DATASPOKE_PROD_*` name declared in it means prod, a
# `DATASPOKE_DEV_*` name means dev, both together are `ambiguous` (the callers
# stop there — guessing decides whether a deployment runs on stub services and
# whether it points at the dev peripheral topology), and neither is `none`.
# Reading the environment instead would let variables exported by the
# operator's shell — `set -a && source helm-charts/.env.dev` is the project's
# canonical integration-test setup — decide the profile of an unrelated file.
#
# Presence of the name decides, not its value. An env file that declares the
# prod block with every value still blank is a prod file whose block is not
# filled in yet; the caller reports that from inside its prod branch, where it
# can name what stops working. Commented-out lines are ignored, so a template's
# `# DATASPOKE_DEV_...=` examples never claim a profile.
seed_profile() {
  local file="$1"
  awk '
    /^[[:space:]]*#/ { next }
    {
      line = $0
      sub(/^[[:space:]]*/, "", line)
      sub(/^export[[:space:]]+/, "", line)
      if (line !~ /^[A-Za-z_][A-Za-z0-9_]*=/) next
      name = substr(line, 1, index(line, "=") - 1)
      if (name ~ /^DATASPOKE_PROD_/) prod = 1
      else if (name ~ /^DATASPOKE_DEV_/) dev = 1
    }
    END {
      if (prod && dev) print "ambiguous"
      else if (prod) print "prod"
      else if (dev) print "dev"
      else print "none"
    }
  ' "$file"
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

# ---------------------------------------------------------------------------
# Airflow Fernet-key Secret discovery (both profiles)
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
  # recovery text in verify_credential_secret's Fernet error — the same
  # grammar install.sh checks SECRET_TO_CHECK against, applied here to a
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

# ---------------------------------------------------------------------------
# Prod gates — shared by bin/install.sh and bin/install-prod-preflight.sh
# ---------------------------------------------------------------------------
# Every function below is a predicate over operator input: the env file, the
# --values overlay, and the cluster. They live here rather than inside
# install.sh so a standalone pre-flight can apply the IDENTICAL checks before
# any release is mutated — spec/feature/HELM_CHART.md §Prod operator workflow
# rests the two-command split on exactly that invariant ("a pass here means
# install.sh's own pre-flight passes"). A gate that drifts back into
# install.sh silently breaks it, because the pre-flight would then approve
# input the install still rejects.

# DATASPOKE_AIRFLOW_USER's allowlist. This project's house convention for
# every OTHER interpolated operator string (SECRET_TO_CHECK, namespaces,
# StorageClass names, --image-tag) is already a positive allowlist rather
# than a denylist of specific bad characters — this username needs the same
# treatment for a sharper reason: _build_airflow_extra_env_file composes it
# into airflow.extraEnv, which the vendored Airflow chart renders through Go
# template `tpl` (custom_airflow_environment in
# charts/airflow-1.20.0.tgz's _helpers.yaml, included by every Airflow
# component's env block), making the username a template-injection sink, not
# merely a config string. A denylist of literal ',' and ':' does not close
# that sink: `{{ printf "%c" 58 }}` / `{{ printf "%c" 44 }}` synthesize the
# denylisted characters inside the template evaluator, a YAML double-quoted
# escape (e.g. "a\x3aADMIN") reaches ':' without the source string ever
# containing the literal character, `{{ lookup ... }}` is live under `helm
# upgrade` and can render an arbitrary cluster Secret the installer's
# kubeconfig can read into the manifests (and the release's stored history),
# and a routine trailing newline (common with --from-file / external-secrets
# / a Vault injector) folds to a space in the rendered env var while the init
# container's own read of the same Secret key keeps the raw value — splitting
# what the two writers agree the username is. The allowlist closes all of
# these by construction instead of enumerating each one.
AIRFLOW_SIMPLE_AUTH_USERNAME_REGEX='^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'

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

# overlay_string_value <overlay_file> <dotted.path>
# Reads one scalar out of a values overlay and prints it — e.g.
# `overlay_string_value overlay.yaml auth.googleClientId`. Prints an empty
# line when the overlay argument is empty, the file does not exist, or the key
# is absent, so a caller comparing an overlay value against an operator input
# needs no special case for "no overlay was passed".
#
# Deliberately the same python3+PyYAML dig() walk as
# _resolve_existing_secret_name above rather than a second parsing mechanism:
# an overlay that is invalid YAML, or whose intermediate node is present but
# is not a mapping, has to fail identically for every reader of it — a reader
# that quietly coerced such a node to {} would resolve the path to "unset" and
# let its caller fall through to a default. A non-string scalar prints as
# Python's str() of it; both sides of any comparison here are operator-typed
# text.
overlay_string_value() {
  local overlay_file="${1:-}" dotted_path="$2"
  if [[ -z "${overlay_file}" || ! -f "${overlay_file}" ]]; then
    echo ""
    return 0
  fi
  if ! python3 -c "import yaml" 2>/dev/null; then
    error "python3 with PyYAML is required to read '${dotted_path}' from the operator overlay. Install: pip install pyyaml"
  fi
  python3 - "${overlay_file}" "${dotted_path}" <<'PYEOF'
import sys, yaml

PATH = sys.argv[1]
DOTTED = sys.argv[2]


def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def dig(node, *keys):
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

value = dig(data, *DOTTED.split("."))
print("" if value is None else str(value))
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

# _csidriver_state <name>
# Echoes exactly one of "found" / "absent" / "forbidden" for the cluster-
# scoped CSIDriver object <name>, used by the StorageClass pre-flight below.
# A plain exit-code check (`kubectl get csidriver ... >/dev/null 2>&1`)
# cannot tell a genuine NotFound apart from an RBAC denial — both are
# non-zero exits with nothing on stdout — and collapsing them would have the
# pre-flight tell an operator to install a driver that is already there,
# purely because the installer's own kubectl identity lacks read access to
# CSIDriver objects (a cluster-scoped resource, so `csidrivers.storage.k8s.io`
# get/list is a real, separate RBAC grant an operator may not have given the
# installer identity — see helm-charts/prod-prereq/). `--` terminates flag
# parsing ahead of <name>: every caller already validates it against a
# DNS-subdomain-with-optional-path grammar before passing it here, but that
# grammar still permits a leading character kubectl's own parser could read
# as a flag.
_csidriver_state() {
  local name="$1" stderr_out
  if stderr_out="$(kubectl get csidriver -- "${name}" 2>&1 >/dev/null)"; then
    echo "found"
  elif [[ "$stderr_out" == *"Forbidden"* || "$stderr_out" == *"forbidden"* ]]; then
    echo "forbidden"
  else
    echo "absent"
  fi
}

# assert_pinned_storage_classes [<overlay_file>]
# Verify every StorageClass the operator's overlay pins exists AND, where
# it names an out-of-tree CSI provisioner, that the matching CSIDriver is
# actually registered (fail fast).
#
# A namespace-scoped Helm release cannot own a cluster-scoped StorageClass —
# see helm-charts/prod-prereq/ for the cluster-admin prerequisite this
# check assumes was applied first. Resolved from fifteen overlay keys across
# the postgresql/redis Bitnami subcharts and the Airflow subchart's
# persistence blocks — see _resolve_storage_classes's docstring above for
# the full list, the `storageClass` (Bitnami) vs `storageClassName`
# (Airflow) spelling difference, and which of the two honour a literal
# `-`. An overlay that pins none of them skips this check cleanly — the
# cluster default StorageClass then applies.
#
# Failing here, rather than later, is the point: a missing class, or a
# provisioner with no driver behind it, otherwise leaves the PVC Pending,
# so PostgreSQL/Redis/Airflow never start, the API's wait-for-postgres init
# container loops, and the install dies on a rollout timeout whose symptom
# names the stalled workload rather than storage. Recovery then needs the
# stuck PVCs deleted by hand, because storageClassName is immutable once
# bound.
#
# Resolved into a variable FIRST, then iterated — not
# `done < <(_resolve_storage_classes ...)`. A process substitution's exit
# status is invisible to `set -e`: the helper's own `error()` (or an
# uncaught parse failure) would terminate only the subshell, the
# while-loop would read zero lines, and this fail-fast gate would silently
# no-op instead of aborting. Assigning via `$( ... )` makes a non-zero
# status trip `set -e` as intended; `|| error ...` gives the parse-failure
# path the same [ERROR] voice as every other pre-flight abort here.
#
# An empty or missing <overlay_file> returns cleanly, so the prod install and
# the standalone pre-flight share one call shape whether or not an overlay
# was passed.
assert_pinned_storage_classes() {
  local overlay_file="${1:-}"
  local pinned_storage_classes sc_class sc_name sc_provisioner _csi_migrated_name
  if [[ -z "${overlay_file}" || ! -f "${overlay_file}" ]]; then
    return 0
  fi
  pinned_storage_classes="$(_resolve_storage_classes "${overlay_file}")" \
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

    # One round trip covers both existence and provisioner: `-o jsonpath`
    # against a StorageClass that does not exist exits non-zero with no
    # output, exactly like the separate `kubectl get storageclass ...`
    # existence probe this replaces — so a single failed read here still
    # reports "not found", and a class pinned under two different overlay
    # keys (see _resolve_storage_classes's (tag, name) de-duplication
    # docstring) no longer pays for two round trips to learn the same
    # thing twice. `$( ... )` assignment, not a bare command inside
    # `[[ ... ]]`: `set -e` sees a command-substitution assignment's exit
    # status directly, so `|| error` reaches the standard `[ERROR]` voice
    # on a genuine failure. A StorageClass whose `.provisioner` field is
    # present but empty — a malformed object, not a missing one — exits 0
    # with empty output here; that case is caught by the grammar check
    # just below, not by this line.
    sc_provisioner="$(kubectl get storageclass "${sc_name}" -o jsonpath='{.provisioner}' 2>/dev/null)" \
      || error "StorageClass '${sc_name}' pinned in your --values overlay was not found in the cluster. Apply the cluster-scoped prerequisites first — see helm-charts/prod-prereq/."
    info "StorageClass '${sc_name}' is present (provisioner: ${sc_provisioner})."

    # Validate the provisioner's own grammar before it reaches any string
    # comparison or a `kubectl get csidriver` argument below. A
    # StorageClass provisioner is not a bare object name: it is either a
    # DNS-subdomain CSI driver name (`ebs.csi.aws.com`) or a
    # `<vendor-domain>/<name>` external non-CSI provisioner
    # (`rancher.io/local-path`, `kubernetes.io/no-provisioner`) —
    # rejecting the slash form outright would misdiagnose a valid,
    # supported cluster (k3s/RKE's `rancher.io/local-path`, OpenEBS, the
    # NFS subdir provisioner) as an invalid provisioner name. The anchored
    # `^...$` still rejects a value beginning with `-`, which would
    # otherwise be parsed as a flag by a subsequent kubectl invocation.
    if ! [[ "$sc_provisioner" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?(/[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?)?$ ]]; then
      error "StorageClass '${sc_name}' has provisioner '${sc_provisioner}', which is not a valid Kubernetes provisioner name."
    fi

    # CSI migration lets a StorageClass keep declaring one of these
    # compiled-in `kubernetes.io/*` names while the operator has installed
    # the matching CSI driver out-of-band and disabled the in-tree
    # plugin — EKS's own default `gp2` StorageClass still reads
    # `kubernetes.io/aws-ebs` while provisioning is actually delegated to
    # the separately-installed `ebs.csi.aws.com` addon. Exempting the
    # whole `kubernetes.io/*` family from the CSIDriver check would skip
    # the single most likely real instance of the failure this block
    # exists to catch. A cluster genuinely still on the in-tree plugin is
    # equally legitimate, though, so a driver absent here is reported and
    # does not abort the install — only a name this installer cannot map
    # at all (the `else` branch below) hard-gates.
    case "$sc_provisioner" in
      kubernetes.io/aws-ebs)    _csi_migrated_name="ebs.csi.aws.com" ;;
      kubernetes.io/gce-pd)     _csi_migrated_name="pd.csi.storage.gke.io" ;;
      kubernetes.io/azure-disk) _csi_migrated_name="disk.csi.azure.com" ;;
      *)                        _csi_migrated_name="" ;;
    esac

    if [[ -n "${_csi_migrated_name}" ]]; then
      case "$(_csidriver_state "${_csi_migrated_name}")" in
        found)
          info "StorageClass '${sc_name}' uses the CSI-migrated provisioner '${sc_provisioner}'; CSIDriver '${_csi_migrated_name}' is registered."
          ;;
        forbidden)
          warn "Could not confirm CSIDriver '${_csi_migrated_name}' for StorageClass '${sc_name}' (provisioner '${sc_provisioner}') — the installer's kubectl identity is denied read access to cluster-scoped CSIDriver objects. Verify manually: kubectl get csidriver ${_csi_migrated_name}"
          ;;
        absent)
          warn "StorageClass '${sc_name}' declares the CSI-migrated provisioner '${sc_provisioner}', but no '${_csi_migrated_name}' CSIDriver is registered — this cluster may genuinely still run the in-tree plugin. If it does not, the PVC will stick Pending; install the CSI driver addon (see helm-charts/prod-prereq/) before this release."
          ;;
      esac
    elif [[ "$sc_provisioner" == kubernetes.io/* ]]; then
      # Every other compiled-in provisioner, including
      # kubernetes.io/no-provisioner (pre-provisioned/static volumes),
      # registers no CSIDriver object at all — nothing to check.
      info "StorageClass '${sc_name}' uses the in-tree provisioner '${sc_provisioner}'; no CSIDriver required."
    elif [[ "$sc_provisioner" == */* ]]; then
      # A slash-bearing provisioner outside the kubernetes.io/ namespace is
      # an external non-CSI provisioner — a controller that watches
      # PersistentVolumeClaims directly rather than a CSI driver — and no
      # CSIDriver object will ever exist for it. Requiring one here would
      # reject k3s/RKE's rancher.io/local-path, openebs.io/local, and the
      # NFS subdir provisioner outright.
      warn "StorageClass '${sc_name}' names provisioner '${sc_provisioner}', an external (non-CSI) provisioner — skipping the CSIDriver check (none will ever exist for it). Confirm its controller is actually running in-cluster; a StorageClass object alone does not guarantee that."
    else
      # A bare DNS-subdomain name with no kubernetes.io/ prefix and no
      # vendor path is the shape of an out-of-tree CSI driver name
      # (ebs.csi.aws.com, pd.csi.storage.gke.io, ...) — the one
      # unambiguous case this gate can enforce as a hard failure.
      case "$(_csidriver_state "${sc_provisioner}")" in
        found)
          info "CSIDriver '${sc_provisioner}' is registered for StorageClass '${sc_name}'."
          ;;
        forbidden)
          error "Could not confirm CSIDriver '${sc_provisioner}' is registered for StorageClass '${sc_name}' — the installer's kubectl identity is denied read access to cluster-scoped CSIDriver objects (get on csidrivers.storage.k8s.io). Grant that read access (see helm-charts/prod-prereq/) or verify manually: kubectl get csidriver ${sc_provisioner}"
          ;;
        absent)
          error "StorageClass '${sc_name}' names CSI provisioner '${sc_provisioner}', but no matching CSIDriver is registered in the cluster. Install the CSI driver (its own Helm chart or manifest bundle, per the vendor) before this release — see helm-charts/prod-prereq/."
          ;;
      esac
    fi
  done <<< "${pinned_storage_classes}"
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

# _assert_no_airflow_simple_auth_overlay_conflict [<overlay_file>]
# Prod-only guard: Helm deep-merges MAPS but REPLACES LIST-typed values
# wholesale, never merges them. install.sh's own -f layer for the Airflow
# SimpleAuthManager passwords mechanism
# (_build_airflow_simple_auth_init_container_file) sets
# airflow.apiServer.{extraInitContainers,extraVolumes,extraVolumeMounts} —
# and sits ahead of the operator's --values overlay in VALUES_ARGS
# specifically so an overlay CAN extend those same fields for an unrelated
# reason (a sidecar, a debug volume). But an overlay that sets ANY of the
# three at all does not append to this release's list — it silently
# REPLACES it, deleting the passwords-file init container/volume/mount with
# no error from either `helm template` or `helm lint` (both still exit 0
# against the resulting pod template, which mounts a path the passwords-file
# env var still names but no container ever writes). Abort here instead of
# letting an overlay silently disable this issue's own fix — see
# helm-charts/README.md for the operator-facing workaround (re-declare the
# simple-auth-manager-passwords entry alongside whatever else the overlay
# adds to these lists).
_assert_no_airflow_simple_auth_overlay_conflict() {
  local overlay_file="${1:-}"
  if [[ -z "${overlay_file}" || ! -f "${overlay_file}" ]]; then
    return 0
  fi
  if ! python3 -c "import yaml" 2>/dev/null; then
    error "python3 with PyYAML is required to check the --values overlay for an Airflow apiServer list conflict. Install: pip install pyyaml"
  fi
  local offenders_file
  offenders_file="$(mktemp)"
  if ! python3 - "${overlay_file}" > "${offenders_file}" <<'PYEOF'
import sys, yaml

OVERLAY_FILE = sys.argv[1]


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


data = load_mapping(OVERLAY_FILE, "overlay file")
for field in ("extraInitContainers", "extraVolumes", "extraVolumeMounts"):
    if dig(data, "airflow", "apiServer", field) is not None:
        print(field)
PYEOF
  then
    rm -f "${offenders_file}"
    error "Could not parse the --values overlay for an Airflow apiServer list conflict (see above)."
  fi

  if [[ -s "${offenders_file}" ]]; then
    local offenders offender_list
    offenders="$(cat "${offenders_file}")"
    rm -f "${offenders_file}"
    offender_list="$(echo "${offenders}" | tr '\n' ' ' | sed 's/ *$//')"
    error "Your --values overlay sets airflow.apiServer.{${offender_list}}. Helm replaces LIST-typed
values wholesale rather than merging them, and install.sh's own -f layer for the Airflow
SimpleAuthManager passwords-file mechanism (the init container, its emptyDir, and its
volumeMount — see spec/feature/HELM_CHART.md §Airflow authentication) sets exactly these fields.
Your overlay would silently delete them, leaving the api-server pointed at a passwords file no
container ever writes, with no error from helm template/lint. Re-declare the
simple-auth-manager-passwords entry alongside whatever else you are adding to these lists (see
helm-charts/README.md's note on this hazard), or move your addition to a field this release does
not already use."
  fi
  rm -f "${offenders_file}"
}

# _resolve_effective_all_admins <chart_values_file> [<overlay_file>]
# Prints the effective airflow.config.core.simple_auth_manager_all_admins,
# NORMALIZED to the lowercase literal "true" or "false" — never the raw
# value — by mirroring Airflow's own boolean coercion
# (airflow/configuration.py: str(v).strip().lower() tested against a fixed
# spelling set) rather than a strict `== "True"` comparison. Airflow accepts
# t/true/1 and f/false/0 case-insensitively and whitespace-trimmed for EVERY
# boolean config key, so an overlay spelling it "true", "TRUE", "t", or "1"
# is honoured by Airflow identically to "True" — a caller comparing against
# the literal string "True" would fail-open on any of those spellings,
# silently skipping the anonymous-admin disclosure warning
# verify_credential_secret exists to print. Anything outside that
# spelling set is what Airflow's OWN parser raises AirflowConfigException
# over and crash-loops every component on, so this hard-errors on it too,
# before any Secret is touched, rather than letting the chart values pass a
# pre-flight that then deploys a release that cannot start.
#
# The chart default, overridden by the operator overlay's value at the same
# path when the overlay sets it at all. Same python3+PyYAML dig() pattern as
# _assert_no_internal_ingress_exposure / _resolve_existing_secret_name above.
# An overlay is free to set this back to a true-ish value, and
# verify_credential_secret must judge the merged result, not the
# chart default. Prints "" if the key is absent from both files (not
# expected in practice — the chart's own values.yaml always sets it — kept
# as a defensive fallback rather than a hard requirement on that fact).
_resolve_effective_all_admins() {
  local chart_values_file="$1" overlay_file="${2:-}"
  if ! python3 -c "import yaml" 2>/dev/null; then
    error "python3 with PyYAML is required to resolve the effective simple_auth_manager_all_admins setting. Install: pip install pyyaml"
  fi
  python3 - "${chart_values_file}" "${overlay_file}" <<'PYEOF'
import sys, yaml

CHART_FILE = sys.argv[1]
OVERLAY_FILE = sys.argv[2] if len(sys.argv) > 2 else ""

TRUE_VALUES = {"t", "true", "1"}
FALSE_VALUES = {"f", "false", "0"}


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
value = dig(chart_data, "airflow", "config", "core", "simple_auth_manager_all_admins")

if OVERLAY_FILE:
    overlay_data = load_mapping(OVERLAY_FILE, "overlay file")
    overlay_value = dig(overlay_data, "airflow", "config", "core", "simple_auth_manager_all_admins")
    if overlay_value is not None:
        value = overlay_value

if value is None:
    print("")
else:
    normalized = str(value).strip().lower()
    if normalized in TRUE_VALUES:
        print("true")
    elif normalized in FALSE_VALUES:
        print("false")
    else:
        fail(
            f"airflow.config.core.simple_auth_manager_all_admins resolved to {value!r}, which "
            "Airflow's own boolean parser does not accept (valid spellings: t/true/1 for True, "
            "f/false/0 for False — case-insensitive, whitespace-trimmed). Airflow raises "
            "AirflowConfigException and every component crash-loops on this value at startup — "
            "fix your --values overlay before re-running the install."
        )
PYEOF
}

# assert_k8s_name <what> <value>
# Grammar-check one Kubernetes object name against the RFC 1123 subdomain
# allowlist before it is spliced into a `kubectl` argv or a `helm --set`
# token. Reports through error_no_exit and returns 1, so a caller running
# inside `$( ... )` still stops (error()'s exit would kill only the subshell).
#
# Every prod gate below that takes a namespace or a Secret name asserts here,
# rather than trusting its caller. install.sh grammar-checks
# secrets.existingSecret at its own call site too, but that check protects
# install.sh's `--set` tokens; these functions are now shared with the
# standalone pre-flight, whose --namespace / --secret-name flags reach them
# directly. The invariant the extraction is sold on — a pass in the pre-flight
# means install.sh's pre-flight passes — only holds while both entry points
# reject the same names.
#
# A leading `-` is the concrete hazard: kubectl parses it as a flag, so
# `kubectl get secret -x -n ns` fails in a way that reads like "absent" on the
# adopt path, which resolves to generating a fresh credential over a live
# deployment. `,` and `=` split a `helm --set` token into extra assignments.
assert_k8s_name() {
  local what="$1" value="$2"
  if [[ "${value}" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]]; then
    return 0
  fi
  error_no_exit "${what} '${value}' is not a valid Kubernetes name — it must match
^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ (lowercase alphanumerics, '-' and '.', starting and ending on an
alphanumeric)."
  return 1
}

# assert_k8s_namespace <what> <value>
# Namespaces are DNS-1123 LABELS, a strictly narrower grammar than the
# subdomain assert_k8s_name allows: no dots, at most 63 characters. Kept
# separate rather than folded into assert_k8s_name because Secret names — the
# other thing that function guards — are genuine subdomains and may carry
# dots. Both entry points that can name a namespace assert here (install.sh's
# _validate_namespace_var over the env file, the standalone pre-flight over
# --namespace), so neither accepts a name the other would reject.
#
# Beyond what the API server itself rejects, every *_NAMESPACE value is
# interpolated into `kubectl apply -f -` YAML documents (metadata.name /
# metadata.namespace) throughout install.sh, where an unvalidated value could
# append an arbitrary extra manifest.
assert_k8s_namespace() {
  local what="$1" value="$2"
  if [[ ! "${value}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ || "${#value}" -gt 63 ]]; then
    error_no_exit "${what} '${value}' is not a valid Kubernetes namespace (DNS-1123 label: lowercase
alphanumeric and '-', starting and ending alphanumeric, max 63 characters)."
    return 1
  fi
}

# assert_secret_data_key <key>
# Grammar-check one Secret data key before it is interpolated into a
# `-o jsonpath={.data.<key>}` expression. Kubernetes already restricts data
# keys to this alphabet, so this rejects only a key this project's own code
# invented — but a `}` or a `[` here rewrites the JSONPath rather than
# selecting a field, and the resulting empty read is indistinguishable from an
# absent key on the adopt path.
assert_secret_data_key() {
  local key="$1"
  if [[ "${key}" =~ ^[A-Za-z0-9]([A-Za-z0-9_.-]*[A-Za-z0-9])?$ ]]; then
    return 0
  fi
  error_no_exit "Secret data key '${key}' is not a valid Kubernetes Secret key — it must match
^[A-Za-z0-9]([A-Za-z0-9_.-]*[A-Za-z0-9])?$."
  return 1
}

# assert_image_tag <tag>
# Grammar-check an image tag before it is interpolated into a `helm --set`
# token or an image reference. helm treats `,` as an assignment separator
# within one --set token, so an unvalidated tag injects an arbitrary values
# path (`v1,api.image.repository=evil/img`), and a newline desyncs the
# one-token-per-line heredoc streams install.sh reads with `while IFS= read
# -r`. Shared so the standalone pre-flight, which derives a tag and PRINTS the
# install command carrying it, rejects exactly what install.sh would.
assert_image_tag() {
  local tag="$1"
  if [[ ! "${tag}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
    error "Invalid image tag '${tag}'. Must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\$ (alphanumeric,
'.', '_', '-' only — no comma, no whitespace, no newline), because it is interpolated into
\`helm --set\` tokens where a comma starts another assignment and a newline becomes a standalone flag."
  fi
}

# assert_ingress_class_present <class>
# Prove the IngressClass the deployment binds to is registered in this
# cluster. Required in prod with no default: install.sh passes the class by
# `--set`, which outranks the className in the operator's overlay, so
# defaulting to `nginx` would silently republish the API, frontend and Airflow
# UI on whatever controller happens to carry that name — often another team's
# internet-facing one. Existence proves the class is real, not that it is the
# one intended.
assert_ingress_class_present() {
  local class="$1"
  if ! kubectl get ingressclass "${class}" >/dev/null 2>&1; then
    error "IngressClass '${class}' (DATASPOKE_KUBE_INGRESS_CLASS) not found in the cluster. Install a
controller, or name the class the operator's controller actually registers."
  fi
}

# assert_admin_password <value> <source_label>
# The two rules DATASPOKE_PROD_ADMIN_PASSWORD has to satisfy for the rotation
# in bin/post-install/seed-admin-user.sh to succeed. Both describe a rotation
# that CANNOT succeed, so both stop rather than warn: reaching the PATCH with
# either would leave the credential published in this repository live while
# reporting a step as done.
#
# The literal check runs first because `dataspoke` is nine characters and
# would otherwise be reported as too short, which is the less useful of the
# two answers. PATCH /api/v1/auth/me bounds the value at 10-128 (MePatchRequest
# in src/api/schemas/auth.py); the value itself is never printed, its length is.
# A blank value is not this function's business — the caller decides whether an
# unrotated admin is a warning (it is) or a stop (it is not).
assert_admin_password() {
  local value="$1" source_label="$2"
  [[ -z "${value}" ]] && return 0
  if [[ "${value}" == "dataspoke" ]]; then
    error "DATASPOKE_PROD_ADMIN_PASSWORD in ${source_label} is the literal 'dataspoke' — the very
credential published in this repository, and the one the rotation exists to replace. Rotating to it
would leave the account exactly as exposed as it is now. Choose another value."
  fi
  local len="${#value}"
  if (( len < 10 || len > 128 )); then
    error "DATASPOKE_PROD_ADMIN_PASSWORD in ${source_label} is ${len} characters. PATCH
/api/v1/auth/me bounds it at 10-128 (MePatchRequest in src/api/schemas/auth.py), so the rotation
would be rejected and the published default would stay live. See spec/feature/HELM_CHART.md
§Policies for the operator standard above that floor."
  fi
}

# assert_credential_value_contract <var_prefix> <where> <effective_all_admins>
# The part of the credentials-Secret content contract that judges VALUES
# rather than a Secret object: the Fernet key's shape, the dev JWT default,
# the Airflow username allowlist, the `admin` Airflow password, and the
# placeholder OAuth client secret. Each value is read from the shell variable
# `<var_prefix><SECRET_KEY>` — an indirection rather than eleven parameters,
# because bash 3.2 (this project's macOS interpreter) has no associative
# arrays to pass instead.
#
# Two callers, which is the point of the split. verify_credential_secret below
# fills the variables from a Secret that already exists in the cluster; the
# standalone pre-flight fills them from the values it has just resolved and
# calls this BEFORE `kubectl create secret`, so a credential this contract
# rejects is never materialised. Without that ordering a hex Fernet key or a
# `placeholder-` client secret would be written into the cluster and only then
# refused, after which every later run takes the "Secret is present" branch —
# which never rewrites — and the operator has to delete the Secret by hand.
#
# <where> names the thing being judged in every message ("Secret 'x' in 'ns'",
# or the resolved values about to become it). <effective_all_admins> is
# _resolve_effective_all_admins's verdict: the Airflow password rules are the
# only ones keyed on it.
#
# Reports through error_no_exit and returns 1 on the first defect, so a caller
# can add its own remediation line; no value is ever printed, since this runs
# against live production in the pre-flight's --verify-only mode and its
# output lands in an operator's terminal and in whatever CI log captures it.
assert_credential_value_contract() {
  local var_prefix="$1" where="$2" effective_all_admins="$3"

  local fernet_var="${var_prefix}DATASPOKE_AIRFLOW_FERNET_KEY"
  local fernet_val="${!fernet_var:-}"
  # `openssl rand -hex 32` — the shape every other high-entropy key here uses,
  # and the one the README's generation block sits next to — decodes to 48 raw
  # bytes, not the 32 Fernet requires. It passes pod startup and fails only the
  # first time Airflow encrypts or decrypts a connection or Variable, long
  # after the install reports success.
  if [[ ! "${fernet_val}" =~ ^[A-Za-z0-9_-]{43}=$ ]]; then
    error_no_exit "DATASPOKE_AIRFLOW_FERNET_KEY in ${where} is not shaped like a Fernet key (must be
URL-safe base64 of exactly 32 raw bytes: 43 base64 characters followed by '='). Generate one with:
  python3 -c \"import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())\"
— but only for a genuinely fresh install; an existing Airflow metadata DB's connections and
Variables are encrypted with the key it already uses and become permanently undecryptable under a
new one."
    return 1
  fi

  local jwt_var="${var_prefix}DATASPOKE_JWT_SECRET_KEY"
  if [[ "${!jwt_var:-}" == "changeme-dev-secret-do-not-use-in-prod" ]]; then
    error_no_exit "DATASPOKE_JWT_SECRET_KEY in ${where} is the dev default — operator must set a unique secret."
    return 1
  fi

  local user_var="${var_prefix}DATASPOKE_AIRFLOW_USER"
  local airflow_user="${!user_var:-}"
  if [[ "${airflow_user}" == "admin" ]]; then
    error_no_exit "DATASPOKE_AIRFLOW_USER in ${where} must not be 'admin' — rename to reduce brute-force exposure."
    return 1
  fi
  # Allowlist, not a denylist of ','/':' — see $AIRFLOW_SIMPLE_AUTH_USERNAME_REGEX's
  # own comment, at the top of this file, for why this username is a Helm
  # `tpl` injection sink and a denylist does not close it.
  #
  # The rejected value is described, never echoed. A byte length plus the
  # first rejecting character class is what an operator needs to find it: a
  # trailing newline and a leading space are both invisible in the value
  # itself, and the source is theirs to read.
  if [[ ! "${airflow_user}" =~ ${AIRFLOW_SIMPLE_AUTH_USERNAME_REGEX} ]]; then
    local user_len user_defect
    user_len="$(printf '%s' "${airflow_user}" | wc -c | tr -d '[:space:]')"
    if [[ -z "${airflow_user}" ]]; then
      user_defect="it is empty"
    elif [[ "${airflow_user}" == *$'\n'* ]]; then
      user_defect="it contains a newline"
    elif [[ "${airflow_user}" == *$'\r'* ]]; then
      user_defect="it contains a carriage return"
    elif [[ "${airflow_user}" == *[[:space:]]* ]]; then
      user_defect="it contains whitespace"
    elif [[ "${airflow_user}" == *[,:]* ]]; then
      user_defect="it contains ',' or ':'"
    elif [[ "${airflow_user}" == *['{}$']* ]]; then
      user_defect="it contains a template metacharacter ('{', '}' or '\$')"
    else
      user_defect="it contains a character outside [A-Za-z0-9._-], or starts or ends on a non-alphanumeric"
    fi
    error_no_exit "DATASPOKE_AIRFLOW_USER in ${where} (${user_len} bytes) does not match
${AIRFLOW_SIMPLE_AUTH_USERNAME_REGEX} — the same allowlist install.sh already applies to
secrets.existingSecret, namespaces, StorageClass names, and --image-tag. This username is composed
into airflow.extraEnv, which the vendored Airflow chart renders through Go template \`tpl\`
(custom_airflow_environment, included by every Airflow component's env block) — a denylist of
specific characters (',' / ':') is not sufficient in front of a template evaluator: Go template
escapes (e.g. {{ printf \"%c\" 58 }}), YAML string escapes, and a trailing newline all reach the
same mis-parse by different routes. The value is not printed here (it is half of a credential and
this runs against production) — what rejects it: ${user_defect}. Rename it to match the allowlist."
    return 1
  fi

  # DATASPOKE_AIRFLOW_PASSWORD's PRESENCE is required unconditionally by
  # verify_credential_secret's required_keys loop — the prod-only init
  # container's secretKeyRef carries no `optional: true`, so an absent key is
  # a kubelet CreateContainerConfigError rather than a graceful skip. Only the
  # "admin"-literal rejection and the anonymous-admin disclosure warning below
  # are keyed on the EFFECTIVE simple_auth_manager_all_admins, since an
  # overlay is free to set the chart's "False" default back to a true-ish value.
  local password_var="${var_prefix}DATASPOKE_AIRFLOW_PASSWORD"
  if [[ "${effective_all_admins}" == "true" ]]; then
    warn "airflow.config.core.simple_auth_manager_all_admins resolves to a true-ish value in the
effective chart values (chart default overridden by your --values overlay) —
DATASPOKE_AIRFLOW_{USER,PASSWORD} is NOT consulted at Airflow login. Anyone who can reach
airflow.<domain> is granted an Airflow ADMIN session with no credential at all
(SimpleAuthManager's GET /auth/token / /auth/token/login). The chart ships no source-range
restriction of its own (see spec/feature/HELM_CHART.md §Ingress & Network Policy) — restrict this
host at the network layer if that exposure is not acceptable.
See spec/feature/HELM_CHART.md §Airflow authentication."
  elif [[ "${!password_var:-}" == "admin" ]]; then
    error_no_exit "DATASPOKE_AIRFLOW_PASSWORD in ${where} must not be 'admin' — it gates every
Airflow login under the default airflow.config.core.simple_auth_manager_all_admins: \"False\". Set
a real password, or set that value to a true-ish value (t/true/1) in your --values overlay to
accept anonymous-admin Airflow access instead (see spec/feature/HELM_CHART.md §Airflow authentication)."
    return 1
  fi

  local oauth_var="${var_prefix}DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET"
  if [[ "${!oauth_var:-}" == placeholder-* ]]; then
    error_no_exit "DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET in ${where} is the dev placeholder — operator
must set a real Google OAuth client secret, issued by the Google Cloud Console alongside the client id."
    return 1
  fi
}

# verify_credential_secret <ns> <secret_name> <chart_values_file> [<overlay_file>] [<report_lengths>]
# Validates ALL 11 required keys are present, non-empty, and not equal to known
# insecure defaults. Also hard-errors if the Secret still carries
# DATASPOKE_POSTGRES_USER or DATASPOKE_POSTGRES_DB — both relocated to the app
# ConfigMap (config.postgres.{user,db}), never rejected in favor of an
# install.sh-driven repair here: install.sh never mutates an operator-owned
# Secret in prod, so a lingering key is surfaced as a pre-flight failure with
# a copy-pasteable removal command instead. Prod profile only.
#
# DATASPOKE_AIRFLOW_PASSWORD's PRESENCE stays in the blanket required_keys
# loop below, unconditionally — the prod-only init container
# (_build_airflow_simple_auth_init_container_file) renders regardless of
# simple_auth_manager_all_admins and its secretKeyRef carries no
# `optional: true`, so an absent key is a kubelet
# CreateContainerConfigError, not a graceful skip. Requiring its presence here
# aborts in Phase 1, before anything is mutated. install.sh's Phase 4
# `rollout status` wait on the Airflow api-server catches the same broken
# Secret, but only after the release has already been upgraded — the cheap
# failure belongs here.
#
# Presence, the two relocated keys and the key report are this function's own —
# they are questions about a Secret OBJECT. Every rule that judges a VALUE is
# assert_credential_value_contract's above, called once at the end with the
# read-back values, so the standalone pre-flight applies the identical rules to
# the values it resolved before creating a Secret out of them. Among those,
# only the "admin"-literal rejection and the anonymous-admin disclosure warning
# are keyed on the EFFECTIVE
# airflow.config.core.simple_auth_manager_all_admins (<chart_values_file>
# merged with <overlay_file> via _resolve_effective_all_admins, since an
# overlay may set it back to a true-ish value).
#
# <report_lengths> ("1" to enable, default "0") additionally prints one line
# per required key: the key name, a set/blank verdict, and the value's BYTE
# length — never the value itself. That report is what makes the standalone
# pre-flight's `--verify-only` usable as an audit against a live prod
# deployment, whose output goes to an operator's terminal and to whatever CI
# log captures it.
verify_credential_secret() {
  local ns="$1"
  local secret_name="$2"
  local chart_values_file="$3"
  local overlay_file="${4:-}"
  local report_lengths="${5:-0}"

  assert_k8s_name "namespace" "${ns}" || return 1
  assert_k8s_name "credentials Secret name" "${secret_name}" || return 1

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

  # Key report (<report_lengths> = "1"), emitted BEFORE the rejections below
  # for two reasons: an operator auditing a deployment sees every key's shape
  # even on a run that aborts on the first bad one, and a key that exists but
  # holds an empty value is named here as `blank` rather than only surfacing
  # as an abort message. Byte length, not the value — see the docstring.
  # The 'X' sentinel preserves a trailing newline the same way the
  # DATASPOKE_AIRFLOW_USER read further down does: `$( ... )` strips trailing
  # newlines, and a stray one arriving from --from-file, an external-secrets
  # sync or a Vault injector is exactly the defect a byte count exposes and a
  # visual inspection does not.
  if [[ "${report_lengths}" == "1" ]]; then
    info "Credential Secret '${secret_name}' in '${ns}' — key report (byte lengths only, no values):"
    local report_key report_val report_len report_verdict
    for report_key in "${required_keys[@]}"; do
      report_val="$(kubectl get secret "${secret_name}" -n "${ns}" \
        -o jsonpath="{.data.${report_key}}" 2>/dev/null | base64 --decode 2>/dev/null; printf 'X')"
      report_val="${report_val%X}"
      report_len="$(printf '%s' "${report_val}" | wc -c | tr -d '[:space:]')"
      if [[ -n "${report_val}" ]]; then
        report_verdict="set"
      else
        report_verdict="blank"
      fi
      info "$(printf '  %-40s %-5s %5s bytes' "${report_key}" "${report_verdict}" "${report_len}")"
    done
  fi

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

  # The value-level rules are assert_credential_value_contract's, above — one
  # implementation, evaluated here against a Secret that exists and evaluated
  # by the standalone pre-flight against the values it is about to create one
  # from. Reading each judged key into `_CREDSECRET_<KEY>` is how those values
  # reach it without passing five parameters through bash 3.2, which has no
  # associative arrays.
  #
  # The 'X' sentinel preserves a trailing newline: `$(...)` eats trailing
  # newlines, so without it a username carrying one would reach the allowlist
  # as a clean value and pass. The init container reads the same key through a
  # secretKeyRef, which preserves every byte — so this check must see the raw
  # value or the two disagree. See
  # _build_airflow_simple_auth_init_container_file in install.sh.
  local judged_key judged_val
  for judged_key in DATASPOKE_AIRFLOW_FERNET_KEY DATASPOKE_JWT_SECRET_KEY \
                    DATASPOKE_AIRFLOW_USER DATASPOKE_AIRFLOW_PASSWORD \
                    DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET; do
    judged_val="$(kubectl get secret "${secret_name}" -n "${ns}" \
      -o jsonpath="{.data.${judged_key}}" 2>/dev/null | base64 --decode 2>/dev/null; printf 'X')"
    printf -v "_CREDSECRET_${judged_key}" '%s' "${judged_val%X}"
  done

  # The chart's own "False" default merged with the operator overlay, not the
  # default alone: an overlay is free to set it back to a true-ish value, and
  # the Airflow password rules are the only ones keyed on it. See
  # spec/feature/HELM_CHART.md §Airflow authentication.
  local effective_all_admins
  effective_all_admins="$(_resolve_effective_all_admins "${chart_values_file}" "${overlay_file}")"

  assert_credential_value_contract "_CREDSECRET_" "Secret '${secret_name}' in '${ns}'" \
    "${effective_all_admins}" || return 1
}

# prod_credential_key_map
# Prints the eleven `DATASPOKE_PROD_<X>\tDATASPOKE_<X>` pairs, one per line:
# the operator's env-file input on the left, the credentials-Secret key it
# supplies on the right. Every path that touches those eleven — the
# pre-flight's populate, its drift report, and verify_credential_secret's own
# required_keys list — reads this one table, which is what buys the invariant
# that a key cannot be populated without also being checked.
#
# Scoped to exactly these eleven suffixes, not to the DATASPOKE_PROD_ prefix.
# DATASPOKE_PROD_PERIPHERAL_*, DATASPOKE_PROD_LLM_* and
# DATASPOKE_PROD_ADMIN_PASSWORD are applied post-install through the admin API
# and have no Secret key at all; a blanket prefix rule would synthesise Secret
# keys the chart never mounts (spec/feature/HELM_CHART.md §Tier 5).
prod_credential_key_map() {
  local suffix
  for suffix in \
    POSTGRES_PASSWORD \
    REDIS_PASSWORD \
    AIRFLOW_USER \
    AIRFLOW_PASSWORD \
    AIRFLOW_WEBSERVER_SECRET_KEY \
    AIRFLOW_JWT_SECRET \
    AIRFLOW_FERNET_KEY \
    INTERNAL_TOKEN \
    JWT_SECRET_KEY \
    OAUTH_STATE_SECRET \
    GOOGLE_OAUTH_CLIENT_SECRET; do
    printf 'DATASPOKE_PROD_%s\tDATASPOKE_%s\n' "${suffix}" "${suffix}"
  done
}

# generate_credential_value <secret_key_name>
# Prints a freshly generated value for one credentials-Secret key, or returns
# non-zero when this installer must not invent one. Takes the SECRET key name
# (DATASPOKE_<X>), the right-hand column of prod_credential_key_map above.
#
# The shapes match what dev's own _ensure_dataspoke_secrets (bin/install.sh)
# generates, so the two profiles produce interchangeable Secrets rather than
# two independently drifting generation tables:
#   - DATASPOKE_AIRFLOW_FERNET_KEY is URL-safe base64 of exactly 32 raw bytes.
#     `openssl rand -hex 32` is the wrong shape here — it decodes to 48 raw
#     bytes, which passes pod startup and fails only the first time Airflow
#     encrypts or decrypts a connection or Variable. verify_credential_secret
#     rejects that shape for the same reason.
#   - The two Airflow signing keys are `-hex 16`, the remaining six
#     high-entropy keys `-hex 32`.
#   - DATASPOKE_AIRFLOW_USER carries a random suffix rather than the bare
#     `dataspoke-admin`, because in prod it is a login name reachable from
#     whatever network the operator's ingress controller sits on, and it must
#     stay inside $AIRFLOW_SIMPLE_AUTH_USERNAME_REGEX (hex does).
#
# DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET has NO generator: it is one half of a
# pair issued by Google, and a generated value would be a well-formed secret
# that authenticates nothing — the failure would surface at the first OAuth
# callback, long after the install reported success. It fails here instead,
# via error_no_exit so the caller decides when to stop.
generate_credential_value() {
  local secret_key="$1"
  case "${secret_key}" in
    DATASPOKE_AIRFLOW_USER)
      echo "dataspoke-admin-$(openssl rand -hex 4)"
      ;;
    DATASPOKE_AIRFLOW_FERNET_KEY)
      python3 -c 'import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
      ;;
    DATASPOKE_AIRFLOW_WEBSERVER_SECRET_KEY|DATASPOKE_AIRFLOW_JWT_SECRET)
      openssl rand -hex 16
      ;;
    DATASPOKE_POSTGRES_PASSWORD|DATASPOKE_REDIS_PASSWORD|DATASPOKE_AIRFLOW_PASSWORD|\
    DATASPOKE_INTERNAL_TOKEN|DATASPOKE_JWT_SECRET_KEY|DATASPOKE_OAUTH_STATE_SECRET)
      openssl rand -hex 32
      ;;
    DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET)
      error_no_exit "DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET cannot be generated — it is issued by Google alongside the client ID. Set DATASPOKE_PROD_GOOGLE_OAUTH_CLIENT_SECRET in the env file, or create the OAuth client first."
      ;;
    *)
      error_no_exit "generate_credential_value: no generator for '${secret_key}' — it is not one of the eleven credentials-Secret keys (see prod_credential_key_map)."
      ;;
  esac
}

# secret_presence <ns> <secret_name>
# Echoes `present` or `absent`, and returns 1 when the read did not answer the
# question at all — RBAC denial, an expired credential, an unreachable API
# server, a kubeconfig context pointing at another cluster.
#
# The distinction is the whole point. `kubectl get secret ... >/dev/null 2>&1`
# reports every one of those as a non-zero status indistinguishable from
# NotFound, and both callers below act on "absent": one generates a fresh
# credential, the other declares there is nothing to compare. Against a
# namespace whose Postgres PVC was retained, generating a new Fernet key on a
# denied read makes every encrypted Airflow connection and Variable
# permanently undecryptable. Only NotFound means what the fall-through
# assumes.
#
# Callers read this through `$( ... )`, so it reports with error_no_exit and
# carries the stop in its status — error()'s exit would end the subshell only.
secret_presence() {
  local ns="$1" secret_name="$2"
  local probe_err probe_status=0
  probe_err="$(kubectl get secret "${secret_name}" -n "${ns}" -o name 2>&1 >/dev/null)" || probe_status=$?
  if (( probe_status == 0 )); then
    echo "present"
    return 0
  fi
  if [[ "${probe_err}" == *NotFound* || "${probe_err}" == *"not found"* ]]; then
    echo "absent"
    return 0
  fi
  error_no_exit "Cannot read Secret '${secret_name}' in namespace '${ns}' — kubectl said:
  ${probe_err}
This is NOT the same as the Secret being absent, so it must not be treated as one: check the
kubeconfig context (kubectl config current-context) and that this identity holds get on secrets
in '${ns}'."
  return 1
}

# adopt_credential_from_cluster <ns> <secret_name> <secret_key>
# Prints the value <secret_name> currently holds for <secret_key>. Three
# outcomes, distinguished by exit STATUS rather than by an empty result:
#   value on stdout, status 0 — the key is present;
#   nothing,         status 0 — the Secret or the key genuinely does not exist;
#   nothing,         status 1 — the read did not answer the question.
#
# This is the middle step of the prod resolution order (operator's value →
# adopt → generate, spec/feature/HELM_CHART.md §Tier 5). Adoption is what
# stops a re-install contradicting a running deployment, and it is what makes
# DATASPOKE_AIRFLOW_FERNET_KEY recoverable rather than regenerated against a
# retained Postgres PVC whose Airflow connections and Variables it decrypts.
#
# Which is exactly why "absent" and "could not read" must not collapse into
# one empty string. `get secrets` denied by RBAC, an expired credential, an
# API server that is unreachable, and a kubeconfig context that landed on the
# wrong cluster would all read as "no Secret here" — and the caller's next
# step after "absent" is to GENERATE. For the Fernet key against a namespace
# whose Postgres PVC was retained, that is the permanent, irreversible loss of
# every encrypted Airflow connection and Variable this function exists to
# prevent. A NotFound is the only failure that means what the fall-through
# assumes; everything else stops the caller.
#
# The status, not error(), carries the stop: every caller reads this function
# through `$( ... )`, where error()'s exit would kill only the subshell and
# leave the caller holding an empty value it would read as "absent" — the very
# collapse being closed here.
adopt_credential_from_cluster() {
  local ns="$1" secret_name="$2" secret_key="$3"
  assert_k8s_name "namespace" "${ns}" || return 1
  assert_k8s_name "credentials Secret name" "${secret_name}" || return 1
  assert_secret_data_key "${secret_key}" || return 1

  # Probe the Secret itself first: only a genuine NotFound may fall through to
  # the caller's generation step.
  local presence
  presence="$(secret_presence "${ns}" "${secret_name}")" || return 1
  [[ "${presence}" == "absent" ]] && return 0

  local encoded
  encoded="$(kubectl get secret "${secret_name}" -n "${ns}" \
    -o jsonpath="{.data.${secret_key}}" 2>/dev/null || true)"
  [[ -z "${encoded}" ]] && return 0

  # `base64 --decode` on truncated or malformed input prints the decodable
  # prefix, and BSD base64 (macOS, where this runs) still exits 0 — a partial
  # credential emitted as though it were the whole one, which the caller would
  # compare against the env file, write into it, or push back into a Secret.
  # The status alone therefore cannot be trusted; check the encoding's grammar
  # first. The API server validates Secret data on write, so this only fires
  # on something that never came from a real Secret read.
  local compact="${encoded//[[:space:]]/}"
  if [[ ! "${compact}" =~ ^[A-Za-z0-9+/]*={0,2}$ ]] || (( ${#compact} % 4 != 0 )); then
    error_no_exit "Secret '${secret_name}' in '${ns}' holds a ${secret_key} value that is not valid
base64 (${#compact} characters). Refusing to hand back the decodable prefix, which would be a
silently truncated credential."
    return 1
  fi

  # The trailing 'X<status>' does two jobs. The X preserves a trailing newline
  # in the credential, which `$( ... )` would otherwise strip — the same
  # sentinel discipline as verify_credential_secret's byte-length report, and
  # the reason a stray newline from --from-file or a Vault injector stays
  # visible to the caller. The digits carry base64's own exit status out of
  # the subshell, which the substitution's status cannot: that status is the
  # last command's, i.e. printf's, and is always 0.
  local decoded decode_status
  decoded="$(printf '%s' "${compact}" | base64 --decode; printf 'X%d' "${PIPESTATUS[1]}")"
  decode_status="${decoded##*X}"
  decoded="${decoded%X"${decode_status}"}"
  if (( decode_status != 0 )); then
    error_no_exit "Could not decode ${secret_key} from Secret '${secret_name}' in '${ns}' (base64 exit ${decode_status})."
    return 1
  fi
  printf '%s' "${decoded}"
}

# report_credential_secret_drift <ns> <secret_name> <env_file>
# Compares each of the eleven env-file credential inputs against the live
# Secret and reports, BY KEY NAME ONLY, which of them disagree. Returns 1 when
# anything disagrees, 0 when the file and the Secret match (or the Secret does
# not exist yet). Reads only — it never writes the env file and never patches
# the Secret.
#
# Reporting by name and stopping there is the contract, not a limitation. An
# existing Secret may hold the only surviving copy of material a retained PVC
# depends on — the Fernet key above all — so which side is authoritative is
# the operator's decision, and no automatic reconciliation can make it
# safely. Values are never printed for the same reason --verify-only exists:
# this runs against production and its output lands in terminals and CI logs.
#
# Values are read from the FILE's own assignments rather than from the process
# environment, matching seed_profile's reasoning above: the canonical shell
# setup for this repo exports one env file's variables, and a comparison
# against those would judge a different deployment's credentials. The last
# assignment of a name wins, as it would when the file is sourced; commented
# lines and an `export ` prefix are handled the same way.
#
# A blank line in the file is a request for the pre-flight to resolve that key
# (§Tier 5), not a claim that the Secret is wrong, so it is skipped here.
#
# Both sides are compared as the bytes they really are. One matched pair of
# surrounding quotes is stripped from the file's value, because `source`
# strips it too and the file's effective value is the unquoted one — an
# operator quoting a password that contains a '#' or a space is exactly who
# would otherwise be told their correct deployment has drifted. The live side
# keeps its trailing newline through an 'X' sentinel, since `$( ... )` would
# strip it and a Secret holding `pw\n` against a file holding `pw` is the very
# defect the sibling byte-length report exists to expose.
#
# An unreadable env file is a hard stop, not an empty comparison. Every key
# would read as blank, every key would be skipped, and the function would
# report an all-clear on a production Secret nobody actually checked — the
# worst possible answer for an audit, and the one a typo'd --env-file or a
# CI run that never mounted the file would produce.
report_credential_secret_drift() {
  local ns="$1" secret_name="$2" env_file="$3"
  assert_k8s_name "namespace" "${ns}" || return 1
  assert_k8s_name "credentials Secret name" "${secret_name}" || return 1

  if [[ ! -r "${env_file}" ]]; then
    error "report_credential_secret_drift: cannot read env file '${env_file}'. A comparison against
a file that is missing or unreadable would report every key as unset and end in a false all-clear."
  fi

  # Genuinely absent is "nothing to compare"; unreadable is a stopped audit.
  # Collapsing the two would let a denied read or a wrong kubeconfig context
  # print a clean verdict on a Secret that was never examined.
  local presence
  presence="$(secret_presence "${ns}" "${secret_name}")" \
    || error "Cannot compare '${env_file}' against Secret '${secret_name}' in '${ns}' — see the error above."
  if [[ "${presence}" == "absent" ]]; then
    info "Secret '${secret_name}' does not exist in '${ns}' — nothing to compare."
    return 0
  fi

  local key_map
  key_map="$(prod_credential_key_map)"

  local prod_name secret_key file_value live_value
  local compared=0
  local differing=() absent=()
  while IFS=$'\t' read -r prod_name secret_key; do
    [[ -z "${secret_key}" ]] && continue
    # Only the NAME reaches awk's argv; the value leaves on stdout and is
    # captured by the shell, never by a child process's command line.
    # `printf "%s"`, not `print`, so awk adds no newline of its own and the
    # shell-side 'X' sentinel measures the value alone.
    file_value="$(awk -v name="${prod_name}" '
      # sprintf, not a literal: the whole program is inside single quotes in
      # the shell, so an apostrophe cannot be written here directly.
      BEGIN { squote = sprintf("%c", 39) }
      /^[[:space:]]*#/ { next }
      {
        line = $0
        sub(/^[[:space:]]*/, "", line)
        sub(/^export[[:space:]]+/, "", line)
        if (index(line, name "=") != 1) next
        value = substr(line, length(name) + 2)
        # Reverses the encoding env_file_set_var writes, and hand quoting by
        # an operator with it. Without the unescaping step below, a
        # single-quoted value carrying an apostrophe compares as its escaped
        # spelling and reports drift against a Secret holding exactly it.
        # (No apostrophe may appear in this comment: the whole awk program is
        # inside single quotes in the shell.)
        if (length(value) >= 2) {
          q = substr(value, 1, 1)
          if ((q == "\"" || q == squote) && substr(value, length(value), 1) == q) {
            value = substr(value, 2, length(value) - 2)
            if (q == squote) {
              esc = squote "\\" squote squote   # the literal '\''
              unescaped = ""
              while ((p = index(value, esc)) > 0) {
                unescaped = unescaped substr(value, 1, p - 1) squote
                value = substr(value, p + length(esc))
              }
              value = unescaped value
            }
          }
        }
      }
      END { printf "%s", value }
    ' "${env_file}"; printf 'X')"
    file_value="${file_value%X}"
    [[ -z "${file_value}" ]] && continue
    compared=$(( compared + 1 ))

    # `&& printf` rather than `; printf`: the sentinel is only appended when
    # adopt succeeded, so the substitution's status still carries a read that
    # did not answer the question (RBAC, wrong context, unreachable API
    # server) instead of turning it into "absent".
    if ! live_value="$(adopt_credential_from_cluster "${ns}" "${secret_name}" "${secret_key}" && printf 'X')"; then
      error "Could not compare ${secret_key} against Secret '${secret_name}' in '${ns}' — see the error above."
    fi
    live_value="${live_value%X}"
    if [[ -z "${live_value}" ]]; then
      absent+=("${secret_key}")
    elif [[ "${file_value}" != "${live_value}" ]]; then
      differing+=("${secret_key}")
    fi
  done <<< "${key_map}"

  if (( compared == 0 )); then
    info "No populated credential in '${env_file}' to compare against Secret '${secret_name}' — every one of the eleven lines is blank, which is a request for the pre-flight to resolve them (spec/feature/HELM_CHART.md §Tier 5), not a verdict on the Secret."
    return 0
  fi

  if (( ${#differing[@]} == 0 && ${#absent[@]} == 0 )); then
    info "All ${compared} populated credential(s) in '${env_file}' match Secret '${secret_name}'."
    return 0
  fi

  if (( ${#differing[@]} > 0 )); then
    warn "These keys differ between '${env_file}' and Secret '${secret_name}': ${differing[*]}"
  fi
  if (( ${#absent[@]} > 0 )); then
    warn "These keys are set in '${env_file}' but absent from Secret '${secret_name}': ${absent[*]}"
  fi
  warn "The Secret is not rewritten. Decide which side is authoritative — an existing Secret may be the only copy of what a retained PersistentVolumeClaim depends on (see spec/feature/HELM_CHART.md §What a prod uninstall leaves behind) — then either correct the env file or replace the Secret deliberately."
  return 1
}
