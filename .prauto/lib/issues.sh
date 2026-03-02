# Issue discovery and claiming for prauto.
# Source this file — do not execute directly.
# Requires: helpers.sh sourced, PRAUTO_GITHUB_REPO, PRAUTO_GITHUB_LABEL_* set, gh CLI available.

# Check if a matching comment already exists (idempotency guard).
# Usage: comment_exists <"issue"|"pr"> <number> <keyword>
# Returns 0 if found, 1 if not found.
comment_exists() {
  local target_type="$1"
  local target_number="$2"
  local keyword="$3"
  local prefix="prauto(${PRAUTO_WORKER_ID}): ${keyword}"

  gh "${target_type}" view "$target_number" \
    -R "$PRAUTO_GITHUB_REPO" \
    --json comments \
    --jq ".comments[] | select(.body | startswith(\"${prefix}\")) | .id" \
  | head -1 | grep -q .
}

# Fetch organization member logins as a JSON array.
# Usage: fetch_org_members
# Sets ORG_MEMBERS_JSON on success (e.g. '["alice","bob"]').
# Returns 0 on success, 1 on failure.
fetch_org_members() {
  local org_name="${PRAUTO_GITHUB_REPO%%/*}"
  ORG_MEMBERS_JSON=$(gh api "orgs/${org_name}/members" --paginate --jq '[.[].login]' \
    2>/dev/null | jq -s 'add') || {
    warn "Failed to fetch org members for '${org_name}'. Is it an organization?"
    return 1
  }
  local count
  count=$(echo "$ORG_MEMBERS_JSON" | jq 'length')
  info "Org-member filter enabled: ${count} members found in '${org_name}'."
  return 0
}

# Find the oldest eligible issue labeled prauto:ready.
# Sets FOUND_ISSUE_NUMBER, FOUND_ISSUE_TITLE, FOUND_ISSUE_BODY on success.
# Returns 0 if found, 1 if none.
find_eligible_issue() {
  local issues_json
  issues_json=$(gh issue list \
    -R "$PRAUTO_GITHUB_REPO" \
    --label "$PRAUTO_GITHUB_LABEL_READY" \
    --state open \
    --json number,title,body,labels,author \
    --limit 50 2>/dev/null) || {
    warn "Failed to list issues from GitHub."
    return 1
  }

  # If org-member filter is enabled, fetch the member list
  local org_members=""
  if [[ -n "${PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY:-}" ]]; then
    if ! fetch_org_members; then
      return 1
    fi
    org_members="$ORG_MEMBERS_JSON"
  fi

  # Filter out issues that already have wip or review labels, sort by number ascending
  local filtered
  if [[ -n "$org_members" ]]; then
    filtered=$(echo "$issues_json" | jq -r \
      --arg wip "$PRAUTO_GITHUB_LABEL_WIP" \
      --arg review "$PRAUTO_GITHUB_LABEL_REVIEW" \
      --argjson members "$org_members" '
      [.[] | select(
        (.labels | map(.name) | index($wip)) == null and
        (.labels | map(.name) | index($review)) == null and
        (.author.login as $a | $members | index($a) != null)
      )] | sort_by(.number) | .[0] // empty
    ')
  else
    filtered=$(echo "$issues_json" | jq -r \
      --arg wip "$PRAUTO_GITHUB_LABEL_WIP" \
      --arg review "$PRAUTO_GITHUB_LABEL_REVIEW" '
      [.[] | select(
        (.labels | map(.name) | index($wip)) == null and
        (.labels | map(.name) | index($review)) == null
      )] | sort_by(.number) | .[0] // empty
    ')
  fi

  if [[ -z "$filtered" ]]; then
    info "No eligible issues found with label ${PRAUTO_GITHUB_LABEL_READY}."
    return 1
  fi

  FOUND_ISSUE_NUMBER=$(echo "$filtered" | jq -r '.number')
  FOUND_ISSUE_TITLE=$(echo "$filtered" | jq -r '.title')
  FOUND_ISSUE_BODY=$(echo "$filtered" | jq -r '.body // ""')

  info "Found eligible issue: #${FOUND_ISSUE_NUMBER} — ${FOUND_ISSUE_TITLE}"
  return 0
}

# Claim an issue with optimistic locking.
# Returns 0 on success, 1 if another worker claimed it.
claim_issue() {
  local issue_number="$1"

  # Step 1: Check if prauto:wip is already present
  local current_labels
  current_labels=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json labels --jq '.labels[].name' 2>/dev/null)
  if echo "$current_labels" | grep -q "^${PRAUTO_GITHUB_LABEL_WIP}$"; then
    warn "Issue #${issue_number} already has ${PRAUTO_GITHUB_LABEL_WIP} — another worker claimed it."
    return 1
  fi

  # Step 2: Add prauto:wip label
  gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --add-label "$PRAUTO_GITHUB_LABEL_WIP" 2>/dev/null || {
    warn "Failed to add ${PRAUTO_GITHUB_LABEL_WIP} label to issue #${issue_number}."
    return 1
  }

  # Step 3: Brief delay then verify no race
  sleep 2
  local wip_comments
  wip_comments=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json comments --jq '[.comments[] | select(.body | startswith("prauto(")) | select(.body | contains("Claimed"))] | length' \
    2>/dev/null)

  if [[ "$wip_comments" -gt 0 ]]; then
    warn "Issue #${issue_number} was claimed by another worker during race window."
    return 1
  fi

  # Step 4: Remove prauto:ready, set assignee, post claim comment
  gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --remove-label "$PRAUTO_GITHUB_LABEL_READY" \
    --add-assignee "$PRAUTO_GITHUB_ACTOR" 2>/dev/null || true

  if ! comment_exists "issue" "$issue_number" "Claimed"; then
    gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
      --body "prauto(${PRAUTO_WORKER_ID}): Claimed this issue. Starting work." \
      2>/dev/null || warn "Failed to post claim comment on issue #${issue_number}."
  fi

  info "Claimed issue #${issue_number}."
  return 0
}

