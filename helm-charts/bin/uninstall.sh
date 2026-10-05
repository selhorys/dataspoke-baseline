#!/usr/bin/env bash
# DataSpoke uninstaller.
#
# Usage: uninstall.sh --profile {dev|prod} [OPTIONS]
#
#   --env-file <path>      Path to the env file (default: helm-charts/.env.<PROFILE>).
#   --profile {dev|prod}   Required. Selects which component set to tear down.
#   --components frontend  Targeted teardown of the frontend only — helm upgrade
#                          with frontend.enabled=false (the frontend is an optional
#                          umbrella subchart). Everything else is left untouched.
#                          Only `frontend` is supported; the api subchart is the
#                          core service and has no partial teardown (stop it with
#                          `kubectl scale deployment/dataspoke-api --replicas=0`).
#   --no-question          Skip every interactive prompt (gate, PVC, namespace).
#   --delete-pvcs          Also delete PersistentVolumeClaims (dev only).
#   --delete-namespaces    Also delete the application namespaces.
#   --delete-all           Shortcut for --delete-pvcs --delete-namespaces.
#   --help, -h             Print this usage message.
#
# Default behaviour: uninstalls Helm releases and chart-derived Secrets.
# PVCs and namespaces are preserved unless explicitly opted in.
#
# Bounded teardown (spec/feature/HELM_CHART.md §Bounded teardown): release
# removal, the controller sweep + pod wait, PVC/namespace deletion and the PV
# wait are all time-bounded. A deletion that does not complete is named in a
# closing summary and the script exits non-zero; it never blocks indefinitely.
#   DATASPOKE_UNINSTALL_RELEASE_TIMEOUT_SECS  helm uninstall --wait per release
#                                             (default 300; best-effort).
#   DATASPOKE_UNINSTALL_DELETE_TIMEOUT_SECS   each pod wait, PVC delete, namespace
#                                             delete, PV wait, namespace presence
#                                             read and the one namespace re-check
#                                             (default 120).
# Invalid values (not a positive integer <= 86400) are warned about and the
# default is used. Every kubectl read in the wait loops, the controller sweep and
# the force delete also carries a short --request-timeout, and a read that fails
# is "unknown", never "gone". The two blocking deletes (PVC, namespace) are the
# exception: they are bounded by --timeout alone, see _delete_pvc_bounded.
# A namespace whose bounded deletion timed out may merely still be finalizing
# (a cloud load balancer being released), so _finish_teardown re-checks it once
# more, for at most DELETE_TIMEOUT_SECS, before the summary.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELM_CHARTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CHART_DIR="$HELM_CHARTS_DIR/dataspoke"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
# shellcheck source=lib/helpers.sh
source "$SCRIPT_DIR/lib/helpers.sh"

# _cleanup_run_with_timeout_state (lib/helpers.sh) is a no-op unless the
# --components frontend path below is mid-`_build_chart_deps`, or the
# cluster-reachability preflight below is mid-`_run_with_timeout`; wired
# unconditionally, same reasoning as install.sh's own EXIT/INT/TERM traps.
trap _cleanup_run_with_timeout_state EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ---------------------------------------------------------------------------
# _require_cluster_reachable <cluster>
#
# One bounded, non-mutating live API round-trip against the context
# `use_context` just selected — called immediately after every `use_context`
# call in this script. This is a PREFLIGHT, not a completion guarantee: it
# proves the cluster answers at this one instant, nothing about whether the
# teardown that follows actually completes (HELM_CHART.md §Uninstallation).
#
# Why this is needed even though `use_context` already ran: `use_context`
# only proves the named context exists in the (pinned, local) kubeconfig —
# a local read that never dials the cluster. Every existence probe further
# down this script is `if <cmd> >/dev/null 2>&1`, so on an unreachable
# cluster (DNS outage, expired credential, network partition) EVERY one of
# those reads as "resource does not exist — skipping", and the script would
# otherwise exit 0 having deleted nothing — which is exactly the failure
# mode that once left a full dev stack running for hours after a caller
# trusted that exit code as proof of deletion.
#
# Bounded two ways, deliberately: `--request-timeout` bounds a single HTTP
# request, not the wall-clock time of this whole probe, so alone it cannot
# stop a connection that is accepted and then never answers (a blackholed
# route behaves differently from a route that refuses outright, and which
# one a given unreachable endpoint hits is not something this script
# controls). `_run_with_timeout` (lib/helpers.sh, already used by this same
# script's `_build_chart_deps` path, so its EXIT-trap cleanup above is
# already wired) adds the wall-clock backstop on top, rather than a
# bespoke sleep/kill loop.
_require_cluster_reachable() {
  local cluster="$1"
  local out rc=0
  local timeout_secs="${DATASPOKE_UNINSTALL_REACHABILITY_TIMEOUT_SECS:-20}"
  # Validated like its siblings (PRAUTO_PROVISION_TIMEOUT_SECS et al): a typo
  # would otherwise reach _run_with_timeout's own error() and abort the teardown
  # with an internal message about <secs>.
  if [[ ! "$timeout_secs" =~ ^[1-9][0-9]*$ ]]; then
    warn "Invalid DATASPOKE_UNINSTALL_REACHABILITY_TIMEOUT_SECS='${timeout_secs}'; using the default of 20."
    timeout_secs=20
  fi

  out="$(mktemp "${TMPDIR:-/tmp}/dataspoke-uninstall-reachability.XXXXXX")" \
    || error "Could not create a temporary file for the cluster reachability preflight."

  # `|| rc=$?` on the SAME logical line, not `rc=$?` on the line after: under
  # this script's `set -e`, a non-zero status from a bare command (one not
  # already part of an if/while/`||` condition) ends the script immediately,
  # before a following `rc=$?` line would ever run — which would silently
  # convert every unreachable-cluster case into an uncaught early exit
  # carrying kubectl's or _run_with_timeout's own status, never this
  # function's message.
  _run_with_timeout "$timeout_secs" \
    kubectl get --raw=/healthz --request-timeout=10s >"$out" 2>&1 || rc=$?

  if [[ "$rc" -ne 0 ]]; then
    local detail
    # A LOCAL setup fault is not a verdict on the cluster. _run_with_timeout
    # returns 1 when it cannot isolate a process group and 125 when group
    # verification changes (lib/helpers.sh) — neither writes to $out, so
    # reporting them as "unreachable" would be a lie that also strands every
    # future teardown attempt: prauto keeps its durable marker on a failed
    # teardown, so a host that always fails this way would bill a cluster
    # forever. The repo draws this same line elsewhere (health-check.sh's exit 2
    # vs a cluster verdict).
    if [[ "$rc" -eq 1 || "$rc" -eq 125 ]] && [[ ! -s "$out" ]]; then
      rm -f "$out"
      error "Could not run the cluster reachability preflight on this host (bounded-run setup fault, status ${rc}). This is a local fault, not a verdict on cluster '${cluster}'."
    fi
    if [[ "$rc" -eq 124 ]]; then
      detail="the probe did not return within ${timeout_secs}s"
    else
      detail="$(sanitize_remote_text "$(cat "$out" 2>/dev/null)" 300)"
      [[ -z "$detail" ]] && detail="kubectl exited ${rc} with no output"
    fi
    rm -f "$out"
    error "Cluster '${cluster}' is unreachable — the reachability preflight failed: ${detail}. This is a preflight check only: it proves reachability at this instant, not that the teardown below completed."
  fi
  rm -f "$out"
}

# ---------------------------------------------------------------------------
# Bounded teardown helpers (HELM_CHART.md §Bounded teardown)
#
# Teardown-only, so they live here and not in lib/helpers.sh (shared by every
# install script). Bash-3.2-clean: no `wait -n`, no associative arrays, no
# mapfile, no `${arr[@]}` on a possibly-empty array under `set -u`.
#
# Every wait below has a wall-clock deadline and every deletion that does not
# complete is recorded in UNRESOLVED instead of blocking or being hidden; the
# script exits non-zero after a closing summary when UNRESOLVED is non-empty.
# ---------------------------------------------------------------------------
RELEASE_TIMEOUT_SECS=300   # `helm uninstall --wait`, per release
DELETE_TIMEOUT_SECS=120    # each pod wait, PVC/namespace delete, namespace presence
                           # read, namespace re-check, PV wait
