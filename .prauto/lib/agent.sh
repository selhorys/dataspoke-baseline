# Coding-agent dispatch for prauto (Claude Code and Codex).
# Source this file — do not execute directly.
# Requires: helpers.sh + quota.sh sourced, config loaded, agent CLIs available.

# Per-phase tool whitelists and the standing denylist, per
# spec/AI_PRAUTO.md §Worker Agent Invocation. The implementation phase drives
# .claude/workflows/wf-minimal.js, so it needs Agent + Workflow on top of the
# direct-edit tools. Granting those voids DENY_TOOLS for delegated work (subagent
# frontmatter, not the parent whitelist, governs subagent tools) — an accepted,
# documented consequence per spec/AI_PRAUTO.md §Security Model.
ANALYSIS_ALLOWED_TOOLS='Read,Write,Glob,Grep,Bash(git log *),Bash(git diff *),Bash(git status *),Bash(git branch *)'
IMPLEMENTATION_ALLOWED_TOOLS='Read,Write,Edit,Glob,Grep,Agent,Workflow,Bash(git log *),Bash(git diff *),Bash(git status *),Bash(git branch *),Bash(git add *),Bash(git commit *),Bash(uv run pytest *),Bash(uv run python3 *),Bash(uv run ruff *),Bash(uv run mypy *),Bash(uv sync *),Bash(npm run *),Bash(npx prettier *),Bash(npx tsc *),Bash(npx eslint *),Bash(pnpm *)'

DENY_TOOLS='Bash(git push *),Bash(rm -rf *),Bash(sudo *),Bash(kubectl *),Bash(helm *),Bash(curl *),Bash(wget *),Bash(gh *),Read(.prauto/config.local.env),Read(.prauto/state/*),WebFetch,WebSearch'

# ACTIVE_AGENT — the agent selected for this wake (`claude` or `codex`).
# AGENT_SESSION_ID / AGENT_OUTPUT / AGENT_STATUS — set by invoke_agent/resume_agent.
# AGENT_STATUS is one of: ok | quota | error.
ACTIVE_AGENT=""
AGENT_SESSION_ID=""
AGENT_OUTPUT=""
AGENT_STATUS=""

# select_agent — probe per PRAUTO_AGENT and set ACTIVE_AGENT.
#   claude -> probe claude only; codex -> codex only; auto -> claude then codex.
# Returns 0 (and sets ACTIVE_AGENT) or 1 if neither agent is available.
select_agent() {
  local want="${PRAUTO_AGENT:-auto}"
  case "$want" in
    claude)
      check_quota claude && { ACTIVE_AGENT=claude; return 0; }
      ;;
    codex)
      check_quota codex && { ACTIVE_AGENT=codex; return 0; }
      ;;
    auto|*)
      check_quota claude && { ACTIVE_AGENT=claude; return 0; }
      check_quota codex && { ACTIVE_AGENT=codex; return 0; }
      ;;
  esac
  ACTIVE_AGENT=""
  return 1
}

# new_session_id — generate a Claude session id the harness can address later.
# Claude accepts --session-id on a fresh invocation. Codex does not: its native
# thread id is emitted only after the process starts (see codex_thread_id).
new_session_id() {
  uuidgen 2>/dev/null | tr '[:upper:]' '[:lower:]' \
    || cat /proc/sys/kernel/random/uuid 2>/dev/null \
    || printf '%s-%s' "$(date +%s)" "$$"
}

# codex_thread_id <jsonl_file>
# Return the native thread id emitted by `codex exec --json`. Never synthesize
# an id: Codex resume can address only a thread the CLI actually created.
codex_thread_id() {
  local output_file="$1"
  jq -r -s '[.[] | select(.type == "thread.started") | .thread_id] | first // empty' \
    "$output_file" 2>/dev/null
}

