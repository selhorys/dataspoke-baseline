# Git worktree and branch operations for prauto.
# Source this file — do not execute directly.
# Requires: helpers.sh sourced, config loaded, git available.

# github_issue_node_id <issue_number>
github_issue_node_id() {
  local issue_number="$1"
  local owner="${PRAUTO_GITHUB_REPO%%/*}" repo="${PRAUTO_GITHUB_REPO#*/}"
  gh api graphql \
    -f query='query($owner: String!, $repo: String!, $number: Int!) { repository(owner: $owner, name: $repo) { issue(number: $number) { id } } }' \
    -f "owner=${owner}" -f "repo=${repo}" -F "number=${issue_number}" 2>/dev/null \
    | jq -r '.data.repository.issue.id // empty'
}

# create_linked_branch_for_issue <issue_number> <branch>
# Create a new remote branch already linked in the issue's Development section.
# This must run before the local worktree branch is created; GitHub exposes no
# supported mutation for attaching an existing unlinked branch retroactively.
create_linked_branch_for_issue() {
  local issue_number="$1" branch="$2" issue_id base_oid linked_name
  issue_id=$(github_issue_node_id "$issue_number") || issue_id=""
  base_oid=$(git -C "$REPO_DIR" rev-parse "origin/${PRAUTO_BASE_BRANCH}" 2>/dev/null || printf '')
  if [[ -z "$issue_id" || -z "$base_oid" ]]; then
    warn "Could not prepare a linked branch for issue #${issue_number}; using a local branch."
    return 1
  fi

  linked_name=$(gh api graphql \
    -f query='mutation($input: CreateLinkedBranchInput!) { createLinkedBranch(input: $input) { linkedBranch { ref { name } } } }' \
    -f "input[issueId]=${issue_id}" \
    -f "input[oid]=${base_oid}" \
    -f "input[name]=${branch}" 2>/dev/null \
    | jq -r '.data.createLinkedBranch.linkedBranch.ref.name // empty')
  if [[ "$linked_name" == "$branch" ]]; then
    info "Created branch ${branch} linked to issue #${issue_number}."
    return 0
  fi

  warn "Could not create a branch linked to issue #${issue_number}; using a local branch."
  return 1
}

# create_branch <issue_number>
# Create a worktree on the prauto/I-<n> branch from the base. New remote
# branches are created through GitHub's linked-branch mutation; an existing
# branch (retry scenario) is reused in a fresh worktree.
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
    if create_linked_branch_for_issue "$issue_number" "$BRANCH_NAME"; then
      git -C "$REPO_DIR" fetch origin "$BRANCH_NAME" 2>/dev/null \
        || error "Failed to fetch linked branch ${BRANCH_NAME}."
      git -C "$REPO_DIR" worktree add "$WORKTREE_DIR" "$BRANCH_NAME" 2>/dev/null \
        || error "Failed to create worktree for linked branch ${BRANCH_NAME}."
    else
      info "Creating branch ${BRANCH_NAME} from origin/${PRAUTO_BASE_BRANCH}..."
      git -C "$REPO_DIR" worktree add -b "$BRANCH_NAME" "$WORKTREE_DIR" "origin/${PRAUTO_BASE_BRANCH}" 2>/dev/null \
        || error "Failed to create worktree for new branch ${BRANCH_NAME}."
    fi
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
# Verify that a branch is linked in the issue's Development section. New
# branches are linked by create_linked_branch_for_issue before they are pushed;
# GitHub exposes no supported mutation for retroactively attaching an existing
# unlinked branch.
link_branch_to_issue() {
  local issue_number="$1" branch="$2" owner="${PRAUTO_GITHUB_REPO%%/*}" repo="${PRAUTO_GITHUB_REPO#*/}"
  local linked_branches

  linked_branches=$(gh api graphql \
    -f query='query($owner: String!, $repo: String!, $number: Int!) { repository(owner: $owner, name: $repo) { issue(number: $number) { linkedBranches(first: 100) { nodes { ref { name } } } } } }' \
    -f "owner=${owner}" -f "repo=${repo}" -F "number=${issue_number}" 2>/dev/null \
    | jq -r '.data.repository.issue.linkedBranches.nodes[].ref.name' 2>/dev/null || printf '')
  if printf '%s\n' "$linked_branches" | grep -Fxq "$branch"; then
    info "Branch ${branch} is already linked to issue #${issue_number}."
    return 0
  fi

  warn "Branch ${branch} is not linked to issue #${issue_number}; existing branches cannot be retroactively linked by the public API."
  return 1
}