REQUEST_TIMEOUT="15s"      # --request-timeout on every kubectl call in the helpers
MAX_TIMEOUT_SECS=86400     # upper bound for the two env knobs (keeps $(( )) sane)
UNRESOLVED=()
TIMED_OUT_NAMESPACES=()    # namespaces whose bounded deletion did not complete
PV_WAIT_LIST=""
# `@deploy:` pod snapshots (see DATASPOKE_SELECTORS): newline-delimited
# `<namespace>|<deployment>|<pod-uid>` records, each list starting and ending
# with a newline so membership is a plain `case` pattern.
DEPLOY_POD_UIDS=$'\n'      # ownership verified at snapshot time (in scope)
DEPLOY_UNKNOWN_UIDS=$'\n'  # ownership unreadable at snapshot time (never a match)
DEPLOY_NOT_OWNED_UIDS=$'\n' # verified NOT owned (look-alikes; out of scope)
WORKLOAD_KINDS="statefulset,deployment,daemonset,job,cronjob"
# The umbrella release's workloads do not share one label: the DataSpoke-owned
# subcharts and Bitnami dependencies carry app.kubernetes.io/instance, the
# Apache Airflow subchart (1.20) labels every scheduler/api-server/triggerer/
# migration object `release=<release>,tier=airflow` only, and the api
# Deployment carries app.kubernetes.io/name=dataspoke-api only — an app-identity
# label, not a release-identity one, so it is NOT used as a delete selector (an
# operator-owned object in the prod namespace could copy it). The API is
# instead targeted by its fixed object name via the `@deploy:<name>` token.
# Space-separated selector list; each label selector is ANDed internally, the
# list is ORed. Tokens: `@all` (every workload; Langfuse guard only) and
# `@deploy:<name>` (that one Deployment by exact name; its pods are the ones
# labelled app.kubernetes.io/name=<name> that are owned by a ReplicaSet named
# <name>-<hash> that is itself controlled by Deployment/<name>, i.e. by that
# Deployment and no look-alike). That ReplicaSet chain is resolved ONCE, by
# _snapshot_deployment_pods before the Helm uninstall / controller sweep, while
# the ReplicaSets still exist (garbage collection removes them afterwards, which
# would make every still-terminating pod's ownership unreadable): the matched
# pods are recorded by UID and a pod is in scope for the token iff its UID is in
# that snapshot. A pod verified as not owned (a look-alike) is out of scope; any
# other labelled pod — ownership unreadable at snapshot time, or created after it
# (e.g. a replacement the ReplicaSet made before the Deployment was deleted) — is
# "unknown": it keeps the pod listing incomplete, so a survivor is reported as
# unverified, but it is never a match and never force-deleted.
DATASPOKE_SELECTORS="app.kubernetes.io/instance=dataspoke release=dataspoke,tier=airflow @deploy:dataspoke-api"

# _resolve_timeout <env-var-name> <default> <result-var-name>
# Validated like DATASPOKE_UNINSTALL_REACHABILITY_TIMEOUT_SECS: a value that is
# not a positive integer is warned about and replaced by the default. An upper
# bound of MAX_TIMEOUT_SECS is enforced with a digit-length test BEFORE any
# arithmetic: a huge value would overflow `SECONDS + timeout` into a negative
# deadline (waits return at once) and make kubectl reject `--timeout=<huge>s`.
_resolve_timeout() {
  local name="$1" default="$2" result="$3"
  local value="${!name:-$default}"
  if [[ ! "$value" =~ ^[1-9][0-9]{0,4}$ ]] || (( value > MAX_TIMEOUT_SECS )); then
    warn "Invalid ${name}='$(sanitize_remote_text "$value" 40)' (need a positive integer <= ${MAX_TIMEOUT_SECS}); using the default of ${default}."
    value="$default"
  fi
  printf -v "$result" '%s' "$value"
}

# _resolve_teardown_timeouts — sets RELEASE_TIMEOUT_SECS / DELETE_TIMEOUT_SECS.
_resolve_teardown_timeouts() {
  _resolve_timeout DATASPOKE_UNINSTALL_RELEASE_TIMEOUT_SECS 300 RELEASE_TIMEOUT_SECS
  _resolve_timeout DATASPOKE_UNINSTALL_DELETE_TIMEOUT_SECS 120 DELETE_TIMEOUT_SECS
}

# _note_unresolved <message> — append a sanitized entry to the unresolved set.
_note_unresolved() {
  local entry existing
  entry="$(sanitize_remote_text "$1" 400)"
  if [[ "${#UNRESOLVED[@]}" -gt 0 ]]; then
    for existing in "${UNRESOLVED[@]}"; do
      [[ "$existing" == "$entry" ]] && return 0   # one entry per surviving object
    done
  fi
  UNRESOLVED+=("$entry")
}

# _drop_unresolved_in_ns <namespace> — forget entries scoped to a namespace that
# has since been deleted: the pods, claims and workloads in it went with it, so
# reporting them as surviving (or as unverifiable) would be a false failure.
# Every pattern ends the namespace name with a delimiter (`/` or a space) and the
# name is quoted (literal, never a glob), so `foo` never matches `foo-bar`; an
# empty name matches nothing. The namespace's own "namespace <ns> not deleted"
# entry goes too; "namespace <ns> could not be read" is never scoped here (the
# namespace was not deleted) and is left alone.
_drop_unresolved_in_ns() {
  local ns="$1" entry kept=()
  [[ -n "$ns" ]] || return 0
  [[ "${#UNRESOLVED[@]}" -gt 0 ]] || return 0
  for entry in "${UNRESOLVED[@]}"; do
    case "$entry" in
      "namespace ${ns} not deleted "*) ;;
      "pod ${ns}/"*|"PVC ${ns}/"*|"workload ${ns}/"*) ;;
      # Namespace-scoped "could not be listed" entries: the read failed, but the
      # namespace (and so everything unverified in it) was then deleted.
      "pods in ${ns} "*|"PVCs in ${ns} "*|"workloads in ${ns} "*) ;;
      *) kept+=("$entry") ;;
    esac
  done
  UNRESOLVED=()
  if [[ "${#kept[@]}" -gt 0 ]]; then
    UNRESOLVED=("${kept[@]}")
  fi
}

# _note_unresolved_lines <prefix> <newline-separated-items> <suffix>
_note_unresolved_lines() {
  local prefix="$1" items="$2" suffix="$3" item
  while IFS= read -r item; do
    [[ -z "$item" ]] && continue
    _note_unresolved "${prefix}${item}${suffix}"
  done <<< "$items"
}

# _run_err <result-var> <cmd...>
# Run <cmd> with stdout discarded and stderr captured into <result-var>;
# returns the command's own status. Replaces `2>/dev/null`, which hid the real
# cause of a failure behind a guessed one.
_run_err() {
  local __var="$1" __out __rc=0
  shift
  __out="$("$@" 2>&1 >/dev/null)" || __rc=$?
  printf -v "$__var" '%s' "$__out"
  return "$__rc"
}

# _owned_by_deployment <deployment> <namespace> <comma-separated Kind/name owners>
# Ownership of a pod by the named Deployment, decided on kind AND name: an owner
# must be a ReplicaSet named exactly `<deployment>-<hash>`, and that ReplicaSet
# is then read (bounded) and must itself be controlled by `Deployment/<deployment>`.
# A look-alike (a StatefulSet, Job or bare controller named `<deployment>-ext`,
# or a Deployment `<deployment>-canary` whose ReplicaSets are
# `<deployment>-canary-<hash>`) therefore never matches. Returns 0 on a match,
# 1 when the pod is not owned by it, and 2 when ownership could not be read (the
# ReplicaSet read failed or is gone) — "unknown", which callers must never treat
# as a match. Called only by _snapshot_deployment_pods, before the controller
# sweep — never from the wait / force-delete / final passes, by which time the
# ReplicaSets have been garbage-collected.
_owned_by_deployment() {
  local name="$1" ns="$2" owner kind rsname rsowners unknown=1
  local IFS=,
  for owner in $3; do
    kind="${owner%%/*}"
    rsname="${owner#*/}"
    [[ "$kind" == "ReplicaSet" ]] || continue
    [[ "$rsname" =~ ^${name}-[a-z0-9]+$ ]] || continue
    if ! rsowners="$(kubectl get replicaset "$rsname" -n "$ns" --request-timeout="$REQUEST_TIMEOUT" \
      -o jsonpath='{range .metadata.ownerReferences[*]}{.kind}{"/"}{.name}{"|"}{.controller}{"\n"}{end}' 2>/dev/null)"; then
      unknown=2
      continue
    fi
    case $'\n'"${rsowners}"$'\n' in
      *$'\n'"Deployment/${name}|true"$'\n'*) return 0 ;;
    esac
  done
  return "$unknown"
}

