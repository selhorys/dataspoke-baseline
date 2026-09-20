# shellcheck shell=bash
# Coding-agent dispatch for prauto (Claude Code and Codex).
# Source this file — do not execute directly.
# Requires: helpers.sh + quota.sh sourced, config loaded, agent CLIs available.

# Per-phase tool whitelists and the standing denylist, per
# spec/AI_PRAUTO.md §Worker Agent Invocation. The implementation phase drives
# .claude/workflows/wf-minimal.js, so it needs the subagent tool plus Workflow on
# top of the direct-edit tools. The subagent tool is named `Task` from Claude Code
# 2.1.273 on (it was `Agent` through 2.1.271), so name both and keep matching
# across the CLI rename. Granting those voids DENY_TOOLS for delegated work
# (subagent frontmatter, not the parent whitelist, governs subagent tools) — an
# accepted, documented consequence per spec/AI_PRAUTO.md §Security Model.
ANALYSIS_ALLOWED_TOOLS='Read,Write,Glob,Grep,Bash(git log *),Bash(git diff *),Bash(git status *),Bash(git branch *)'
IMPLEMENTATION_ALLOWED_TOOLS='Read,Write,Edit,Glob,Grep,Task,Agent,Workflow,Bash(git log *),Bash(git diff *),Bash(git status *),Bash(git branch *),Bash(git add *),Bash(git commit *),Bash(uv run pytest *),Bash(uv run python3 *),Bash(uv run ruff *),Bash(uv run mypy *),Bash(uv sync *),Bash(npm run *),Bash(npx prettier *),Bash(npx tsc *),Bash(npx eslint *),Bash(pnpm *)'

# IMPLEMENTATION_CLAUDE_ENV — Claude CLI environment for the implementation phase
# only, applied per invocation (see invoke_agent's env_overrides parameter) rather
# than exported process-wide. The implementation phase is the only one with a
# wf-minimal binding to satisfy, and the only one that runs its work as a
# background task, so it is the only one either knob below is for. The two fix
# phases in particular must not receive them: they hold the dev-env lock while
# they run, so a lifted background-wait ceiling would extend the worst-case hold
# on that lock — and on a live cluster — from minutes to PRAUTO_AGENT_TIMEOUT_SECS.
#
# Note what this scoping does NOT buy. Only `Workflow` is gated by
# CLAUDE_CODE_WORKFLOWS; the subagent tool is registered by CLI default, and
# pr-review runs with the same IMPLEMENTATION_ALLOWED_TOOLS grant, so it can
# delegate and its delegated work escapes DENY_TOOLS exactly as implementation's
# does — the consequence spec/AI_PRAUTO.md §Security Model already accepts. The
# phases whose grant is genuinely narrowed are the fix sessions, via
# FIX_DENY_TOOLS_EXTRA's hard --disallowedTools block.
#
# CLAUDE_CODE_WORKFLOWS — the dynamic-workflow opt-in. Claude Code registers its
# `Workflow` tool only when dynamic workflows are enabled for the session, and its
# settings schema says `enableWorkflows` is "Unset = default by plan". This
# account's plan default is OFF, so a bare `claude -p` session has no `Workflow`
# tool at all — `--allowedTools` cannot re-enable an unregistered tool, which left
# the implementation prompt's wf-minimal binding unsatisfiable and escalated issue
# #182 with zero commits. Verified against the installed CLI: without this variable
# the headless tool list has no `Workflow`; with `CLAUDE_CODE_WORKFLOWS=1` it does.
# Scoping it here keeps spec/AI_PRAUTO.md §Security Model's accepted
# "granting those voids DENY_TOOLS for delegated work" consequence a property of
# the implementation phase, as that section states it.
#
# CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS — how long `claude -p` waits for background
# tasks before terminating the session. Unset, the CLI applies a 600s ceiling and
# kills the session with `Background tasks still running after 600s; terminating.`
# The implementation phase runs wf-minimal as a background task, so it hits that
# ceiling structurally: the session dies before the workflow reports completion,
# the sentinel is never printed, and the executor scores real committed work as a
# failed attempt (issue #182 attempt 3, issue #176). The default here is generous
# but FINITE, deliberately: 0 (wait forever) would leave a genuinely wedged task
# holding the executor's PID lock for the full PRAUTO_AGENT_TIMEOUT_SECS, and would
# make the truncation-refund path below unreachable — a wedged session could then
# never report the signature that classifies it as a harness fault. Override with
# PRAUTO_CLAUDE_PRINT_BG_WAIT_CEILING_MS in config.local.env; 0 restores the
# wait-forever behaviour.
PRAUTO_CLAUDE_WORKFLOWS="${PRAUTO_CLAUDE_WORKFLOWS:-1}"
PRAUTO_CLAUDE_PRINT_BG_WAIT_CEILING_MS="${PRAUTO_CLAUDE_PRINT_BG_WAIT_CEILING_MS:-14400000}"
IMPLEMENTATION_CLAUDE_ENV=(
  "CLAUDE_CODE_WORKFLOWS=${PRAUTO_CLAUDE_WORKFLOWS}"
  "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=${PRAUTO_CLAUDE_PRINT_BG_WAIT_CEILING_MS}"
)

