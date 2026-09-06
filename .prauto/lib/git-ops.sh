# Git worktree and branch operations for prauto.
# Source this file — do not execute directly.
# Requires: helpers.sh sourced, config loaded, git available.

# create_branch <issue_number>
# Create a worktree on the prauto/I-<n> branch from the base. If the branch
# already exists (retry scenario), reuse it in a fresh worktree.
# Sets: BRANCH_NAME, WORKTREE_DIR.
create_branch() {
  local issue_number="$1"
  BRANCH_NAME="${PRAUTO_BRANCH_PREFIX}I-${issue_number}"
  WORKTREE_DIR="${PRAUTO_DIR}/worktrees/I-${issue_number}"

  info "Fetching from origin..."
  git -C "$REPO_DIR" fetch origin 2>/dev/null || warn "git fetch failed — continuing with local refs."

  if [[ -d "$WORKTREE_DIR" ]]; then
    warn "Removing stale worktree at ${WORKTREE_DIR}."
    git -C "$REPO_DIR" worktree remove --force "$WORKTREE_DIR" 2>/dev/null || rm -rf "$WORKTREE_DIR"
    git -C "$REPO_DIR" worktree prune 2>/dev/null || true
  fi

  if git -C "$REPO_DIR" show-ref --verify --quiet "refs/remotes/origin/${BRANCH_NAME}" ||
     git -C "$REPO_DIR" show-ref --verify --quiet "refs/heads/${BRANCH_NAME}"; then
    info "Branch ${BRANCH_NAME} already exists. Reusing in a new worktree."
    git -C "$REPO_DIR" worktree add "$WORKTREE_DIR" "$BRANCH_NAME" 2>/dev/null \
      || error "Failed to create worktree for ${BRANCH_NAME}."
  else
    info "Creating branch ${BRANCH_NAME} from origin/${PRAUTO_BASE_BRANCH}..."
    git -C "$REPO_DIR" worktree add -b "$BRANCH_NAME" "$WORKTREE_DIR" "origin/${PRAUTO_BASE_BRANCH}" 2>/dev/null \
      || error "Failed to create worktree for new branch ${BRANCH_NAME}."
  fi
  info "Worktree ready at ${WORKTREE_DIR} (branch: ${BRANCH_NAME})."
}

# checkout_branch_worktree <branch>
# Create a worktree for an existing remote branch (resume or PR review).
# Sets: WORKTREE_DIR.
checkout_branch_worktree() {
  local branch="$1"
  local safe_name="${branch//\//-}"
  WORKTREE_DIR="${PRAUTO_DIR}/worktrees/${safe_name}"

  git -C "$REPO_DIR" fetch origin "$branch" 2>/dev/null || warn "git fetch failed for ${branch}."

  if [[ -d "$WORKTREE_DIR" ]]; then
    warn "Removing stale worktree at ${WORKTREE_DIR}."
    git -C "$REPO_DIR" worktree remove --force "$WORKTREE_DIR" 2>/dev/null || rm -rf "$WORKTREE_DIR"
    git -C "$REPO_DIR" worktree prune 2>/dev/null || true
  fi

  git -C "$REPO_DIR" worktree add "$WORKTREE_DIR" "$branch" 2>/dev/null \
    || error "Failed to create worktree for branch ${branch}."
  info "Worktree ready at ${WORKTREE_DIR} (branch: ${branch})."
}

# cleanup_worktree
# Remove the current worktree and reset WORKTREE_DIR. Safe to call when none is active.
cleanup_worktree() {
  if [[ -n "$WORKTREE_DIR" ]] && [[ -d "$WORKTREE_DIR" ]]; then
    git -C "$REPO_DIR" worktree remove --force "$WORKTREE_DIR" 2>/dev/null || rm -rf "$WORKTREE_DIR"
    git -C "$REPO_DIR" worktree prune 2>/dev/null || true
  fi
  WORKTREE_DIR=""
}

# push_branch <branch>
# Push the current branch to origin. This is loop-master-owned — the worker never
# pushes; only the harness finalize path calls this.
push_branch() {
  local branch="$1"
  info "Pushing ${branch} to origin..."
  git push -u origin "$branch" 2>/dev/null || error "Failed to push ${branch} to origin."
  info "Pushed ${branch}."
}

# push_checkpoint_branch <branch>
# Best-effort checkpoint push used before a paused or failed worker is cleaned
# up. Unlike push_branch, this must not abort the whole heartbeat: a local commit
# remains useful for a later retry even when GitHub/SSH is temporarily down.
push_checkpoint_branch() {
  local branch="$1"
  info "Pushing checkpoint ${branch} to origin..."
  if git push -u origin "$branch" 2>/dev/null; then
    info "Checkpoint pushed for ${branch}."
    return 0
  fi
  warn "Failed to push checkpoint for ${branch}; local commits remain available for retry."
  return 1
}

# link_branch_to_issue <issue_number> <branch>
# Link an already-pushed branch in the issue's Development section. GitHub's
# issue-branch endpoint is not present in every gh version, so this is
# idempotent and best-effort; commit comments remain the durable fallback.
link_branch_to_issue() {
  local issue_number="$1" branch="$2"
  local linked_branches

  linked_branches=$(gh api "repos/${PRAUTO_GITHUB_REPO}/issues/${issue_number}/branches" \
    --jq '.[].name' 2>/dev/null || printf '')
  if printf '%s\n' "$linked_branches" | grep -Fxq "$branch"; then
    info "Branch ${branch} is already linked to issue #${issue_number}."
    return 0
  fi

  if gh api --method POST "repos/${PRAUTO_GITHUB_REPO}/issues/${issue_number}/branches" \
      -f "branch=${branch}" >/dev/null 2>&1; then
    info "Linked branch ${branch} to issue #${issue_number}."
    return 0
  fi

  warn "Could not link branch ${branch} to issue #${issue_number}; continuing."
  return 1
}