# _snapshot_deployment_pods <namespace> <deployment>
# Resolve the `@deploy:<deployment>` pod set once, BEFORE the Helm uninstall and
# the controller sweep, while the ReplicaSets still exist: list the
# app.kubernetes.io/name=<deployment> pods, verify each one's ReplicaSet chain
# (_owned_by_deployment) and record the verified pods by UID in DEPLOY_POD_UIDS
# the unreadable ones in DEPLOY_UNKNOWN_UIDS and the verified look-alikes in
# DEPLOY_NOT_OWNED_UIDS. A failed pod list is recorded
# as unresolved at once — the pods that should have been in scope are not known.
_snapshot_deployment_pods() {
  local ns="$1" dep="$2" out line name rest owners uid orc
  local fmt='{range .items[*]}{.metadata.name}{"|"}{range .metadata.ownerReferences[*]}{.kind}{"/"}{.name}{","}{end}{"|"}{.metadata.uid}{"\n"}{end}'
  if ! out="$(kubectl get pods -n "$ns" -l "app.kubernetes.io/name=${dep}" \
    --request-timeout="$REQUEST_TIMEOUT" -o jsonpath="$fmt" 2>/dev/null)"; then
    _note_unresolved "pods in ${ns} could not be listed before teardown — the removal of Deployment/${dep}'s pods could not be verified"
    return 0
  fi
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    name="${line%%|*}"
    rest="${line#*|}"
    owners="${rest%%|*}"
    uid="${rest#*|}"
    if [[ -z "$uid" ]]; then
      _note_unresolved "pod ${ns}/${name} has no readable UID before teardown — its removal could not be verified"
      continue
    fi
    orc=0
    _owned_by_deployment "$dep" "$ns" "$owners" || orc=$?
    case "$orc" in
      0) DEPLOY_POD_UIDS="${DEPLOY_POD_UIDS}${ns}|${dep}|${uid}"$'\n' ;;
      2) DEPLOY_UNKNOWN_UIDS="${DEPLOY_UNKNOWN_UIDS}${ns}|${dep}|${uid}"$'\n' ;;
      *) DEPLOY_NOT_OWNED_UIDS="${DEPLOY_NOT_OWNED_UIDS}${ns}|${dep}|${uid}"$'\n' ;;
    esac
  done <<< "$out"
}

# _pod_lines <namespace> <selectors-or-empty>
# One line per pod: `<name>|<claim>,<claim>,...` (empty claim slots for volumes
# that are not PVC-backed), de-duplicated across selectors. <selectors> is a
# space-separated list (tokens as documented at DATASPOKE_SELECTORS); empty lists
# every pod in the namespace. For an `@deploy:` token a pod is listed iff its UID
# is in the _snapshot_deployment_pods snapshot — no live ReplicaSet read happens
# here. A snapshot-verified look-alike is skipped; any other still-present
# labelled pod (unknown at snapshot time, or not seen then) is unverified: never
# listed, but it makes the result incomplete. Returns non-zero when ANY read failed or the
# result is incomplete in that way — an empty result must then be read as
# "unknown", never as "no pods".
_pod_lines() {
  local ns="$1" selectors="$2" sel one out="" rc=0 label dep line name rest claims uid
  local fmt='{range .items[*]}{.metadata.name}{"|"}{range .spec.volumes[*]}{.persistentVolumeClaim.claimName}{","}{end}{"|"}{.metadata.uid}{"\n"}{end}'
  for sel in ${selectors:-@all}; do
    label="" dep=""
    case "$sel" in
      @all) ;;
      @deploy:*) dep="${sel#@deploy:}"; label="app.kubernetes.io/name=${dep}" ;;
      *) label="$sel" ;;
    esac
    if [[ -z "$label" ]]; then
      one="$(kubectl get pods -n "$ns" --request-timeout="$REQUEST_TIMEOUT" \
        -o jsonpath="$fmt" 2>/dev/null)" || rc=1
    else
      one="$(kubectl get pods -n "$ns" -l "$label" --request-timeout="$REQUEST_TIMEOUT" \
        -o jsonpath="$fmt" 2>/dev/null)" || rc=1
    fi
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      name="${line%%|*}"
      rest="${line#*|}"
      claims="${rest%%|*}"
      uid="${rest#*|}"
      if [[ -n "$dep" ]]; then
        if [[ -n "$uid" && "$DEPLOY_POD_UIDS" == *$'\n'"${ns}|${dep}|${uid}"$'\n'* ]]; then
          :   # in the pre-sweep snapshot: in scope
        elif [[ -n "$uid" && "$DEPLOY_NOT_OWNED_UIDS" == *$'\n'"${ns}|${dep}|${uid}"$'\n'* ]]; then
          continue   # verified look-alike at snapshot time: out of scope
        else
          rc=1   # unknown / unseen at snapshot: incomplete result, never a match
          continue
        fi
      fi
      out="${out}${name}|${claims}"$'\n'
    done <<< "$one"
  done
  printf '%s' "$out" | sed '/^$/d' | sort -u
  return "$rc"
}

# _workload_names <namespace> <selectors-or-empty> — `kind/name` per line.
# Non-zero when any read failed (same contract as _pod_lines).
_workload_names() {
  local ns="$1" selectors="$2" sel one out="" rc=0
  for sel in ${selectors:-@all}; do
    case "$sel" in
      @all)
        one="$(kubectl get "$WORKLOAD_KINDS" -n "$ns" --request-timeout="$REQUEST_TIMEOUT" \
          -o name 2>/dev/null)" || rc=1 ;;
      @deploy:*)
        one="$(kubectl get deployment "${sel#@deploy:}" -n "$ns" --ignore-not-found \
          --request-timeout="$REQUEST_TIMEOUT" -o name 2>/dev/null)" || rc=1 ;;
      *)
        one="$(kubectl get "$WORKLOAD_KINDS" -n "$ns" -l "$sel" --request-timeout="$REQUEST_TIMEOUT" \
          -o name 2>/dev/null)" || rc=1 ;;
    esac
    out="${out}${one}"$'\n'
  done
  printf '%s' "$out" | sed '/^$/d' | sort -u
  return "$rc"
}

# _wait_pods_gone <namespace> <selectors-or-empty>
# Poll until no pod matches, up to DELETE_TIMEOUT_SECS of wall-clock time in
# total (a `kubectl wait` over N pods bounds each pod, not the whole step).
# Returns 0 when verified gone, 1 when pods remain at the deadline, and 2 when
# the deadline passed with every read failing — "could not verify", which the
# caller records rather than assuming success. Listing is the source of truth.
_wait_pods_gone() {
  local ns="$1" selectors="$2" lines rc
  local deadline=$(( SECONDS + DELETE_TIMEOUT_SECS ))
  while :; do
    rc=0
    lines="$(_pod_lines "$ns" "$selectors")" || rc=$?
    if [[ "$rc" -eq 0 && -z "$lines" ]]; then
      return 0
    fi
    if (( SECONDS >= deadline )); then
      [[ -n "$lines" ]] && return 1
      return 2
    fi
    sleep 1
  done
}

# _sweep_controllers <namespace> <selectors-or-empty>
# Delete the workload CONTROLLERS (never PVCs): deleting only pods cannot work,
# a surviving StatefulSet/Deployment recreates them. Empty selectors means
# every workload in the namespace (only used under the Langfuse guard).
# Idempotent — also recovers workloads orphaned by an earlier timed-out run.
# <selectors> takes the tokens documented at DATASPOKE_SELECTORS.
_sweep_controllers() {
  local ns="$1" selectors="$2" sel err rc
  for sel in ${selectors:-@all}; do
    rc=0
    case "$sel" in
      @all)
        _run_err err kubectl delete "$WORKLOAD_KINDS" -n "$ns" --all \
          --ignore-not-found --wait=false --request-timeout="$REQUEST_TIMEOUT" || rc=$? ;;
      @deploy:*)
        # By exact object name, never by label (see DATASPOKE_SELECTORS).
        _run_err err kubectl delete deployment "${sel#@deploy:}" -n "$ns" \
          --ignore-not-found --wait=false --request-timeout="$REQUEST_TIMEOUT" || rc=$? ;;
      *)
        _run_err err kubectl delete "$WORKLOAD_KINDS" -n "$ns" -l "$sel" \
          --ignore-not-found --wait=false --request-timeout="$REQUEST_TIMEOUT" || rc=$? ;;
    esac
    if [[ "$rc" -ne 0 ]]; then
      warn "Controller sweep in '${ns}' (${sel//@all/all workloads}) failed (status ${rc}): $(sanitize_remote_text "$err" 300)"
    fi
  done
}

# _record_unverified <namespace> — a pod list that could not be read before the
# deadline is "unknown": record it instead of assuming the pods are gone.
_record_unverified() {
  local ns="$1"
  _note_unresolved "pods in ${ns} could not be listed within ${DELETE_TIMEOUT_SECS}s — their removal could not be verified"
}

