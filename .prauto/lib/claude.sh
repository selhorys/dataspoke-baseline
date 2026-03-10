# Claude Code CLI wrapper for prauto.
# Source this file — do not execute directly.
# Requires: helpers.sh sourced, PRAUTO_DIR set, config loaded, claude CLI available.

# Tool whitelists and denylists per spec.
ANALYSIS_ALLOWED_TOOLS='Read,Write,Glob,Grep,Bash(git log *),Bash(git diff *),Bash(git status *),Bash(git branch *)'

IMPLEMENTATION_ALLOWED_TOOLS='Read,Write,Edit,Glob,Grep,Bash(git log *),Bash(git diff *),Bash(git status *),Bash(git branch *),Bash(git add *),Bash(git commit *),Bash(uv run pytest *),Bash(uv run python3 *),Bash(uv run ruff *),Bash(uv run mypy *),Bash(uv sync *),Bash(npm run *),Bash(npx prettier *),Bash(npx tsc *)'

DENY_TOOLS='Bash(git push *),Bash(rm -rf *),Bash(sudo *),Bash(kubectl *),Bash(helm *),Bash(curl *),Bash(wget *),Bash(gh *),Read(.prauto/config.local.env),Read(.prauto/state/*),WebFetch,WebSearch'

# Substitute template variables in a prompt file.
# Usage: render_prompt <template_file> <var1=val1> <var2=val2> ...
render_prompt() {
  local template_file="$1"
  shift

  if [[ ! -f "$template_file" ]]; then
    error "Prompt template not found: $template_file"
  fi

  local content
  content=$(cat "$template_file")

  for assignment in "$@"; do
    local key="${assignment%%=*}"
    local value="${assignment#*=}"
    content="${content//\{$key\}/$value}"
  done

  echo "$content"
}

# Prepare the system-append prompt with worker identity substituted.
prepare_system_prompt() {
  local system_file="${PRAUTO_DIR}/prompts/system-append.md"
  local rendered_file="${PRAUTO_DIR}/state/.system-append-rendered.md"

  render_prompt "$system_file" \
    "PRAUTO_WORKER_ID=${PRAUTO_WORKER_ID}" \
    "PRAUTO_GIT_AUTHOR_NAME=${PRAUTO_GIT_AUTHOR_NAME}" \
    "PRAUTO_GIT_AUTHOR_EMAIL=${PRAUTO_GIT_AUTHOR_EMAIL}" \
    > "$rendered_file"

  echo "$rendered_file"
}

# Run Claude and capture output + session ID.
# Usage: invoke_claude <prompt> <allowed_tools> <max_turns> [budget]
# Sets: CLAUDE_SESSION_ID, CLAUDE_OUTPUT
invoke_claude() {
  local prompt="$1"
  local allowed_tools="$2"
  local max_turns="$3"
  local budget="${4:-}"

  local system_prompt_file
  system_prompt_file=$(prepare_system_prompt)

  local -a cmd=(claude)

  cmd+=(
    -p "$prompt"
    --append-system-prompt-file "$system_prompt_file"
    --model "$PRAUTO_CLAUDE_MODEL"
    --output-format json
    --max-turns "$max_turns"
    --allowedTools "$allowed_tools"
    --disallowedTools "$DENY_TOOLS"
    --dangerously-skip-permissions
  )

  if [[ -n "$budget" ]]; then
    cmd+=(--max-budget-usd "$budget")
  fi

  local output_file
  if [[ -n "${CUR_SESSION_DIR:-}" ]] && [[ -d "${CUR_SESSION_DIR:-}" ]]; then
    output_file="${CUR_SESSION_DIR}/claude-output-$$.json"
  else
    output_file=$(mktemp)
  fi

  # NOTE: claude -p stdout is invisible to the Bash tool's stdout capture.
  # File redirect is required — do NOT use $(...) command substitution.
  info "Invoking Claude (max_turns=$max_turns)..."
  if "${cmd[@]}" > "$output_file" 2>&1; then
    info "Claude invocation completed."
  else
    warn "Claude invocation exited with non-zero status."
  fi

  # Extract session ID and result from JSON output
  CLAUDE_SESSION_ID=$(jq -r '.session_id // empty' "$output_file" 2>/dev/null || echo "")
  CLAUDE_OUTPUT=$(jq -r '.result // empty' "$output_file" 2>/dev/null || echo "")

  # Detect error subtypes (e.g. error_max_turns) — output has no .result field
  local subtype
  subtype=$(jq -r '.subtype // empty' "$output_file" 2>/dev/null || echo "")
  if [[ "$subtype" == error_* ]]; then
    warn "Claude returned subtype '${subtype}'. Output may be incomplete."
    CLAUDE_OUTPUT=""
  fi

  # If JSON parsing produced no result and it's not an error subtype, use raw output
  if [[ -z "$CLAUDE_OUTPUT" ]] && [[ "$subtype" != error_* ]]; then
    CLAUDE_OUTPUT=$(cat "$output_file")
  fi

  # Keep output file in session dir for debugging; delete only system temp files
  if [[ -z "${CUR_SESSION_DIR:-}" ]] || [[ "$output_file" != "${CUR_SESSION_DIR}/"* ]]; then
    rm -f "$output_file"
  fi

  # Return non-zero when output is empty so callers can handle gracefully
  if [[ -z "$CLAUDE_OUTPUT" ]]; then
    return 1
  fi
}

