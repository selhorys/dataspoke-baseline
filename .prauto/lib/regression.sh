# shellcheck shell=bash
# Include guard: this file defines top-level state, so re-sourcing it
# mid-run would reset that state to its startup values.
[[ -n "${PRAUTO_REGRESSION_SH_LOADED:-}" ]] && return 0
PRAUTO_REGRESSION_SH_LOADED=1
# Regression execution and result classification for prauto: running the test
# stages, parsing what failed, and deciding whether a failure is the branch's or
# the environment's.
# Source this file — do not execute directly.
# Requires: helpers.sh and dev-env.sh sourced, config loaded.
#
# The classification here gates whether a red run is retried or reported, so it
# fails closed: only an explicitly allowlisted, undisqualified signature counts
# as environmental, and anything ambiguous stays blocking.

# run_integration_groups <env_file>
# Run the pytest integration groups separately, spot then api-wired (TESTING.md
# mandates the split — mixing groups flakes on Airflow contention).
run_integration_groups() {
  local env_file="$1"
  INTEG_SPOT_EXIT=0; INTEG_SPOT_OUTPUT="tests/integration/spot/ not present — skipped."
  INTEG_API_WIRED_EXIT=0; INTEG_API_WIRED_OUTPUT="tests/integration/api_wired/ not present — skipped."

  if [[ -d "tests/integration/spot" ]]; then
    info "Running spot integration tests..."
    INTEG_SPOT_OUTPUT=$(ENV_FILE="$env_file" DATASPOKE_DEV_LOCK_PREACQUIRED=1 with_dev_env "$env_file" \
      uv run pytest tests/integration/spot/ --tb=short 2>&1) || INTEG_SPOT_EXIT=$?
  fi
  if [[ -d "tests/integration/api_wired" ]]; then
    info "Running api-wired integration tests..."
    INTEG_API_WIRED_OUTPUT=$(ENV_FILE="$env_file" DATASPOKE_DEV_LOCK_PREACQUIRED=1 with_dev_env "$env_file" \
      uv run pytest tests/integration/api_wired/ --tb=short 2>&1) || INTEG_API_WIRED_EXIT=$?
  fi

  INTEG_EXIT=0; INTEG_OUTPUT=""
  if [[ "$INTEG_SPOT_EXIT" -ne 0 ]]; then
    INTEG_EXIT=1
    INTEG_OUTPUT="=== Integration (spot) — exit ${INTEG_SPOT_EXIT} ===
$(tail_chars "$INTEG_SPOT_OUTPUT" 14000)"
  fi
  if [[ "$INTEG_API_WIRED_EXIT" -ne 0 ]]; then
    INTEG_EXIT=1
    INTEG_OUTPUT="${INTEG_OUTPUT}

=== Integration (api-wired) — exit ${INTEG_API_WIRED_EXIT} ===
$(tail_chars "$INTEG_API_WIRED_OUTPUT" 14000)"
  fi
}

# Flake signatures. Every grep reads a here-string rather than a pipe: under
# `pipefail`, `grep -q` exiting on its first match would SIGPIPE a large writer
# and turn a match into a miss.
#
# TRANSPORT_FLAKE_DISQUALIFIER_RE: assertions, contract/schema mismatches, and
# application response statuses. Any match keeps a failure blocking. Gateway
# statuses (502/503/504) are judged per failure instead, against
# TRANSPORT_FLAKE_GATEWAY_SOURCE_RE.
TRANSPORT_FLAKE_DISQUALIFIER_RE='assertionerror|assert .*failed|^E[[:space:]]+assert[[:space:]]|[[:space:]]-[[:space:]]assert[[:space:]]|^E[[:space:]]+Failed:[[:space:]]|expected .*(got|but)|^[[:space:]]*expected:|^[[:space:]]*received:|expect\(.*\)\.(to|not)|api mismatch|validationerror|(status[_ ]?code|http/[0-9.]+|<response \[|returned|status)[^0-9]{0,12}\b(400|401|403|404|409|422|500)\b'

# TRANSPORT_FLAKE_GATEWAY_SOURCE_RE: a 502/503/504 attributed to the ingress
# controller or the Kubernetes control plane. A failure reason carrying a
# gateway status without this source is not a transport flake.
TRANSPORT_FLAKE_GATEWAY_STATUS_RE='\b(502|503|504)\b'

TRANSPORT_FLAKE_GATEWAY_SOURCE_RE='(ingress-nginx|ingress controller|nginx).{0,40}\b(502|503|504)\b|\b(502|503|504)\b.{0,80}<center>nginx</center>|(kubernetes|kube-apiserver|apiserver|control[- ]plane|gke).{0,60}\b(502|503|504)\b'

# TRANSPORT_FLAKE_ALLOWLIST_RE: the closed spec allowlist. Client-side
# transport errors; control-plane-sourced statuses and timeouts; pod/node
# lifecycle events; ingress-controller-sourced gateway statuses.
TRANSPORT_FLAKE_ALLOWLIST_RE='econnreset|econnrefused|etimedout|eai_again|temporary failure in name resolution|upstream reset|connectionrefusederror|connection refused|connect call failed|connecterror|all connection attempts failed|(kubernetes|kube-apiserver|apiserver|control[- ]plane|gke).{0,60}(\b(429|500|502|503|504)\b|i/o timeout|context deadline exceeded|tls handshake timeout|connection reset)|pod.{0,80}\b(evicted|preempted)\b|reason:[[:space:]]*(evicted|preempted)\b|\bnodenotready\b|(ingress-nginx|ingress controller|nginx).{0,40}\b(502|503|504)\b|\b(502|503|504)\b.{0,80}<center>nginx</center>'

# is_environmental_transport_failure <text>
# True only when text matches the allowlist and nothing in it disqualifies.
# Unrecognized or ambiguous text (e.g. a bare gateway status with no ingress
# or control-plane source) stays blocking.
#
# The gateway-status gate below is NOT redundant with the allowlist, though it
# reads that way: every pattern it accepts also appears there. It is a veto, not
# a second acceptance. A reason mentioning an unattributed 502/503/504 stays
# blocking even when some other allowlist alternative matches it — "econnreset
# while polling; server later returned 503" is a real failure to investigate,
# not a transport flake to retry. Removing the gate silently reclassifies that
# whole shape as environmental.
is_environmental_transport_failure() {
  local text="$1"
  grep -Eqi "$TRANSPORT_FLAKE_DISQUALIFIER_RE" <<< "$text" && return 1
  if grep -Eqi "$TRANSPORT_FLAKE_GATEWAY_STATUS_RE" <<< "$text"; then
    grep -Eqi "$TRANSPORT_FLAKE_GATEWAY_SOURCE_RE" <<< "$text" || return 1
  fi
  grep -Eqi "$TRANSPORT_FLAKE_ALLOWLIST_RE" <<< "$text"
}

# transport_flake_category <text>
# Name the allowlisted category an (already qualifying) failure reason matched,
# for the public flake notice.
#
# Ordered most-specific mechanism first, because these signals co-occur. A
# cluster-sourced failure usually names the cluster too, so testing for the
# cluster first would report every such failure as a control-plane error — a GKE
# node whose DNS failed would be filed as "control-plane request failure", which
# points an operator at the wrong subsystem. What failed is more useful than
# where it failed, so the mechanism wins and the cluster is the fallback.
transport_flake_category() {
  local text="$1"
  if grep -Eqi 'evicted|preempted|nodenotready' <<< "$text"; then
    printf 'pod eviction/preemption or node not ready'
  elif grep -Eqi 'eai_again|temporary failure in name resolution' <<< "$text"; then
    printf 'DNS resolution failure'
  elif grep -Eqi '(ingress-nginx|ingress controller|nginx)' <<< "$text" \
    && grep -Eqi "$TRANSPORT_FLAKE_GATEWAY_STATUS_RE" <<< "$text"; then
    # Only an ingress-attributed gateway status is an ingress error; a bare
    # 502/503/504 alongside a cluster name is a control-plane one.
    printf 'ingress gateway error'
  elif grep -Eqi '(kubernetes|kube-apiserver|apiserver|control[- ]plane|gke)' <<< "$text"; then
    printf 'control-plane request failure'
  else
    printf 'client connection refused/reset/timeout'
  fi
}