# _reap_pods <namespace> <selectors-or-empty> <force-pvc-pods: true|false>
# Wait (bounded) for pods to terminate. Only the pods that outlive the wait are
# force-deleted by name — effective now, because their controllers are gone.
# With force-pvc-pods=false (prod) a pod that mounts a PVC is NEVER force-
# deleted: the API object going away does not stop the container writing to the
# retained claim, and a prompt reinstall could attach a second writer. It, and
# any pod that survives a force delete, is recorded as unresolved. A pod list
# that cannot be read is "unknown" and is recorded, never assumed gone.
_reap_pods() {
  local ns="$1" selectors="$2" force_pvc_pods="$3"
  local lines line name claims shown force_list="" held_list=" " err rc=0 wrc=0

  _wait_pods_gone "$ns" "$selectors" || wrc=$?
  [[ "$wrc" -eq 0 ]] && return 0
  if [[ "$wrc" -eq 2 ]]; then
    _record_unverified "$ns"
    return 0
  fi

  lines="$(_pod_lines "$ns" "$selectors")" || true
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    name="${line%%|*}"
    claims="${line#*|}"
    if [[ "$force_pvc_pods" != true && -n "${claims//,/}" ]]; then
      shown="$(printf '%s' "$claims" | tr -s ',' | sed 's/^,//;s/,$//')"
      held_list="${held_list}${name} "
      _note_unresolved "pod ${ns}/${name} still running after ${DELETE_TIMEOUT_SECS}s and mounts PVC ${shown} — not force-deleted in prod (a retained claim must not gain a second writer)"
    else
      force_list="${force_list} ${name}"
    fi
  done <<< "$lines"

  if [[ -n "$force_list" ]]; then
    warn "Pods outlived the ${DELETE_TIMEOUT_SECS}s wait in '${ns}' — force-deleting (controllers are already gone):$(sanitize_remote_text "$force_list" 300)"
    # shellcheck disable=SC2086  # pod names carry no whitespace
    _run_err err kubectl delete pod $force_list -n "$ns" \
      --force --grace-period=0 --wait=false --ignore-not-found \
      --request-timeout="$REQUEST_TIMEOUT" || rc=$?
    if [[ "$rc" -ne 0 ]]; then
      warn "Force delete in '${ns}' failed (status ${rc}): $(sanitize_remote_text "$err" 300)"
    fi
    wrc=0
    _wait_pods_gone "$ns" "$selectors" || wrc=$?
    if [[ "$wrc" -eq 2 ]]; then
      _record_unverified "$ns"
      return 0
    fi
  fi

  # Whatever is still listed now (other than prod pods already recorded above)
  # could not be removed.
  rc=0
  lines="$(_pod_lines "$ns" "$selectors")" || rc=$?
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    name="${line%%|*}"
    if [[ "$held_list" != *" ${name} "* ]]; then
      _note_unresolved "pod ${ns}/${name} still present after the bounded wait and force delete"
    fi
  done <<< "$lines"
  if [[ "$rc" -ne 0 ]]; then
    _record_unverified "$ns"
  fi
  return 0
}

# _teardown_release <release> <namespace> <selectors> <force-pvc-pods>
# First the `@deploy:` pod snapshot (before Helm, while the ReplicaSets exist),
# then best-effort `helm uninstall --wait` (never aborts, carries Helm's own
# error), then — whatever Helm's outcome — the controller sweep and bounded pod
# reap over every selector in <selectors> (space-separated).
_teardown_release() {
  local release="$1" ns="$2" selectors="$3" force_pvc_pods="$4"
  local err detail rc=0 sel
  for sel in $selectors; do
    case "$sel" in
      @deploy:*) _snapshot_deployment_pods "$ns" "${sel#@deploy:}" ;;
    esac
  done
  if helm status "$release" --namespace "$ns" >/dev/null 2>&1; then
    _run_err err helm uninstall "$release" --namespace "$ns" \
      --wait --timeout "${RELEASE_TIMEOUT_SECS}s" || rc=$?
    if [[ "$rc" -eq 0 ]]; then
      info "Helm release '${release}' uninstalled."
    else
      detail="$(sanitize_remote_text "$err" 300)"
      warn "Helm uninstall of '${release}' did not complete (status ${rc}): ${detail:-no error output} — sweeping its controllers."
    fi
  else
    warn "Helm release '${release}' not found in namespace '${ns}' — skipping the Helm step."
  fi
  _sweep_controllers "$ns" "$selectors"
  _reap_pods "$ns" "$selectors" "$force_pvc_pods"
}

# _langfuse_wide_sweep_allowed — the guard for sweeping EVERY workload in the
# Langfuse namespace. The dev teardown treats that namespace as Langfuse-owned
# and deletes it wholesale, so pointing the variable at a shared namespace is
# unsupported; the guard keeps a misconfiguration from widening the sweep.
_langfuse_wide_sweep_allowed() {
  local ns="${LANGFUSE_NS:-}"
  [[ -n "$ns" ]] || return 1
  [[ "$ns" != "${NS:-}" && "$ns" != "${DATAHUB_NS:-}" && "$ns" != "${DUMMY_NS:-}" ]] || return 1
  case "$ns" in
    default|kube-system|kube-public|kube-node-lease|ingress-nginx) return 1 ;;
  esac
  return 0
}

# _pods_mounting_claim <namespace> <pvc> — pod names mounting the claim.
_pods_mounting_claim() {
  local ns="$1" pvc="$2" line out=""
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    if [[ ",${line#*|}" == *",${pvc},"* ]]; then
      out="${out}${out:+ }${line%%|*}"
    fi
  done <<< "$(_pod_lines "$ns" "")"
  printf '%s' "$out"
}

# _delete_pvc_bounded <pvc> <namespace>
# Returns 1 (and records the claim as unresolved) when the deletion does not
# complete within DELETE_TIMEOUT_SECS — typically the kubernetes.io/pvc-protection
# finalizer held by a pod that is still mounting it.
_delete_pvc_bounded() {
  local pvc="$1" ns="$2" err rc=0 mounts finalizers
  # No --request-timeout here, deliberately: kubectl applies it as the HTTP
  # client's overall timeout, which would also cut the --wait=true watch short at
  # that value instead of DELETE_TIMEOUT_SECS. The wait is bounded by --timeout;
  # the initial DELETE request and API dial are not separately bounded (the
  # cluster-reachability preflight is the only guard against an unreachable API
  # server before this point).
  _run_err err kubectl delete pvc "$pvc" -n "$ns" --ignore-not-found \
    --wait=true --timeout="${DELETE_TIMEOUT_SECS}s" || rc=$?
  if [[ "$rc" -eq 0 ]]; then
    info "  Deleted PVC '$(sanitize_remote_text "$pvc" 100)'."
    return 0
  fi
  mounts="$(_pods_mounting_claim "$ns" "$pvc")"
  finalizers="$(kubectl get pvc "$pvc" -n "$ns" --request-timeout="$REQUEST_TIMEOUT" \
    -o jsonpath='{.metadata.finalizers}' 2>/dev/null || true)"
  warn "  Could not delete PVC '$(sanitize_remote_text "$pvc" 100)' in '${ns}' within ${DELETE_TIMEOUT_SECS}s: $(sanitize_remote_text "$err" 200)"
  warn "    mounted by pod(s): $(sanitize_remote_text "${mounts:-none}" 200); finalizers: $(sanitize_remote_text "${finalizers:-none}" 200)"
  _note_unresolved "PVC ${ns}/${pvc} not deleted within ${DELETE_TIMEOUT_SECS}s; mounted by: ${mounts:-none}; finalizers: ${finalizers:-none}"
  return 1
}

# _read_namespace <namespace> <timeout-secs> <result-var>
# One bounded `kubectl get namespace --ignore-not-found -o name` read: stdout goes
# to <result-var>, the return value is kubectl's own status. Exit 0 with an empty
# result is the only "not found" signal; a non-zero status (error, unauthorized,
# request timeout) is "unknown", which callers must never read as absent or gone.
_read_namespace() {
  local __ns="$1" __secs="$2" __var="$3" __out __rc=0
  __out="$(kubectl get namespace "$__ns" --ignore-not-found -o name \
    --request-timeout="${__secs}s" 2>/dev/null)" || __rc=$?
  printf -v "$__var" '%s' "$__out"
  return "$__rc"
}

# _namespace_presence <namespace>
# The pre-deletion presence check, bounded by DELETE_TIMEOUT_SECS. Returns 0 when
# the namespace exists (a successful read with output), 1 when it is absent (a
# successful empty read), and 2 when the read failed — in which case the
# namespace is recorded as unresolved and warned about here, and the caller must
# not attempt the deletion.
_namespace_presence() {
  local ns="$1" out="" rc=0
  _read_namespace "$ns" "$DELETE_TIMEOUT_SECS" out || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    warn "Could not read namespace '$(sanitize_remote_text "$ns" 100)' within ${DELETE_TIMEOUT_SECS}s (kubectl exit ${rc}) — its deletion was not attempted."
    _note_unresolved "namespace ${ns} could not be read — deletion not attempted"
    return 2
  fi
  [[ -n "${out//[[:space:]]/}" ]] || return 1
  return 0
}