# Phase 1: Analysis (read + plan write).
# Usage: run_analysis <issue_number> <issue_title> <issue_body> [counter_proposal]
# Sets: ANALYSIS_OUTPUT, ANALYSIS_SESSION_ID
run_analysis() {
  local issue_number="$1"
  local issue_title="$2"
  local issue_body="$3"
  local counter_proposal="${4:-}"
  local previous_plan="${5:-}"

  # Plan file: Claude writes the full plan here via Write tool
  local session_out_dir="${CUR_SESSION_DIR:-${SESSIONS_DIR}}"
  local plan_file="${session_out_dir}/plan.md"

  local prompt
  prompt=$(render_prompt "${PRAUTO_DIR}/prompts/issue-analysis.md" \
    "number=${issue_number}" \
    "title=${issue_title}" \
    "body=${issue_body}" \
    "plan_file=${plan_file}")

  if [[ -n "$counter_proposal" ]]; then
    if [[ -n "$previous_plan" ]]; then
      prompt="${prompt}

## Previous Plan

Use this as your starting point. Revise it based on the feedback below — do not start from scratch.

${previous_plan}"
    fi

    prompt="${prompt}

## Feedback on Previous Plan

The following counter-proposal was made. Revise the plan above to address this feedback:

${counter_proposal}"
  fi

  local budget="${PRAUTO_CLAUDE_MAX_BUDGET_ANALYSIS:-}"

  if ! invoke_claude "$prompt" "$ANALYSIS_ALLOWED_TOOLS" "$PRAUTO_CLAUDE_MAX_TURNS_ANALYSIS" "$budget"; then
    warn "Analysis produced no usable output for issue #${issue_number}."
    ANALYSIS_OUTPUT=""
    ANALYSIS_SESSION_ID="$CLAUDE_SESSION_ID"
    return 1
  fi

  ANALYSIS_SESSION_ID="$CLAUDE_SESSION_ID"

  # Prefer the plan file written by Claude via Write tool over .result
  if [[ -f "$plan_file" ]] && [[ -s "$plan_file" ]]; then
    ANALYSIS_OUTPUT=$(cat "$plan_file")
    info "Plan captured from file ($(wc -c < "$plan_file") bytes)."
  else
    warn "Plan file not found at ${plan_file}. Falling back to .result output."
    ANALYSIS_OUTPUT="$CLAUDE_OUTPUT"
  fi

  # Save to session dir
  echo "$ANALYSIS_OUTPUT" > "${session_out_dir}/analysis.txt"
  if [[ -n "$CLAUDE_SESSION_ID" ]]; then
    info "Analysis claude session saved: ${CLAUDE_SESSION_ID}"
  fi
}