# record_heartbeat_stage_pass <stage> <sha>
# Record an executor-observed passing cluster stage against the exact commit
# it ran on. Only a full 40-hex sha is recorded.
record_heartbeat_stage_pass() {
  local stage="$1" sha="$2"
  [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || return 0
  HEARTBEAT_STAGE_PASSES="${HEARTBEAT_STAGE_PASSES:-}${stage}"$'\t'"${sha}"$'\n'
}

# heartbeat_stage_passed_at <stage> <sha>
# True when this heartbeat's executor recorded <stage> passing at exactly <sha>.
heartbeat_stage_passed_at() {
  local stage="$1" sha="$2" entry_stage entry_sha
  [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || return 1
  while IFS=$'\t' read -r entry_stage entry_sha; do
    [[ "$entry_stage" == "$stage" && "$entry_sha" == "$sha" ]] && return 0
  done <<< "${HEARTBEAT_STAGE_PASSES:-}"
  return 1
}

# executor_test_head
# Print HEAD for a pass record or deployed-artifact binding, or nothing when the
# worktree has any modified or untracked file (then the sha would not describe
# what ran or was built). Nothing is excluded: test artifacts (pytest cache,
# Playwright reports/auth, node_modules) are gitignored and so never listed.
executor_test_head() {
  local head status
  head=$(git rev-parse HEAD 2>/dev/null) || return 0
  status=$(git status --porcelain --untracked-files=all 2>/dev/null) || return 0
  [[ -z "$status" ]] || return 0
  printf '%s' "$head"
}

# current_head
# Print HEAD, or nothing when it cannot be resolved.
current_head() {
  git rev-parse HEAD 2>/dev/null || true
}

# is_cluster_health_abort <output>
# True when an integration pytest session aborted at the conftest session-start
# health gate (require_server) before any test ran. That is an infrastructure
# condition, never a branch-attributable failure or a flake.
is_cluster_health_abort() {
  local output="$1"
  grep -Eq 'helm-charts/bin/health-check\.sh (failed \(exit|did not finish within)' <<< "$output"
}

# integration_health_abort_blocked <issue> <branch> <stage> <exit> <output>
# Returns 0 after releasing the required lock and posting the blocked notice
# when a non-zero integration run aborted at the harness health gate; the
# caller then records exit 2 and stops. Returns 1 otherwise.
integration_health_abort_blocked() {
  local issue_number="$1" branch="$2" stage="$3" exit_code="$4" output="$5"
  [[ "$exit_code" -ne 0 ]] || return 1
  is_cluster_health_abort "$output" || return 1
  release_required_dev_lock
  regression_blocked "$issue_number" "integration harness health gate failed during ${stage}" "$branch" || true
  return 0
}

# pytest clips short-summary reasons to the terminal width (80 columns when not
# a TTY). Regression runs widen it so every FAILED/ERROR line keeps the reason
# that the listing and per-failure flake classification read.
PYTEST_REPORT_COLUMNS=1000

# failed_test_output_kind <stage>
# Map a regression stage name to its output parser; prints nothing for a stage
# without test identifiers (deploys) or an unknown stage.
failed_test_output_kind() {
  case "$1" in
    "Unit (Python)"|"Integration (spot)"|"Integration (api-wired)") printf 'pytest' ;;
    "E2E") printf 'playwright' ;;
    "Static (ruff)") printf 'ruff' ;;
    "Static (mypy)") printf 'mypy' ;;
    "Static (frontend typecheck)"|"Static (E2E typecheck)") printf 'tsc' ;;
    "Static (frontend eslint)") printf 'eslint' ;;
    "Unit (frontend)") printf 'vitest' ;;
  esac
}

# parse_failed_test_entries <kind> <output>
# Shared parser for the PR listing and flake classification, so both see the
# same failures. Prints raw "E<TAB>identifier<TAB>reason" lines (pytest short
# summary FAILED/ERROR lines; Playwright numbered failure headers with their
# first Error: line; static-check diagnostics; Vitest failures) and
# "S<TAB>text" summary count lines. Only tabs are normalized and ANSI colour
# sequences stripped; no escaping happens here. sed and awk both consume their
# whole input, so the pipeline has no early-exit reader.
parse_failed_test_entries() {
  local kind="$1" output="$2"
  [[ -n "$kind" ]] || return 0
  # sed rather than ${var//}: bash pattern substitution is slow on multi-MB logs.
  # shellcheck disable=SC2001
  sed $'s/\x1b\\[[0-9;]*[A-Za-z]//g' <<< "$output" | awk -v kind="$kind" '
    function trim(s) { sub(/^[ \t]+/, "", s); sub(/[ \t]+$/, "", s); return s }
    function clean(s) { gsub(/\t/, " ", s); return trim(s) }
    function emit(id, reason) {
      id = clean(id)
      if (id == "" || seen[id]++) return
      printf "E\t%s\t%s\n", id, clean(reason)
    }
    function summary(s) { s = clean(s); if (s != "" && !seen_summary[s]++) printf "S\t%s\n", s }
    kind == "pytest" {
      if ($0 ~ /^(FAILED|ERROR) /) {
        line = $0
        sub(/^(FAILED|ERROR) /, "", line)
        i = index(line, " - ")
        if (i > 0) emit(substr(line, 1, i - 1), substr(line, i + 3)); else emit(line, "")
      } else if ($0 ~ /^=+ .*(failed|error|passed).* in [0-9.]+s/) {
        line = $0; gsub(/^=+ /, "", line); gsub(/ =+$/, "", line); summary(line)
      }
      next
    }
    kind == "playwright" {
      if ($0 ~ /^[ \t]*[0-9]+\) \[[^]]+\] › /) {
        if (pending != "") emit(pending, "")
        line = $0; sub(/^[ \t]*[0-9]+\) /, "", line); gsub(/[ \t]*(─)+[ \t]*$/, "", line)
        pending = line
      } else if (pending != "" && $0 ~ /^[ \t]*Error:/) {
        emit(pending, $0); pending = ""
      } else if ($0 ~ /^[ \t]*[0-9]+ (failed|flaky)/) {
        if (pending != "") { emit(pending, ""); pending = "" }
        summary($0)
      }
      next
    }
    kind == "ruff" {
      if ($0 ~ /^[^ \t]+:[0-9]+:[0-9]+: [A-Z]+[0-9]+ /) {
        i = index($0, ": "); emit(substr($0, 1, i - 1), substr($0, i + 2))
      } else if ($0 ~ /^[A-Z]+[0-9]+ /) {
        rule = $0
      } else if (rule != "" && $0 ~ /^[ \t]*--> [^ \t]+:[0-9]+:[0-9]+/) {
        line = $0; sub(/^[ \t]*--> /, "", line); emit(line, rule); rule = ""
      }
      next
    }
    kind == "mypy" {
      if ($0 ~ /^[^ \t]+:[0-9]+: error: /) {
        i = index($0, ": error: "); emit(substr($0, 1, i - 1), substr($0, i + 9))
      }
      next
    }
    kind == "tsc" {
      if ($0 ~ /^[^ \t]+\([0-9]+,[0-9]+\): error TS[0-9]+/) {
        i = index($0, "): error "); loc = substr($0, 1, i)
        sub(/\(/, ":", loc); sub(/,/, ":", loc); sub(/\)$/, "", loc)
        emit(loc, substr($0, i + 9))
      }
      next
    }
    kind == "eslint" {
      if ($0 ~ /^[ \t]*[0-9]+:[0-9]+[ \t]+(error|Error:)[ \t]/) {
        line = trim($0); split(line, parts, /[ \t]+/); pos = parts[1]
        sub(/^[0-9]+:[0-9]+[ \t]+(error|Error:)[ \t]+/, "", line)
        emit((file != "" ? file ":" : "") pos, line)
      } else if ($0 ~ /^[^ \t✖]/ && $0 !~ /^[0-9]+:[0-9]+/ && $0 !~ /^(>|info|warn|Warning|ESLint)/) {
        file = trim($0)
      }
      next
    }
    kind == "vitest" {
      if ($0 ~ /^[ \t]*(FAIL|×|✗) /) {
        if (pending != "") emit(pending, "")
        line = $0; sub(/^[ \t]*(FAIL|×|✗) +/, "", line); sub(/ [0-9]+ms$/, "", line)
        pending = line
      } else if (pending != "" && $0 ~ /^[ \t]*([A-Za-z]*Error:|→ )/) {
        line = $0; sub(/^[ \t]*→ /, "", line); emit(pending, line); pending = ""
      }
      next
    }
    END { if (pending != "") emit(pending, "") }
  '
}

