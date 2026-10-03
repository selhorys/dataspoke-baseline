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

# reconcile_branch_with_origin <branch>
# Point the local branch at the head a reused worktree should run. `worktree add`
# prefers refs/heads/<branch> over origin/<branch>, and the local ref is stale
# after a human rebases and force-pushes the remote. "Unpushed" means a local
# commit that origin never had: one unreachable from origin's current head and
# from every earlier value in its remote-tracking reflog (fetches and the
# executor's own pushes record those). Without that reflog this degrades to a
# strict ancestor check, which fails closed. If the fetch failed, this runs
# against the last-known origin ref.
#   local missing                  -> created at origin
#   origin missing                 -> local kept (local-only branch)
#   no unpushed commits            -> local moved to origin (fast-forward or rewrite)
#   origin is an ancestor of local -> local kept, warning (unpushed checkpoint)
#   otherwise (diverged)           -> returns 1; a human must reconcile
# Must run after the stale prauto worktree is removed: `git branch -f` refuses a
# branch checked out in any worktree, and that refusal also returns 1.
reconcile_branch_with_origin() {
  local branch="$1" local_sha origin_sha unpushed
  local_sha=$(git -C "$REPO_DIR" rev-parse --verify --quiet "refs/heads/${branch}^{commit}" 2>/dev/null || printf '')
  origin_sha=$(git -C "$REPO_DIR" rev-parse --verify --quiet "refs/remotes/origin/${branch}^{commit}" 2>/dev/null || printf '')

  if [[ -z "$origin_sha" ]]; then
    [[ -n "$local_sha" ]] && info "Branch ${branch} has no origin counterpart; reusing the local branch."
    return 0
  fi
  [[ "$local_sha" == "$origin_sha" ]] && return 0

  if [[ -n "$local_sha" ]]; then
    unpushed=$( { printf '^%s\n' "$origin_sha"
                  git -C "$REPO_DIR" reflog show --format='^%H' "refs/remotes/origin/${branch}" -- 2>/dev/null || true
                } | git -C "$REPO_DIR" rev-list --count --stdin "$local_sha" 2>/dev/null ) || unpushed=""
    if [[ ! "$unpushed" =~ ^[0-9]+$ ]]; then
      warn "Could not compare ${branch} with origin/${branch}."
      return 1
    fi
    if [[ "$unpushed" -gt 0 ]]; then
      if git -C "$REPO_DIR" merge-base --is-ancestor "$origin_sha" "$local_sha" 2>/dev/null; then
        warn "Branch ${branch} has ${unpushed} unpushed commit(s) on top of origin/${branch}; reusing the local branch."
        return 0
      fi
      warn "Branch ${branch} has diverged: local ${local_sha:0:12} has ${unpushed} commit(s) origin never had, and origin/${branch} (${origin_sha:0:12}) does not contain it."
      return 1
    fi
  fi

  if ! git -C "$REPO_DIR" branch --no-track -f "$branch" "$origin_sha" >/dev/null 2>&1; then
    warn "Could not move ${branch} to origin/${branch} (${origin_sha:0:12}); is it checked out in another worktree?"
    return 1
  fi
  if [[ -z "$local_sha" ]]; then
    info "Created ${branch} at origin/${branch} (${origin_sha:0:12})."
  else
    info "Moved ${branch} from ${local_sha:0:12} to origin/${branch} (${origin_sha:0:12})."
  fi
}

# create_branch <issue_number>
# Create a worktree on the prauto/I-<n> branch from the base. New remote
# branches are created through GitHub's linked-branch mutation; an existing
# branch (retry scenario) is reconciled with origin (reconcile_branch_with_origin)
# and then reused in a fresh worktree.
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
    reconcile_branch_with_origin "$BRANCH_NAME" \
      || error "Cannot safely reuse ${BRANCH_NAME}; reconcile it with origin by hand before resuming."
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
# Create a worktree for an existing remote branch (resume or PR review). The
# local ref is reconciled with origin first (reconcile_branch_with_origin): the
# squash and feedback paths later force-push with a lease on the freshly fetched
# origin ref, so a stale local head would silently overwrite human commits.
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

  reconcile_branch_with_origin "$branch" \
    || error "Cannot safely reuse ${branch}; reconcile it with origin by hand before resuming."
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

# executor_git <args...>
# git with repository hooks, fsmonitor, commit signing programs, credential
# helpers/askpass and the ext:: transport disabled. Executor git commands that
# run in a worktree a worker session used go through this. It does NOT neutralize
# every config-driven program (core.sshCommand must stay: the bot's push key is
# configured there; filter drivers are not overridden); resolve_pr_conflicts
# covers that by rejecting a session that changed git's effective configuration,
# attributes, or hooks (git_control_fingerprint). Plain git elsewhere in the
# harness is unaffected.
executor_git() {
  git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c commit.gpgSign=false \
    -c credential.helper= -c core.askPass= -c protocol.ext.allow=never "$@"
}