# Phase 2: Implementation (read + write).
# Always starts a fresh session. Claude checks the branch for existing work.
# Usage: run_implementation <issue_number> <branch> <analysis_output>
# Sets: IMPL_SESSION_ID
run_implementation() {
  local issue_number="$1"
  local branch="$2"
  local analysis_output="$3"

  local prompt
  prompt=$(render_prompt "${PRAUTO_DIR}/prompts/implementation.md" \
    "number=${issue_number}" \
    "branch=${branch}" \
    "base_branch=${PRAUTO_BASE_BRANCH}" \
    "author_name=${PRAUTO_GIT_AUTHOR_NAME}" \
    "author_email=${PRAUTO_GIT_AUTHOR_EMAIL}" \
    "analysis_output=${analysis_output}")

  local budget="${PRAUTO_CLAUDE_MAX_BUDGET_IMPLEMENTATION:-}"

  invoke_claude "$prompt" "$IMPLEMENTATION_ALLOWED_TOOLS" "$PRAUTO_CLAUDE_MAX_TURNS_IMPLEMENTATION" "$budget"

  IMPL_SESSION_ID="$CLAUDE_SESSION_ID"

  # Save session output to session dir
  if [[ -n "$CLAUDE_SESSION_ID" ]]; then
    local session_out_dir="${CUR_SESSION_DIR:-${SESSIONS_DIR}}"
    echo "$CLAUDE_OUTPUT" > "${session_out_dir}/implementation.json"
    info "Implementation claude session saved: ${CLAUDE_SESSION_ID}"
  fi
}

