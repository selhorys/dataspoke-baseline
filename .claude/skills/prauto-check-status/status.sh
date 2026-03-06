#!/usr/bin/env bash
# prauto status — quick dashboard + next heartbeat prediction.
# Usage: .claude/skills/prauto-check-status/status.sh [filter]
#   filter: ready, wip, review, done, failed, next (default: all)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
PRAUTO_DIR="$REPO_DIR/.prauto"

# ---------------------------------------------------------------------------
# Load config
# ---------------------------------------------------------------------------
source "$PRAUTO_DIR/config.env"
source "$PRAUTO_DIR/config.local.env"

REPO="$PRAUTO_GITHUB_REPO"
LABEL_READY="$PRAUTO_GITHUB_LABEL_READY"
LABEL_WIP="$PRAUTO_GITHUB_LABEL_WIP"
LABEL_REVIEW="$PRAUTO_GITHUB_LABEL_REVIEW"
LABEL_DONE="$PRAUTO_GITHUB_LABEL_DONE"
LABEL_FAILED="$PRAUTO_GITHUB_LABEL_FAILED"
LABEL_PLAN_REVIEW="$PRAUTO_GITHUB_LABEL_PLAN_REVIEW"
BRANCH_PREFIX="$PRAUTO_BRANCH_PREFIX"
MAX_RETRIES="$PRAUTO_MAX_RETRIES_PER_JOB"
OPEN_LIMIT="${PRAUTO_OPEN_ISSUE_LIMIT:-1}"
ORG_FILTER="${PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY:-}"
FILTER="${1:-all}"

[[ -n "${GH_TOKEN:-}" ]] && export GH_TOKEN

# Resolve actor
ACTOR=$(gh api user --jq '.login' 2>/dev/null) || { echo "ERROR: gh auth failed"; exit 1; }

# Colors
bold="\033[1m" dim="\033[2m" reset="\033[0m"
green="\033[32m" yellow="\033[33m" red="\033[31m" cyan="\033[36m" blue="\033[34m"

header() { echo -e "\n${bold}${cyan}### $1${reset}"; }
row() { printf "  %-6s %-70s %s\n" "$1" "$2" "$3"; }