# _delete_namespace_bounded <namespace>
# Returns 1 (and records the namespace as unresolved, and for the closing
# re-check) on a timeout, naming its termination conditions.
_delete_namespace_bounded() {
  local target="$1" err rc=0 conditions
  # No --request-timeout, for the same reason as _delete_pvc_bounded: it would cap
  # the --wait=true watch. Bounded by --timeout only.
  _run_err err kubectl delete namespace "$target" --ignore-not-found \
    --wait=true --timeout="${DELETE_TIMEOUT_SECS}s" || rc=$?
  if [[ "$rc" -eq 0 ]]; then
    # Everything recorded as surviving inside it went with it.
    _drop_unresolved_in_ns "$target"
    return 0
  fi
  conditions="$(kubectl get namespace "$target" --request-timeout="$REQUEST_TIMEOUT" -o jsonpath='{range .status.conditions[*]}{.type}{": "}{.message}{"; "}{end}' 2>/dev/null || true)"
  warn "Namespace '${target}' not deleted within ${DELETE_TIMEOUT_SECS}s: $(sanitize_remote_text "$err" 200)"
  warn "  termination conditions: $(sanitize_remote_text "${conditions:-none reported}" 400)"
  _note_unresolved "namespace ${target} not deleted within ${DELETE_TIMEOUT_SECS}s; conditions: ${conditions:-none reported}"
  TIMED_OUT_NAMESPACES+=("$target")
  return 1
}

# _recheck_timed_out_namespaces — one more look at every namespace whose bounded
# deletion timed out, before the summary: it may merely have been finalizing
# (cloud load balancer release). Polls all of them within a single wall-clock
# window of DELETE_TIMEOUT_SECS (so each waits at most that long more, and a
# namespace's deletion at most two windows in total), each read bounded by the
# time left. A namespace counts as gone only on a successful not-found read
# (exit 0, empty output); it then loses its "not deleted" entry and every entry
# scoped to it. One that still exists, or whose read fails or times out, keeps
# its entries and so keeps the exit status non-zero.
_recheck_timed_out_namespaces() {
  [[ "${#TIMED_OUT_NAMESPACES[@]}" -gt 0 ]] || return 0
  local ns out rc remaining read_secs pending=() still=()
  local deadline=$(( SECONDS + DELETE_TIMEOUT_SECS ))
  pending=("${TIMED_OUT_NAMESPACES[@]}")
  TIMED_OUT_NAMESPACES=()
  info "Re-checking ${#pending[@]} namespace(s) whose deletion timed out (up to ${DELETE_TIMEOUT_SECS}s more)..."
  while :; do
    still=()
    for ns in "${pending[@]}"; do
      remaining=$(( deadline - SECONDS ))
      read_secs=15
      if (( remaining < read_secs )); then read_secs=$remaining; fi
      if (( read_secs < 1 )); then read_secs=1; fi
      out=""; rc=0
      _read_namespace "$ns" "$read_secs" out || rc=$?
      if [[ "$rc" -eq 0 && -z "${out//[[:space:]]/}" ]]; then
        info "Namespace '$(sanitize_remote_text "$ns" 100)' finished terminating after the timeout — no longer unresolved."
        _drop_unresolved_in_ns "$ns"
      else
        still+=("$ns")
      fi
    done
    pending=()
    if [[ "${#still[@]}" -gt 0 ]]; then
      pending=("${still[@]}")
    fi
    [[ "${#pending[@]}" -gt 0 ]] || return 0
    if (( SECONDS >= deadline )); then
      break
    fi
    sleep 1
  done
  for ns in "${pending[@]}"; do
    warn "Namespace '$(sanitize_remote_text "$ns" 100)' is still present or could not be read after the ${DELETE_TIMEOUT_SECS}s re-check."
  done
}

# _wait_pvs_gone — best-effort, bounded wait for the PVs that were bound to the
# claims this run deleted. A survivor is only a warning: a `Retain`
# StorageClass legitimately leaves the PV, so it is not part of UNRESOLVED.
_wait_pvs_gone() {
  [[ -n "${PV_WAIT_LIST// /}" ]] || return 0
  local deadline=$(( SECONDS + DELETE_TIMEOUT_SECS )) pv left found
  while :; do
    left=""
    for pv in $PV_WAIT_LIST; do
      # `--ignore-not-found`: empty output = gone; a failed read = unknown, which
      # is kept in the list rather than assumed gone.
      found="$(kubectl get pv "$pv" --ignore-not-found -o name \
        --request-timeout="$REQUEST_TIMEOUT" 2>/dev/null)" || found="?"
      if [[ -n "$found" ]]; then
        left="${left}${left:+ }${pv}"
      fi
    done
    [[ -z "$left" ]] && return 0
    if (( SECONDS >= deadline )); then
      warn "PV(s) retained or still releasing after ${DELETE_TIMEOUT_SECS}s: $(sanitize_remote_text "$left" 300) (a Retain StorageClass leaves them by design)."
      return 0
    fi
    sleep 1
  done
}

# _finish_teardown — re-checks timed-out namespaces once, then prints the closing
# summary; exits non-zero on a non-empty unresolved set.
_finish_teardown() {
  _recheck_timed_out_namespaces
  echo ""
  if [[ "${#UNRESOLVED[@]}" -gt 0 ]]; then
    local item
    warn "Uninstall finished with ${#UNRESOLVED[@]} unresolved item(s) that could not be removed:"
    for item in "${UNRESOLVED[@]}"; do
      warn "  - ${item}"
    done
    warn "Re-run this script after clearing the cause (the controller sweep is idempotent), or remove them manually."
    echo ""
    exit 1
  fi
  info "Uninstall complete."
  echo ""
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
PROFILE=""
ENV_FILE_ARG=""
COMPONENTS_CSV=""
NO_QUESTION=false
DELETE_PVCS=false
DELETE_NAMESPACES=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="${2:-}"; shift 2 ;;
    --env-file) ENV_FILE_ARG="${2:-}"; shift 2 ;;
    --components) COMPONENTS_CSV="${2:-}"; shift 2 ;;
    --no-question) NO_QUESTION=true; shift ;;
    --delete-pvcs) DELETE_PVCS=true; shift ;;
    --delete-namespaces) DELETE_NAMESPACES=true; shift ;;
    --delete-all) DELETE_PVCS=true; DELETE_NAMESPACES=true; shift ;;
    --help|-h) print_usage; exit 0 ;;
    *) error "Unknown option: $1 (use --help)" ;;
  esac
done

# After argument parsing so `--help` works on a host without kubectl/helm
# installed — matches install.sh's placement of the same check.
require_tools kubectl helm

if [[ -z "$PROFILE" ]]; then
  error "--profile {dev|prod} is required. Use --help for usage."
fi
if [[ "$PROFILE" != "dev" && "$PROFILE" != "prod" ]]; then
  error "Invalid profile '${PROFILE}'. Must be 'dev' or 'prod'."
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

echo ""
echo "=== Uninstalling DataSpoke (profile: ${PROFILE}) ==="
echo ""

# ---------------------------------------------------------------------------
# Targeted component teardown (--components)
# Only `frontend` is supported: it is an optional umbrella subchart, so teardown
# is a helm upgrade with frontend.enabled=false (not a `helm uninstall`). The api
# subchart is the core service (Airflow callbacks + seeding depend on it) and has
# no coherent partial teardown — stop it with `kubectl scale --replicas=0`.
# ---------------------------------------------------------------------------
if [[ -n "$COMPONENTS_CSV" ]]; then
  NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"
  case "$COMPONENTS_CSV" in
    frontend)
      use_context "${DATASPOKE_KUBE_CLUSTER}"
      _require_cluster_reachable "${DATASPOKE_KUBE_CLUSTER}"
      if ! helm status dataspoke --namespace "${NS}" >/dev/null 2>&1; then
        error "Helm release 'dataspoke' not found in namespace '${NS}' — nothing to do."
      fi
      if [[ "${NO_QUESTION}" != true ]]; then
        read -r -p "Disable the frontend (helm upgrade frontend.enabled=false) in '${NS}'? [y/N] " CONFIRM
        if [[ ! "${CONFIRM}" =~ ^[Yy]$ ]]; then
          info "Aborted — no changes made."
          exit 0
        fi
      fi
      info "Disabling frontend subchart (frontend.enabled=false)..."
      # This intentional fail-open path preserves the old frontend-disable
      # behavior, but invokes the resolver in this shell so its EXIT/INT/TERM
      # cleanup state remains reachable; a subshell could orphan its timeout
      # process group when the outer uninstaller is interrupted.
      if ! _build_chart_deps "$CHART_DIR"; then
        warn "helm dependency build for '${CHART_DIR}' failed or timed out — continuing; the helm upgrade below may fail if charts/ is stale or incomplete."
      fi
      helm upgrade dataspoke "$CHART_DIR" \
        --namespace "${NS}" \
        --reuse-values \
        --set frontend.enabled=false \
        --wait --timeout 120s
      kubectl wait --for=delete deployment/dataspoke-frontend -n "${NS}" --timeout=120s 2>/dev/null || true
      echo ""
      info "Frontend removed; other components untouched."
      info "Redeploy with: ./helm-charts/bin/install.sh --profile ${PROFILE} --components frontend"
      echo ""
      exit 0
      ;;
    api)
      error "uninstall --components api is unsupported: api is the core service (Airflow callbacks + seeding depend on it). To stop it temporarily: kubectl scale deployment/dataspoke-api --replicas=0 -n '${NS}'"
      ;;
    *)
      error "uninstall --components supports only 'frontend' (got '${COMPONENTS_CSV}'). Omit --components for a full teardown."
      ;;
  esac
