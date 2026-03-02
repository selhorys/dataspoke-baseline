# Claude Code CLI wrapper for prauto.
# Source this file — do not execute directly.
# Requires: helpers.sh sourced, PRAUTO_DIR set, config loaded, claude CLI available.

# Tool whitelists and denylists per spec.
ANALYSIS_ALLOWED_TOOLS='Read,Glob,Grep,Bash(git log *),Bash(git diff *),Bash(git status *),Bash(git branch *)'

IMPLEMENTATION_ALLOWED_TOOLS='Read,Write,Edit,Glob,Grep,Bash(git log *),Bash(git diff *),Bash(git status *),Bash(git branch *),Bash(git add *),Bash(git commit *),Bash(pytest *),Bash(python3 *),Bash(npm run *),Bash(npx prettier *),Bash(npx tsc *),Bash(ruff *)'

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
# Usage: invoke_claude <prompt> <allowed_tools> <max_turns> [budget] [resume_session_id]
# Sets: CLAUDE_SESSION_ID, CLAUDE_OUTPUT
invoke_claude() {
  local prompt="$1"
  local allowed_tools="$2"
  local max_turns="$3"
  local budget="${4:-}"
  local resume_session="${5:-}"

  local system_prompt_file
  system_prompt_file=$(prepare_system_prompt)

  local -a cmd=(claude)

  if [[ -n "$resume_session" ]]; then
    cmd+=(--resume "$resume_session")
  fi

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
  output_file=$(mktemp)

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

  # If JSON parsing fails, treat entire output as text
  if [[ -z "$CLAUDE_OUTPUT" ]]; then
    CLAUDE_OUTPUT=$(cat "$output_file")
  fi

  rm -f "$output_file"
}

# Phase 1: Analysis (read-only).
# Usage: run_analysis <issue_number> <issue_title> <issue_body> [counter_proposal]
# Sets: ANALYSIS_OUTPUT, ANALYSIS_SESSION_ID
run_analysis() {
  local issue_number="$1"
  local issue_title="$2"
  local issue_body="$3"
  local counter_proposal="${4:-}"

  local prompt
  prompt=$(render_prompt "${PRAUTO_DIR}/prompts/issue-analysis.md" \
    "number=${issue_number}" \
    "title=${issue_title}" \
    "body=${issue_body}")

  if [[ -n "$counter_proposal" ]]; then
    prompt="${prompt}

## Previous Plan Feedback

The following counter-proposal was made. Incorporate this feedback into your revised plan:

${counter_proposal}"
  fi

  local budget="${PRAUTO_CLAUDE_MAX_BUDGET_ANALYSIS:-}"

  invoke_claude "$prompt" "$ANALYSIS_ALLOWED_TOOLS" "$PRAUTO_CLAUDE_MAX_TURNS_ANALYSIS" "$budget"

  ANALYSIS_OUTPUT="$CLAUDE_OUTPUT"
  ANALYSIS_SESSION_ID="$CLAUDE_SESSION_ID"

  # Save session output
  if [[ -n "$CLAUDE_SESSION_ID" ]]; then
    echo "$CLAUDE_OUTPUT" > "${SESSIONS_DIR}/analysis-I-${issue_number}.txt"
    info "Analysis session saved: ${CLAUDE_SESSION_ID}"
  fi
}

# Phase 2: Implementation (read + write).
# Sets: IMPL_SESSION_ID
run_implementation() {
  local issue_number="$1"
  local branch="$2"
  local analysis_output="$3"
  local resume_session="${4:-}"

  local prompt
  if [[ -n "$resume_session" ]]; then
    prompt="Continue the implementation. Check what has been done so far and pick up where you left off."
  else
    prompt=$(render_prompt "${PRAUTO_DIR}/prompts/implementation.md" \
      "number=${issue_number}" \
      "branch=${branch}" \
      "author_name=${PRAUTO_GIT_AUTHOR_NAME}" \
      "author_email=${PRAUTO_GIT_AUTHOR_EMAIL}" \
      "analysis_output=${analysis_output}")
  fi

  local budget="${PRAUTO_CLAUDE_MAX_BUDGET_IMPLEMENTATION:-}"

  invoke_claude "$prompt" "$IMPLEMENTATION_ALLOWED_TOOLS" "$PRAUTO_CLAUDE_MAX_TURNS_IMPLEMENTATION" "$budget" "$resume_session"

  IMPL_SESSION_ID="$CLAUDE_SESSION_ID"

  # Save session output for potential resume
  if [[ -n "$CLAUDE_SESSION_ID" ]]; then
    echo "$CLAUDE_OUTPUT" > "${SESSIONS_DIR}/impl-I-${issue_number}.json"
    info "Implementation session saved: ${CLAUDE_SESSION_ID}"
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
# Sets: REVIEW_SESSION_ID
run_pr_review() {
  local issue_number="$1"
  local branch="$2"
  local reviewer_comments="$3"
  local resume_session="${4:-}"

  local prompt
  if [[ -n "$resume_session" ]]; then
    prompt="Continue addressing the reviewer feedback. Check what has been done so far and pick up where you left off."
  else
    prompt="Address the following reviewer feedback on PR for issue #${issue_number} (branch \`${branch}\`).

## Reviewer Comments

${reviewer_comments}

## Instructions

1. Read each reviewer comment carefully.
2. Make the requested changes.
3. Run tests to verify.
4. Run formatters (ruff for Python, npx prettier for TypeScript).
5. Stage and commit with conventional commit messages.
   Use: git commit --author=\"${PRAUTO_GIT_AUTHOR_NAME} <${PRAUTO_GIT_AUTHOR_EMAIL}>\"
6. Do NOT push. The orchestrator handles pushing."
  fi

  local budget="${PRAUTO_CLAUDE_MAX_BUDGET_IMPLEMENTATION:-}"

  invoke_claude "$prompt" "$IMPLEMENTATION_ALLOWED_TOOLS" "$PRAUTO_CLAUDE_MAX_TURNS_IMPLEMENTATION" "$budget" "$resume_session"

  REVIEW_SESSION_ID="$CLAUDE_SESSION_ID"

  if [[ -n "$CLAUDE_SESSION_ID" ]]; then
    echo "$CLAUDE_OUTPUT" > "${SESSIONS_DIR}/review-I-${issue_number}.json"
    info "PR review session saved: ${CLAUDE_SESSION_ID}"
  fi
}