# Check plan approval status for an issue (mirrors check_plan_approval in issues.sh).
# Usage: check_plan_status <issue_number>
# Sets: PLAN_STATUS ("approved"|"waiting"|"counter-proposal"|"no-plan")
#       PLAN_COUNTER_PROPOSAL_PREVIEW (first line of counter-proposal, if any)
check_plan_status() {
  local issue_number="$1"
  local plan_prefix="prauto(${PRAUTO_WORKER_ID}): Plan"
  PLAN_STATUS="waiting"
  PLAN_COUNTER_PROPOSAL_PREVIEW=""

  local comments_json
  comments_json=$(gh issue view "$issue_number" -R "$REPO" \
    --json comments --jq '.comments' 2>/dev/null) || { PLAN_STATUS="waiting"; return; }

  # Find timestamp of the last plan comment
  local plan_timestamp
  plan_timestamp=$(echo "$comments_json" | jq -r --arg prefix "$plan_prefix" '
    [.[] | select(.body | startswith($prefix))] | last | .createdAt // empty')

  if [[ -z "$plan_timestamp" ]]; then
    PLAN_STATUS="no-plan"; return
  fi

  # Get non-prauto comments after the plan
  local after_comments
  after_comments=$(echo "$comments_json" | jq -r --arg ts "$plan_timestamp" '
    [.[] | select(.createdAt > $ts) | select(.body | startswith("prauto(") | not)]')
  local comment_count
  comment_count=$(echo "$after_comments" | jq 'length')

  if [[ "$comment_count" -eq 0 ]]; then
    PLAN_STATUS="waiting"; return
  fi

  # Check for "go ahead"
  local i body_trimmed
  for (( i = 0; i < comment_count; i++ )); do
    body_trimmed=$(echo "$after_comments" | jq -r ".[$i].body" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    if [[ "$body_trimmed" == "go ahead" ]]; then
      PLAN_STATUS="approved"; return
    fi
  done

  # No "go ahead" — latest non-prauto comment is a counter-proposal
  PLAN_STATUS="counter-proposal"
  PLAN_COUNTER_PROPOSAL_PREVIEW=$(echo "$after_comments" | jq -r '.[-1].body' | head -1 | cut -c1-60)
}

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
echo -e "${bold}## Prauto Status — $(date +%Y-%m-%dT%H:%M:%S)${reset}"
echo -e "  Repo: ${REPO}  |  Worker: ${PRAUTO_WORKER_ID}  |  Actor: ${ACTOR}"
echo -e "  Open issue limit: ${OPEN_LIMIT}  |  Max retries: ${MAX_RETRIES}"

# ---------------------------------------------------------------------------
# Fetch all claimed issues (used by multiple sections)
# ---------------------------------------------------------------------------
ALL_CLAIMED=$(gh issue list -R "$REPO" --assignee "$ACTOR" --state open \
  --json number,title,labels --limit 50 2>/dev/null | \
  jq '[.[] | select(.labels | any(.name | startswith("prauto:")))] | sort_by(.number)')
CLAIMED_COUNT=$(echo "$ALL_CLAIMED" | jq 'length')

# ---------------------------------------------------------------------------
# prauto:ready
# ---------------------------------------------------------------------------
show_ready() {
  header "prauto:ready — Available Issues"
  local ready_json
  ready_json=$(gh issue list -R "$REPO" --label "$LABEL_READY" --state open \
    --json number,title,author --limit 50 2>/dev/null)
  local count
  count=$(echo "$ready_json" | jq 'length')
  if [[ "$count" -eq 0 ]]; then
    echo -e "  ${dim}None${reset}"; return
  fi
  echo "$ready_json" | jq -r '.[] | "  #\(.number)\t\(.title) (\(.author.login))"'
  echo -e "  ${dim}(${count} total)${reset}"
}

# ---------------------------------------------------------------------------
# prauto:wip
# ---------------------------------------------------------------------------
show_wip() {
  header "prauto:wip — Work in Progress"
  local wip_json
  wip_json=$(gh issue list -R "$REPO" --label "$LABEL_WIP" --state open \
    --json number,title,assignees,labels --limit 50 2>/dev/null)
  local count
  count=$(echo "$wip_json" | jq 'length')
  if [[ "$count" -eq 0 ]]; then
    echo -e "  ${dim}None${reset}"; return
  fi

  local i=0
  while [[ "$i" -lt "$count" ]]; do
    local num title assignee labels_str has_plan_review
    num=$(echo "$wip_json" | jq -r ".[$i].number")
    title=$(echo "$wip_json" | jq -r ".[$i].title")
    assignee=$(echo "$wip_json" | jq -r ".[$i].assignees[0].login // \"unassigned\"")
    has_plan_review=$(echo "$wip_json" | jq -r ".[$i].labels | any(.name == \"$LABEL_PLAN_REVIEW\")")

    # Derive phase
    local phase="analysis"
    local plan_detail=""
    local pr_exists
    pr_exists=$(gh pr list -R "$REPO" --head "${BRANCH_PREFIX}I-${num}" \
      --json number --jq '.[0].number // empty' 2>/dev/null)
    if [[ -n "$pr_exists" ]]; then
      phase="pr"
    elif [[ "$has_plan_review" == "true" ]]; then
      phase="plan-approval"
      check_plan_status "$num"
      case "$PLAN_STATUS" in
        approved)           plan_detail=" → approved, will implement" ;;
        counter-proposal)   plan_detail=" → counter-proposal: \"${PLAN_COUNTER_PROPOSAL_PREVIEW}\"" ;;
        no-plan)            plan_detail=" → plan missing, will re-analyze" ;;
        *)                  plan_detail=" → waiting for response" ;;
      esac
    else
      check_plan_status "$num"
      case "$PLAN_STATUS" in
        approved)       phase="implementation" ;;
        counter-proposal) phase="plan-approval"; plan_detail=" → counter-proposal: \"${PLAN_COUNTER_PROPOSAL_PREVIEW}\"" ;;
        no-plan)        phase="analysis" ;;
        *)              phase="analysis" ;;
      esac
    fi

    # Count heartbeat comments for retry
    local hb_count
    hb_count=$(gh issue view "$num" -R "$REPO" --json comments \
      --jq "[.comments[] | select(.body | startswith(\"prauto($PRAUTO_WORKER_ID): Heartbeat\"))] | length" 2>/dev/null)

    # Check quota-paused
    local quota_paused=""
    local latest_prauto_body
    latest_prauto_body=$(gh issue view "$num" -R "$REPO" --json comments \
      --jq "[.comments[] | select(.body | startswith(\"prauto(\"))] | sort_by(.createdAt) | last | .body // \"\"" 2>/dev/null)
    if echo "$latest_prauto_body" | grep -q "prauto:quota-paused"; then
      quota_paused=" ${red}[QUOTA-PAUSED]${reset}"
    fi

    local phase_color="$yellow"
    [[ "$phase" == "implementation" ]] && phase_color="$green"
    [[ "$phase" == "pr" ]] && phase_color="$blue"

    echo -e "  #${num}\t${title}"
    echo -e "    ${dim}assignee=${assignee}  phase=${phase_color}${phase}${reset}${dim}${plan_detail}  retries=${hb_count}/${MAX_RETRIES}${quota_paused}${reset}"

    i=$((i + 1))
  done
}