# Phase 2b: Fix integration test failures.
# Invokes Claude with the failing test output to diagnose and fix.
# Usage: run_integration_fix_session <issue_number> <branch> <test_output>
# Sets: INTEG_FIX_SESSION_ID
run_integration_fix_session() {
  local issue_number="$1"
  local branch="$2"
  local test_output="$3"

  # Truncate test output if too long to fit in prompt
  local truncated_output="$test_output"
  if [[ ${#test_output} -gt 30000 ]]; then
    truncated_output="${test_output:0:30000}
... (truncated)"
  fi

  local prompt
  prompt=$(render_prompt "${PRAUTO_DIR}/prompts/integration-fix.md" \
    "number=${issue_number}" \
    "branch=${branch}" \
    "test_output=${truncated_output}" \
    "author_name=${PRAUTO_GIT_AUTHOR_NAME}" \
    "author_email=${PRAUTO_GIT_AUTHOR_EMAIL}")

  local budget="${PRAUTO_CLAUDE_MAX_BUDGET_INTEGRATION_FIX:-${PRAUTO_CLAUDE_MAX_BUDGET_IMPLEMENTATION:-}}"
  local max_turns="${PRAUTO_CLAUDE_MAX_TURNS_INTEGRATION_FIX:-50}"

  invoke_claude "$prompt" "$IMPLEMENTATION_ALLOWED_TOOLS" "$max_turns" "$budget"

  INTEG_FIX_SESSION_ID="$CLAUDE_SESSION_ID"

  if [[ -n "$CLAUDE_SESSION_ID" ]]; then
    local session_out_dir="${CUR_SESSION_DIR:-${SESSIONS_DIR}}"
    echo "$CLAUDE_OUTPUT" > "${session_out_dir}/integration-fix.json"
    info "Integration fix claude session saved: ${CLAUDE_SESSION_ID}"
  fi
}

# Phase 4: Generate squash commit message from issue description and diff.
# Uses a single-turn, no-tool Claude invocation to produce a conventional commit message.
# Usage: generate_squash_commit_message <issue_number> <issue_title> <issue_body> <pr_number> <diff_stat> <diff>
# Sets: SQUASH_COMMIT_MESSAGE
generate_squash_commit_message() {
  local issue_number="$1"
  local issue_title="$2"
  local issue_body="$3"
  local pr_number="$4"
  local diff_stat="$5"
  local diff="$6"

  # Truncate diff to ~4000 chars to stay within prompt budget
  local truncated_diff="$diff"
  if [[ ${#diff} -gt 4000 ]]; then
    truncated_diff="${diff:0:4000}
... (truncated)"
  fi

  local prompt
  prompt=$(render_prompt "${PRAUTO_DIR}/prompts/squash-commit.md" \
    "issue_number=${issue_number}" \
    "issue_title=${issue_title}" \
    "issue_body=${issue_body}" \
    "pr_number=${pr_number}" \
    "diff_stat=${diff_stat}" \
    "diff=${truncated_diff}")

  local budget="${PRAUTO_CLAUDE_MAX_BUDGET_ANALYSIS:-}"

  # Minimal invocation: 1 turn, no tools
  invoke_claude "$prompt" "" "1" "$budget"

  SQUASH_COMMIT_MESSAGE="$CLAUDE_OUTPUT"

  # Strip markdown fences if Claude wrapped the output
  SQUASH_COMMIT_MESSAGE=$(echo "$SQUASH_COMMIT_MESSAGE" | sed '/^```/d')

  if [[ -z "$SQUASH_COMMIT_MESSAGE" ]]; then
    warn "Claude failed to generate commit message. Falling back to PR title."
    SQUASH_COMMIT_MESSAGE="${issue_title}

(issue #${issue_number}, PR #${pr_number})"
  fi

  info "Squash commit message generated."
}

# PR review phase: address reviewer feedback.
# Always starts a fresh session with full reviewer comments as context.
# Usage: run_pr_review <issue_number> <branch> <reviewer_comments> <plan>
# Sets: REVIEW_SESSION_ID, REVIEW_RESPONSE
run_pr_review() {
  local issue_number="$1"
  local branch="$2"
  local reviewer_comments="$3"
  local plan="${4:-}"

  local prompt
  prompt=$(render_prompt "${PRAUTO_DIR}/prompts/pr-review.md" \
    "number=${issue_number}" \
    "branch=${branch}" \
    "plan=${plan}" \
    "reviewer_comments=${reviewer_comments}" \
    "author_name=${PRAUTO_GIT_AUTHOR_NAME}" \
    "author_email=${PRAUTO_GIT_AUTHOR_EMAIL}")

  local budget="${PRAUTO_CLAUDE_MAX_BUDGET_IMPLEMENTATION:-}"

  invoke_claude "$prompt" "$IMPLEMENTATION_ALLOWED_TOOLS" "$PRAUTO_CLAUDE_MAX_TURNS_IMPLEMENTATION" "$budget"

  REVIEW_SESSION_ID="$CLAUDE_SESSION_ID"
  REVIEW_RESPONSE="$CLAUDE_OUTPUT"

  if [[ -n "$CLAUDE_SESSION_ID" ]]; then
    local session_out_dir="${CUR_SESSION_DIR:-${SESSIONS_DIR}}"
    echo "$CLAUDE_OUTPUT" > "${session_out_dir}/review.json"
    info "PR review claude session saved: ${CLAUDE_SESSION_ID}"
  fi
}

# Generate a response to plan feedback (counter-proposal).
# Uses a single-turn, no-tool Claude invocation to produce a concise response.
# Usage: generate_feedback_response <issue_number> <issue_title> <feedback> <previous_plan>
# Sets: FEEDBACK_RESPONSE_TEXT
generate_feedback_response() {
  local issue_number="$1"
  local issue_title="$2"
  local feedback="$3"
  local previous_plan="$4"

  local prompt
  prompt=$(render_prompt "${PRAUTO_DIR}/prompts/feedback-response.md" \
    "number=${issue_number}" \
    "title=${issue_title}" \
    "feedback=${feedback}" \
    "plan=${previous_plan}")

  local budget="${PRAUTO_CLAUDE_MAX_BUDGET_ANALYSIS:-}"

  # Minimal invocation: 1 turn, no tools
  invoke_claude "$prompt" "" "1" "$budget"

  FEEDBACK_RESPONSE_TEXT="$CLAUDE_OUTPUT"

  # Strip markdown fences if Claude wrapped the output
  FEEDBACK_RESPONSE_TEXT=$(echo "$FEEDBACK_RESPONSE_TEXT" | sed '/^```/d')

  if [[ -z "$FEEDBACK_RESPONSE_TEXT" ]]; then
    warn "Claude failed to generate feedback response for issue #${issue_number}."
  fi
}