# DENY_TOOLS — the standing block for every phase.
#
# Read this as defence in depth, NOT as a boundary. Sessions run under
# `--dangerously-skip-permissions`, and the implementation grant includes
# `Bash(uv run python3 *)` — an interpreter running as this executor's own OS
# user. A worker that wants to reach the state tree can, whatever this list says.
# The file denials below still earn their place: they stop casual and accidental
# access, and they make the intent explicit. They are not what makes any guarantee
# hold.
#
# The absolute forms matter because a session's cwd is its worktree at
# ${PRAUTO_DIR}/worktrees/I-<n>, two levels below ${PRAUTO_DIR}/state, so a
# cwd-relative pattern never covered the real path at all. The worktree itself
# stays writable — that is where a worker's legitimate work goes.
#
# The consequence for design: treat every value under the state tree as
# worker-influenceable, and rest control decisions on what this executor computes
# (see AGENT_ELAPSED_SECS) or on bounds it enforces itself
# (PRAUTO_MAX_REFUNDS_PER_JOB, RETRY_COUNT_CONSUMED).
DENY_TOOLS="Bash(git push *),Bash(rm -rf *),Bash(sudo *),Bash(kubectl *),Bash(helm *),Bash(curl *),Bash(wget *),Bash(gh *),Read(.prauto/config.local.env),Read(.prauto/state/*),Write(.prauto/state/*),Edit(.prauto/state/*),Read(${PRAUTO_DIR}/config.local.env),Read(${PRAUTO_DIR}/state/**),Write(${PRAUTO_DIR}/config.local.env),Write(${PRAUTO_DIR}/state/**),Edit(${PRAUTO_DIR}/config.local.env),Edit(${PRAUTO_DIR}/state/**),WebFetch,WebSearch"
# FIX_DENY_TOOLS_EXTRA — appended to DENY_TOOLS's --disallowedTools value for
# the integration and E2E fix sessions. `--allowedTools` is not itself an
# enforced removal under `--dangerously-skip-permissions` (everything is
# already permission-approved regardless of the allow list), so keeping
# `Agent`/`Workflow` out of a fix session's tool vocabulary — those sessions
# must verify foreground, turn-bounded, within a single session, never by
# delegating and ending the turn early (see prompts/integration-fix.md and
# prompts/e2e-fix.md) — has to go through the hard `--disallowedTools` block
# instead. `Task` is included alongside `Agent`/`Workflow` as the same class of
# delegation tool. Claude-only: Codex has no equivalent tool-enforcement flag.
FIX_DENY_TOOLS_EXTRA='Agent,Workflow,Task'

