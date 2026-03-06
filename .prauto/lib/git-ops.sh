# Git worktree and branch operations for prauto.
# Source this file — do not execute directly.
# Requires: helpers.sh sourced, config loaded, gh/git available.

# Create a new worktree on a fresh branch from the base.
# If the branch already exists (retry scenario), reuses it in a new worktree.
# Usage: create_branch <issue_number>
# Sets: BRANCH_NAME, WORKTREE_DIR
create_branch() {
  local issue_number="$1"
  BRANCH_NAME="${PRAUTO_BRANCH_PREFIX}I-${issue_number}"
  WORKTREE_DIR="${PRAUTO_DIR}/worktrees/I-${issue_number}"

  info "Fetching from origin..."
  git fetch origin 2>/dev/null || warn "git fetch failed — continuing with local refs."

  # Remove stale worktree from a previous attempt.
  if [[ -d "$WORKTREE_DIR" ]]; then
    warn "Removing stale worktree at ${WORKTREE_DIR}."
    git worktree remove --force "$WORKTREE_DIR" 2>/dev/null || rm -rf "$WORKTREE_DIR"
    git worktree prune 2>/dev/null || true
  fi

  if git show-ref --verify --quiet "refs/remotes/origin/${BRANCH_NAME}" ||
     git show-ref --verify --quiet "refs/heads/${BRANCH_NAME}"; then
    info "Branch ${BRANCH_NAME} already exists. Reusing in new worktree."
    git worktree add "$WORKTREE_DIR" "$BRANCH_NAME" 2>/dev/null ||
      error "Failed to create worktree for ${BRANCH_NAME}."
  else
    info "Creating branch ${BRANCH_NAME} from origin/${PRAUTO_BASE_BRANCH}..."
    git worktree add -b "$BRANCH_NAME" "$WORKTREE_DIR" "origin/${PRAUTO_BASE_BRANCH}" 2>/dev/null ||
      error "Failed to create worktree for new branch ${BRANCH_NAME}."
  fi

  info "Worktree ready at ${WORKTREE_DIR} (branch: ${BRANCH_NAME})."
}

# Create a worktree for an existing remote branch (resume or PR review).
# Usage: checkout_branch_worktree <branch_name>
# Sets: WORKTREE_DIR
checkout_branch_worktree() {
  local branch="$1"
  local safe_name="${branch//\//-}"
  WORKTREE_DIR="${PRAUTO_DIR}/worktrees/${safe_name}"

  info "Fetching origin/${branch}..."
  git fetch origin "$branch" 2>/dev/null || warn "git fetch failed for ${branch}."

  if [[ -d "$WORKTREE_DIR" ]]; then
    warn "Removing stale worktree at ${WORKTREE_DIR}."
    git worktree remove --force "$WORKTREE_DIR" 2>/dev/null || rm -rf "$WORKTREE_DIR"
    git worktree prune 2>/dev/null || true
  fi

  git worktree add "$WORKTREE_DIR" "$branch" 2>/dev/null ||
    error "Failed to create worktree for branch ${branch}."

  info "Worktree ready at ${WORKTREE_DIR} (branch: ${branch})."
}

# Remove the current worktree and reset WORKTREE_DIR.
# Safe to call when no worktree is active (no-op).
# Usage: cleanup_worktree
cleanup_worktree() {
  if [[ -n "$WORKTREE_DIR" ]] && [[ -d "$WORKTREE_DIR" ]]; then
    cd "$REPO_DIR"
    git worktree remove --force "$WORKTREE_DIR" 2>/dev/null || rm -rf "$WORKTREE_DIR"
  fi
  WORKTREE_DIR=""
}

# Push the current branch to origin.
push_branch() {
  local branch="$1"
  info "Pushing ${branch} to origin..."
  git push -u origin "$branch" 2>/dev/null || error "Failed to push ${branch} to origin."
  info "Pushed ${branch}."
}