# codex_final_output <jsonl_file>
# `item.completed` agent_message is Codex exec's final user-facing response.
# Keep all JSONL in the artifact for diagnostics; callers consume only its last
# agent message as the phase result.
codex_final_output() {
  local output_file="$1"
  jq -r -s '
    [ .[]
      | select(.type == "item.completed" or .type == "item_completed")
      | (.item // .payload.item // {})
      | select(.type == "agent_message" or .type == "AgentMessage")
      | (.text // .content // empty)
    ]
    | last // empty
  ' "$output_file" 2>/dev/null
}

# codex_has_terminal_error <jsonl_file>
# A tool item can fail while the agent recovers, so only terminal error events
# classify the whole invocation as failed.
codex_has_terminal_error() {
  local output_file="$1"
  jq -e '
    select(type == "object")
    | select(.type == "error" or .type == "turn.failed"
             or .payload.type == "error" or .payload.type == "turn.failed")
  ' "$output_file" >/dev/null 2>&1
}

# prepare_system_prompt — render prompts/system-append.md with worker identity.
prepare_system_prompt() {
  local rendered_file="${STATE_DIR}/.system-append-rendered.md"
  local content
  content=$(cat "${PRAUTO_DIR}/prompts/system-append.md")
  content="${content//\{PRAUTO_WORKER_ID\}/${PRAUTO_WORKER_ID}}"
  content="${content//\{PRAUTO_GIT_AUTHOR_NAME\}/${PRAUTO_GIT_AUTHOR_NAME}}"
  content="${content//\{PRAUTO_GIT_AUTHOR_EMAIL\}/${PRAUTO_GIT_AUTHOR_EMAIL}}"
  printf '%s' "$content" > "$rendered_file"
  printf '%s' "$rendered_file"
}

# classify_exit <output_file> <exit_code> [agent] [stderr_file]
# Set AGENT_STATUS from a completed claude/codex run: ok, quota, or error.
# Claude retains its established textual classification. Codex quota is more
# strict: only codex_jsonl_has_quota_signal's vetted protocol fields may create
# a resumable pause; arbitrary messages and agent text are ordinary errors.
classify_exit() {
  local output_file="$1" exit_code="$2" agent="${3:-claude}" stderr_file="${4:-}"
  local raw
  raw=$(cat "$output_file" 2>/dev/null || printf '')
  if [[ -n "$stderr_file" ]] && [[ -s "$stderr_file" ]]; then
    raw="${raw}
$(cat "$stderr_file" 2>/dev/null || printf '')"
  fi
  if [[ "$agent" == "codex" ]]; then
    if codex_jsonl_has_quota_signal "$output_file"; then
      AGENT_STATUS=quota
    elif codex_has_terminal_error "$output_file" || [[ "$exit_code" -ne 0 ]]; then
      AGENT_STATUS=error
    else
      AGENT_STATUS=ok
    fi
    return
  fi

  # Claude emits auth/api/budget failures as an is_error result object while
  # still exiting 0. Check that before the exit-code fast path below, so a
  # "successful" exit that actually errored is never marked ok.
  if claude_result_is_error "$output_file"; then
    if printf '%s' "$raw" | grep -qi "rate limit\|quota\|session limit"; then
      AGENT_STATUS=quota
    else
      AGENT_STATUS=error
    fi
    return
  fi

  if [[ "$exit_code" -eq 0 ]]; then
    AGENT_STATUS=ok
  elif printf '%s' "$raw" | grep -qi "rate limit\|quota\|session limit\|api_error\|API error"; then
    AGENT_STATUS=quota
  else
    AGENT_STATUS=error
  fi
}

# invoke_agent <prompt> <allowed_tools> <max_turns> [budget]
# Dispatch a fresh session under ACTIVE_AGENT. Claude receives a harness-created
# id; Codex records only the native `thread.started` id. Sets AGENT_SESSION_ID,
# AGENT_OUTPUT, AGENT_STATUS.
invoke_agent() {
  local prompt="$1" allowed_tools="$2" max_turns="$3" budget="${4:-}"
  local system_file=""
  [[ "$ACTIVE_AGENT" == "claude" ]] && system_file=$(prepare_system_prompt)
  local session_id=""
  [[ "$ACTIVE_AGENT" == "claude" ]] && session_id=$(new_session_id)

  local output_suffix="${session_id:-codex-$(date +%s)-$$}"
  local output_file="${CUR_SESSION_DIR}/agent-${output_suffix}.json"
  local stderr_file="${output_file}.stderr"
  local code=0
  local -a cmd

  if [[ "$ACTIVE_AGENT" == "codex" ]]; then
    # Codex deliberately receives none of Claude's session/tool/turn/budget
    # flags. Its thread id is emitted in JSONL after startup.
    cmd=(codex exec --json --sandbox workspace-write)
    cmd+=("$prompt")
  else
    cmd=(claude -p "$prompt"
      --append-system-prompt-file "$system_file"
      --model "${PRAUTO_CLAUDE_MODEL:-opus}"
      --output-format json
      --session-id "$session_id"
      --max-turns "$max_turns"
      --allowedTools "$allowed_tools"
      --disallowedTools "$DENY_TOOLS"
      --dangerously-skip-permissions)
    [[ -n "$budget" ]] && cmd+=(--max-budget-usd "$budget")
  fi

  info "Invoking ${ACTIVE_AGENT} (session=${session_id:-native-pending}, max_turns=${max_turns})..."
  # claude -p stdout is unreliable through $(...); redirect stdout to a file and
  # retain stderr separately so diagnostics cannot corrupt the JSON result.
  # Codex JSONL must remain stdout-only: stderr can contain non-JSON runtime
  # diagnostics, which would otherwise make thread.started unparsable. Retain
  # that raw stderr sidecar for postmortem diagnosis.
  #
  # The invocation runs under a wall-clock backstop (PRAUTO_AGENT_TIMEOUT_SECS,
  # default 24h): a hung/stalled agent (network wait, token-reset wait) is killed
  # rather than wedging the heartbeat forever. A kill normalizes to exit 124,
  # which classify_exit treats as an ordinary error (retry), never a resume.
  local agent_timeout="${PRAUTO_AGENT_TIMEOUT_SECS:-86400}"
  if [[ "$ACTIVE_AGENT" == "codex" ]]; then
    if run_with_timeout "$agent_timeout" "${cmd[@]}" > "$output_file" 2> "$stderr_file"; then
      code=0
    else
      code=$?
    fi
  else
    if run_with_timeout "$agent_timeout" "${cmd[@]}" > "$output_file" 2> "$stderr_file"; then
      code=0
    else
      code=$?
    fi
  fi

  if [[ "$ACTIVE_AGENT" == "codex" ]]; then
    AGENT_SESSION_ID=$(codex_thread_id "$output_file")
    # The local anchor is written immediately after the native event and before
    # quota handling can publish a resume marker. A failed write means no safe
    # resume target even if Codex did start successfully.
    if [[ -n "$AGENT_SESSION_ID" ]] && ! record_codex_native_session \
        "${CUR_ISSUE_NUMBER:-}" "${READY_LABEL_TIMESTAMP:-}" "$AGENT_SESSION_ID"; then
      warn "Could not persist Codex native-session anchor; leaving this attempt non-resumable."
      AGENT_SESSION_ID=""
    fi
    AGENT_OUTPUT=$(codex_final_output "$output_file")
    [[ -z "$AGENT_OUTPUT" ]] && AGENT_OUTPUT=$(cat "$output_file" 2>/dev/null || printf '')
    if [[ -z "$AGENT_OUTPUT" ]] && [[ -s "$stderr_file" ]]; then
      AGENT_OUTPUT=$(cat "$stderr_file" 2>/dev/null || printf '')
    fi
    classify_exit "$output_file" "$code" codex
    # No native identity means no safe resume target. Treat an otherwise quota
    # exit as an ordinary failure so the heartbeat retries/restarts instead of
    # publishing a misleading resumable pause marker.
    if [[ -z "$AGENT_SESSION_ID" ]] && [[ "$AGENT_STATUS" != "ok" ]]; then
      warn "Codex exited before emitting thread.started; leaving this attempt non-resumable."
      AGENT_STATUS=error
    fi
  else
    AGENT_SESSION_ID="$session_id"
    AGENT_OUTPUT=$(jq -r '.result // empty' "$output_file" 2>/dev/null || printf '')
    # An error subtype (error_max_turns, error_budget, api_error) carries no .result.
    local subtype; subtype=$(jq -r '.subtype // empty' "$output_file" 2>/dev/null || printf '')
    if [[ "$subtype" == error_* ]] || [[ -z "$AGENT_OUTPUT" ]]; then
      [[ -z "$AGENT_OUTPUT" ]] && AGENT_OUTPUT=$(cat "$output_file" 2>/dev/null || printf '')
    fi
    if [[ -s "$stderr_file" ]]; then
      local claude_stderr
      claude_stderr=$(cat "$stderr_file" 2>/dev/null || printf '')
      [[ -n "$AGENT_OUTPUT" ]] && AGENT_OUTPUT="${AGENT_OUTPUT}
${claude_stderr}" || AGENT_OUTPUT="$claude_stderr"
    fi
    classify_exit "$output_file" "$code" claude "$stderr_file"
  fi
}

# resume_agent <prompt> <allowed_tools> <max_turns> <session_id> [budget]
# Resume an existing session under ACTIVE_AGENT. Same agent as the original —
# a session cannot migrate agents; agent-switch is only reachable via
# abandon+restart. Sets AGENT_OUTPUT, AGENT_STATUS.
resume_agent() {
  local prompt="$1" allowed_tools="$2" max_turns="$3" session_id="$4" budget="${5:-}"
  if [[ "$ACTIVE_AGENT" == "codex" ]]; then
    if [[ "$session_id" != "${PAUSED_SESSION_ID:-}" ]] || \
       ! codex_pause_marker_is_trusted "${CUR_ISSUE_NUMBER:-}"; then
      warn "Refusing Codex resume without a matching trusted native-session anchor."
      AGENT_SESSION_ID=""
      AGENT_OUTPUT=""
      AGENT_STATUS=error
      return 0
    fi
  fi
  local system_file=""
  [[ "$ACTIVE_AGENT" == "claude" ]] && system_file=$(prepare_system_prompt)
  local output_file="${CUR_SESSION_DIR}/agent-${session_id}-resume.json"
  local stderr_file="${output_file}.stderr"
  local code=0
  local -a cmd

  if [[ "$ACTIVE_AGENT" == "codex" ]]; then
    # Resume accepts the agent-native id and prompt only; do not append fresh
    # execution options (sandbox/tool/turn/budget/session flags are Claude-only).
    cmd=(codex exec resume --json "$session_id" "$prompt")
  else
    cmd=(claude -p "$prompt"
      --resume "$session_id"
      --append-system-prompt-file "$system_file"
      --model "${PRAUTO_CLAUDE_MODEL:-opus}"
      --output-format json
      --max-turns "$max_turns"
      --allowedTools "$allowed_tools"
      --disallowedTools "$DENY_TOOLS"
      --dangerously-skip-permissions)
    [[ -n "$budget" ]] && cmd+=(--max-budget-usd "$budget")
  fi

  info "Resuming ${ACTIVE_AGENT} session ${session_id}..."
  # Same wall-clock backstop as the fresh invocation (see invoke_agent): a resume
  # that hangs waiting for token reset must not wedge the heartbeat.
  local agent_timeout="${PRAUTO_AGENT_TIMEOUT_SECS:-86400}"
  if [[ "$ACTIVE_AGENT" == "codex" ]]; then
    if run_with_timeout "$agent_timeout" "${cmd[@]}" > "$output_file" 2> "$stderr_file"; then
      code=0
    else
      code=$?
    fi
  else
    if run_with_timeout "$agent_timeout" "${cmd[@]}" > "$output_file" 2> "$stderr_file"; then
      code=0
    else
      code=$?
    fi
  fi

  if [[ "$ACTIVE_AGENT" == "codex" ]]; then
    AGENT_OUTPUT=$(codex_final_output "$output_file")
    [[ -z "$AGENT_OUTPUT" ]] && AGENT_OUTPUT=$(cat "$output_file" 2>/dev/null || printf '')
    if [[ -z "$AGENT_OUTPUT" ]] && [[ -s "$stderr_file" ]]; then
      AGENT_OUTPUT=$(cat "$stderr_file" 2>/dev/null || printf '')
    fi
    classify_exit "$output_file" "$code" codex
  else
    AGENT_OUTPUT=$(jq -r '.result // empty' "$output_file" 2>/dev/null || printf '')
    [[ -z "$AGENT_OUTPUT" ]] && AGENT_OUTPUT=$(cat "$output_file" 2>/dev/null || printf '')
    if [[ -s "$stderr_file" ]]; then
      local claude_stderr
      claude_stderr=$(cat "$stderr_file" 2>/dev/null || printf '')
      [[ -n "$AGENT_OUTPUT" ]] && AGENT_OUTPUT="${AGENT_OUTPUT}
${claude_stderr}" || AGENT_OUTPUT="$claude_stderr"
    fi
    classify_exit "$output_file" "$code" claude "$stderr_file"
  fi
}

# render_prompt <template_file> <var1=val1> [var2=val2 ...]
# Substitute {var} placeholders in a prompt template. Plain string replacement —
# not shell `eval`, so a value containing quotes/backticks/$ stays inert.
render_prompt() {
  local template_file="$1"; shift
  local content assignment key value
  content=$(cat "$template_file")
  for assignment in "$@"; do
    key="${assignment%%=*}"
    value="${assignment#*=}"
    content="${content//\{$key\}/$value}"
  done
  printf '%s' "$content"
}

# run_analysis <issue_number> <issue_title> <issue_body> [counter_proposal] [previous_plan]
# Analysis phase: read + write plan.md only. Sets ANALYSIS_OUTPUT, ANALYSIS_SESSION_ID.
run_analysis() {
  local issue_number="$1" issue_title="$2" issue_body="$3"
  local counter_proposal="${4:-}" previous_plan="${5:-}"
  local plan_file="${CUR_SESSION_DIR}/plan.md"
  local prompt
  prompt=$(render_prompt "${PRAUTO_DIR}/prompts/issue-analysis.md" \
    "number=${issue_number}" "title=${issue_title}" "body=${issue_body}" "plan_file=${plan_file}")

  if [[ -n "$counter_proposal" ]]; then
    [[ -n "$previous_plan" ]] && prompt="${prompt}

## Previous Plan

Use this as your starting point. Revise it based on the feedback below — do not start from scratch.

${previous_plan}"
    prompt="${prompt}

## Feedback on Previous Plan

The following counter-proposal was made. Revise the plan above to address this feedback:

${counter_proposal}"
  fi

  invoke_agent "$prompt" "$ANALYSIS_ALLOWED_TOOLS" "${PRAUTO_CLAUDE_MAX_TURNS_ANALYSIS:-100}" "${PRAUTO_CLAUDE_MAX_BUDGET_ANALYSIS:-}"
  ANALYSIS_SESSION_ID="$AGENT_SESSION_ID"

  if [[ "$AGENT_STATUS" != "ok" ]]; then
    warn "Analysis produced no usable output for issue #${issue_number} (status=${AGENT_STATUS})."
    ANALYSIS_OUTPUT=""
    return 1
  fi

  # Prefer the plan file the worker wrote via Write over the .result field.
  if [[ -f "$plan_file" ]] && [[ -s "$plan_file" ]]; then
    ANALYSIS_OUTPUT=$(cat "$plan_file")
    info "Plan captured from file ($(wc -c < "$plan_file" | tr -d ' ') bytes)."
  else
    warn "Plan file not found at ${plan_file}. Falling back to .result output."
    ANALYSIS_OUTPUT="$AGENT_OUTPUT"
  fi
  printf '%s' "$ANALYSIS_OUTPUT" > "${CUR_SESSION_DIR}/analysis.txt"
}

# run_implementation <issue_number> <branch> <analysis_output>
# Implementation phase: drive wf-minimal via the Workflow tool. Fresh session each
# time; the workflow restarts rather than resumes, so only committed work is
# continuity. Sets IMPL_SESSION_ID, IMPL_OUTPUT (carries the outcome sentinel).
run_implementation() {
  local issue_number="$1" branch="$2" analysis_output="$3"
  local prompt
  prompt=$(render_prompt "${PRAUTO_DIR}/prompts/implementation.md" \
    "number=${issue_number}" "branch=${branch}" "base_branch=${PRAUTO_BASE_BRANCH}" \
    "author_name=${PRAUTO_GIT_AUTHOR_NAME}" "author_email=${PRAUTO_GIT_AUTHOR_EMAIL}" \
    "analysis_output=${analysis_output}")

  invoke_agent "$prompt" "$IMPLEMENTATION_ALLOWED_TOOLS" "${PRAUTO_CLAUDE_MAX_TURNS_IMPLEMENTATION:-400}" "${PRAUTO_CLAUDE_MAX_BUDGET_IMPLEMENTATION:-}"
  IMPL_SESSION_ID="$AGENT_SESSION_ID"
  IMPL_OUTPUT="$AGENT_OUTPUT"
  printf '%s' "$AGENT_OUTPUT" > "${CUR_SESSION_DIR}/implementation.json"
}

# run_integration_fix_session <issue_number> <branch> <test_output>
run_integration_fix_session() {
  local issue_number="$1" branch="$2" test_output="$3"
  if [[ ${#test_output} -gt 30000 ]]; then test_output="${test_output:0:30000}
... (truncated)"; fi
  local prompt
  prompt=$(render_prompt "${PRAUTO_DIR}/prompts/integration-fix.md" \
    "number=${issue_number}" "branch=${branch}" "test_output=${test_output}" \
    "author_name=${PRAUTO_GIT_AUTHOR_NAME}" "author_email=${PRAUTO_GIT_AUTHOR_EMAIL}")
  invoke_agent "$prompt" "$IMPLEMENTATION_ALLOWED_TOOLS" "${PRAUTO_CLAUDE_MAX_TURNS_INTEGRATION_FIX:-200}" \
    "${PRAUTO_CLAUDE_MAX_BUDGET_INTEGRATION_FIX:-${PRAUTO_CLAUDE_MAX_BUDGET_IMPLEMENTATION:-}}"
}

# run_e2e_fix_session <issue_number> <branch> <test_output>
run_e2e_fix_session() {
  local issue_number="$1" branch="$2" test_output="$3"
  if [[ ${#test_output} -gt 30000 ]]; then test_output="${test_output:0:30000}
... (truncated)"; fi
  local prompt
  prompt=$(render_prompt "${PRAUTO_DIR}/prompts/e2e-fix.md" \
    "number=${issue_number}" "branch=${branch}" "test_output=${test_output}" \
    "author_name=${PRAUTO_GIT_AUTHOR_NAME}" "author_email=${PRAUTO_GIT_AUTHOR_EMAIL}")
  invoke_agent "$prompt" "$IMPLEMENTATION_ALLOWED_TOOLS" "${PRAUTO_CLAUDE_MAX_TURNS_E2E_FIX:-${PRAUTO_CLAUDE_MAX_TURNS_INTEGRATION_FIX:-50}}" \
    "${PRAUTO_CLAUDE_MAX_BUDGET_E2E_FIX:-${PRAUTO_CLAUDE_MAX_BUDGET_IMPLEMENTATION:-}}"
}

# generate_squash_commit_message <issue_number> <issue_title> <issue_body> <pr_number> <diff_stat> <diff>
# Single-turn, no-tool invocation. Sets SQUASH_COMMIT_MESSAGE.
generate_squash_commit_message() {
  local issue_number="$1" issue_title="$2" issue_body="$3" pr_number="$4" diff_stat="$5" diff="$6"
  if [[ ${#diff} -gt 4000 ]]; then diff="${diff:0:4000}
... (truncated)"; fi
  local prompt
  prompt=$(render_prompt "${PRAUTO_DIR}/prompts/squash-commit.md" \
    "issue_number=${issue_number}" "issue_title=${issue_title}" "issue_body=${issue_body}" \
    "pr_number=${pr_number}" "diff_stat=${diff_stat}" "diff=${diff}")
  invoke_agent "$prompt" "" "1" "${PRAUTO_CLAUDE_MAX_BUDGET_ANALYSIS:-}"
  if [[ "$AGENT_STATUS" != "ok" ]]; then
    warn "Claude failed to generate commit message (status=${AGENT_STATUS}). Falling back to PR title."
    SQUASH_COMMIT_MESSAGE="${issue_title}

(issue #${issue_number}, PR #${pr_number})"
    return 0
  fi

  SQUASH_COMMIT_MESSAGE=$(printf '%s' "$AGENT_OUTPUT" | sed '/^```/d')
  if [[ -z "$SQUASH_COMMIT_MESSAGE" ]]; then
    warn "Claude returned an empty commit message. Falling back to PR title."
    SQUASH_COMMIT_MESSAGE="${issue_title}

(issue #${issue_number}, PR #${pr_number})"
  fi
}

# run_pr_review <issue_number> <branch> <reviewer_comments> <plan>
run_pr_review() {
  local issue_number="$1" branch="$2" reviewer_comments="$3" plan="${4:-}"
  local prompt
  prompt=$(render_prompt "${PRAUTO_DIR}/prompts/pr-review.md" \
    "number=${issue_number}" "branch=${branch}" "plan=${plan}" "reviewer_comments=${reviewer_comments}" \
    "author_name=${PRAUTO_GIT_AUTHOR_NAME}" "author_email=${PRAUTO_GIT_AUTHOR_EMAIL}")
  invoke_agent "$prompt" "$IMPLEMENTATION_ALLOWED_TOOLS" "${PRAUTO_CLAUDE_MAX_TURNS_IMPLEMENTATION:-400}" \
    "${PRAUTO_CLAUDE_MAX_BUDGET_IMPLEMENTATION:-}"
  REVIEW_RESPONSE="$AGENT_OUTPUT"
}

# generate_feedback_response <issue_number> <issue_title> <feedback> <previous_plan>
generate_feedback_response() {
  local issue_number="$1" issue_title="$2" feedback="$3" previous_plan="$4"
  local prompt
  prompt=$(render_prompt "${PRAUTO_DIR}/prompts/feedback-response.md" \
    "number=${issue_number}" "title=${issue_title}" "feedback=${feedback}" "plan=${previous_plan}")
  invoke_agent "$prompt" "" "1" "${PRAUTO_CLAUDE_MAX_BUDGET_ANALYSIS:-}"
  FEEDBACK_RESPONSE_TEXT=$(printf '%s' "$AGENT_OUTPUT" | sed '/^```/d')
}