# Extract the change-size from the issue body.
# GitHub renders the dropdown as: ### Change Size\n\nMedium (...)
# Returns: "minor", "medium", or "major". Defaults to "medium" if unparseable.
extract_change_size() {
  local issue_body="$1"

  local size_line
  size_line=$(echo "$issue_body" | sed -n '/^### Change Size/,/^###/{/^### Change Size/d;/^###/d;/^$/d;p;}' | head -1)

  case "$size_line" in
    Minor*|minor*) echo "minor" ;;
    Major*|major*) echo "major" ;;
    *)             echo "medium" ;;
  esac
}

# Post the analysis plan as an issue comment.
# Usage: post_plan_comment <issue_number> <analysis_output> <change_size> [revision]
# revision defaults to 1. Each revision uses a unique keyword for idempotency.
post_plan_comment() {
  local issue_number="$1"
  local analysis_output="$2"
  local change_size="$3"
  local revision="${4:-1}"

  local keyword="Plan"
  if [[ "$revision" -gt 1 ]]; then
    keyword="Plan (rev ${revision})"
  fi

  # Check for existing plan comment with this revision (idempotency)
  if comment_exists "issue" "$issue_number" "$keyword"; then
    info "Plan comment (${keyword}) already exists on issue #${issue_number}. Skipping."
    return 0
  fi

  local footer
  if [[ "$change_size" == "minor" ]]; then
    footer='> This is a **Minor** change. Implementation will proceed automatically.'
  else
    local size_label
    size_label="$(tr '[:lower:]' '[:upper:]' <<< "${change_size:0:1}")${change_size:1}"
    footer="> This is a **${size_label}** change. Please review the plan above.
> Reply with \`go ahead\` to approve, or post a counter-proposal."
  fi

  local body
  body="prauto(${PRAUTO_WORKER_ID}): ${keyword}

## Implementation Plan

${analysis_output}

---
${footer}"

  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "$body" \
    2>/dev/null || warn "Failed to post plan comment on issue #${issue_number}."

  info "Plan comment posted on issue #${issue_number} (change_size=${change_size})."
}

# Check whether a plan has been approved on an issue.
# Looks for comments after the plan comment.
# Returns: 0 = approved ("go ahead"), 1 = no response yet, 2 = counter-proposal found.
# Sets COUNTER_PROPOSAL on return 2.
check_plan_approval() {
  local issue_number="$1"

  local plan_prefix="prauto(${PRAUTO_WORKER_ID}): Plan"

  # Fetch all comments as JSON array
  local comments_json
  comments_json=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json comments \
    --jq '.comments' 2>/dev/null) || {
    warn "Failed to fetch comments for issue #${issue_number}."
    return 1
  }

  # Find the timestamp of the last plan comment
  local plan_timestamp
  plan_timestamp=$(echo "$comments_json" | jq -r --arg prefix "$plan_prefix" '
    [.[] | select(.body | startswith($prefix))] | last | .createdAt // empty
  ')

  if [[ -z "$plan_timestamp" ]]; then
    warn "No plan comment found on issue #${issue_number}."
    return 1
  fi

  # Get non-prauto comments after the plan timestamp
  local after_comments
  after_comments=$(echo "$comments_json" | jq -r --arg ts "$plan_timestamp" '
    [.[] | select(.createdAt > $ts) | select(.body | startswith("prauto(") | not)]
  ')

  local comment_count
  comment_count=$(echo "$after_comments" | jq 'length')

  if [[ "$comment_count" -eq 0 ]]; then
    info "No response to plan yet on issue #${issue_number}."
    return 1
  fi

  # Check each comment — look for "go ahead" first
  local i body_trimmed
  for (( i = 0; i < comment_count; i++ )); do
    body_trimmed=$(echo "$after_comments" | jq -r ".[$i].body" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    if [[ "$body_trimmed" == "go ahead" ]]; then
      info "Plan approved on issue #${issue_number}."
      return 0
    fi
  done

  # No "go ahead" found — treat the latest non-prauto comment as a counter-proposal
  COUNTER_PROPOSAL=$(echo "$after_comments" | jq -r '.[-1].body')
  info "Counter-proposal found on issue #${issue_number}."
  return 2
}

# Check if the LATEST prauto comment on an issue has the quota-paused marker.
# Usage: has_quota_paused_comment <issue_number>
# Returns 0 if found, 1 if not found.
has_quota_paused_comment() {
  local issue_number="$1"
  local prefix="prauto(${PRAUTO_WORKER_ID}):"

  local latest_body
  latest_body=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json comments \
    --jq "[.comments[] | select(.body | startswith(\"${prefix}\"))] | last | .body // \"\"" \
    2>/dev/null) || return 1

  echo "$latest_body" | grep -q '<!-- prauto:quota-paused -->'
}

# Post a quota-paused notification on an issue.
# Idempotent: skips if the LATEST prauto comment already has the marker.
# Usage: post_quota_paused_comment <issue_number>
post_quota_paused_comment() {
  local issue_number="$1"

  if has_quota_paused_comment "$issue_number"; then
    info "Quota-paused comment already present on issue #${issue_number}. Skipping."
    return 0
  fi

  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "prauto(${PRAUTO_WORKER_ID}): Paused — Claude token quota exhausted. Will resume automatically when quota is available.

<!-- prauto:quota-paused -->" \
    2>/dev/null || warn "Failed to post quota-paused comment on issue #${issue_number}."

  info "Quota-paused comment posted on issue #${issue_number}."
}

# Post a quota-resumed notification on an issue.
# No idempotency guard — gated externally by has_quota_paused_comment.
# Usage: post_quota_resumed_comment <issue_number>
post_quota_resumed_comment() {
  local issue_number="$1"

  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "prauto(${PRAUTO_WORKER_ID}): Resumed — Claude token quota is now available. Continuing work." \
    2>/dev/null || warn "Failed to post quota-resumed comment on issue #${issue_number}."

  info "Quota-resumed comment posted on issue #${issue_number}."
}
