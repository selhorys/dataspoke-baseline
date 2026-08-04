#!/usr/bin/env bash
# Seed the built-in default admin user (dataspoke@dataspoke.local / dataspoke) via the
# internal bootstrap endpoint. Safe to re-run — the endpoint is idempotent:
# if any Admin already exists, it returns {created: false} and this script
# exits cleanly.
#
# On a prod env file carrying DATASPOKE_PROD_ADMIN_PASSWORD, the same run then
# rotates that account to it, closing the window in which the credential
# published in this repository is live. The rotation is idempotent by
# construction — it tries the target password first — and reports one word.
#
# Auth: the API pod's own DATASPOKE_INTERNAL_TOKEN, read from inside the pod
# by api_internal_request (bin/lib/helpers.sh) and sent as X-Internal-Token —
# never extracted to this machine.
# Endpoint: POST /internal/admin/bootstrap, reached over the API's own
# loopback port from inside its pod (no ingress, no DNS).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${ENV_FILE:-$(cd "$BIN_DIR/.." && pwd)/.env.dev}"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
# shellcheck source=../lib/helpers.sh
source "$BIN_DIR/lib/helpers.sh"

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  error "Env file not found at $ENV_FILE — copy helm-charts/.env.dev.example and edit it."
fi
# shellcheck disable=SC1090
source "$ENV_FILE"

NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"

# ---------------------------------------------------------------------------
# Bootstrap default admin user
# ---------------------------------------------------------------------------
info "Calling POST /internal/admin/bootstrap to seed default admin user..."
RESPONSE="$(api_internal_request "${NS}" POST "/internal/admin/bootstrap" '{}')"
HTTP_CODE="$(printf '%s\n' "$RESPONSE" | head -n1)"
BODY="$(printf '%s\n' "$RESPONSE" | tail -n +2)"

# Parse error_code from body when present (used by the 503 branch). A raw
# FastAPI HTTPException(detail={"error_code": ..., ...}) serializes as a
# NESTED {"detail": {"error_code": ...}} envelope, not a top-level
# error_code key — check both shapes.
ERROR_CODE="$(printf '%s' "$BODY" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
detail = d.get('detail')
ec = d.get('error_code') or (detail.get('error_code', '') if isinstance(detail, dict) else '')
print(ec)
" 2>/dev/null || true)"

case "$HTTP_CODE" in
  200|201)
    CREATED="$(printf '%s' "$BODY" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('created',''))" 2>/dev/null || true)"
    if [[ "$CREATED" == "True" || "$CREATED" == "true" ]]; then
      info "Seeded default admin user 'dataspoke@dataspoke.local'."
      warn "Default admin 'dataspoke@dataspoke.local / dataspoke' seeded with the password published
in this repository. The rotation step below replaces it when the env file names a prod deployment
carrying DATASPOKE_PROD_ADMIN_PASSWORD; otherwise rotate via PATCH /api/v1/auth/me before
production use."
    else
      info "Admin user already exists; skipping seed."
    fi
    ;;
  503)
    if [[ "$ERROR_CODE" == "INTERNAL_AUTH_NOT_CONFIGURED" ]]; then
      error "Bootstrap got HTTP 503 (error_code=INTERNAL_AUTH_NOT_CONFIGURED) from POST /internal/admin/bootstrap — the API pod's own DATASPOKE_INTERNAL_TOKEN is unset or blank. Check dataspoke-secrets and roll the dataspoke-api deployment."
    else
      error "Bootstrap got HTTP 503 (error_code=${ERROR_CODE:-unknown}) from POST /internal/admin/bootstrap. The bootstrap endpoint makes no external call, so any other 503 means the API's own storage (Postgres) is unavailable; fix that and re-run. Response body: ${BODY}"
    fi
    ;;
  000)
    error "Could not reach the API's own port (127.0.0.1:8002) from inside the dataspoke-api pod (namespace ${NS}) after 5 retries — check that deploy/dataspoke-api's 'api' container is Ready and listening."
    ;;
  *)
    error "POST failed (HTTP ${HTTP_CODE}) from /internal/admin/bootstrap. Response body: ${BODY}"
    ;;
esac