# ACTIVE_AGENT — the agent selected for this wake (`claude` or `codex`).
# AGENT_SESSION_ID / AGENT_RESULT / AGENT_OUTPUT / AGENT_STATUS — set by
# invoke_agent/resume_agent. AGENT_STATUS is one of: ok | quota | error.
#
# The executor sees three planes of agent output, and which one a decision reads
# is a security property, not a style choice:
#   AGENT_RESULT — the agent's own answer. AGENT-AUTHORED, therefore UNTRUSTED:
#     the worker runs unreviewed branch code and can emit any string it likes.
#   AGENT_STDERR — the session's stderr, as written by the agent CLI. Useful for
#     diagnosis, but NOT trustworthy input for a control decision: the sidecar is
#     a file under the state tree and the worker has a shell, so it can be
#     rewritten. Do not gate behaviour on a string found here.
#   AGENT_ELAPSED_SECS — how long the invocation took, measured by this executor.
#     The only account of the session the worker cannot author, and therefore the
#     one a control decision may rest on.
#   AGENT_OUTPUT — AGENT_RESULT plus AGENT_STDERR, for diagnostics and issue
#     comments. Never key a control decision on a string found here: a match can
#     have come from either plane.
# Anything parsing what the agent SAID — the workflow outcome sentinel above all —
# reads AGENT_RESULT, because a stderr line appended to AGENT_OUTPUT lands after
# the answer and would displace a sentinel the contract requires to be final.
ACTIVE_AGENT=""
AGENT_SESSION_ID=""
AGENT_RESULT=""
AGENT_STDERR=""
AGENT_OUTPUT=""
AGENT_STATUS=""
AGENT_ELAPSED_SECS=0

# codex_model_is_supported <model>
# PRauto accepts only formal model identifiers when a deployment explicitly
# opts into `codex exec -m`. An empty value means inherit the authenticated
# account's Codex default model. Do not pass legacy role aliases (for example,
# "terra") through to the CLI: they are not valid identifiers.
codex_model_is_supported() {
  case "$1" in
    ""|gpt-5.6|gpt-5.6-terra|gpt-5.6-luna) return 0 ;;
    *) return 1 ;;
  esac
}

# codex_effort_is_supported <effort>
# Keep the explicit override contract narrow and aligned with the Codex
# reasoning-effort values supported by the configured model family.
codex_effort_is_supported() {
  case "$1" in
    low|medium|high|xhigh|max|ultra) return 0 ;;
    *) return 1 ;;
  esac
}

# validate_codex_override
# The model and effort are an atomic explicit override. A missing pair leaves
# both CLI flags absent so Codex uses account defaults; a partial/invalid pair
# fails before an external Codex invocation.
validate_codex_override() {
  local model="${PRAUTO_CODEX_MODEL-}"
  local effort="${PRAUTO_CODEX_EFFORT-}"
  if [[ -z "$model" && -z "$effort" ]]; then
    return 0
  fi
  if [[ -z "$model" || -z "$effort" ]]; then
    warn "PRAUTO_CODEX_MODEL and PRAUTO_CODEX_EFFORT must be set together, or both left unset."
    return 1
  fi
  if ! codex_model_is_supported "$model"; then
    warn "Invalid PRAUTO_CODEX_MODEL='${model}'. Use one of: gpt-5.6, gpt-5.6-terra, gpt-5.6-luna."
    return 1
  fi
  if ! codex_effort_is_supported "$effort"; then
    warn "Invalid PRAUTO_CODEX_EFFORT='${effort}'. Use one of: low, medium, high, xhigh, max, ultra."
    return 1
  fi
  return 0
}

# post_codex_override_invalid_comment <issue_number>
# Diagnostic evidence for a rejected/malformed PRAUTO_CODEX_MODEL/PRAUTO_CODEX_EFFORT
# override (see validate_codex_override). This is a configuration/account-
# compatibility failure, not a work-item failure: the issue is left exactly as
# it was, no worker starts, and no retry is burned.
post_codex_override_invalid_comment() {
  local issue_number="$1"
  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "prauto(${PRAUTO_WORKER_ID}): Codex model/effort override is invalid or unsupported (PRAUTO_CODEX_MODEL='${PRAUTO_CODEX_MODEL:-}', PRAUTO_CODEX_EFFORT='${PRAUTO_CODEX_EFFORT:-}'). Fix the override in this worker's configuration; the issue is left unchanged and will be retried on a later heartbeat once resolved." \
    2>/dev/null || warn "Failed to post Codex-override-invalid comment on issue #${issue_number}."
}

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
    if grep -qi "rate limit\|quota\|session limit" <<< "$raw"; then
      AGENT_STATUS=quota
    else
      AGENT_STATUS=error
    fi
    return
  fi

  if [[ "$exit_code" -eq 0 ]]; then
    AGENT_STATUS=ok
  elif grep -qi "rate limit\|quota\|session limit\|api_error\|API error" <<< "$raw"; then
    AGENT_STATUS=quota
  else
    AGENT_STATUS=error
  fi
}