# sanitize_failed_test_entries <parsed_entries>
# Neutralize already-scrubbed parser entries for markdown rendering. Invalid
# UTF-8 becomes U+FFFD. Identifiers are restricted to a conservative charset
# (anything else becomes '?'; '>' is kept for Vitest name paths and is inert
# inside a code span); reasons and summaries have backticks replaced so
# they stay inside their code spans. Every field is clipped to 200 characters
# (whole characters, never a split byte sequence).
sanitize_failed_test_entries() {
  local parsed="$1"
  # shellcheck disable=SC2016
  perl -MEncode=decode,encode -e '
    sub clip { my ($s) = @_; return length($s) > 200 ? substr($s, 0, 200) : $s }
    while (my $line = <STDIN>) {
      chomp $line;
      my ($tag, $first, $second) = split /\t/, $line, 3;
      next unless defined $tag && ($tag eq "E" || $tag eq "S");
      my $a = decode("UTF-8", $first // "");
      my $b = decode("UTF-8", $second // "");
      if ($tag eq "E") {
        $a =~ s{[^A-Za-z0-9_./:\[\]()=,+\- \@*>\x{203A}]}{?}g;
        $b =~ tr/`/\x27/;
        print encode("UTF-8", "E\t" . clip($a) . "\t" . clip($b)), "\n";
      } else {
        $a =~ tr/`/\x27/;
        print encode("UTF-8", "S\t" . clip($a)), "\n";
      }
    }
  ' <<< "$parsed"
}

# extract_failed_tests <stage> <output> [with_reasons]
# Print a bounded, secret-scrubbed markdown bullet list of the failing test
# identifiers in one stage's output: pytest node IDs, Playwright test titles,
# Vitest test names, or static-check diagnostics as path:line entries. With
# with_reasons=false (flake notices) only identifiers are listed. Raw entries
# are scrubbed before any neutralization or clipping, and the rendered block is
# scrubbed again. Identifiers, reasons, and summaries each sit in their own
# code span, so mentions, links, images, issue references, and HTML are inert.
FAILED_TESTS_MAX_ENTRIES=50

FAILED_TESTS_MAX_SUMMARIES=5

extract_failed_tests() {
  local stage="$1" output="$2" with_reasons="${3:-true}"
  local kind parsed=""
  case "$stage" in
    "Deploy (API)"|"Deploy (frontend)")
      printf '%s\n' "- (deploy stage; no test identifiers)"; return 0 ;;
  esac
  if ! command -v perl >/dev/null 2>&1; then
    printf '%s\n' "- Failing test identifiers withheld: the output sanitizer is unavailable."; return 0
  fi
  kind=$(failed_test_output_kind "$stage")
  if [[ -n "$kind" ]]; then
    parsed=$(parse_failed_test_entries "$kind" "$output")
    parsed=$(scrub_secrets "$parsed")
    parsed=$(sanitize_failed_test_entries "$parsed") || parsed=""
  fi

  local rendered="" total=0 shown=0 summaries="" summary_count=0 tag id reason
  while IFS=$'\t' read -r tag id reason; do
    case "$tag" in
      E)
        total=$((total + 1))
        [[ "$total" -le "$FAILED_TESTS_MAX_ENTRIES" ]] || continue
        shown=$((shown + 1))
        if [[ "$with_reasons" == true && -n "$reason" ]]; then
          rendered="${rendered}- \`${id}\` — \`${reason}\`"$'\n'
        elif [[ "$with_reasons" == true ]]; then
          rendered="${rendered}- \`${id}\` — no reason reported"$'\n'
        else
          rendered="${rendered}- \`${id}\`"$'\n'
        fi
        ;;
      S)
        summary_count=$((summary_count + 1))
        [[ "$summary_count" -le "$FAILED_TESTS_MAX_SUMMARIES" ]] || continue
        summaries="${summaries}- Summary: \`${id}\`"$'\n'
        ;;
    esac
  done <<< "$parsed"

  if [[ "$shown" -eq 0 ]]; then
    rendered="- No failing test identifiers could be extracted from this stage's output."$'\n'
  elif [[ "$total" -gt "$shown" ]]; then
    rendered="${rendered}- … and $((total - shown)) more"$'\n'
  fi
  rendered="${rendered}${summaries}"
  scrub_secrets "${rendered%$'\n'}"
}

# render_failed_tests_block <stage> <exit_code> <output> <with_reasons>
# One collapsible block per stage. The heading is built only from the fixed
# internal stage name and a numeric exit code (empty for identifier-only flake
# listings); it never carries branch or output text.
render_failed_tests_block() {
  local stage="$1" exit_code="$2" output="$3" with_reasons="$4" heading
  heading="$stage"
  [[ "$exit_code" =~ ^[0-9]+$ ]] && heading="${stage} — failed (exit ${exit_code})"
  printf '<details><summary>%s</summary>\n\n%s\n\n</details>' "$heading" "$(extract_failed_tests "$stage" "$output" "$with_reasons")"
}