fi

# ---------------------------------------------------------------------------
# Confirm before proceeding
# ---------------------------------------------------------------------------
if [[ "${NO_QUESTION}" != true ]]; then
  read -r -p "Remove all ${PROFILE} resources? [y/N] " CONFIRM
  if [[ ! "${CONFIRM}" =~ ^[Yy]$ ]]; then
    info "Aborted — no changes made."
    exit 0
  fi
fi

echo ""
use_context "${DATASPOKE_KUBE_CLUSTER}"
_require_cluster_reachable "${DATASPOKE_KUBE_CLUSTER}"
_resolve_teardown_timeouts

# ---------------------------------------------------------------------------
# DEV PROFILE — reverse install order
# ---------------------------------------------------------------------------
if [[ "$PROFILE" == "dev" ]]; then
  NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"
  DATAHUB_NS="${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE}"
  LANGFUSE_NS="${DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE}"
  DUMMY_NS="${DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE}"

  # 1. dev-lock
  info "Removing dev-lock resources..."
  for RESOURCE in deployment/dev-lock service/dev-lock configmap/dev-lock-script; do
    if kubectl get "${RESOURCE}" -n "${NS}" >/dev/null 2>&1; then
      kubectl delete "${RESOURCE}" -n "${NS}"
    fi
  done

  # 2. dummy-data
  info "Removing dummy-data resources..."
  if kubectl get namespace "${DUMMY_NS}" >/dev/null 2>&1; then
    PERIPHERALS_DIR="$(cd "$SCRIPT_DIR/../dev-peripherals" && pwd)"
    kubectl delete -f "$PERIPHERALS_DIR/dummy-data/manifests/" \
      --namespace "${DUMMY_NS}" --ignore-not-found=true || true
    if kubectl get secret/example-postgres-secret -n "${DUMMY_NS}" >/dev/null 2>&1; then
      kubectl delete secret/example-postgres-secret -n "${DUMMY_NS}"
    fi
  else
    info "Namespace '${DUMMY_NS}' does not exist — skipping dummy-data cleanup."
  fi

  # 3. dataspoke umbrella chart
  info "Removing DataSpoke umbrella Helm release..."
  # Best-effort Helm uninstall, then the controller sweep + bounded pod reap
  # (_teardown_release) — deleting pods alone would just have the surviving
  # controllers recreate them.
  _teardown_release dataspoke "${NS}" "${DATASPOKE_SELECTORS}" true
  # dataspoke-secrets (source of DATASPOKE_AIRFLOW_FERNET_KEY and
  # DATASPOKE_POSTGRES_PASSWORD) and its dataspoke-airflow-metadata-
  # encryption-key projection are deleted unconditionally below, while the
  # Postgres PVC — which holds the Fernet-encrypted Airflow connections/
  # Variables that key decrypts, and which was provisioned against the old
  # Postgres password — is retained by default (the PVC prompt is further
  # down, at step 7, and is skipped entirely under --no-question, which
  # retains by default). If you keep the PVC, the next install regenerates
  # the whole credential set (see HELM_CHART.md §Secrets Management) with no
  # live Fernet key to adopt in any carrier, permanently stranding that PVC's
  # encrypted rows — and a new Postgres password the retained PVC's user
  # doesn't have either way. Pass --delete-pvcs, or answer 'y' to the PVC
  # prompt when one is shown, for a clean dev reset.
  warn "Deleting dataspoke-secrets now — if you retain the Postgres PVC (default under"
  warn "--no-question, and the PVC prompt below defaults to No when shown), the next"
  warn "install regenerates the whole credential set — Fernet key AND Postgres password —"
  warn "permanently stranding that PVC's encrypted Airflow rows and its Postgres user."
  warn "Recommended: pass --delete-pvcs (or answer 'y' to the PVC prompt) for a clean reset."
  # Read before the loop below deletes dataspoke-secrets — the legacy-Secret
  # guard further down needs this value to decide whether
  # dataspoke-airflow-fernet-key is a safe-to-drop redundant copy.
  _contract_fernet_key=""
  if kubectl get secret dataspoke-secrets -n "${NS}" >/dev/null 2>&1; then
    _contract_fernet_key="$(kubectl get secret dataspoke-secrets -n "${NS}" \
      -o jsonpath='{.data.DATASPOKE_AIRFLOW_FERNET_KEY}' 2>/dev/null | base64 --decode 2>/dev/null || true)"
  fi
  for SECRET in dataspoke-secrets \
                dataspoke-airflow-metadata-db \
                dataspoke-airflow-api-secret-key \
                dataspoke-airflow-jwt-secret \
                dataspoke-airflow-metadata-encryption-key \
                dataspoke-llm-secret \
                dataspoke-datahub-secret \
                dataspoke-langfuse-secret; do
    if kubectl get secret "${SECRET}" -n "${NS}" >/dev/null 2>&1; then
      kubectl delete secret "${SECRET}" -n "${NS}"
    fi
  done

  # dataspoke-airflow-fernet-key is a different case from the Secrets above:
  # it is the Airflow subchart's own pre-install-hook Secret
  # (`hook-delete-policy: before-hook-creation`), so `helm uninstall` never
  # removes it, and it only exists on a cluster that ran a release before
  # airflow.fernetKeySecretName was pinned to dataspoke-airflow-metadata-
  # encryption-key. On such a cluster it may be the ONLY live carrier of the
  # Fernet key that decrypts the retained Postgres PVC's Airflow connections/
  # Variables (dataspoke-secrets predating the Fernet key joining the
  # credentials contract has no DATASPOKE_AIRFLOW_FERNET_KEY of its own to
  # compare against). Delete it
  # only when its value agrees with what dataspoke-secrets carries — a
  # redundant copy, safe to drop — otherwise leave it as a last-resort
  # adoption carrier for a future install and warn instead.
  _legacy_fernet_key=""
  if kubectl get secret dataspoke-airflow-fernet-key -n "${NS}" >/dev/null 2>&1; then
    _legacy_fernet_key="$(kubectl get secret dataspoke-airflow-fernet-key -n "${NS}" \
      -o jsonpath='{.data.fernet-key}' 2>/dev/null | base64 --decode 2>/dev/null || true)"
  fi
  if [[ -n "${_legacy_fernet_key}" ]]; then
    if [[ "${_legacy_fernet_key}" == "${_contract_fernet_key}" ]]; then
      kubectl delete secret dataspoke-airflow-fernet-key -n "${NS}"
    else
      warn "Leaving dataspoke-airflow-fernet-key in place — it disagrees with (or"
      warn "dataspoke-secrets no longer carries) DATASPOKE_AIRFLOW_FERNET_KEY, so it may hold"
      warn "the only live copy of the key that decrypts the retained Postgres PVC's Airflow"
      warn "connections/Variables. Delete it manually once you've confirmed the PVC no longer"
      warn "needs it: kubectl delete secret dataspoke-airflow-fernet-key -n ${NS}"
    fi
  fi

  # 4. Langfuse
  info "Removing Langfuse..."
  # Release label first; the widened sweep of every workload in the namespace
  # runs only when that leaves workloads behind AND the namespace passes
  # _langfuse_wide_sweep_allowed (spec §Bounded teardown). A guard-blocked
  # leftover is recorded as unresolved, never swept.
  _teardown_release langfuse "${LANGFUSE_NS}" "app.kubernetes.io/instance=langfuse" true
  _lf_rc=0
  _langfuse_left="$(_workload_names "${LANGFUSE_NS}" "")" || _lf_rc=$?
  if [[ "${_lf_rc}" -ne 0 && -z "${_langfuse_left}" ]]; then
    warn "Could not list workloads in '${LANGFUSE_NS}' to verify the Langfuse teardown."
    _note_unresolved "workloads in ${LANGFUSE_NS} could not be listed — Langfuse removal could not be verified"
  elif [[ -n "${_langfuse_left}" ]]; then
    if _langfuse_wide_sweep_allowed; then
      warn "Workloads outside the release label remain in '${LANGFUSE_NS}' — widening the sweep to every workload in that namespace."
      _sweep_controllers "${LANGFUSE_NS}" ""
      _reap_pods "${LANGFUSE_NS}" "" true
      _lf_rc=0
      _langfuse_left="$(_workload_names "${LANGFUSE_NS}" "")" || _lf_rc=$?
      if [[ "${_lf_rc}" -ne 0 && -z "${_langfuse_left}" ]]; then
        warn "Could not list workloads in '${LANGFUSE_NS}' after the widened sweep."
        _note_unresolved "workloads in ${LANGFUSE_NS} could not be listed — Langfuse removal could not be verified"
      else
        _note_unresolved_lines "workload ${LANGFUSE_NS}/" "${_langfuse_left}" " still present after the widened sweep"
      fi
    else
      warn "Workloads remain in '${LANGFUSE_NS}' but that namespace fails the widened-sweep guard — not sweeping it."
      _note_unresolved_lines "workload ${LANGFUSE_NS}/" "${_langfuse_left}" " left behind: widened sweep blocked by the Langfuse namespace guard"
    fi
  fi
  if kubectl get secret dataspoke-langfuse-secret -n "${LANGFUSE_NS}" >/dev/null 2>&1; then
    kubectl delete secret dataspoke-langfuse-secret -n "${LANGFUSE_NS}"
  fi

  # 5. DataHub
  info "Removing DataHub..."
  if helm status datahub --namespace "${DATAHUB_NS}" >/dev/null 2>&1; then
    helm uninstall datahub --namespace "${DATAHUB_NS}"
  else
    warn "Helm release 'datahub' not found — skipping."
  fi
  if helm status datahub-prerequisites --namespace "${DATAHUB_NS}" >/dev/null 2>&1; then
    helm uninstall datahub-prerequisites --namespace "${DATAHUB_NS}"
  else
    warn "Helm release 'datahub-prerequisites' not found — skipping."
  fi
  for RESOURCE in ingress/datahub-gms service/datahub-kafka-external secret/mysql-secrets; do
    if kubectl get "${RESOURCE}" -n "${DATAHUB_NS}" >/dev/null 2>&1; then
      kubectl delete "${RESOURCE}" -n "${DATAHUB_NS}"
    fi
  done

  # 6. nginx-ingress
  if [[ "$(ingress_mode)" == "shared" ]]; then
    info "Ingress mode: shared — leaving the pre-existing cluster ingress controller untouched."
  else
    info "Removing nginx-ingress controller..."
    if helm status ingress-nginx -n "ingress-nginx" >/dev/null 2>&1; then
      helm uninstall ingress-nginx -n "ingress-nginx"
    else
      warn "Helm release 'ingress-nginx' not found — skipping."
    fi
    _ns_presence=0
    _namespace_presence "ingress-nginx" || _ns_presence=$?
    if [[ "${_ns_presence}" -eq 0 ]]; then
      _delete_namespace_bounded "ingress-nginx" || true
    elif [[ "${_ns_presence}" -eq 1 ]]; then
      info "Namespace 'ingress-nginx' does not exist — skipping."
    fi
  fi

  # 7. Optionally delete PVCs (dataspoke + Langfuse)
  echo ""
  if [[ "${DELETE_PVCS}" != true && "${NO_QUESTION}" != true ]]; then
    warn "dataspoke-secrets is already gone — keeping the Postgres PVC now strands it on the"
    warn "old DATASPOKE_POSTGRES_PASSWORD and, unless dataspoke-airflow-fernet-key survived as"
    warn "a last-resort carrier above, its encrypted Airflow rows too. 'y' here is the clean"
    warn "dev reset."
    read -r -p "Delete PVCs in '${NS}' and '${LANGFUSE_NS}'? [y/N] " CONFIRM_PVC
    [[ "${CONFIRM_PVC}" =~ ^[Yy]$ ]] && DELETE_PVCS=true
  fi
  if [[ "${DELETE_PVCS}" == true ]]; then
    for PVC_NS_LABEL in "${NS}:app.kubernetes.io/instance=dataspoke" \
                        "${LANGFUSE_NS}:app.kubernetes.io/instance=langfuse"; do
      PVC_NS="${PVC_NS_LABEL%%:*}"
      PVC_LABEL="${PVC_NS_LABEL#*:}"
      info "Deleting PVCs in '${PVC_NS}' (label ${PVC_LABEL})..."
      _pvc_rc=0
      _pvc_names="$(kubectl get pvc -n "${PVC_NS}" -l "${PVC_LABEL}" \
        --request-timeout="${REQUEST_TIMEOUT}" \
        -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)" || _pvc_rc=$?
      if [[ "${_pvc_rc}" -ne 0 ]]; then
        warn "  Could not list PVCs in '${PVC_NS}' — their deletion could not be attempted."
        _note_unresolved "PVCs in ${PVC_NS} could not be listed — deletion not attempted"
        continue
      fi
      for pvc in ${_pvc_names}; do
        # Record the bound PV before the claim goes (best-effort PV wait below).
        _pv="$(kubectl get pvc "$pvc" -n "${PVC_NS}" --request-timeout="${REQUEST_TIMEOUT}" \
          -o jsonpath='{.spec.volumeName}' 2>/dev/null || true)"
        if _delete_pvc_bounded "$pvc" "${PVC_NS}"; then
          if [[ -n "${_pv}" ]]; then
            PV_WAIT_LIST="${PV_WAIT_LIST} ${_pv}"
          fi
        fi
      done
    done
  else
    info "PVCs retained."
  fi

  # 8. Optionally delete application namespaces
  NAMESPACES=("${DATAHUB_NS}" "${NS}" "${LANGFUSE_NS}" "${DUMMY_NS}")
  echo ""
  if [[ "${DELETE_NAMESPACES}" != true && "${NO_QUESTION}" != true ]]; then
    read -r -p "Delete namespaces (${NAMESPACES[*]})? [y/N] " CONFIRM_NS
    [[ "${CONFIRM_NS}" =~ ^[Yy]$ ]] && DELETE_NAMESPACES=true
  fi
  if [[ "${DELETE_NAMESPACES}" == true ]]; then
    _ns_failed=false
    for NS_TO_DEL in "${NAMESPACES[@]}"; do
      _ns_presence=0
      _namespace_presence "${NS_TO_DEL}" || _ns_presence=$?
      if [[ "${_ns_presence}" -eq 0 ]]; then
        info "Deleting namespace '${NS_TO_DEL}'..."
        _delete_namespace_bounded "${NS_TO_DEL}" || _ns_failed=true
      elif [[ "${_ns_presence}" -eq 1 ]]; then
        info "Namespace '${NS_TO_DEL}' does not exist — skipping."
      else
        _ns_failed=true   # unreadable: recorded as unresolved, deletion not attempted
      fi
    done
    if [[ "${_ns_failed}" == true ]]; then
      warn "Not every namespace was deleted — see the summary below."
    else
      info "Namespaces deleted."
    fi
  else
    info "Namespaces retained."
  fi

  # 9. PVs bound to the claims deleted above (best-effort, bounded).
  _wait_pvs_gone

