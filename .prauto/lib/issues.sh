# Issue discovery, claiming, and plan lifecycle for prauto.
# Source this file — do not execute directly.
# Requires: helpers.sh sourced (for comment_exists), config loaded, gh CLI available.

# Fetch the timestamp of the last time the prauto:ready label was set on an issue.
# Uses the GitHub timeline API to find the most recent "labeled" event for prauto:ready.
# All comment-scanning functions use this as a floor — comments before this timestamp are ignored.
# Usage: get_ready_label_timestamp <issue_number>
# Sets: READY_LABEL_TIMESTAMP (ISO 8601 string, or empty if not found)
get_ready_label_timestamp() {
  local issue_number="$1"
  READY_LABEL_TIMESTAMP=$(gh api "repos/${PRAUTO_GITHUB_REPO}/issues/${issue_number}/timeline" \
    --paginate \
    --jq '[.[] | select(.event == "labeled") | select(.label.name == "'"${PRAUTO_GITHUB_LABEL_READY}"'")] | last | .created_at // empty' \
    2>/dev/null) || READY_LABEL_TIMESTAMP=""
  if [[ -n "$READY_LABEL_TIMESTAMP" ]]; then
    info "Ready label timestamp for #${issue_number}: ${READY_LABEL_TIMESTAMP}"
  else
    warn "Could not determine ready label timestamp for #${issue_number}. No comment filtering."
  fi
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

# Post a response to plan feedback as an issue comment.
# Usage: post_feedback_response_comment <issue_number> <response_text>
post_feedback_response_comment() {
  local issue_number="$1"
  local response_text="$2"
  [[ -z "$response_text" ]] && return 0
  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "prauto(${PRAUTO_WORKER_ID}): Feedback response

${response_text}" \
    2>/dev/null || warn "Failed to post feedback response on issue #${issue_number}."
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
# Supports restart: issues reset to prauto:ready with stale comments are re-claimable.
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

  # Step 2: Record pre-claim timestamp, then add prauto:wip label
  local pre_claim_ts
  pre_claim_ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --add-label "$PRAUTO_GITHUB_LABEL_WIP" 2>/dev/null || {
    warn "Failed to add ${PRAUTO_GITHUB_LABEL_WIP} label to issue #${issue_number}."
    return 1
  }

  # Step 3: Brief delay then verify no race — only consider Claimed comments
  # posted AFTER pre_claim_ts (ignores stale comments from previous attempts)
  sleep 2
  local race_comments
  race_comments=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json comments --jq '.comments' 2>/dev/null \
    | jq --arg ts "$pre_claim_ts" '
      [.[] |
        select(.body | startswith("prauto(")) |
        select(.body | contains("Claimed")) |
        select(.createdAt > $ts)
      ] | length
    ') || race_comments=0

  if [[ "$race_comments" -gt 0 ]]; then
    warn "Issue #${issue_number} was claimed by another worker during race window."
    return 1
  fi

  # Step 4: Remove prauto:ready, set assignee, post claim comment.
  # Always post a fresh Claimed comment (no idempotency guard) so that
  # restarted issues get a new timestamp anchor for retry counting.
  gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --remove-label "$PRAUTO_GITHUB_LABEL_READY" \
    --add-assignee "$PRAUTO_GITHUB_ACTOR" 2>/dev/null || true

  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "prauto(${PRAUTO_WORKER_ID}): Claimed this issue. Starting work." \
    2>/dev/null || warn "Failed to post claim comment on issue #${issue_number}."

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

  # Add prauto:plan-review label for non-minor plans (makes approval-wait visible on boards)
  if [[ "$change_size" != "minor" ]]; then
    gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
      --add-label "${PRAUTO_GITHUB_LABEL_PLAN_REVIEW}" 2>/dev/null || \
      warn "Failed to add ${PRAUTO_GITHUB_LABEL_PLAN_REVIEW} label to issue #${issue_number}."
  fi

  info "Plan comment posted on issue #${issue_number} (change_size=${change_size})."
}

# Find ALL open issues claimed by this worker (any prauto: label), sorted oldest-first.
# Usage: find_all_claimed_issues
# Sets: ALL_CLAIMED_ISSUES (JSON array with number, title, labels), ALL_CLAIMED_COUNT
# Returns 0 if at least one found, 1 if none.
find_all_claimed_issues() {
  local issues_json
  issues_json=$(gh issue list -R "$PRAUTO_GITHUB_REPO" \
    --assignee "$PRAUTO_GITHUB_ACTOR" \
    --state open \
    --json number,title,labels --limit 50 2>/dev/null) || {
    warn "Failed to list claimed issues from GitHub."; return 1
  }
  ALL_CLAIMED_ISSUES=$(echo "$issues_json" | jq \
    '[.[] | select(.labels | any(.name | startswith("prauto:")))] | sort_by(.number)')
  ALL_CLAIMED_COUNT=$(echo "$ALL_CLAIMED_ISSUES" | jq 'length')
  [[ "$ALL_CLAIMED_COUNT" -eq 0 ]] && return 1
  info "Found ${ALL_CLAIMED_COUNT} claimed issue(s) on GitHub."
  return 0
}

# Count heartbeat comments posted by this worker on an issue.
# Only counts comments from the CURRENT claim lifecycle — heartbeat comments
# posted after the most recent "Claimed" comment by this worker AND after the
# last prauto:ready label event (READY_LABEL_TIMESTAMP). This ensures that
# restarted issues start with a fresh retry counter.
# Usage: count_heartbeat_comments <issue_number>
# Requires: READY_LABEL_TIMESTAMP set (via get_ready_label_timestamp)
# Sets: HEARTBEAT_COMMENT_COUNT
count_heartbeat_comments() {
  local issue_number="$1"
  local hb_marker="prauto(${PRAUTO_WORKER_ID}): Heartbeat"
  local claim_marker="prauto(${PRAUTO_WORKER_ID}): Claimed"
  local ready_ts="${READY_LABEL_TIMESTAMP:-}"
  HEARTBEAT_COMMENT_COUNT=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json comments --jq '.comments' 2>/dev/null \
    | jq --arg hb "$hb_marker" --arg cl "$claim_marker" --arg ready_ts "$ready_ts" '
      [.[] | select($ready_ts == "" or .createdAt > $ready_ts)] as $scoped |
      ($scoped | [.[] | select(.body | startswith($cl))] | last | .createdAt // "") as $anchor |
      [$scoped[] | select(.body | startswith($hb)) | select(.createdAt > $anchor)] | length
    ') || HEARTBEAT_COMMENT_COUNT=0
}

# Post a heartbeat marker comment on an issue.
# Usage: post_heartbeat_comment <issue_number> <phase> <attempt> <max>
post_heartbeat_comment() {
  local issue_number="$1"
  local phase="$2"
  local attempt="$3"
  local max="$4"

  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "prauto(${PRAUTO_WORKER_ID}): Heartbeat — ${phase} (attempt ${attempt}/${max})" \
    2>/dev/null || warn "Failed to post heartbeat comment on issue #${issue_number}."
}

# Derive the current phase from GitHub signals (PR existence, labels, plan comments, approval).
# Usage: derive_phase_from_github <issue_number> <branch>
# Sets: DERIVED_PHASE
derive_phase_from_github() {
  local issue_number="$1" branch="$2"
  # Check if PR already exists for this branch
  local pr_number
  pr_number=$(gh pr list -R "$PRAUTO_GITHUB_REPO" --head "$branch" \
    --json number --jq '.[0].number // empty' 2>/dev/null)
  if [[ -n "$pr_number" ]]; then
    DERIVED_PHASE="pr"; return 0
  fi
  # Fast path: prauto:plan-review label means plan is posted, awaiting approval
  local issue_labels
  issue_labels=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json labels --jq '[.labels[].name]' 2>/dev/null) || issue_labels="[]"
  local has_plan_review
  has_plan_review=$(echo "$issue_labels" | jq -r --arg label "${PRAUTO_GITHUB_LABEL_PLAN_REVIEW}" \
    'index($label) != null')
  if [[ "$has_plan_review" == "true" ]]; then
    DERIVED_PHASE="plan-approval"; return 0
  fi
  # Check if plan comment exists (no plan-review label = either approved or minor)
  # Only consider comments after the last prauto:ready label event
  local plan_prefix="prauto(${PRAUTO_WORKER_ID}): Plan"
  local ready_ts="${READY_LABEL_TIMESTAMP:-}"
  local plan_exists
  plan_exists=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json comments \
    --jq '.comments' 2>/dev/null \
    | jq --arg prefix "$plan_prefix" --arg ready_ts "$ready_ts" '
      [.[] | select($ready_ts == "" or .createdAt > $ready_ts) | select(.body | startswith($prefix))] | length
    ') || plan_exists=0
  if [[ "$plan_exists" -gt 0 ]]; then
    local approval_status=0
    check_plan_approval "$issue_number" || approval_status=$?
    if [[ "$approval_status" -eq 0 ]]; then
      DERIVED_PHASE="implementation"
    else
      DERIVED_PHASE="plan-approval"
    fi
    return 0
  fi
  DERIVED_PHASE="analysis"; return 0
}

# Count existing plan comments by this worker, derive next revision number.
# Only counts plan comments after the last prauto:ready label event.
# Usage: get_plan_revision_from_github <issue_number>
# Requires: READY_LABEL_TIMESTAMP set (via get_ready_label_timestamp)
# Sets: GITHUB_PLAN_REVISION
get_plan_revision_from_github() {
  local issue_number="$1"
  local prefix="prauto(${PRAUTO_WORKER_ID}): Plan"
  local ready_ts="${READY_LABEL_TIMESTAMP:-}"
  local plan_count
  plan_count=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json comments \
    --jq '.comments' 2>/dev/null \
    | jq --arg prefix "$prefix" --arg ready_ts "$ready_ts" '
      [.[] | select($ready_ts == "" or .createdAt > $ready_ts) | select(.body | startswith($prefix))] | length
    ') || plan_count=0
  GITHUB_PLAN_REVISION=$(( plan_count + 1 ))
}

# Check whether a plan has been approved on an issue.
# Looks for comments after the plan comment, scoped to the current lifecycle
# (only considers comments after the last prauto:ready label event).
# Returns: 0 = approved ("go ahead"), 1 = no response yet, 2 = counter-proposal found, 3 = no plan comment found.
# Requires: READY_LABEL_TIMESTAMP set (via get_ready_label_timestamp)
# Sets COUNTER_PROPOSAL on return 2.
check_plan_approval() {
  local issue_number="$1"

  local plan_prefix="prauto(${PRAUTO_WORKER_ID}): Plan"
  local ready_ts="${READY_LABEL_TIMESTAMP:-}"

  # Fetch all comments as JSON array, scoped to current lifecycle
  local comments_json
  comments_json=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json comments \
    --jq '.comments' 2>/dev/null) || {
    warn "Failed to fetch comments for issue #${issue_number}."
    return 1
  }
  # Filter to only comments after the last prauto:ready label event
  comments_json=$(echo "$comments_json" | jq --arg ready_ts "$ready_ts" '
    [.[] | select($ready_ts == "" or .createdAt > $ready_ts)]
  ')

  # Find the timestamp of the last plan comment
  local plan_timestamp
  plan_timestamp=$(echo "$comments_json" | jq -r --arg prefix "$plan_prefix" '
    [.[] | select(.body | startswith($prefix))] | last | .createdAt // empty
  ')

  if [[ -z "$plan_timestamp" ]]; then
    warn "No plan comment found on issue #${issue_number}."
    return 3
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