# truncate_failed_tests_block <block> <max_chars>
# Clip a single rendered block by whole lines so it fits max_chars, keeping the
# opening <details> line and closing </details> tag.
truncate_failed_tests_block() {
  local block="$1" max_chars="$2" out="" line first=true
  local footer=$'- Listing truncated (size limit).\n\n</details>'
  [[ ${#block} -le $max_chars ]] && { printf '%s' "$block"; return 0; }
  while IFS= read -r line; do
    if [[ "$first" == true ]]; then out="$line"; first=false; continue; fi
    [[ "$line" == "</details>" ]] && break
    (( ${#out} + ${#line} + 1 + ${#footer} + 1 <= max_chars )) || break
    out="${out}"$'\n'"${line}"
  done <<< "$block"
  printf '%s\n%s' "$out" "$footer"
}

# append_failed_tests_block <var_name> <block>
# Append one rendered stage block to an accumulator, bounded so a PR comment
# stays within size limits. A first block larger than the cap is clipped by
# whole lines; later blocks are appended whole (keeping <details> tags
# balanced) and, once the cap would be exceeded, a single omission line is
# added instead.
FAILED_TESTS_DETAILS_MAX_CHARS=20000

append_failed_tests_block() {
  local var_name="$1" block="$2" current marker="- Further failed-stage listings omitted (size limit)."
  current="${!var_name:-}"
  if [[ -z "$current" ]]; then
    printf -v "$var_name" '%s' "$(truncate_failed_tests_block "$block" "$FAILED_TESTS_DETAILS_MAX_CHARS")"
    return 0
  fi
  if [[ $(( ${#current} + ${#block} )) -gt "$FAILED_TESTS_DETAILS_MAX_CHARS" ]]; then
    case "$current" in
      *"$marker") ;;
      *) printf -v "$var_name" '%s\n\n%s' "$current" "$marker" ;;
    esac
    return 0
  fi
  printf -v "$var_name" '%s\n\n%s' "$current" "$block"
}

# stage_failures_are_transport_flakes <stage> <output>
# Per-failure, fail-closed flake signature check for a cluster stage. The
# whole output must carry no disqualifier, at least one failure must be
# extracted (pytest short-summary FAILED/ERROR lines, or Playwright failure
# headers), and every extracted failure's own reason must match the allowlist
# without a disqualifier.
stage_failures_are_transport_flakes() {
  local stage="$1" output="$2" kind entries tag id reason count=0
  case "$stage" in
    "Integration (spot)"|"Integration (api-wired)") kind=pytest ;;
    "E2E") kind=playwright ;;
    *) return 1 ;;
  esac
  grep -Eqi "$TRANSPORT_FLAKE_DISQUALIFIER_RE" <<< "$output" && return 1
  entries=$(parse_failed_test_entries "$kind" "$output")
  while IFS=$'\t' read -r tag id reason; do
    [[ "$tag" == E ]] || continue
    count=$((count + 1))
    [[ -n "$reason" ]] || return 1
    is_environmental_transport_failure "$reason" || return 1
  done <<< "$entries"
  [[ "$count" -gt 0 ]]
}

# stage_transport_flake_categories <stage> <output>
# Distinct allowlisted categories across a qualifying stage's failure reasons.
stage_transport_flake_categories() {
  local stage="$1" output="$2" kind entries tag id reason category categories=""
  case "$stage" in
    "Integration (spot)"|"Integration (api-wired)") kind=pytest ;;
    "E2E") kind=playwright ;;
    *) return 0 ;;
  esac
  entries=$(parse_failed_test_entries "$kind" "$output")
  while IFS=$'\t' read -r tag id reason; do
    [[ "$tag" == E && -n "$reason" ]] || continue
    category=$(transport_flake_category "$reason")
    case "|${categories}|" in
      *"|${category}|"*) ;;
      *) categories="${categories:+${categories}|}${category}" ;;
    esac
  done <<< "$entries"
  printf '%s' "${categories//|/, }"
}

# record_post_pr_flake_classification <stage> <output>
# A failed cluster stage is an environmental flake only when every failure is
# an allowlisted transport signature AND this heartbeat's executor recorded the
# same stage passing at exactly the commit the post-PR regression tested.
# Changed paths are never a basis: the initial full regression has no
# preceding fix.
record_post_pr_flake_classification() {
  local stage="$1" output="$2"
  case "$stage" in
    "Integration (spot)"|"Integration (api-wired)"|"E2E")
      if stage_failures_are_transport_flakes "$stage" "$output" && \
         heartbeat_stage_passed_at "$stage" "${POST_PR_REGRESSION_HEAD:-}"; then
        POST_PR_FLAKE_STAGES="${POST_PR_FLAKE_STAGES:+${POST_PR_FLAKE_STAGES}, }${stage}"
        POST_PR_FLAKE_CATEGORIES="${POST_PR_FLAKE_CATEGORIES:+${POST_PR_FLAKE_CATEGORIES}; }${stage}: $(stage_transport_flake_categories "$stage" "$output")"
        return 0
      fi
      ;;
  esac
  POST_PR_NON_FLAKE_STAGES="${POST_PR_NON_FLAKE_STAGES:+${POST_PR_NON_FLAKE_STAGES}, }${stage}"
  return 1
}

# post_result <branch> <stage> <exit> <output>
post_result() {
  local branch="$1" stage="$2" exit_code="$3" output="$4"
  # Full post-PR regression deliberately emits one concise status comment per
  # pushed head.  Keep the per-stage output available to the local worker
  # process, but do not expose it in a series of public PR comments.
  if [[ "${POST_PR_REGRESSION_SUMMARY_MODE:-false}" == true ]]; then
    if [[ "$exit_code" -ne 0 ]]; then
      case "|${POST_PR_FAILED_STAGES:-}|" in
        *"|${stage}|"*) ;;
        *) POST_PR_FAILED_STAGES="${POST_PR_FAILED_STAGES:+${POST_PR_FAILED_STAGES}, }${stage}" ;;
      esac
      # Keep stage-specific, bounded evidence for the one post-regression fix
      # session.  It is intentionally local-only: public PR status comments
      # name failed stages but never expose raw test output.
      POST_PR_FAILURE_EVIDENCE="${POST_PR_FAILURE_EVIDENCE:+${POST_PR_FAILURE_EVIDENCE}

}=== ${stage} (exit ${exit_code}) ===
$(tail_chars "$output" 14000)"
      # The public listing carries only extracted, scrubbed identifiers and
      # one-line reasons; flake notices carry identifiers only.
      append_failed_tests_block POST_PR_FAILED_TEST_DETAILS \
        "$(render_failed_tests_block "$stage" "$exit_code" "$output" true)"
      if record_post_pr_flake_classification "$stage" "$output"; then
        append_failed_tests_block POST_PR_FLAKE_TEST_IDS \
          "$(render_failed_tests_block "$stage" "" "$output" false)"
      fi
    fi
    return 0
  fi
  get_pr_number_for_branch "$branch"
  [[ -n "$BRANCH_PR_NUMBER" ]] && post_test_results_comment "$BRANCH_PR_NUMBER" "$stage" "$exit_code" "$output"
}

# stage_is_recorded <stage>
# POST_PR_FAILED_STAGES is a human-readable comma-separated list assembled by
# post_result.  Match whole entries so similarly named stages cannot select one
# another by accident.
stage_is_recorded() {
  local stage="$1" entry
  local -a _prauto_stage_entries
  IFS=',' read -r -a _prauto_stage_entries <<< "${POST_PR_FAILED_STAGES:-}"
  # An empty array expansion is unbound under `set -u` in bash 3.2.
  [[ "${#_prauto_stage_entries[@]}" -gt 0 ]] || return 1
  for entry in "${_prauto_stage_entries[@]}"; do
    entry="${entry# }"; entry="${entry% }"
    [[ "$entry" == "$stage" ]] && return 0
  done
  return 1
}

# require_pushed_head <branch>
# Targeted verification must test precisely the head the executor published,
# never an unpushed worker commit or an asynchronously changed remote ref.
require_pushed_head() {
  local branch="$1" local_head remote_head
  local_head=$(git rev-parse HEAD 2>/dev/null || printf '')
  remote_head=$(git ls-remote origin "refs/heads/${branch}" 2>/dev/null | awk 'NR == 1 { print $1 }')
  if [[ -z "$local_head" || -z "$remote_head" || "$local_head" != "$remote_head" ]]; then
    warn "Targeted regression refuses to run: local HEAD and origin/${branch} do not match."
    return 1
  fi
  return 0
}

# targeted_verification_json <agent_output>
# Extract only an explicitly prefixed, single-line JSON record. Agent prose and
# untrusted test output are never parsed as verification authority.
targeted_verification_json() {
  local agent_output="$1"
  printf '%s\n' "$agent_output" | sed -n 's/^PRAUTO_TARGETED_VERIFICATION_JSON: //p' | tail -n 1
}

# validate_targeted_verification <agent_output> <expected_revision>
# Accept a worker attestation only when it is well-formed, names exactly the
# originally failed stages, reports pass for each, and binds them to the local
# committed revision which the executor subsequently pushes and re-verifies.
validate_targeted_verification() {
  local agent_output="$1" expected_revision="$2" payload stage expected_count=0 actual_count
  local -a _prauto_expected_stage_entries
  payload=$(targeted_verification_json "$agent_output")
  [[ -n "$payload" ]] || return 1
  jq -e --arg revision "$expected_revision" '
    (.revision == $revision)
    and (.stages | type == "array")
    and all(.stages[]; (.name | type == "string" and length > 0)
        and (.outcome == "pass")
        and (.evidence_ref | type == "string" and length > 0))
  ' >/dev/null 2>&1 <<< "$payload" || return 1
  actual_count=$(jq '.stages | length' <<< "$payload" 2>/dev/null) || return 1
  local stage_entry
  IFS=',' read -r -a _prauto_expected_stage_entries <<< "${POST_PR_FAILED_STAGES:-}"
  # An attestation over zero recorded stages verifies nothing (and an empty
  # array expansion is unbound under `set -u` in bash 3.2).
  [[ "${#_prauto_expected_stage_entries[@]}" -gt 0 ]] || return 1
  for stage_entry in "${_prauto_expected_stage_entries[@]}"; do
    stage_entry="${stage_entry# }"; stage_entry="${stage_entry% }"
    [[ -n "$stage_entry" ]] || continue
    expected_count=$((expected_count + 1))
    jq -e --arg stage "$stage_entry" '[.stages[] | select(.name == $stage)] | length == 1' \
      >/dev/null 2>&1 <<< "$payload" || return 1
  done
  [[ "$expected_count" -gt 0 ]] || return 1
  [[ "$actual_count" -eq "$expected_count" ]]
}