# ---------------------------------------------------------------------------
# PROD PROFILE
# ---------------------------------------------------------------------------
elif [[ "$PROFILE" == "prod" ]]; then
  NS="${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"

  # Resolve the operator's existingSecret name from the live release's values
  # before uninstalling it — once the release is gone, "helm get values" is no
  # longer available, so this is the only place this script can learn that
  # name rather than hardcoding the chart default "dataspoke-secrets".
  SECRET_TO_CHECK="dataspoke-secrets"
  if helm status dataspoke --namespace "${NS}" >/dev/null 2>&1; then
    require_tools python3
    # `or {}` guards a release with no user-supplied overrides, where `helm
    # get values` prints bare `null` and `json.load(...)` returns `None` —
    # `d.get(...)` on `None` would otherwise raise `AttributeError`.
    _resolved_secret="$(helm get values dataspoke --namespace "${NS}" -o json 2>/dev/null \
      | python3 -c 'import json,sys; d=json.load(sys.stdin) or {}; print((d.get("secrets") or {}).get("existingSecret",""))' 2>/dev/null || true)"
    [[ -n "${_resolved_secret}" ]] && SECRET_TO_CHECK="${_resolved_secret}"
  fi

  info "Removing DataSpoke umbrella Helm release (prod)..."
  # Best-effort: a timeout here must not skip the Secret cleanup below. The
  # sweep is scoped to the release (release-identity labels, plus the API
  # Deployment by exact name — the prod namespace may hold operator-owned
  # objects), never touches PVCs, and never force-deletes a pod that mounts a
  # retained claim (force-pvc-pods=false).
  _teardown_release dataspoke "${NS}" "${DATASPOKE_SELECTORS}" false

  # Delete only the chart-derived Secrets; the operator-owned credentials
  # Secret is preserved. These four are projections of keys held in that
  # Secret, so deleting them is safe — the next install rebuilds them
  # byte-identically from the retained source.
  for SECRET in dataspoke-airflow-metadata-db \
                dataspoke-airflow-api-secret-key \
                dataspoke-airflow-jwt-secret \
                dataspoke-airflow-metadata-encryption-key; do
    if kubectl get secret "${SECRET}" -n "${NS}" >/dev/null 2>&1; then
      info "Deleting chart-derived Secret '${SECRET}'..."
      kubectl delete secret "${SECRET}" -n "${NS}"
    fi
  done

  # dataspoke-airflow-fernet-key is a different case: the Airflow subchart's
  # own pre-install-hook Secret (`hook-delete-policy: before-hook-creation`),
  # so `helm uninstall` above never removed it. It only exists on a cluster
  # that ran a release before airflow.fernetKeySecretName was pinned to
  # dataspoke-airflow-metadata-encryption-key on every install — and on that
  # cluster it may be the ONLY live carrier of the Fernet key that decrypts
  # the retained Postgres PVC's Airflow connections/Variables, if the
  # operator's pre-created Secret predates DATASPOKE_AIRFLOW_FERNET_KEY
  # joining the credentials contract. Delete it only when its value agrees with
  # what the operator's Secret carries — a redundant copy, safe to drop —
  # otherwise leave it in place and warn.
  _legacy_fernet_key=""
  if kubectl get secret dataspoke-airflow-fernet-key -n "${NS}" >/dev/null 2>&1; then
    _legacy_fernet_key="$(kubectl get secret dataspoke-airflow-fernet-key -n "${NS}" \
      -o jsonpath='{.data.fernet-key}' 2>/dev/null | base64 --decode 2>/dev/null || true)"
  fi
  _contract_fernet_key=""
  if kubectl get secret "${SECRET_TO_CHECK}" -n "${NS}" >/dev/null 2>&1; then
    _contract_fernet_key="$(kubectl get secret "${SECRET_TO_CHECK}" -n "${NS}" \
      -o jsonpath='{.data.DATASPOKE_AIRFLOW_FERNET_KEY}' 2>/dev/null | base64 --decode 2>/dev/null || true)"
  fi
  if [[ -n "${_legacy_fernet_key}" ]]; then
    if [[ "${_legacy_fernet_key}" == "${_contract_fernet_key}" ]]; then
      info "Deleting chart-derived Secret 'dataspoke-airflow-fernet-key'..."
      kubectl delete secret dataspoke-airflow-fernet-key -n "${NS}"
    else
      warn "Leaving dataspoke-airflow-fernet-key in place — it disagrees with (or"
      warn "'${SECRET_TO_CHECK}' no longer carries) DATASPOKE_AIRFLOW_FERNET_KEY, so it may"
      warn "hold the only live copy of the key that decrypts the retained Postgres PVC's"
      warn "Airflow connections/Variables. Delete it manually once you've confirmed the PVC"
      warn "no longer needs it: kubectl delete secret dataspoke-airflow-fernet-key -n ${NS}"
    fi
  fi
  info "Operator-owned Secret '${SECRET_TO_CHECK}' retained."

  # ---------------------------------------------------------------------------
  # Delete namespace? Ask BEFORE printing the retained-resources summary below —
  # printing a "here's what survives" list and then immediately asking "delete
  # the namespace?" invites a 'y' that destroys everything just listed. If the
  # namespace ends up deleted, the summary is moot (it takes the PVCs/Secrets
  # below with it) and is skipped entirely.
  # ---------------------------------------------------------------------------
  echo ""
  if [[ "${DELETE_NAMESPACES}" != true && "${NO_QUESTION}" != true ]]; then
    read -r -p "Delete namespace '${NS}'? [y/N] " CONFIRM_NS
    [[ "${CONFIRM_NS}" =~ ^[Yy]$ ]] && DELETE_NAMESPACES=true
  fi
  if [[ "${DELETE_NAMESPACES}" == true ]]; then
    _ns_presence=0
    _namespace_presence "${NS}" || _ns_presence=$?
    if [[ "${_ns_presence}" -eq 0 ]]; then
      if _delete_namespace_bounded "${NS}"; then
        info "Namespace '${NS}' deleted."
      fi
    elif [[ "${_ns_presence}" -eq 1 ]]; then
      info "Namespace '${NS}' does not exist — skipping."
    fi
  else
    info "Namespace '${NS}' retained."

    # -------------------------------------------------------------------------
    # Retained-resources summary (echo/info only — this uninstaller never
    # deletes PVCs in prod; --delete-pvcs is dev-only).
    # -------------------------------------------------------------------------
    echo ""
    info "Resources retained in '${NS}' after this uninstall:"
    info "  PVCs:"
    CORE_PVCS_FOUND=()
    for pvc in data-dataspoke-postgresql-0 \
               redis-data-dataspoke-redis-master-0 \
               redis-data-dataspoke-redis-replicas-0; do
      if kubectl get pvc "${pvc}" -n "${NS}" >/dev/null 2>&1; then
        CORE_PVCS_FOUND+=("${pvc}")
        SIZE="$(kubectl get pvc "${pvc}" -n "${NS}" -o jsonpath='{.spec.resources.requests.storage}' 2>/dev/null || echo '?')"
        info "    ${pvc}   ${SIZE}"
      else
        warn "    ${pvc} not found — expected to exist alongside a running install; check for a naming drift."
      fi
    done

    # Airflow log PVCs only exist when your overlay enables Airflow log
    # persistence (disabled in the shipped chart default — see
    # values-prod.example.yaml §Airflow log persistence) — probe first so the
    # common case (persistence off) prints nothing.
    LOG_PVCS=()
    for pvc in logs-dataspoke-airflow-scheduler-0 logs-dataspoke-airflow-triggerer-0; do
      if kubectl get pvc "${pvc}" -n "${NS}" >/dev/null 2>&1; then
        LOG_PVCS+=("${pvc}")
      fi
    done
    if [[ "${#LOG_PVCS[@]}" -gt 0 ]]; then
      info "  Also found Airflow log PVCs (present because your overlay enables Airflow log"
      info "  persistence — disabled in the shipped chart default):"
      for pvc in "${LOG_PVCS[@]}"; do
        SIZE="$(kubectl get pvc "${pvc}" -n "${NS}" -o jsonpath='{.spec.resources.requests.storage}' 2>/dev/null || echo '?')"
        info "    ${pvc}   ${SIZE}"
      done
      warn "  These hold retained task logs by design — delete only if you no longer need that"
      warn "  post-mortem history."
    fi

    info "  Secrets:"
    info "    ${SECRET_TO_CHECK} — operator-owned"
    for oob_secret in dataspoke-llm-secret dataspoke-datahub-secret \
                      dataspoke-langfuse-secret dataspoke-smtp-secret; do
      if kubectl get secret "${oob_secret}" -n "${NS}" >/dev/null 2>&1; then
        info "    ${oob_secret} — out-of-band, not managed by this script"
      fi
    done
    warn "  Coupled to the Postgres PVC above: '${SECRET_TO_CHECK}' carries"
    warn "  DATASPOKE_AIRFLOW_FERNET_KEY, which Airflow uses to decrypt the connections and"
    warn "  Variables stored in that PVC's metadata DB. Keep or drop the Secret and the PVC"
    warn "  together — changing DATASPOKE_AIRFLOW_FERNET_KEY in '${SECRET_TO_CHECK}' while the"
    warn "  PVC survives leaves that data permanently undecryptable. Re-running install.sh"
    warn "  against this still-live release aborts on a disagreeing key rather than silently"
    warn "  re-projecting it — this teardown deleted the projection Secret that comparison reads,"
    warn "  so a full uninstall/reinstall cycle trusts whatever DATASPOKE_AIRFLOW_FERNET_KEY"
    warn "  '${SECRET_TO_CHECK}' holds unchecked. Do not edit that key by hand between teardown"
    warn "  and reinstall while the PVC survives."

    info "  To delete manually:"
    if [[ "${#CORE_PVCS_FOUND[@]}" -gt 0 ]]; then
      info "    kubectl delete pvc ${CORE_PVCS_FOUND[*]} -n '${NS}'"
    fi
    if [[ "${#LOG_PVCS[@]}" -gt 0 ]]; then
      info "    kubectl delete pvc ${LOG_PVCS[*]} -n '${NS}'"
    fi
    info "    kubectl delete secret ${SECRET_TO_CHECK} -n '${NS}'"
    warn "  Deleting '${SECRET_TO_CHECK}' destroys the only copy of all 11 credentials unless"
    warn "  they also live in an external secrets manager, AND strands the Postgres PVC above if"
    warn "  you keep it (the running cluster still expects the old DATASPOKE_POSTGRES_PASSWORD and"
    warn "  DATASPOKE_AIRFLOW_FERNET_KEY). Delete the Secret only together with the PVCs above, or"
    warn "  not at all."
    info "  Or delete the namespace '${NS}' for a full wipe — the only sanctioned full teardown in prod."
    echo ""
  fi
fi

_finish_teardown