# invoke_agent <prompt> <allowed_tools> <max_turns> [budget] [deny_tools] [env_overrides...]
# Dispatch a fresh session under ACTIVE_AGENT. Claude receives a harness-created
# id; Codex records only the native `thread.started` id. Sets AGENT_SESSION_ID,
# AGENT_RESULT, AGENT_STDERR, AGENT_OUTPUT, AGENT_STATUS. Any trailing
# NAME=VALUE arguments are applied to this invocation's environment only (Claude
# only), so a phase that needs a CLI knob does not impose it on every other phase.
# deny_tools defaults to DENY_TOOLS; a caller that
# needs a stricter block (e.g. a fix session appending FIX_DENY_TOOLS_EXTRA)
# passes its own combined value — see DENY_TOOLS's declaration comment for why
# this, not allowed_tools, is the only Claude-enforced removal, and why this is
# Claude-only (Codex receives no tool flags at all, below).
invoke_agent() {
  local prompt="$1" allowed_tools="$2" max_turns="$3" budget="${4:-}" deny_tools="${5:-$DENY_TOOLS}"
  shift 5 2>/dev/null || shift $#
  local -a env_overrides=("$@")
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
    if ! validate_codex_override; then
      AGENT_SESSION_ID=""
      AGENT_OUTPUT="Invalid Codex model/effort override; Codex was not invoked."
      AGENT_RESULT="$AGENT_OUTPUT"
      AGENT_STDERR=""
      AGENT_STATUS=error
      return 0
    fi
    # Codex deliberately receives none of Claude's session/tool/turn/budget
    # flags. Its thread id is emitted in JSONL after startup.
    cmd=(codex exec --json --sandbox workspace-write)
    # ChatGPT-authenticated Codex accounts can reject an explicit `-m` even
    # for formal identifiers. With no model override, leave both model knobs
    # absent so the CLI uses its account-default model and reasoning settings.
    # API/account environments that opt into an explicit model receive its
    # configured reasoning effort as the same deliberate override.
    if [[ -n "${PRAUTO_CODEX_MODEL-}" ]]; then
      cmd+=(-m "$PRAUTO_CODEX_MODEL"
        -c "model_reasoning_effort=$PRAUTO_CODEX_EFFORT")
    fi
    # `--` prevents a prompt beginning with '-' from being parsed as a Codex
    # option. It follows all explicit fresh-session options.
    cmd+=(-- "$prompt")
  else
    # `env NAME=VALUE ... claude` scopes the knobs to this child process. A bare
    # `NAME=VALUE func` prefix would not: bash does not export such assignments to
    # the commands a function runs unless the name was already exported.
    cmd=()
    [[ "${#env_overrides[@]}" -gt 0 ]] && cmd=(env "${env_overrides[@]}")
    cmd+=(claude -p "$prompt"
      --append-system-prompt-file "$system_file"
      --model "${PRAUTO_CLAUDE_MODEL:-opus}"
      --effort "${PRAUTO_CLAUDE_EFFORT:-high}"
      --output-format json
      --session-id "$session_id"
      --max-turns "$max_turns"
      --allowedTools "$allowed_tools"
      --disallowedTools "$deny_tools"
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
  # Measure the wall-clock span here, in the executor. This is the one account of
  # how long a session ran that the worker has no way to influence, and it is what
  # classifies a session the agent CLI killed on its own background-task wait
  # ceiling. Deriving that from the session's output instead would key a control
  # decision on text a worker can write.
  local started_at ended_at
  started_at=$(date +%s)
  if run_with_timeout "$agent_timeout" "${cmd[@]}" > "$output_file" 2> "$stderr_file"; then
    code=0
  else
    code=$?
  fi
  ended_at=$(date +%s)
  AGENT_ELAPSED_SECS=$(( ended_at - started_at ))

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
    AGENT_RESULT="$AGENT_OUTPUT"
    AGENT_STDERR=""
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
    AGENT_RESULT="$AGENT_OUTPUT"
    AGENT_STDERR=""
    if [[ -s "$stderr_file" ]]; then
      local claude_stderr
      claude_stderr=$(cat "$stderr_file" 2>/dev/null || printf '')
      AGENT_STDERR="$claude_stderr"
      [[ -n "$AGENT_OUTPUT" ]] && AGENT_OUTPUT="${AGENT_OUTPUT}
${claude_stderr}" || AGENT_OUTPUT="$claude_stderr"
    fi
    classify_exit "$output_file" "$code" claude "$stderr_file"
  fi
}

# resume_agent <prompt> <allowed_tools> <max_turns> <session_id> [budget] [deny_tools] [env_overrides...]
# Resume an existing session under ACTIVE_AGENT. Same agent as the original —
# a session cannot migrate agents; agent-switch is only reachable via
# abandon+restart. Sets AGENT_SESSION_ID, AGENT_RESULT, AGENT_STDERR,
# AGENT_OUTPUT, AGENT_STATUS. deny_tools defaults to DENY_TOOLS, and trailing
# NAME=VALUE arguments scope this invocation's environment — both the same
# contract as invoke_agent. A resumed session is a continuation of the phase that
# started it, so it must be handed the SAME environment: a resumed implementation
# that lost the dynamic-workflow opt-in could not satisfy its wf-minimal binding.
resume_agent() {
  local prompt="$1" allowed_tools="$2" max_turns="$3" session_id="$4" budget="${5:-}" deny_tools="${6:-$DENY_TOOLS}"
  shift 6 2>/dev/null || shift $#
  local -a env_overrides=("$@")
  if [[ "$ACTIVE_AGENT" == "codex" ]]; then
    if [[ "$session_id" != "${PAUSED_SESSION_ID:-}" ]] || \
       ! codex_pause_marker_is_trusted "${CUR_ISSUE_NUMBER:-}"; then
      warn "Refusing Codex resume without a matching trusted native-session anchor."
      AGENT_SESSION_ID=""
      AGENT_OUTPUT=""
      AGENT_RESULT=""
      AGENT_STDERR=""
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
    # The documented resume grammar is [SESSION_ID] [PROMPT]; `--` protects
    # either positional value from option parsing without changing that order.
    cmd=(codex exec resume --json -- "$session_id" "$prompt")
  else
    cmd=()
    [[ "${#env_overrides[@]}" -gt 0 ]] && cmd=(env "${env_overrides[@]}")
    cmd+=(claude -p "$prompt"
      --resume "$session_id"
      --append-system-prompt-file "$system_file"
      --model "${PRAUTO_CLAUDE_MODEL:-opus}"
      --effort "${PRAUTO_CLAUDE_EFFORT:-high}"
      --output-format json
      --max-turns "$max_turns"
      --allowedTools "$allowed_tools"
      --disallowedTools "$deny_tools"
      --dangerously-skip-permissions)
    [[ -n "$budget" ]] && cmd+=(--max-budget-usd "$budget")
  fi

  info "Resuming ${ACTIVE_AGENT} session ${session_id}..."
  # Same wall-clock backstop as the fresh invocation (see invoke_agent): a resume
  # that hangs waiting for token reset must not wedge the heartbeat.
  local agent_timeout="${PRAUTO_AGENT_TIMEOUT_SECS:-86400}"
  # Measure the wall-clock span here, in the executor. This is the one account of
  # how long a session ran that the worker has no way to influence, and it is what
  # classifies a session the agent CLI killed on its own background-task wait
  # ceiling. Deriving that from the session's output instead would key a control
  # decision on text a worker can write.
  local started_at ended_at
  started_at=$(date +%s)
  if run_with_timeout "$agent_timeout" "${cmd[@]}" > "$output_file" 2> "$stderr_file"; then
    code=0
  else
    code=$?
  fi
  ended_at=$(date +%s)
  AGENT_ELAPSED_SECS=$(( ended_at - started_at ))

  if [[ "$ACTIVE_AGENT" == "codex" ]]; then
    AGENT_OUTPUT=$(codex_final_output "$output_file")
    [[ -z "$AGENT_OUTPUT" ]] && AGENT_OUTPUT=$(cat "$output_file" 2>/dev/null || printf '')
    if [[ -z "$AGENT_OUTPUT" ]] && [[ -s "$stderr_file" ]]; then
      AGENT_OUTPUT=$(cat "$stderr_file" 2>/dev/null || printf '')
    fi
    AGENT_RESULT="$AGENT_OUTPUT"
    AGENT_STDERR=""
    classify_exit "$output_file" "$code" codex
  else
    AGENT_OUTPUT=$(jq -r '.result // empty' "$output_file" 2>/dev/null || printf '')
    [[ -z "$AGENT_OUTPUT" ]] && AGENT_OUTPUT=$(cat "$output_file" 2>/dev/null || printf '')
    AGENT_RESULT="$AGENT_OUTPUT"
    AGENT_STDERR=""
    if [[ -s "$stderr_file" ]]; then
      local claude_stderr
      claude_stderr=$(cat "$stderr_file" 2>/dev/null || printf '')
      AGENT_STDERR="$claude_stderr"
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
    # AGENT_RESULT, not AGENT_OUTPUT: this text is published verbatim by
    # post_plan_comment and parsed by resolve_change_size, so a session stderr
    # line must not reach either.
    ANALYSIS_OUTPUT="$AGENT_RESULT"
  fi
  printf '%s' "$ANALYSIS_OUTPUT" > "${CUR_SESSION_DIR}/analysis.txt"
}

# run_implementation <issue_number> <branch> <analysis_output>
# Implementation phase: drive wf-minimal via the Workflow tool. Fresh session each
# time; the workflow restarts rather than resumes, so only committed work is
# continuity. Sets IMPL_SESSION_ID, IMPL_RESULT (the agent's answer, which carries
# the outcome sentinel), IMPL_STDERR (the CLI's stderr, for diagnosis only),
# IMPL_ELAPSED_SECS (measured by this executor) and IMPL_OUTPUT (for reporting).
run_implementation() {
  local issue_number="$1" branch="$2" analysis_output="$3"
  local prompt
  prompt=$(render_prompt "${PRAUTO_DIR}/prompts/implementation.md" \
    "number=${issue_number}" "branch=${branch}" "base_branch=${PRAUTO_BASE_BRANCH}" \
    "author_name=${PRAUTO_GIT_AUTHOR_NAME}" "author_email=${PRAUTO_GIT_AUTHOR_EMAIL}" \
    "analysis_output=${analysis_output}")

  invoke_agent "$prompt" "$IMPLEMENTATION_ALLOWED_TOOLS" "${PRAUTO_CLAUDE_MAX_TURNS_IMPLEMENTATION:-400}" \
    "${PRAUTO_CLAUDE_MAX_BUDGET_IMPLEMENTATION:-}" "$DENY_TOOLS" "${IMPLEMENTATION_CLAUDE_ENV[@]}"
  IMPL_SESSION_ID="$AGENT_SESSION_ID"
  IMPL_RESULT="$AGENT_RESULT"
  IMPL_STDERR="$AGENT_STDERR"
  IMPL_ELAPSED_SECS="$AGENT_ELAPSED_SECS"
  IMPL_OUTPUT="$AGENT_OUTPUT"
  printf '%s' "$AGENT_OUTPUT" > "${CUR_SESSION_DIR}/implementation.json"
}

# encode_regression_evidence <failed_stages> <test_output>
# Serialize untrusted regression output as one base64-encoded JSON value before
# it crosses into an agent prompt.  The prompt labels it data, never trusted
# instructions.  Base64 is a transport delimiter, not a security boundary.
encode_regression_evidence() {
  local failed_stages="$1" test_output="$2" payload
  payload=$(jq -cn --arg failed_stages "$failed_stages" --arg test_output "$test_output" \
    '{failed_stages: $failed_stages, test_output: $test_output}') || return 1
  printf '%s' "$payload" | base64 | tr -d '\n'
}

# run_integration_fix_session <issue_number> <branch> <failed_stages> <test_output> <mode>
# The single, turn-bounded integration repair loop shared by Stage 3 (pre-PR,
# mode=pre-pr — no PR exists yet; the executor reruns the failed stage(s)
# itself rather than consuming this session's structured record) and Stage 5
# (post-PR readiness gate, mode=post-pr — the record is validated pass
# evidence for the executor's targeted retry). The executor — never the
# worker — pushes and independently repeats exactly failed_stages against the
# committed head afterward; mode only changes what the prompt tells the worker
# about that context.
run_integration_fix_session() {
  local issue_number="$1" branch="$2" failed_stages="$3" test_output="$4" mode="${5:-post-pr}" evidence_base64
  if [[ ${#test_output} -gt 30000 ]]; then test_output="${test_output:0:30000}
... (truncated)"; fi
  evidence_base64=$(encode_regression_evidence "$failed_stages" "$test_output") || {
    warn "Could not serialize targeted regression evidence."
    AGENT_STATUS=error; AGENT_OUTPUT="Targeted regression evidence serialization failed."; AGENT_RESULT="$AGENT_OUTPUT"; AGENT_STDERR=""; return 0
  }
  local mode_context
  if [[ "$mode" == "pre-pr" ]]; then
    mode_context="No PR exists yet for this issue — this is the pre-PR integration fix loop, which runs before the branch is ever pushed to a PR. After this session ends, the executor keeps the dev-env lock it already holds (no reacquire), redeploys the branch's API from your committed head only when it changed \`src/api/\`, \`src/backend/\`, or \`src/shared/\`, and reruns BOTH integration groups — not only the ones named as failed below — as a fresh attempt. It does not read or validate the structured JSON record below as pass evidence for this loop — only its own rerun decides whether this attempt passed — so report an unresolved stage honestly rather than withholding it because the record could not be emitted."
  else
    mode_context="A PR already exists and is open for this issue — this is the post-PR readiness gate's fix session. After this session ends, the executor rebuilds and redeploys only the branch artifacts required by the recorded failed stages, reacquires the dev-env lock, and reruns exactly those stages against your committed head. A successful targeted retry — validated against the structured JSON record below — is what allows the executor to mark the PR review-ready."
  fi
  local prompt
  prompt=$(render_prompt "${PRAUTO_DIR}/prompts/integration-fix.md" \
    "number=${issue_number}" "branch=${branch}" "failed_stages=${failed_stages}" "evidence_base64=${evidence_base64}" \
    "mode_context=${mode_context}" \
    "author_name=${PRAUTO_GIT_AUTHOR_NAME}" "author_email=${PRAUTO_GIT_AUTHOR_EMAIL}")
  invoke_agent "$prompt" "$IMPLEMENTATION_ALLOWED_TOOLS" "${PRAUTO_CLAUDE_MAX_TURNS_INTEGRATION_FIX:-200}" \
    "${PRAUTO_CLAUDE_MAX_BUDGET_INTEGRATION_FIX:-${PRAUTO_CLAUDE_MAX_BUDGET_IMPLEMENTATION:-}}" \
    "${DENY_TOOLS},${FIX_DENY_TOOLS_EXTRA}"
}

# run_e2e_fix_session <issue_number> <branch> <test_output>
# Pre-PR only (Stage 4 report-only fix attempt); no post-PR caller exists.
run_e2e_fix_session() {
  local issue_number="$1" branch="$2" test_output="$3" evidence_base64
  if [[ ${#test_output} -gt 30000 ]]; then test_output="${test_output:0:30000}
... (truncated)"; fi
  evidence_base64=$(encode_regression_evidence "E2E" "$test_output") || {
    warn "Could not serialize E2E fix evidence."
    AGENT_STATUS=error; AGENT_OUTPUT="E2E fix evidence serialization failed."; AGENT_RESULT="$AGENT_OUTPUT"; AGENT_STDERR=""; return 0
  }
  local prompt
  prompt=$(render_prompt "${PRAUTO_DIR}/prompts/e2e-fix.md" \
    "number=${issue_number}" "branch=${branch}" "evidence_base64=${evidence_base64}" \
    "author_name=${PRAUTO_GIT_AUTHOR_NAME}" "author_email=${PRAUTO_GIT_AUTHOR_EMAIL}")
  invoke_agent "$prompt" "$IMPLEMENTATION_ALLOWED_TOOLS" "${PRAUTO_CLAUDE_MAX_TURNS_E2E_FIX:-${PRAUTO_CLAUDE_MAX_TURNS_INTEGRATION_FIX:-50}}" \
    "${PRAUTO_CLAUDE_MAX_BUDGET_E2E_FIX:-${PRAUTO_CLAUDE_MAX_BUDGET_IMPLEMENTATION:-}}" \
    "${DENY_TOOLS},${FIX_DENY_TOOLS_EXTRA}"
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