# post_post_pr_regression_comment <branch> <body>
# Regression status belongs to the PR conversation, not its linked issue.
# The body is caller-supplied summary text. Beyond fixed status wording it may
# carry only the bounded, secret-scrubbed failed-test listing produced by
# extract_failed_tests and the sanitized targeted evidence from
# targeted_failure_comment_evidence — never raw command output. The body is
# passed through a private temporary file, not argv, so it is neither exposed
# in the process table nor limited by argument size.
post_post_pr_regression_comment() {
  local branch="$1" body="$2" body_dir rc=0
  get_pr_number_for_branch "$branch"
  if [[ -z "${BRANCH_PR_NUMBER:-}" ]]; then
    warn "No PR found for branch ${branch}; could not post regression status."
    return 1
  fi
  if ! body_dir=$(mktemp -d); then
    warn "Could not create a temp dir for the regression status on PR #${BRANCH_PR_NUMBER}."
    return 1
  fi
  chmod 700 "$body_dir" 2>/dev/null || true
  if ! printf '%s%s' "$(prauto_comment_prefix)" "$body" > "${body_dir}/body.md"; then
    rm -rf "$body_dir"
    warn "Could not write the regression status body for PR #${BRANCH_PR_NUMBER}."
    return 1
  fi
  gh pr comment "$BRANCH_PR_NUMBER" -R "$PRAUTO_GITHUB_REPO" \
    --body-file "${body_dir}/body.md" 2>/dev/null || rc=$?
  rm -rf "$body_dir"
  if [[ "$rc" -ne 0 ]]; then
    warn "Failed to post regression status on PR #${BRANCH_PR_NUMBER}."
    return 1
  fi
  return 0
}

# --- Local regression stages -------------------------------------------------
#
# The static and unit checks run in two places: the full regression, which
# selects by what the diff touched, and the targeted retry, which selects by
# what previously failed. Those differ only in how a stage is SELECTED and where
# its result is RECORDED — the commands are identical. Holding them in one table
# with one driver is what stops the two paths drifting apart, which they had.
#
# Stage names are the public identifiers: they appear in PR status comments and
# in POST_PR_FAILED_STAGES, and stage_is_recorded matches them whole. Changing
# one renames a stage everywhere, including in a retry reading an older list.
LOCAL_REGRESSION_STAGES=(
  "Static (ruff)"
  "Static (mypy)"
  "Static (frontend typecheck)"
  "Static (frontend eslint)"
  "Static (E2E typecheck)"
  "Unit (Python)"
  "Unit (frontend)"
)

# local_stage_command <stage>
# Run one stage. Output goes to stdout; the exit status is the stage's.
local_stage_command() {
  case "$1" in
    "Static (ruff)")                uv run ruff check src/ tests/ 2>&1 ;;
    "Static (mypy)")                uv run mypy src/ 2>&1 ;;
    "Static (frontend typecheck)")  pnpm -C src/frontend exec tsc --noEmit 2>&1 ;;
    "Static (frontend eslint)")     pnpm -C src/frontend run lint 2>&1 ;;
    "Static (E2E typecheck)")       pnpm -C tests/e2e typecheck 2>&1 ;;
    "Unit (Python)")                COLUMNS="$PYTEST_REPORT_COLUMNS" uv run pytest tests/unit/ --tb=short 2>&1 ;;
    "Unit (frontend)")              pnpm -C src/frontend test 2>&1 ;;
    *) warn "Unknown local regression stage: $1"; return 2 ;;
  esac
}

# local_stage_applies <stage>
# The full regression's selector: run a stage when the diff reaches what it
# covers. The Python checks always apply; the frontend and E2E ones are scoped
# to their trees so an unrelated backend change does not pay for them.
local_stage_applies() {
  case "$1" in
    "Static (frontend typecheck)"|"Static (frontend eslint)"|"Unit (frontend)")
      diff_touches src/frontend/ ;;
    "Static (E2E typecheck)")
      diff_touches tests/e2e/ ;;
    *) return 0 ;;
  esac
}

# run_local_stages <selector_fn> <recorder_fn>
# Drive the table: for each stage the selector accepts, run it and hand the
# result to the recorder. Returns 1 if any stage failed, 0 otherwise.
#
# The recorder is called as <recorder_fn> <stage> <exit_code> <output>, so both
# callers supply a wrapper with that shape rather than the driver knowing about
# branches or targeted-result bookkeeping.
run_local_stages() {
  local selector="$1" recorder="$2"
  local stage exit_code output rc=0
  for stage in "${LOCAL_REGRESSION_STAGES[@]}"; do
    "$selector" "$stage" || continue
    exit_code=0
    output=$(local_stage_command "$stage") || exit_code=$?
    "$recorder" "$stage" "$exit_code" "$output"
    [[ "$exit_code" -eq 0 ]] || rc=1
  done
  return "$rc"
}

# run_static_and_unit_regression <branch>
# Sets LOCAL_REGRESSION_EXIT.  Commands are checks only; none mutates the diff.
run_static_and_unit_regression() {
  local branch="$1" output

  LOCAL_REGRESSION_EXIT=0
  [[ -f pyproject.toml ]] || { LOCAL_REGRESSION_EXIT=2; LOCAL_REGRESSION_REASON="pyproject.toml is missing"; return 0; }
  output=$(uv sync 2>&1) || { LOCAL_REGRESSION_EXIT=2; LOCAL_REGRESSION_REASON="uv sync failed"; return 0; }
  [[ -d tests/unit ]] || { LOCAL_REGRESSION_EXIT=2; LOCAL_REGRESSION_REASON="tests/unit is missing"; return 0; }

  # post_result takes the branch first; the driver calls recorders as
  # <stage> <exit> <output>, so close over the branch here.
  _record_full_stage() { post_result "$branch" "$1" "$2" "$3"; }

  LOCAL_REGRESSION_EXIT=0
  run_local_stages local_stage_applies _record_full_stage || LOCAL_REGRESSION_EXIT=1
}