# ---------------------------------------------------------------------------
# Rotate the seeded admin to DATASPOKE_PROD_ADMIN_PASSWORD
# ---------------------------------------------------------------------------
# Static in-pod Python source for the rotation exchange below. The target
# password reaches it on stdin and never as argv — the whole exchange runs
# inside the API pod in ONE `kubectl exec`, so the access token is obtained and
# discarded in the same process and only a one-word verdict crosses back.
# Nothing here is assembled by interpolating caller data into this string.
#
# The verdicts, and what each one means for the account:
#   ROTATED            the published default authenticated, the PATCH answered
#                      200, and the new password then authenticated.
#   ALREADY_ROTATED    the target password authenticated on the first attempt.
#                      Nothing was written — this is what a re-run against an
#                      already-rotated deployment reports, and it is why the
#                      target is tried FIRST.
#   NO_KNOWN_PASSWORD  neither the target nor the published default
#                      authenticates. The account is left alone: someone
#                      rotated it to a third value, and guessing further is
#                      worse than reporting.
#   PATCH_FAILED_<n>   PATCH /auth/me answered <n>. `000` is a connection
#                      failure, where the request may or may not have
#                      committed — the same spelling api_internal_request uses.
#   VERIFY_FAILED      the PATCH answered 200 but the confirming login did not
#                      succeed, so which password is live is unknown.
#   UNREACHABLE        a login could not be completed at all — no connection, or
#                      a status (429, 5xx) that is not a verdict about a
#                      password. Nothing was written.
#
# PATCH_FAILED_*, VERIFY_FAILED, UNREACHABLE, an unrecognised verdict, and a
# failed `kubectl exec` all exit this script with ROTATION_FAILED_EXIT (2)
# rather than error()'s 1: they describe a rotation attempt that did not
# land, not a broken deployment, and install.sh's own call site treats exit 2
# as "report loudly in the summary" rather than "the install failed" — a
# completed helm upgrade must not be discarded over a rotation as transient
# as a 429 from the auth limiter's 10/minute POST /auth/token cap.
read -r -d '' _ADMIN_ROTATE_PY <<'PYEOF' || true
import json, sys, urllib.request, urllib.error

BASE = "http://127.0.0.1:8002/api/v1"
# Not configurable: PATCH /auth/me sets name and password only, so the address
# of the account this rotates is the one the bootstrap endpoint seeds.
EMAIL = "dataspoke@dataspoke.local"
PUBLISHED_DEFAULT = "dataspoke"
TIMEOUT = 10

target = sys.stdin.read()
# `kubectl exec -i` carries the shell here-string, which appends exactly one
# newline. Strip that one and nothing else: trailing whitespace is legal in a
# password and stripping it would rotate to a value the operator did not set.
if target.endswith("\n"):
    target = target[:-1]


