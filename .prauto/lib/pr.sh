# PR lifecycle operations for prauto.
# Source this file — do not execute directly.
# Requires: helpers.sh, state.sh, agent.sh, config loaded, gh/git available.

# scrub_secret_values
# Print, one per line, the exact secret values scrub_secrets redacts literally:
# the worker's own GH_TOKEN / ANTHROPIC_API_KEY, plus credential-bearing
# entries of the dev env file(s). The env file is parsed as data and never
# sourced, decoded the way helm-charts/bin/lib/helpers.sh writes and reads it:
# optional `export ` prefix, a single-quoted value with '\'' escapes reversed,
# a double-quoted value, or an unquoted value with any trailing ` # comment`
# stripped. Only a key that names a credential (length >= 8, or >= 6 for
# PASS/PASSWORD keys), a value embedding URL userinfo, or a generated-looking
# value (SCRUB_GENERATED_VALUE_RE, not a slug) is redacted, so plain
# configuration (namespaces, database, model, and cluster names) stays legible.
SCRUB_CREDENTIAL_KEY_RE='(PASSWORD|PASSWD|PASS|SECRET|TOKEN|API_?KEY|_KEY$|PAT$|CREDENTIAL|AUTH|DSN|PRIVATE|SALT|SIGNING|CERT)'
# A generated secret whatever its key: >= 16 hex/base64/base64url characters.
# A lowercase hyphen/underscore slug (cluster names, namespaces, project ids)
# has the same alphabet but is configuration, so it is exempt.
SCRUB_GENERATED_VALUE_RE='^[A-Za-z0-9+/=_-]{16,}$'
SCRUB_SLUG_VALUE_RE='^[a-z0-9]+([-_][a-z0-9]+)+$'
scrub_secret_values() {
  local file line key upper_key value min_len
  local sq="'" sq_escaped="'\\''" userinfo_re='://[^@/]*:[^@/]+@'
  local -a files=()
  [[ -n "${DEV_ENV_FILE:-}" ]] && files+=("$DEV_ENV_FILE")
  if [[ -n "${REPO_DIR:-}" ]]; then
    local configured="${PRAUTO_DEV_ENV_FILE:-helm-charts/.env.dev}"
    [[ "$configured" == /* ]] || configured="${REPO_DIR}/${configured}"
    files+=("$configured")
  elif [[ "${PRAUTO_DEV_ENV_FILE:-}" == /* ]]; then
    files+=("$PRAUTO_DEV_ENV_FILE")
  fi
  [[ -n "${GH_TOKEN:-}" ]] && printf '%s\n' "$GH_TOKEN"
  [[ -n "${ANTHROPIC_API_KEY:-}" ]] && printf '%s\n' "$ANTHROPIC_API_KEY"
  [[ "${#files[@]}" -gt 0 ]] || return 0
  for file in "${files[@]}"; do
    [[ -f "$file" && -r "$file" ]] || continue
    while IFS= read -r line || [[ -n "$line" ]]; do
      line="${line%$'\r'}"
      line="${line#"${line%%[![:space:]]*}"}"
      [[ -z "$line" || "$line" == \#* ]] && continue
      [[ "$line" =~ ^export[[:space:]]+(.*)$ ]] && line="${BASH_REMATCH[1]}"
      [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
      key="${BASH_REMATCH[1]}"; value="${BASH_REMATCH[2]}"
      if [[ ${#value} -ge 2 && "$value" == "$sq"*"$sq" ]]; then
        value="${value:1:${#value}-2}"
        value="${value//"$sq_escaped"/$sq}"
      elif [[ ${#value} -ge 2 && "$value" == \"*\" ]]; then
        value="${value:1:${#value}-2}"
      else
        value="${value%%[[:space:]]#*}"
        value="${value%"${value##*[![:space:]]}"}"
      fi
      [[ -n "$value" ]] || continue
      if [[ "$value" =~ $userinfo_re ]] || \
         [[ "$value" =~ $SCRUB_GENERATED_VALUE_RE && ! "$value" =~ $SCRUB_SLUG_VALUE_RE ]]; then
        printf '%s\n' "$value"
        continue
      fi
      upper_key=$(tr '[:lower:]' '[:upper:]' <<< "$key")
      [[ "$upper_key" =~ $SCRUB_CREDENTIAL_KEY_RE ]] || continue
      min_len=8
      [[ "$upper_key" == *PASS* ]] && min_len=6
      [[ ${#value} -ge $min_len ]] && printf '%s\n' "$value"
    done < "$file"
  done
  return 0
}

# scrub_secrets <text>
# Sanitize text before it reaches a public PR comment. The E2E and api-wired
# suites exercise auth flows with DATASPOKE_DEV_* credentials, so a failing
# assertion can echo a token or a postgresql://user:***@host URL verbatim.
# In order:
#   1. Normalize: strip CR and other C0/C1 controls (keeping LF and TAB), ESC
#      CSI/OSC sequences (including OSC 8 hyperlinks), and invisible Unicode
#      format characters (zero-width, bidi controls, BOM).
#   2. Literally redact every exact value from scrub_secret_values.
#   3. Redact credential shapes: DATASPOKE_* assignments (=, :, quoted keys),
#      Authorization/Bearer tokens, password/secret/token/api-key assignments,
#      JWTs, dsk_/sk-/GitHub token shapes, and URL userinfo (including an
#      empty username).
# Value classes stop at a backtick so a redaction never consumes the closing
# delimiter of a markdown code span. Runs in perl, byte-oriented so malformed
# UTF-8 cannot abort it. Without a working perl the text is withheld entirely
# rather than published unscrubbed.
scrub_secrets() {
  local text="$1" values scrubbed
  if ! command -v perl >/dev/null 2>&1; then
    printf '%s' "(output withheld: secret scrubber unavailable)"
    return 0
  fi
  values=$(scrub_secret_values)
  # shellcheck disable=SC2016
  if ! scrubbed=$(PRAUTO_SCRUB_VALUES="$values" perl -0777 -pe '
      BEGIN {
        our @values = sort { length($b) <=> length($a) }
          grep { length($_) >= 6 } split /\n/, ($ENV{PRAUTO_SCRUB_VALUES} // "");
      }
      our @values;
      s/\e\][^\a\e\n]*(?:\a|\e\\)?//g;
      s/\e\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]//g;
      s/\e[\x40-\x5f]//g;
      s/[\x00-\x08\x0b-\x1f\x7f]//g;
      s/\xc2[\x80-\x9f]//g;
      s/\xe2\x80[\x8b-\x8f\xaa-\xae]|\xe2\x81[\xa0-\xa4\xa6-\xa9]|\xef\xbb\xbf|\xd8\x9c//g;
      for my $v (@values) { s/\Q$v\E/***REDACTED***/g; }
      s/(DATASPOKE_[A-Z0-9_]*=)[^\s\x60]*/${1}***REDACTED***/g;
      s/(["\x27]?DATASPOKE_[A-Z0-9_]*["\x27]?[ \t]*:[ \t]*["\x27]?)[^\s"\x27,}\x60]+/${1}***REDACTED***/g;
      s/\b(Authorization|Bearer)([: \t]+)(?:(?:Bearer|Basic|Token)[ \t]+)?[^\s"\x27,;\x60]+/${1}${2}***REDACTED***/gi;
      s/((?:password|passwd|pwd|secret|token|api[_-]?key)["\x27]?[ \t]*[:=][ \t]*["\x27]?)[^\s"\x27&,;\x60]+/${1}***REDACTED***/gi;
      s/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/***REDACTED***/g;
      s/dsk_[A-Za-z0-9_-]+/***REDACTED***/g;
      s/\bsk-[A-Za-z0-9_-]{16,}/***REDACTED***/g;
      s/\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}/***REDACTED***/g;
      s/\bgithub_pat_[A-Za-z0-9_]{16,}/***REDACTED***/g;
      s#://[^@/\s\x60]*:[^@/\s\x60]+@#://***REDACTED***@#g;
    ' <<< "$text"); then
    printf '%s' "(output withheld: secret scrubber failed)"
    return 0
  fi
  printf '%s' "$scrubbed"
}

# create_or_update_pr <issue_number> <issue_title> <branch>
set_pr_wip_label() {
  local branch="$1"
  get_pr_number_for_branch "$branch"
  [[ -n "${BRANCH_PR_NUMBER:-}" ]] || return 1
  gh pr edit "$BRANCH_PR_NUMBER" -R "$PRAUTO_GITHUB_REPO" \
    --remove-label "$PRAUTO_GITHUB_LABEL_REVIEW" \
    --add-label "$PRAUTO_GITHUB_LABEL_WIP" 2>/dev/null || return 1
}

set_pr_review_label() {
  local branch="$1"
  get_pr_number_for_branch "$branch"
  [[ -n "${BRANCH_PR_NUMBER:-}" ]] || return 1
  gh pr edit "$BRANCH_PR_NUMBER" -R "$PRAUTO_GITHUB_REPO" \
    --remove-label "$PRAUTO_GITHUB_LABEL_WIP" \
    --add-label "$PRAUTO_GITHUB_LABEL_REVIEW" 2>/dev/null || return 1
}

create_or_update_pr() {
  local issue_number="$1" issue_title="$2" branch="$3"
  get_pr_number_for_branch "$branch"
  local existing_pr="$BRANCH_PR_NUMBER"

  if [[ -n "$existing_pr" ]]; then
    info "PR #${existing_pr} already exists for ${branch}. Adding update comment."
    local commit_log
    commit_log=$(git log --oneline "origin/${PRAUTO_BASE_BRANCH}..HEAD" 2>/dev/null || printf '(no commits)')
    prauto_pr_comment "$existing_pr" "Updated with new commits.

\`\`\`
${commit_log}
\`\`\`" \
      "Failed to comment on PR #${existing_pr}."
    gh pr edit "$existing_pr" -R "$PRAUTO_GITHUB_REPO" \
      --remove-label "$PRAUTO_GITHUB_LABEL_REVIEW" \
      --add-label "$PRAUTO_GITHUB_LABEL_WIP" 2>/dev/null || warn "Failed to set PR #${existing_pr} to prauto:wip."
    return 0
  fi

  local commit_log
  commit_log=$(git log --oneline "origin/${PRAUTO_BASE_BRANCH}..HEAD" 2>/dev/null || printf '(no commits)')
  local pr_body="## Summary

Automated implementation for #${issue_number}.
Generated by \`prauto(${PRAUTO_WORKER_ID})\`.

## Changes

\`\`\`
${commit_log}
\`\`\`

---
*Generated by prauto -- autonomous PR worker*"

  info "Creating PR for issue #${issue_number}..."
  if gh pr create -R "$PRAUTO_GITHUB_REPO" \
    --base "$PRAUTO_BASE_BRANCH" --head "$branch" \
    --title "$issue_title" --body "$pr_body" \
    --assignee "$PRAUTO_GITHUB_ACTOR" --label "${PRAUTO_GITHUB_LABEL_WIP}"; then
    info "PR created for issue #${issue_number}."
    return 0
  fi

  # gh pr create can fail on a post-create update mutation (e.g. GitHub GraphQL
  # errors) after the PR itself was already created. Check before treating this
  # as a hard failure.
  get_pr_number_for_branch "$branch"
  if [[ -n "${BRANCH_PR_NUMBER:-}" ]]; then
    warn "PR #${BRANCH_PR_NUMBER} exists for ${branch} but gh's post-create update failed. Continuing."
    gh pr edit "$BRANCH_PR_NUMBER" -R "$PRAUTO_GITHUB_REPO" \
      --add-assignee "$PRAUTO_GITHUB_ACTOR" 2>/dev/null \
      || warn "Failed to set assignee on PR #${BRANCH_PR_NUMBER}."
    info "PR created for issue #${issue_number}."
    return 0
  fi
  error "Failed to create PR for ${branch}."
}

# check_review_pr <issue_number>
# For a prauto:review issue, determine the next action. Priority: squash-finalize
# (approved + mergeable + clean) > address feedback > waiting.
# Sets: REVIEW_PR_NUMBER/BRANCH/ACTION/TITLE/BODY, ACTIONABLE_COMMENTS.
# Returns 0 if actionable, 1 if waiting/no-pr.
check_review_pr() {
  local issue_number="$1"
  REVIEW_PR_BRANCH="${PRAUTO_BRANCH_PREFIX}I-${issue_number}"
  REVIEW_PR_ACTION=""

  local owner="${PRAUTO_GITHUB_REPO%%/*}" pr_list_json
  pr_list_json=$(gh api "repos/${PRAUTO_GITHUB_REPO}/pulls?head=${owner}:${REVIEW_PR_BRANCH}&state=open" 2>/dev/null \
    | jq -r --arg repo "$PRAUTO_GITHUB_REPO" \
      'if type == "array" then [.[] | select(.head.repo.full_name == $repo)] | .[0] // empty else empty end') || pr_list_json=""
  [[ -z "$pr_list_json" ]] && return 1

  REVIEW_PR_NUMBER=$(printf '%s' "$pr_list_json" | jq -r '.number')
  REVIEW_PR_TITLE=$(printf '%s' "$pr_list_json" | jq -r '.title')
  REVIEW_PR_BODY=$(printf '%s' "$pr_list_json" | jq -r '.body // ""')

  local pr_detail
  pr_detail=$(gh pr view "$REVIEW_PR_NUMBER" -R "$PRAUTO_GITHUB_REPO" \
    --json mergeable,mergeStateStatus,reviews 2>/dev/null) || pr_detail=""

  # Priority 1: squash-ready (approved + MERGEABLE + CLEAN).
  if [[ -n "$pr_detail" ]] && fetch_org_members 2>/dev/null; then
    local mergeable merge_state approver
    mergeable=$(printf '%s' "$pr_detail" | jq -r '.mergeable')
    merge_state=$(printf '%s' "$pr_detail" | jq -r '.mergeStateStatus')
    approver=$(printf '%s' "$pr_detail" | jq -r --argjson members "$ORG_MEMBERS_JSON" '
      (.reviews // [])
      | map(select(.author.login as $a | $members | index($a) != null))
      | group_by(.author.login)
      | map(sort_by(.submittedAt) | last)
      | map(select(.state == "APPROVED"))
      | first // empty | .author.login // empty')
    if [[ -n "$approver" ]] && [[ "$mergeable" == "MERGEABLE" ]] && [[ "$merge_state" == "CLEAN" ]]; then
      REVIEW_PR_ACTION="squash_ready"
      info "PR #${REVIEW_PR_NUMBER}: approved by '${approver}', mergeable, clean → squash-ready."
      return 0
    fi
  fi

  # Priority 2: feedback-needed (unaddressed non-prauto comments).
  local pr_review_comments pr_issue_comments
  pr_review_comments=$(gh api "repos/${PRAUTO_GITHUB_REPO}/pulls/${REVIEW_PR_NUMBER}/comments" \
    --jq '[.[] | {id: .id, body: .body, user: .user.login, created_at: .created_at}]' 2>/dev/null || printf '[]')
  pr_issue_comments=$(gh api "repos/${PRAUTO_GITHUB_REPO}/issues/${REVIEW_PR_NUMBER}/comments" \
    --jq '[.[] | {id: .id, body: .body, user: .user.login, created_at: .created_at}]' 2>/dev/null || printf '[]')

  local latest_prauto_body latest_prauto_time
  latest_prauto_body=$(printf '%s' "$pr_issue_comments" | jq -r --arg actor "$PRAUTO_GITHUB_ACTOR" \
    '[.[] | select(.user == $actor)] | sort_by(.created_at) | last | .body // ""')
  latest_prauto_time=$(printf '%s' "$pr_issue_comments" | jq -r --arg actor "$PRAUTO_GITHUB_ACTOR" \
    '[.[] | select(.user == $actor)] | sort_by(.created_at) | last | .created_at // ""')

  if grep -q "Reviewer feedback addressed" <<< "$latest_prauto_body"; then
    local newer_issue newer_reviews
    newer_issue=$(printf '%s' "$pr_issue_comments" | jq --arg actor "$PRAUTO_GITHUB_ACTOR" --arg ts "$latest_prauto_time" \
      '[.[] | select(.user != $actor) | select(.created_at > $ts)] | length')
    newer_reviews=$(printf '%s' "$pr_review_comments" | jq --arg ts "$latest_prauto_time" \
      '[.[] | select(.created_at > $ts)] | length')
    if [[ "$newer_issue" -eq 0 ]] && [[ "$newer_reviews" -eq 0 ]]; then
      info "PR #${REVIEW_PR_NUMBER}: feedback already addressed. Waiting."
      return 1
    fi
    info "PR #${REVIEW_PR_NUMBER}: new comments after marker. Re-evaluating."
  fi

  local all_comments
  all_comments=$(jq -s 'add' <<< "${pr_review_comments}${pr_issue_comments}" 2>/dev/null || printf '[]')
  local unaddressed unaddressed_count
  unaddressed=$(printf '%s' "$all_comments" | jq -r --arg worker "prauto(${PRAUTO_WORKER_ID})" \
    '[.[] | select(.body | startswith($worker) | not)]')
  unaddressed_count=$(printf '%s' "$unaddressed" | jq 'length')

  local has_review_signal=false
  if [[ -n "$pr_detail" ]]; then
    local has_review
    has_review=$(printf '%s' "$pr_detail" | jq \
      '[(.reviews // [])[] | select(.state == "CHANGES_REQUESTED" or .state == "COMMENTED")] | length > 0')
    [[ "$has_review" == "true" ]] && has_review_signal=true
  fi
  if [[ "$has_review_signal" == "false" ]] && [[ "$unaddressed_count" -gt 0 ]]; then
    local external_count
    external_count=$(printf '%s' "$unaddressed" | jq -r --arg actor "$PRAUTO_GITHUB_ACTOR" \
      '[.[] | select(.user != $actor)] | length')
    [[ "$external_count" -gt 0 ]] && has_review_signal=true
  fi

  if [[ "$unaddressed_count" -gt 0 ]] && [[ "$has_review_signal" == "true" ]]; then
    ACTIONABLE_COMMENTS=$(printf '%s' "$all_comments" | jq -r --arg worker "prauto(${PRAUTO_WORKER_ID})" '
      [.[] | select(.body | startswith($worker) | not)]
      | map("Comment by \(.user):\n\(.body)")
      | join("\n\n---\n\n")')
    REVIEW_PR_ACTION="feedback_needed"
    info "PR #${REVIEW_PR_NUMBER}: unaddressed comments → feedback needed."
    return 0
  fi

  info "PR #${REVIEW_PR_NUMBER}: waiting for review."
  return 1
}

# Rebase only when the current PR branch does not already contain the fetched
# base branch. Replaying an already-merged base through `git rebase` can invent
# conflicts for changes which the PR branch has already integrated.
rebase_pr_branch_if_needed() {
  local pr_number="$1"

  git fetch origin "$PRAUTO_BASE_BRANCH" 2>/dev/null || { warn "PR #${pr_number}: git fetch failed."; return 1; }
  if git merge-base --is-ancestor "origin/${PRAUTO_BASE_BRANCH}" HEAD 2>/dev/null; then
    info "PR #${pr_number}: branch already contains origin/${PRAUTO_BASE_BRANCH}; skipping rebase."
    return 0
  fi

  git rebase "origin/${PRAUTO_BASE_BRANCH}" 2>/dev/null || {
    warn "PR #${pr_number}: rebase failed. Aborting."
    git rebase --abort 2>/dev/null || true
    return 1
  }
}

# squash_and_finalize_pr <pr_number> <pr_branch> <pr_title> <pr_body> <issue_number>
# Squash the PR branch into one commit, force-push, mark prauto:done. Does NOT
# merge or close — left to the human. Must be called from inside the worktree.
squash_and_finalize_pr() {
  local pr_number="$1" pr_branch="$2" pr_title="$3" pr_body="$4" issue_number="$5"

  export GIT_AUTHOR_NAME="$PRAUTO_GIT_AUTHOR_NAME" GIT_AUTHOR_EMAIL="$PRAUTO_GIT_AUTHOR_EMAIL"
  export GIT_COMMITTER_NAME="$PRAUTO_GIT_AUTHOR_NAME" GIT_COMMITTER_EMAIL="$PRAUTO_GIT_AUTHOR_EMAIL"

  rebase_pr_branch_if_needed "$pr_number" || return 1

  local merge_base
  merge_base=$(git merge-base HEAD "origin/${PRAUTO_BASE_BRANCH}" 2>/dev/null) || {
    warn "PR #${pr_number}: could not find merge base."
    return 1
  }

  local issue_title issue_body diff_stat diff_content
  issue_title=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" --json title --jq '.title // ""' 2>/dev/null || printf '')
  issue_body=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" --json body --jq '.body // ""' 2>/dev/null || printf '')
  diff_stat=$(git diff --stat "${merge_base}..HEAD" 2>/dev/null || printf '(no diff stat)')
  diff_content=$(git diff "${merge_base}..HEAD" 2>/dev/null || printf '(no diff)')

  generate_squash_commit_message "$issue_number" "$issue_title" "$issue_body" "$pr_number" "$diff_stat" "$diff_content"

  local approver_login approver_name approver_email co_authored_by=""
  while IFS= read -r approver_login; do
    [[ -z "$approver_login" ]] && continue
    approver_name=$(gh api "users/${approver_login}" --jq '.name // .login' 2>/dev/null || printf '%s' "$approver_login")
    approver_email=$(gh api "users/${approver_login}" --jq '.email // ""' 2>/dev/null || printf '')
    [[ -z "$approver_email" ]] && approver_email="${approver_login}@users.noreply.github.com"
    co_authored_by+="Co-Authored-By: ${approver_name} <${approver_email}>"$'\n'
  done < <(gh pr view "$pr_number" -R "$PRAUTO_GITHUB_REPO" \
    --json reviews --jq '[.reviews[] | select(.state=="APPROVED") | .author.login] | unique | .[]' 2>/dev/null)

  [[ -n "$co_authored_by" ]] && SQUASH_COMMIT_MESSAGE="${SQUASH_COMMIT_MESSAGE}

${co_authored_by%$'\n'}"

  local msg_file
  if [[ -n "${CUR_SESSION_DIR:-}" ]] && [[ -d "${CUR_SESSION_DIR:-}" ]]; then
    msg_file="${CUR_SESSION_DIR}/squash-msg.txt"
  else
    msg_file=$(mktemp)
  fi
  printf '%s\n' "$SQUASH_COMMIT_MESSAGE" > "$msg_file"

  local author_arg="${PRAUTO_GIT_AUTHOR_NAME} <${PRAUTO_GIT_AUTHOR_EMAIL}>"
  git reset --soft "$merge_base" 2>/dev/null || { warn "PR #${pr_number}: git reset --soft failed."; rm -f "$msg_file"; return 1; }
  git commit --author="$author_arg" --file="$msg_file" 2>/dev/null || {
    warn "PR #${pr_number}: git commit (squash) failed."; rm -f "$msg_file"; return 1
  }
  rm -f "$msg_file"

  # Force-push with lease, ALWAYS over SSH via the worker's dedicated key (scoped
  # by ~/.gitconfig includeIf). GH_TOKEN is API-only and never near the push path.
  local expected_sha lease_flag="--force-with-lease"
  expected_sha=$(git rev-parse "refs/remotes/origin/${pr_branch}" 2>/dev/null || printf '')
  [[ -n "$expected_sha" ]] && lease_flag="--force-with-lease=refs/heads/${pr_branch}:${expected_sha}"

  git push "$lease_flag" origin "HEAD:refs/heads/${pr_branch}" 2>/dev/null || {
    warn "PR #${pr_number}: force-push failed. Skipping."
    return 1
  }
  info "PR #${pr_number}: force-pushed squashed commit."
  link_branch_to_issue "$issue_number" "$pr_branch" || true
  publish_commit_checkpoints "$issue_number" "$pr_branch" || true

  local final_commit_title
  final_commit_title=$(git log -1 --format='%s' HEAD 2>/dev/null || printf '%s' "$pr_title")
  gh api "repos/${PRAUTO_GITHUB_REPO}/pulls/${pr_number}" \
    -X PATCH -f title="$final_commit_title" --silent 2>/dev/null \
    || warn "PR #${pr_number}: failed to update PR title."

  local target
  for target in "$pr_number" "$issue_number"; do
    gh api "repos/${PRAUTO_GITHUB_REPO}/issues/${target}/labels" \
      -X POST -f "labels[]=${PRAUTO_GITHUB_LABEL_DONE}" --silent 2>/dev/null \
      || warn "#${target}: failed to add ${PRAUTO_GITHUB_LABEL_DONE} label."
    gh api "repos/${PRAUTO_GITHUB_REPO}/issues/${target}/labels/${PRAUTO_GITHUB_LABEL_REVIEW}" \
      -X DELETE --silent 2>/dev/null \
      || warn "#${target}: failed to remove ${PRAUTO_GITHUB_LABEL_REVIEW} label."
  done
  info "PR #${pr_number}: marked prauto:done (PR and issue #${issue_number}). NOT merged."
}

# post_review_response_comment <pr_number> <response_text>
post_review_response_comment() {
  local pr_number="$1" response_text="$2"
  [[ -z "$response_text" ]] && return 0
  prauto_pr_comment "$pr_number" "Review response

${response_text}" \
    "Failed to post review response on PR #${pr_number}."
}

# get_pr_number_for_branch <branch>  — Sets: BRANCH_PR_NUMBER (empty if none).
# Owner-qualified REST head query (server-side): `gh pr list --head` paginates
# at 30, so client-side fork filtering can drop the same-repo PR off the page.
get_pr_number_for_branch() {
  local branch="$1" owner="${PRAUTO_GITHUB_REPO%%/*}"
  BRANCH_PR_NUMBER=$(gh api "repos/${PRAUTO_GITHUB_REPO}/pulls?head=${owner}:${branch}&state=open" 2>/dev/null \
    | jq -r --arg repo "$PRAUTO_GITHUB_REPO" \
      'if type == "array" then [.[] | select(.head.repo.full_name == $repo)] | .[0].number // empty else empty end') || BRANCH_PR_NUMBER=""
}

# post_test_results_comment <pr_number> <test_type> <exit_code> <output>
post_test_results_comment() {
  local pr_number="$1" test_type="$2" exit_code="$3" output="$4"
  output=$(scrub_secrets "$output")

  local status_label
  if [[ "$exit_code" -eq 0 ]]; then status_label="Passed"; else status_label="Failed (exit code ${exit_code})"; fi
  if [[ ${#output} -gt 60000 ]]; then output="${output:0:60000}
... (truncated)"; fi

  prauto_pr_comment "$pr_number" "${test_type} Test Results — ${status_label}

<details>
<summary>${test_type} test output</summary>

\`\`\`
${output}
\`\`\`

</details>" \
    "Failed to post ${test_type} test results on PR #${pr_number}."
}

# post_feedback_addressed_comment <pr_number>
post_feedback_addressed_comment() {
  local pr_number="$1"
  prauto_pr_comment "$pr_number" "Reviewer feedback addressed." \
    "Failed to post feedback-addressed comment on PR #${pr_number}."
}