# run_full_cluster_regression <issue> <branch>
# Acquires separately for integration and E2E, enforcing API deploy -> spot ->
# api-wired -> frontend deploy -> E2E ordering. Sets CLUSTER_REGRESSION_EXIT: 0 pass, 1 branch
# failure, 2 infrastructure/setup block.
run_full_cluster_regression() {
  local issue_number="$1" branch="$2" output exit_code=0
  CURRENT_REGRESSION_BRANCH="$branch"
  CLUSTER_REGRESSION_EXIT=0
  [[ -d tests/integration/spot && -d tests/integration/api_wired && -d tests/e2e ]] || {
    CLUSTER_REGRESSION_EXIT=2; regression_blocked "$issue_number" "required integration or E2E test directory is missing" "$branch"; return 0; }
  if ! acquire_required_dev_lock "$issue_number" "full regression"; then CLUSTER_REGRESSION_EXIT=2; return 0; fi
  # acquire_required_dev_lock has just completed the required pre-test health
  # check. Preserve that executor-owned fact for possible flake classification.
  POST_PR_FLAKE_HEALTH_BEFORE=true

  if ! deploy_branch_api "$DEV_ENV_FILE"; then
    release_required_dev_lock
    if [[ "$DEPLOY_FAILURE_KIND" == "infrastructure" ]]; then
      CLUSTER_REGRESSION_EXIT=2; regression_blocked "$issue_number" "API deploy could not reach or operate the development environment" "$branch"
    else
      CLUSTER_REGRESSION_EXIT=1; post_result "$branch" "Deploy (API)" 1 "Branch API build/deploy failed."
    fi
    return 0
  fi
  # A session aborted at the integration harness health gate ran no test: it is
  # infrastructure-blocked and ends this regression without recording a stage.
  exit_code=0; output=$(COLUMNS="$PYTEST_REPORT_COLUMNS" ENV_FILE="$DEV_ENV_FILE" DATASPOKE_DEV_LOCK_PREACQUIRED=1 with_dev_env "$DEV_ENV_FILE" uv run pytest tests/integration/spot/ --tb=short 2>&1) || exit_code=$?
  if integration_health_abort_blocked "$issue_number" "$branch" "Integration (spot)" "$exit_code" "$output"; then CLUSTER_REGRESSION_EXIT=2; return 0; fi
  post_result "$branch" "Integration (spot)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || CLUSTER_REGRESSION_EXIT=1
  exit_code=0; output=$(COLUMNS="$PYTEST_REPORT_COLUMNS" ENV_FILE="$DEV_ENV_FILE" DATASPOKE_DEV_LOCK_PREACQUIRED=1 with_dev_env "$DEV_ENV_FILE" uv run pytest tests/integration/api_wired/ --tb=short 2>&1) || exit_code=$?
  if integration_health_abort_blocked "$issue_number" "$branch" "Integration (api-wired)" "$exit_code" "$output"; then CLUSTER_REGRESSION_EXIT=2; return 0; fi
  post_result "$branch" "Integration (api-wired)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || CLUSTER_REGRESSION_EXIT=1
  # E2E reset-seeds independently and frontend deployment rolls the API, so it
  # must acquire after the integration group has fully released its lock.
  release_required_dev_lock
  if ! acquire_required_dev_lock "$issue_number" "full regression E2E"; then CLUSTER_REGRESSION_EXIT=2; return 0; fi
  if ! deploy_branch_frontend "$DEV_ENV_FILE"; then
    release_required_dev_lock
    if [[ "$DEPLOY_FAILURE_KIND" == "infrastructure" ]]; then
      CLUSTER_REGRESSION_EXIT=2; regression_blocked "$issue_number" "frontend deploy could not reach or operate the development environment" "$branch"
    else
      CLUSTER_REGRESSION_EXIT=1; post_result "$branch" "Deploy (frontend)" 1 "Branch frontend build/deploy failed."
    fi
    return 0
  fi
  if ! pnpm -C tests/e2e install --frozen-lockfile >/dev/null 2>&1 || ! pnpm -C tests/e2e exec playwright install chromium >/dev/null 2>&1; then
    release_required_dev_lock; CLUSTER_REGRESSION_EXIT=2; regression_blocked "$issue_number" "E2E runner/browser setup failed" "$branch"; return 0
  fi
  exit_code=0; output=$(ENV_FILE="$DEV_ENV_FILE" DATASPOKE_DEV_LOCK_PREACQUIRED=1 with_dev_env "$DEV_ENV_FILE" pnpm -C tests/e2e test 2>&1) || exit_code=$?
  post_result "$branch" "E2E" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || CLUSTER_REGRESSION_EXIT=1
  release_required_dev_lock
}

# record_targeted_result <stage> <exit> <output>
# Unlike the initial full regression recorder, this deliberately distinguishes
# successful targeted retries in the final PR status.
record_targeted_result() {
  local stage="$1" exit_code="$2" output="$3"
  if [[ "$exit_code" -eq 0 ]]; then
    POST_PR_TARGETED_PASSES="${POST_PR_TARGETED_PASSES:+${POST_PR_TARGETED_PASSES}, }${stage}"
  else
    POST_PR_TARGETED_FAILURES="${POST_PR_TARGETED_FAILURES:+${POST_PR_TARGETED_FAILURES}, }${stage}"
    POST_PR_TARGETED_EVIDENCE="${POST_PR_TARGETED_EVIDENCE:+${POST_PR_TARGETED_EVIDENCE}

}=== ${stage} (exit ${exit_code}) ===
$(tail_chars "$output" 14000)"
    append_failed_tests_block POST_PR_TARGETED_FAILED_TEST_DETAILS \
      "$(render_failed_tests_block "$stage" "$exit_code" "$output" true)"
  fi
}