# ---------------------------------------------------------------------------
# prauto:review
# ---------------------------------------------------------------------------
show_review() {
  header "prauto:review — PRs Awaiting Review"
  local review_json
  review_json=$(gh issue list -R "$REPO" --label "$LABEL_REVIEW" --state open \
    --json number,title,assignees --limit 50 2>/dev/null)
  local count
  count=$(echo "$review_json" | jq 'length')
  if [[ "$count" -eq 0 ]]; then
    echo -e "  ${dim}None${reset}"; return
  fi

  local i=0
  while [[ "$i" -lt "$count" ]]; do
    local num title
    num=$(echo "$review_json" | jq -r ".[$i].number")
    title=$(echo "$review_json" | jq -r ".[$i].title")

    local pr_num pr_detail
    pr_num=$(gh pr list -R "$REPO" --head "${BRANCH_PREFIX}I-${num}" \
      --json number --jq '.[0].number // empty' 2>/dev/null)

    if [[ -z "$pr_num" ]]; then
      echo -e "  #${num}\t${title}  ${red}(no PR found)${reset}"
    else
      pr_detail=$(gh pr view "$pr_num" -R "$REPO" \
        --json mergeable,mergeStateStatus,reviews 2>/dev/null)
      local mergeable merge_state approver
      mergeable=$(echo "$pr_detail" | jq -r '.mergeable')
      merge_state=$(echo "$pr_detail" | jq -r '.mergeStateStatus')
      approver=$(echo "$pr_detail" | jq -r '
        (.reviews // [])
        | group_by(.author.login) | map(sort_by(.submittedAt) | last)
        | map(select(.state == "APPROVED")) | first // empty
        | .author.login // empty')

      local status_str
      if [[ -n "$approver" ]] && [[ "$mergeable" == "MERGEABLE" ]] && [[ "$merge_state" == "CLEAN" ]]; then
        status_str="${green}squash-ready${reset} (approved by ${approver})"
      elif [[ -n "$approver" ]]; then
        status_str="${yellow}approved${reset} but mergeable=${mergeable} status=${merge_state}"
      else
        status_str="${dim}waiting for review${reset}"
      fi
      echo -e "  #${num}\t${title}  PR#${pr_num} ${status_str}"
    fi
    i=$((i + 1))
  done
}

# ---------------------------------------------------------------------------
# prauto:done
# ---------------------------------------------------------------------------
show_done() {
  header "prauto:done — Finalized"
  local open_done
  open_done=$(gh pr list -R "$REPO" --label "$LABEL_DONE" --state open \
    --json number,title,headRefName --limit 50 2>/dev/null)
  local open_count
  open_count=$(echo "$open_done" | jq 'length')
  if [[ "$open_count" -gt 0 ]]; then
    echo -e "  ${bold}Awaiting merge:${reset}"
    echo "$open_done" | jq -r '.[] | "  #\(.number)\t\(.title) (\(.headRefName))"'
  fi

  local merged_done
  merged_done=$(gh pr list -R "$REPO" --label "$LABEL_DONE" --state merged \
    --json number,title,mergedAt --limit 5 2>/dev/null)
  local merged_count
  merged_count=$(echo "$merged_done" | jq 'length')
  if [[ "$merged_count" -gt 0 ]]; then
    echo -e "  ${dim}Recently merged:${reset}"
    echo "$merged_done" | jq -r '.[] | "  #\(.number)\t\(.title) (\(.mergedAt[:10]))"'
  fi

  if [[ "$open_count" -eq 0 ]] && [[ "$merged_count" -eq 0 ]]; then
    echo -e "  ${dim}None${reset}"
  fi
}

# ---------------------------------------------------------------------------
# prauto:failed
# ---------------------------------------------------------------------------
show_failed() {
  header "prauto:failed — Failed (Needs Intervention)"
  local failed_json
  failed_json=$(gh issue list -R "$REPO" --label "$LABEL_FAILED" --state open \
    --json number,title,assignees --limit 50 2>/dev/null)
  local count
  count=$(echo "$failed_json" | jq 'length')
  if [[ "$count" -eq 0 ]]; then
    echo -e "  ${dim}None${reset}"; return
  fi
  echo "$failed_json" | jq -r '.[] | "  #\(.number)\t\(.title)"'
}

# ---------------------------------------------------------------------------
# Next heartbeat prediction
# ---------------------------------------------------------------------------
show_next() {
  header "Next Heartbeat Prediction"

  # Stage 1: Claim
  echo -e "  ${bold}Stage 1 — Claim:${reset} ${CLAIMED_COUNT}/${OPEN_LIMIT} open issues"
  if [[ "$CLAIMED_COUNT" -ge "$OPEN_LIMIT" ]]; then
    echo -e "    Will ${yellow}skip${reset} new issue pickup (at limit)."
  else
    local oldest_ready
    oldest_ready=$(gh issue list -R "$REPO" --label "$LABEL_READY" --state open \
      --json number,title --limit 50 --jq 'sort_by(.number) | .[0] // empty' 2>/dev/null)
    if [[ -n "$oldest_ready" ]]; then
      local rnum rtitle
      rnum=$(echo "$oldest_ready" | jq -r '.number')
      rtitle=$(echo "$oldest_ready" | jq -r '.title')
      echo -e "    Will ${green}claim${reset} #${rnum} — ${rtitle}"
    else
      echo -e "    ${dim}No eligible issues to claim.${reset}"
    fi
  fi

  # Stage 2: Process all
  echo ""
  echo -e "  ${bold}Stage 2 — Process claimed issues${reset} (${CLAIMED_COUNT} total, oldest first):"
  if [[ "$CLAIMED_COUNT" -eq 0 ]]; then
    echo -e "    ${dim}No claimed issues to process.${reset}"
    return
  fi

  printf "    %-6s %-50s %-16s %s\n" "#" "Issue" "Label" "Action"
  printf "    %-6s %-50s %-16s %s\n" "---" "-----" "-----" "------"

  local ci=0
  while [[ "$ci" -lt "$CLAIMED_COUNT" ]]; do
    local num title labels label action
    num=$(echo "$ALL_CLAIMED" | jq -r ".[$ci].number")
    title=$(echo "$ALL_CLAIMED" | jq -r ".[$ci].title" | cut -c1-48)
    labels=$(echo "$ALL_CLAIMED" | jq -r ".[$ci].labels | map(.name) | join(\",\")")

    # Determine primary prauto label
    if echo "$labels" | grep -q "$LABEL_DONE"; then
      label="done"; action="${dim}Skip (terminal)${reset}"
    elif echo "$labels" | grep -q "$LABEL_FAILED"; then
      label="failed"; action="${dim}Skip (terminal)${reset}"
    elif echo "$labels" | grep -q "$LABEL_WIP"; then
      label="wip"
      # Derive phase
      local pr_exists
      pr_exists=$(gh pr list -R "$REPO" --head "${BRANCH_PREFIX}I-${num}" \
        --json number --jq '.[0].number // empty' 2>/dev/null)
      local has_plan_review
      has_plan_review=$(echo "$ALL_CLAIMED" | jq -r ".[$ci].labels | any(.name == \"$LABEL_PLAN_REVIEW\")")

      local hb_count
      hb_count=$(gh issue view "$num" -R "$REPO" --json comments \
        --jq "[.comments[] | select(.body | startswith(\"prauto($PRAUTO_WORKER_ID): Heartbeat\"))] | length" 2>/dev/null)

      if [[ "$hb_count" -ge "$MAX_RETRIES" ]]; then
        action="${red}Abandon${reset} — max retries exceeded"
      elif [[ -n "$pr_exists" ]]; then
        action="${blue}Push branch${reset} + create/update PR (attempt $((hb_count+1))/${MAX_RETRIES})"
      elif [[ "$has_plan_review" == "true" ]]; then
        # prauto:plan-review label present — check actual comment state
        check_plan_status "$num"
        case "$PLAN_STATUS" in
          approved)         action="${green}Start implementation${reset} (plan approved)" ;;
          counter-proposal) action="${yellow}Revise plan${reset} — counter-proposal: \"${PLAN_COUNTER_PROPOSAL_PREVIEW}\"" ;;
          no-plan)          action="${yellow}Re-run analysis${reset} — plan comment missing" ;;
          *)                action="${yellow}Skip${reset} — waiting for plan approval" ;;
        esac
      else
        check_plan_status "$num"
        case "$PLAN_STATUS" in
          approved)         action="${green}Start implementation${reset} (plan approved)" ;;
          counter-proposal) action="${yellow}Revise plan${reset} — counter-proposal: \"${PLAN_COUNTER_PROPOSAL_PREVIEW}\"" ;;
          no-plan)          action="${yellow}Run analysis${reset} (attempt $((hb_count+1))/${MAX_RETRIES})" ;;
          *)                action="${yellow}Run analysis${reset} (attempt $((hb_count+1))/${MAX_RETRIES})" ;;
        esac
      fi
    elif echo "$labels" | grep -q "$LABEL_REVIEW"; then
      label="review"
      local pr_num
      pr_num=$(gh pr list -R "$REPO" --head "${BRANCH_PREFIX}I-${num}" \
        --json number --jq '.[0].number // empty' 2>/dev/null)
      if [[ -z "$pr_num" ]]; then
        action="${dim}Skip — no PR found${reset}"
      else
        local pr_detail mergeable merge_state approver
        pr_detail=$(gh pr view "$pr_num" -R "$REPO" \
          --json mergeable,mergeStateStatus,reviews 2>/dev/null)
        mergeable=$(echo "$pr_detail" | jq -r '.mergeable')
        merge_state=$(echo "$pr_detail" | jq -r '.mergeStateStatus')
        approver=$(echo "$pr_detail" | jq -r '
          (.reviews // [])
          | group_by(.author.login) | map(sort_by(.submittedAt) | last)
          | map(select(.state == "APPROVED")) | first // empty
          | .author.login // empty')
        if [[ -n "$approver" ]] && [[ "$mergeable" == "MERGEABLE" ]] && [[ "$merge_state" == "CLEAN" ]]; then
          action="${green}Squash-finalize${reset} PR#${pr_num}"
        else
          action="${dim}Skip${reset} — waiting for review"
        fi
      fi
    else
      label="??"; action="${dim}Skip (unknown label)${reset}"
    fi

    printf "    %-6s %-50s %-16s " "#${num}" "$title" "$label"
    echo -e "$action"
    ci=$((ci + 1))
  done
}

# ---------------------------------------------------------------------------
# Lock check
# ---------------------------------------------------------------------------
LOCK_FILE="$PRAUTO_DIR/state/heartbeat.lock"
if [[ -f "$LOCK_FILE" ]]; then
  lock_pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "?")
  echo -e "\n  ${red}WARNING: heartbeat.lock exists (PID ${lock_pid})${reset}"
fi

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
case "$FILTER" in
  ready)   show_ready ;;
  wip)     show_wip ;;
  review)  show_review ;;
  done)    show_done ;;
  failed)  show_failed ;;
  next)    show_next ;;
  all)     show_ready; show_wip; show_review; show_done; show_failed; show_next ;;
  *)       echo "Usage: $0 [ready|wip|review|done|failed|next|all]"; exit 1 ;;
esac
echo ""