# git_control_fingerprint
# One hash over git's control surface as seen from the current worktree: the
# effective configuration with every origin (so include.path / includeIf targets,
# such as the file carrying the bot's core.sshCommand, are covered), the content
# of each config origin file, the attributes files (info/attributes and any
# core.attributesFile), and the hooks directories. Taken before and after a
# worker session; any difference means the session changed how later executor
# git commands behave. It detects tampering DURING that session only; state an
# earlier phase left behind is part of the baseline (see spec §Security Model).
# Prints nothing and returns 1 when it cannot be computed. Hashes with git
# itself, so it needs nothing beyond git.
git_control_fingerprint() {
  local common gitdir listing origins attributes f d
  local -a hook_dirs=()
  common=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || return 1
  gitdir=$(git rev-parse --path-format=absolute --git-dir 2>/dev/null) || return 1
  listing=$(git config --list --show-origin --includes 2>/dev/null) || return 1
  origins=$(printf '%s\n' "$listing" | awk -F'\t' '$1 ~ /^file:/ { print substr($1, 6) }' | sort -u)
  attributes=$(git config --path --get core.attributesFile 2>/dev/null || printf '')
  {
    printf '%s\n' "$listing"
    while IFS= read -r f; do
      [[ -n "$f" ]] || continue
      if [[ -f "$f" ]]; then printf '%s %s\n' "$f" "$(git hash-object --no-filters --stdin < "$f")"; else printf '%s absent\n' "$f"; fi
    done <<< "$(printf '%s\n%s\n%s\n' "$origins" "${common}/info/attributes" "$attributes")"
    # A linked worktree has no per-worktree hooks directory; enumerate only the
    # directories that exist, so a missing one is not a failure under pipefail.
    for d in "${common}/hooks" "${gitdir}/hooks"; do
      [[ -d "$d" ]] && hook_dirs+=("$d")
    done
    if [[ "${#hook_dirs[@]}" -gt 0 ]]; then
      find "${hook_dirs[@]}" -type f | sort -u | while IFS= read -r f; do
        printf '%s %s\n' "$f" "$(git hash-object --no-filters --stdin < "$f")"
      done
    fi
  } | git hash-object --no-filters --stdin
}

# push_branch_ref <branch>
# Push HEAD to the named branch, allowing the intentional history rewrite from a
# rebase while refusing to overwrite a remote update made after this worktree was
# created. Push ALWAYS authenticates over SSH via the worker's dedicated key
# (scoped to worktree gitdirs by ~/.gitconfig includeIf — see .prauto/README.md
# §Dedicated GitHub Bot Account). GH_TOKEN is for `gh` API calls only and is
# never near the push path, so no credential can leak into the log or Slack.
push_branch_ref() {
  local branch="$1" expected_sha lease_flag=""
  expected_sha=$(git rev-parse --verify --quiet "refs/remotes/origin/${branch}" 2>/dev/null || printf '')
  if [[ -n "$expected_sha" ]]; then
    lease_flag="--force-with-lease=refs/heads/${branch}:${expected_sha}"
  fi

  local refspec="HEAD:refs/heads/${branch}"
  local -a push_options=(-u)
  [[ -n "$lease_flag" ]] && push_options=("$lease_flag" -u)
  executor_git push "${push_options[@]}" origin "$refspec"
}

# push_branch <branch>
# Push the current branch to origin. This is executor-owned — the worker never
# pushes; only the harness finalize path calls this.
push_branch() {
  local branch="$1"
  info "Pushing ${branch} to origin..."
  push_branch_ref "$branch" 2>/dev/null || error "Failed to push ${branch} to origin."
  info "Pushed ${branch}."
}

# push_checkpoint_branch <branch>
# Best-effort checkpoint push used before a paused or failed worker is cleaned
# up. Unlike push_branch, this must not abort the whole heartbeat: a local commit
# remains useful for a later retry even when GitHub/SSH is temporarily down.
push_checkpoint_branch() {
  local branch="$1"
  info "Pushing checkpoint ${branch} to origin..."
  if push_branch_ref "$branch" 2>/dev/null; then
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
  if grep -Fxq "$branch" <<< "$linked_branches"; then
    info "Branch ${branch} is already linked to issue #${issue_number}."
    return 0
  fi

  warn "Branch ${branch} is not linked to issue #${issue_number}; existing branches cannot be retroactively linked by the public API."
  return 1
}