def call(method, path, payload, token=None):
    """(status, body). Status 0 means the request did not complete at all."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        sys.stderr.write("admin rotation: %s %s did not complete: %s\n" % (method, path, exc))
        return 0, ""


def login(password):
    """(access_token, blocking_verdict). Exactly one of the two is truthy."""
    status, body = call("POST", "/auth/token", {"email": EMAIL, "password": password})
    if status == 200:
        try:
            return json.loads(body).get("access_token", ""), ""
        except ValueError:
            sys.stderr.write("admin rotation: POST /auth/token answered 200 with a non-JSON body\n")
            return "", "UNREACHABLE"
    if status == 401:
        # A verdict about this password, not about the API. The caller decides
        # what to try next.
        return "", ""
    # 429 from the auth limiter, 503 when its Redis or the DB is unavailable,
    # 5xx, or 0 for no connection: none of these says anything about the
    # password, so none may be reported as one.
    sys.stderr.write("admin rotation: POST /auth/token answered %s\n" % status)
    return "", "UNREACHABLE"


token, blocked = login(target)
if blocked:
    print(blocked)
    raise SystemExit(0)
if token:
    print("ALREADY_ROTATED")
    raise SystemExit(0)

token, blocked = login(PUBLISHED_DEFAULT)
if blocked:
    print(blocked)
    raise SystemExit(0)
if not token:
    print("NO_KNOWN_PASSWORD")
    raise SystemExit(0)

status, body = call("PATCH", "/auth/me", {"password": target}, token=token)
if status != 200:
    # The body is not echoed: a 422 from MePatchRequest carries the rejected
    # password back in its envelope.
    print("PATCH_FAILED_%03d" % status)
    raise SystemExit(0)

token, blocked = login(target)
print("ROTATED" if token else "VERIFY_FAILED")
PYEOF

# Exit code this script reports when the ONLY thing that failed was the
# rotation itself (PATCH_FAILED_*, VERIFY_FAILED, UNREACHABLE, an
# unrecognised verdict, or a failed kubectl exec) — distinct from a plain
# error() exit (1), which install.sh's `bash seed-admin-user.sh` (no `set -e`
# guard of its own around that call) would otherwise treat identically to a
# bootstrap failure and abort on. install.sh's own caller inspects this exit
# code (see the comment at its call site) so a rotation failure — which can
# include a transient 429 from the auth limiter — is reported loudly in the
# install summary instead of discarding a completed helm upgrade.
ROTATION_FAILED_EXIT=2

_rotate_admin() {
  local target="$1" verdict=""
  local exec_status=0

  # `-i` attaches the pipe the password rides in on; `-t` is deliberately never
  # passed — a TTY would translate newlines and corrupt the value, and a CI run
  # has no terminal to allocate one from. The here-string is what feeds it, for
  # the same reason api_internal_request uses one: bash re-expands it from a
  # variable, and nothing but the verdict comes back on stdout.
  # `if cmd; then ... else exec_status=$?; fi`, never `if ! cmd`: `!` resets $?
  # to 0 inside the branch, so the reported exit code would always be zero.
  if verdict="$(kubectl exec -i -n "${NS}" deploy/dataspoke-api -c api -- \
      python3 -c "${_ADMIN_ROTATE_PY}" <<<"${target}")"; then
    exec_status=0
  else
    exec_status=$?
    warn "kubectl exec into dataspoke-api (-n ${NS}) failed (exit ${exec_status}) while rotating the
built-in admin. The account still holds whatever password it had; nothing was written."
    exit "${ROTATION_FAILED_EXIT}"
  fi

  case "${verdict}" in
    ROTATED)
      info "Rotated 'dataspoke@dataspoke.local' to DATASPOKE_PROD_ADMIN_PASSWORD and verified the new credential. The password published in this repository is no longer live."
      ;;
    ALREADY_ROTATED)
      info "'dataspoke@dataspoke.local' already authenticates with DATASPOKE_PROD_ADMIN_PASSWORD — nothing to rotate."
      ;;
    NO_KNOWN_PASSWORD)
      warn "'dataspoke@dataspoke.local' accepts neither DATASPOKE_PROD_ADMIN_PASSWORD nor the
password published in this repository, so the account was left alone rather than guessed at.
Someone has rotated it to a third value. Reconcile ${ENV_FILE} with what that account actually
holds, or reset it through PATCH /api/v1/auth/me as its current owner."
      ;;
    # The four verdicts below describe a FAILED rotation attempt, never a
    # broken deployment — the helm upgrade this runs after already succeeded.
    # warn(), not error(): an error() exit here is indistinguishable from a
    # bootstrap failure to install.sh's caller, which would otherwise discard
    # a completed install over something as transient as a 429 from the
    # auth limiter's 10/minute POST /auth/token cap. The account's password is
    # still whatever it was before this attempt; nothing was written.
    PATCH_FAILED_*)
      warn "Rotating 'dataspoke@dataspoke.local' failed: PATCH /api/v1/auth/me answered
${verdict#PATCH_FAILED_} (000 means the request never completed, so it may or may not have taken
effect). The password published in this repository may still be live — check before treating this
deployment as production-ready."
      exit "${ROTATION_FAILED_EXIT}"
      ;;
    VERIFY_FAILED)
      warn "PATCH /api/v1/auth/me answered 200 for 'dataspoke@dataspoke.local' but the confirming
login did not succeed, so which password is live is unknown. Try both DATASPOKE_PROD_ADMIN_PASSWORD
and the published default before treating this deployment as production-ready."
      exit "${ROTATION_FAILED_EXIT}"
      ;;
    UNREACHABLE)
      warn "Could not complete the admin rotation exchange against the API's own port
(127.0.0.1:8002) from inside the dataspoke-api pod (namespace ${NS}) — see the reported status
above. Nothing was written; the password published in this repository is still live."
      exit "${ROTATION_FAILED_EXIT}"
      ;;
    *)
      warn "The in-pod admin rotation returned an unrecognised verdict '${verdict}'. Nothing about the account's password can be concluded from it."
      exit "${ROTATION_FAILED_EXIT}"
      ;;
  esac
}

# The profile comes from the env file's own variable names through the shared
# seed_profile (bin/lib/helpers.sh), the same resolver the other two seeds use,
# rather than from the presence of the password variable alone: the canonical
# integration-test setup exports one env file's variables into the shell, and a
# rotation must not be decided by a leftover export while a different file is
# being seeded.
case "$(seed_profile "$ENV_FILE")" in
  prod)
    if [[ -z "${DATASPOKE_PROD_ADMIN_PASSWORD:-}" ]]; then
      warn "DATASPOKE_PROD_ADMIN_PASSWORD is blank in ${ENV_FILE}, so the built-in admin keeps the
password published in this repository — 'dataspoke@dataspoke.local / dataspoke' is LIVE. Rotation is
required, not advisory: set that variable and re-run this script, or rotate by hand through
PATCH /api/v1/auth/me."
    else
      # The same two rejections the pre-flight applies to this variable, through
      # the one shared gate (assert_admin_password, bin/lib/helpers.sh): both
      # describe a rotation that cannot succeed, so both stop rather than warn,
      # and reaching them here means the pre-flight was not run.
      assert_admin_password "${DATASPOKE_PROD_ADMIN_PASSWORD}" "${ENV_FILE}"
      info "Rotating 'dataspoke@dataspoke.local' to DATASPOKE_PROD_ADMIN_PASSWORD..."
      _rotate_admin "${DATASPOKE_PROD_ADMIN_PASSWORD}"
    fi
    ;;
  ambiguous)
    warn "${ENV_FILE} declares both DATASPOKE_PROD_* and DATASPOKE_DEV_* variables, so which
deployment its admin password belongs to cannot be told from it — the built-in admin was left
alone. Keep one profile's block per env file, then re-run."
    ;;
  *)
    # Dev and profile-less files: the built-in admin keeps the published
    # credential, which is the dev deployment's documented login.
    :
    ;;
esac