# Render bounded, secret-scrubbed executor evidence for a public PR comment.
targeted_failure_comment_evidence() {
  local safe body max_chars=12000
  # Scrub the complete evidence before truncating, so a cut can never split a
  # secret into an unrecognizable fragment; drop the partial first line the
  # cut leaves, then scrub the bounded text again.
  safe=$(scrub_secrets "${POST_PR_TARGETED_EVIDENCE:-}")
  if [[ ${#safe} -gt $max_chars ]]; then
    body="${safe: -max_chars}"
    [[ "$body" == *$'\n'* ]] && body="${body#*$'\n'}"
    safe="(truncated — last ${#body} characters)"$'\n'"${body}"
  fi
  safe=$(scrub_secrets "$safe")
  safe=$(printf '%s' "$safe" | sed 's/```/`&#8203;``/g')
  printf '%s' "$safe"
}

# run_targeted_post_pr_regression <issue> <branch>
# Re-run only stages which failed the initial full regression.  Cluster stages
# reacquire the lock and rebuild their prerequisite artifact from the exact
# pushed branch head.  Sets TARGETED_REGRESSION_EXIT: 0 pass, 1 code failure,
# 2 setup/infrastructure block.
run_targeted_post_pr_regression() {
  local issue_number="$1" branch="$2" output exit_code=0 targeted_head
  TARGETED_REGRESSION_EXIT=0
  POST_PR_TARGETED_PASSES=""; POST_PR_TARGETED_FAILURES=""; POST_PR_TARGETED_EVIDENCE=""
  POST_PR_TARGETED_FAILED_TEST_DETAILS=""
  CURRENT_REGRESSION_BRANCH="$branch"
  # A retry with nothing recorded to verify can never be readiness success.
  local recorded_stages="${POST_PR_FAILED_STAGES:-}"
  if [[ -z "${recorded_stages//[[:space:],]/}" ]]; then
    TARGETED_REGRESSION_EXIT=2
    regression_blocked "$issue_number" "targeted regression invoked with no recorded failed stages" "$branch"
    return 0
  fi
  require_pushed_head "$branch" || { TARGETED_REGRESSION_EXIT=2; regression_blocked "$issue_number" "pushed branch head could not be verified" "$branch"; return 0; }
  # All success evidence below is executor-observed. Bind the entire targeted
  # run to an already-pushed revision before any command starts, then repeat
  # this check after the selected stages complete. A test runner may create
  # ignored artifacts, so cleanliness is checked before the worker's push;
  # it is not a meaningful post-test criterion. An agent attestation is only
  # an admission ticket to this executor retry; it can never supply pass
  # evidence or bridge a moved head.
  targeted_head=$(current_head)
  if [[ -z "$targeted_head" ]]; then
    TARGETED_REGRESSION_EXIT=2
    regression_blocked "$issue_number" "targeted regression could not resolve the executor head" "$branch"
    return 0
  fi

  # Local checks: dependency sync is setup, while every selected check is a
  # branch-attributable target. Do not rerun an unrelated successful gate.
  local any_local_recorded=false stage
  for stage in "${LOCAL_REGRESSION_STAGES[@]}"; do
    stage_is_recorded "$stage" && { any_local_recorded=true; break; }
  done
  if [[ "$any_local_recorded" == true ]]; then
    output=$(uv sync 2>&1) || { TARGETED_REGRESSION_EXIT=2; regression_blocked "$issue_number" "uv sync failed during targeted regression" "$branch"; return 0; }
    # Same table and commands as the full run; only the selector and the
    # recorder differ, which is the whole reason they share a driver.
    _record_targeted_stage() { record_targeted_result "$1" "$2" "$3"; }
    run_local_stages stage_is_recorded _record_targeted_stage || TARGETED_REGRESSION_EXIT=1
  fi

  # API is the prerequisite for both integration groups. If the deploy itself
  # failed initially, retry it alone; otherwise deploy before only the failed
  # integration groups.
  if stage_is_recorded "Deploy (API)" || stage_is_recorded "Integration (spot)" || stage_is_recorded "Integration (api-wired)"; then
    if ! acquire_required_dev_lock "$issue_number" "targeted regression API/integration"; then TARGETED_REGRESSION_EXIT=2; return 0; fi
    if ! deploy_branch_api "$DEV_ENV_FILE"; then
      release_required_dev_lock
      if [[ "$DEPLOY_FAILURE_KIND" == "infrastructure" ]]; then TARGETED_REGRESSION_EXIT=2; regression_blocked "$issue_number" "API deploy could not reach or operate the development environment" "$branch"; return 0
      else record_targeted_result "Deploy (API)" 1 "Branch API build/deploy failed."; TARGETED_REGRESSION_EXIT=1; fi
    else
      stage_is_recorded "Deploy (API)" && record_targeted_result "Deploy (API)" 0 "Branch API build/deploy passed."
      if stage_is_recorded "Integration (spot)"; then
        exit_code=0; output=$(COLUMNS="$PYTEST_REPORT_COLUMNS" ENV_FILE="$DEV_ENV_FILE" DATASPOKE_DEV_LOCK_PREACQUIRED=1 with_dev_env "$DEV_ENV_FILE" uv run pytest tests/integration/spot/ --tb=short 2>&1) || exit_code=$?
        if integration_health_abort_blocked "$issue_number" "$branch" "Integration (spot)" "$exit_code" "$output"; then TARGETED_REGRESSION_EXIT=2; return 0; fi
        record_targeted_result "Integration (spot)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || TARGETED_REGRESSION_EXIT=1
      fi
      if stage_is_recorded "Integration (api-wired)"; then
        exit_code=0; output=$(COLUMNS="$PYTEST_REPORT_COLUMNS" ENV_FILE="$DEV_ENV_FILE" DATASPOKE_DEV_LOCK_PREACQUIRED=1 with_dev_env "$DEV_ENV_FILE" uv run pytest tests/integration/api_wired/ --tb=short 2>&1) || exit_code=$?
        if integration_health_abort_blocked "$issue_number" "$branch" "Integration (api-wired)" "$exit_code" "$output"; then TARGETED_REGRESSION_EXIT=2; return 0; fi
        record_targeted_result "Integration (api-wired)" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || TARGETED_REGRESSION_EXIT=1
      fi
      release_required_dev_lock
    fi
  fi

  if stage_is_recorded "Deploy (frontend)" || stage_is_recorded "E2E"; then
    if ! acquire_required_dev_lock "$issue_number" "targeted regression frontend/E2E"; then TARGETED_REGRESSION_EXIT=2; return 0; fi
    if ! deploy_branch_frontend "$DEV_ENV_FILE"; then
      release_required_dev_lock
      if [[ "$DEPLOY_FAILURE_KIND" == "infrastructure" ]]; then TARGETED_REGRESSION_EXIT=2; regression_blocked "$issue_number" "frontend deploy could not reach or operate the development environment" "$branch"; return 0
      else record_targeted_result "Deploy (frontend)" 1 "Branch frontend build/deploy failed."; TARGETED_REGRESSION_EXIT=1; fi
    else
      stage_is_recorded "Deploy (frontend)" && record_targeted_result "Deploy (frontend)" 0 "Branch frontend build/deploy passed."
      if stage_is_recorded "E2E"; then
        if ! pnpm -C tests/e2e install --frozen-lockfile >/dev/null 2>&1 || ! pnpm -C tests/e2e exec playwright install chromium >/dev/null 2>&1; then
          release_required_dev_lock; TARGETED_REGRESSION_EXIT=2; regression_blocked "$issue_number" "E2E runner/browser setup failed" "$branch"; return 0
        fi
        exit_code=0; output=$(ENV_FILE="$DEV_ENV_FILE" DATASPOKE_DEV_LOCK_PREACQUIRED=1 with_dev_env "$DEV_ENV_FILE" pnpm -C tests/e2e test 2>&1) || exit_code=$?
        record_targeted_result "E2E" "$exit_code" "$output"; [[ "$exit_code" -eq 0 ]] || TARGETED_REGRESSION_EXIT=1
      fi
      release_required_dev_lock
    fi
  fi

  # Do not turn successful command exits into a pass if the local or remote
  # branch moved while the executor was testing. This is deliberately an
  # infrastructure block, not a branch failure: the recorded stage result no
  # longer proves the exact pushed head and must be rerun on a later heartbeat.
  if [[ "$TARGETED_REGRESSION_EXIT" -eq 0 ]] && \
     { [[ "$(current_head)" != "$targeted_head" ]] || ! require_pushed_head "$branch"; }; then
    TARGETED_REGRESSION_EXIT=2
    regression_blocked "$issue_number" "targeted regression lost its exact clean pushed-head binding" "$branch"
    return 0
  fi
}

# run_post_pr_regression <issue> <branch>
# Run one full regression. If it finds branch-attributable failures, a single
# turn-bounded worker fixes them and the executor verifies only those recorded
# stages against the exact pushed head. A targeted pass is final; no second
# full regression is permitted.
run_post_pr_regression() {
  local issue_number="$1" branch="$2"
  if ! branch_is_code_affecting "$branch"; then
    info "Diff is confined to the explicit non-code exclusion set; full regression is not required."
    return 0
  fi
  [[ -n "$issue_number" ]] || { warn "No issue number for regression gate."; return 1; }
  POST_PR_REGRESSION_SUMMARY_MODE=true
  POST_PR_FAILED_STAGES=""; POST_PR_FAILURE_EVIDENCE=""
  POST_PR_FLAKE_STAGES=""; POST_PR_NON_FLAKE_STAGES=""; POST_PR_FLAKE_HEALTH_BEFORE=false
  POST_PR_FAILED_TEST_DETAILS=""; POST_PR_FLAKE_TEST_IDS=""; POST_PR_FLAKE_CATEGORIES=""
  # The commit every initial-regression stage tests; finalize_issue_pr has
  # just pushed it. The flake basis compares recorded passes against it.
  POST_PR_REGRESSION_HEAD=$(git rev-parse HEAD 2>/dev/null || printf '')
  run_static_and_unit_regression "$branch"
  if [[ "$LOCAL_REGRESSION_EXIT" -eq 2 ]]; then
    regression_blocked "$issue_number" "$LOCAL_REGRESSION_REASON" "$branch"; POST_PR_REGRESSION_SUMMARY_MODE=false; return 1
  fi
  run_full_cluster_regression "$issue_number" "$branch"
  if [[ "$CLUSTER_REGRESSION_EXIT" -eq 2 ]]; then POST_PR_REGRESSION_SUMMARY_MODE=false; return 1; fi
  if [[ "$LOCAL_REGRESSION_EXIT" -eq 0 && "$CLUSTER_REGRESSION_EXIT" -eq 0 ]]; then
    if ! post_post_pr_regression_comment "$branch" "Full post-PR regression passed for the current pushed PR head."; then
      regression_blocked "$issue_number" "required regression success notice could not be posted" "$branch"; POST_PR_REGRESSION_SUMMARY_MODE=false; return 1
    fi
    POST_PR_REGRESSION_SUMMARY_MODE=false; return 0
  fi
  # A flake shortcut is deliberately narrow: every failure in every failed stage
  # must match the deterministic transport allowlist, each such stage must have
  # an executor-recorded pass from earlier in this heartbeat, bound to the
  # deployed artifact, at POST_PR_REGRESSION_HEAD, and the real dev
  # environment must be healthy both before and after.
  # The post-stage check is a probe only; it never provisions a cluster.
  # No worker is dispatched on this path, and no raw failure output is posted.
  if [[ -n "$POST_PR_FLAKE_STAGES" && -z "$POST_PR_NON_FLAKE_STAGES" && \
        "$POST_PR_FLAKE_HEALTH_BEFORE" == true && -n "$POST_PR_REGRESSION_HEAD" && \
        "$(git rev-parse HEAD 2>/dev/null || printf '')" == "$POST_PR_REGRESSION_HEAD" ]] && \
     require_pushed_head "$branch" && dev_env_probe_healthy "${DEV_ENV_FILE:-}"; then
    local flake_ids_section=""
    [[ -n "$POST_PR_FLAKE_TEST_IDS" ]] && flake_ids_section="

Failing test identifiers:

${POST_PR_FLAKE_TEST_IDS}"
    if ! post_post_pr_regression_comment "$branch" "Initial full regression reported environmental transport failures in ${POST_PR_FLAKE_STAGES} (allowlisted categories — ${POST_PR_FLAKE_CATEGORIES}). Pre- and post-test dev-environment health checks passed; the same stages passed earlier in this heartbeat against commit ${POST_PR_REGRESSION_HEAD:0:12}. Classified as an environmental flake; no coding agent was dispatched.${flake_ids_section}"; then
      regression_blocked "$issue_number" "environmental-flake status notice could not be posted" "$branch"; POST_PR_REGRESSION_SUMMARY_MODE=false; return 1
    fi
    POST_PR_REGRESSION_SUMMARY_MODE=false; return 0
  fi
  if ! post_post_pr_regression_comment "$branch" "Initial full post-PR regression failed: ${POST_PR_FAILED_STAGES:-an unclassified branch-attributable stage}. One turn-bounded coding-agent fix-and-targeted-test loop will retry only these failed stages.

Failed tests:

${POST_PR_FAILED_TEST_DETAILS:-- No failing test identifiers were recorded.}"; then
    regression_blocked "$issue_number" "required regression failure notice could not be posted" "$branch"; POST_PR_REGRESSION_SUMMARY_MODE=false; return 1
  fi
  info "Initial full regression failed; invoking one targeted worker fix loop."
  run_integration_fix_session "$issue_number" "$branch" "$POST_PR_FAILED_STAGES" "$POST_PR_FAILURE_EVIDENCE" post-pr
  checkpoint_branch "$issue_number" "$branch"
    # A PR already exists by this point, so derive_phase_from_github will always
    # report "pr" on the next wake — a phase the quota-resume dispatch in
    # heartbeat.sh does not know how to resume. Posting the normal resumable
    # pause marker here would strand the issue forever (paused, unresumable).
    # Defer instead: no marker, no burned fix attempt, plain retry next wake.
  if [[ "$AGENT_STATUS" == "quota" ]]; then
    warn "Issue #${issue_number}: targeted regression fix worker hit quota (${ACTIVE_AGENT}). Deferring without a second invocation."
    post_post_pr_regression_comment "$branch" "Targeted regression fix paused: ${ACTIVE_AGENT} quota is exhausted. The PR remains in prauto:wip and will retry on a later heartbeat." || true
    POST_PR_REGRESSION_SUMMARY_MODE=false; return 1
  fi
  local worker_revision
  worker_revision=$(git rev-parse HEAD 2>/dev/null || printf '')
  if [[ "$AGENT_STATUS" != "ok" ]] || ! validate_targeted_verification "$AGENT_OUTPUT" "$worker_revision"; then
    regression_set_wip "$issue_number" "$branch"
    post_post_pr_regression_comment "$branch" "Targeted regression fix did not produce a valid structured verification record for the committed head; the PR remains in prauto:wip." || true
    POST_PR_REGRESSION_SUMMARY_MODE=false; return 1
  fi
  local dirty_worktree
  dirty_worktree=$(git status --porcelain --untracked-files=all 2>/dev/null || true)
  if [[ -n "$dirty_worktree" ]]; then
    regression_set_wip "$issue_number" "$branch"
    post_post_pr_regression_comment "$branch" "Targeted regression fix left uncommitted changes; the PR remains in prauto:wip." || true
    POST_PR_REGRESSION_SUMMARY_MODE=false; return 1
  fi
  push_branch "$branch"
  create_or_update_pr "$issue_number" "" "$branch"
  if ! require_pushed_head "$branch"; then
    regression_blocked "$issue_number" "pushed branch head could not be verified after targeted fix" "$branch"; POST_PR_REGRESSION_SUMMARY_MODE=false; return 1
  fi
  run_targeted_post_pr_regression "$issue_number" "$branch"
  # Exit 2 has already posted its infrastructure-blocked notice and established
  # prauto:wip; it is never readiness success and never a code-failure report.
  if [[ "$TARGETED_REGRESSION_EXIT" -eq 2 ]]; then POST_PR_REGRESSION_SUMMARY_MODE=false; return 1; fi
  if [[ "$TARGETED_REGRESSION_EXIT" -eq 0 ]]; then
    if ! post_post_pr_regression_comment "$branch" "Initial full regression failures: ${POST_PR_FAILED_STAGES}. Targeted retry passed on the current pushed PR head: ${POST_PR_TARGETED_PASSES}. No second full regression was run."; then
      regression_blocked "$issue_number" "required targeted regression success notice could not be posted" "$branch"; POST_PR_REGRESSION_SUMMARY_MODE=false; return 1
    fi
    POST_PR_REGRESSION_SUMMARY_MODE=false; return 0
  fi
  regression_set_wip "$issue_number" "$branch"
  local targeted_evidence
  targeted_evidence=$(targeted_failure_comment_evidence)
  post_post_pr_regression_comment "$branch" "Initial full regression failures: ${POST_PR_FAILED_STAGES}. Targeted retry still failed: ${POST_PR_TARGETED_FAILURES:-an unrecorded stage}. The PR remains in prauto:wip.

Failed tests:

${POST_PR_TARGETED_FAILED_TEST_DETAILS:-- No failing test identifiers were recorded.}

<details><summary>Sanitized executor evidence</summary>

\`\`\`text
${targeted_evidence:-No executor output was captured.}
\`\`\`
</details>" || true
  POST_PR_REGRESSION_SUMMARY_MODE=false; return 1
}

# run_pre_pr_selected_verification <issue> <branch>
# The current selector is intentionally conservative: it selects the full
# affected layer while per-test impact metadata is unavailable.  It never runs
# an unrelated cluster layer, and it never turns uncertainty into an omission.
run_pre_pr_selected_verification() {
  local issue_number="$1" branch="$2"
  if ! branch_is_code_affecting "$branch"; then
    info "Pre-PR verification: explicit non-code diff; no code-test layer selected."
    return 0
  fi
  info "Pre-PR verification: selected full static/unit suites (conservative impact fallback)."
  run_static_and_unit_regression "$branch"
  if [[ "$LOCAL_REGRESSION_EXIT" -eq 2 ]]; then regression_blocked "$issue_number" "$LOCAL_REGRESSION_REASON" "$branch"; return 1; fi
  if [[ "$LOCAL_REGRESSION_EXIT" -ne 0 ]]; then
    info "Pre-PR static/unit failure will be handled by the mandatory post-PR fix gate."
  fi
  if diff_touches src/api/ src/backend/ src/shared/ tests/integration/; then
    info "Pre-PR verification: selected spot and api-wired suites (conservative affected-layer fallback)."
    # A non-zero return here means the fix worker died on quota mid-loop (a
    # pause marker is already posted) — stop before PR creation so the next
    # wake resumes the same session instead of finalizing an unverified branch.
    run_integration_test_fix "$issue_number" "$branch" || return 1
  fi
  if diff_touches src/frontend/ src/api/ tests/e2e/; then
    info "Pre-PR verification: selected E2E suite (conservative affected-layer fallback)."
    run_e2e_test_fix "$issue_number" "$branch" || return 1
  fi
}
