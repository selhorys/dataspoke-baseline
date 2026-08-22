#!/usr/bin/env bash
# Bash port of .claude/workflows/wf-minimal.js's generate -> evaluate stage loop, for coding-agent
# backends with no built-in subagent-orchestration primitive (Codex today). A Claude Code session
# should keep using its native Agent/Workflow tool (wf-minimal.js) instead — it's more integrated
# (parallel tool calls, structured-output schema enforcement, no subprocess overhead per stage).
# This script exists only to cover the gap for agents without that primitive.
#
# Usage:
#   scaffold/bin/run-workflow.sh <plan-file> --agent {claude|codex} [--security s1,s2,...] <stage> [<stage> ...]
#
# Each <stage> is a role name from scaffold/roles/ (spec, backend, airflow-dag, test, frontend,
# k8s-helm). Stages run in the given order, sequentially — unlike wf-minimal.js's inner-array
# concurrent-group syntax, this script has no parallelism; add it if a real need shows up.
# --security lists stages whose diff touches security-reviewer's sensitive-path list (per
# scaffold/roles/security-reviewer.md) — those stages get a second, security review pass merged
# worst-of with the primary reviewer's verdict. k8s-helm has no primary review loop (matches
# CLAUDE.md/AGENTS.md step 9) but still gets a security pass if listed in --security.
#
# Per stage: generate -> reviewer(s) -> [one fix pass on REVISE] -> re-review -> ESCALATE if
# REVISE persists or a reviewer produced no parseable verdict. Any ESCALATE halts the run.

set -euo pipefail

plan_file="${1:?usage: run-workflow.sh PLAN-FILE --agent claude-or-codex [--security s1,s2] STAGE [STAGE ...]}"
shift

agent="" security_csv=""
declare -a stages=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent) agent="$2"; shift 2 ;;
    --security) security_csv="$2"; shift 2 ;;
    *) stages+=("$1"); shift ;;
  esac
done

[[ -n "$agent" ]] || { echo "--agent {claude|codex} is required" >&2; exit 1; }
[[ ${#stages[@]} -gt 0 ]] || { echo "at least one stage is required" >&2; exit 1; }

# Each stage interpolates into a role-file path (via run-stage.sh) and into $work_dir file names
# below — reject anything but a bare stage name up front, same reasoning as run-stage.sh's own
# role-name check.
for s in "${stages[@]}"; do
  [[ "$s" =~ ^[a-z0-9-]+$ ]] || { echo "invalid stage name: $s (expected lowercase letters, digits, hyphens only)" >&2; exit 1; }
done

# Split --security on commas, trimming whitespace around each name (so "backend, frontend" and
# "backend,frontend" behave identically instead of the former silently dropping "frontend"), and
# reject any name that isn't actually one of the stages this run was given — a typo here should
# fail loudly, not silently skip a security review.
declare -a security_stages=()
if [[ -n "$security_csv" ]]; then
  IFS=',' read -ra _sec_raw <<< "$security_csv"
  for raw in "${_sec_raw[@]}"; do
    trimmed="${raw#"${raw%%[![:space:]]*}"}"
    trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
    [[ -z "$trimmed" ]] && continue
    match=0
    for s in "${stages[@]}"; do
      [[ "$s" == "$trimmed" ]] && match=1 && break
    done
    [[ "$match" -eq 1 ]] || { echo "--security names '$trimmed' which is not in the stage list (${stages[*]})" >&2; exit 1; }
    security_stages+=("$trimmed")
  done
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
run_stage="$repo_root/scaffold/bin/run-stage.sh"
work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT

is_security_stage() {
  local s
  for s in ${security_stages[@]+"${security_stages[@]}"}; do
    [[ "$s" == "$1" ]] && return 0
  done
  return 1
}

# Primary reviewer for a stage; empty string means no primary review loop (k8s-helm).
primary_reviewer_for() {
  case "$1" in
    test) echo "test-reviewer" ;;
    spec) echo "spec-reviewer" ;;
    k8s-helm) echo "" ;;
    *) echo "reviewer" ;;
  esac
}

# Reviewers to run for a stage, one per line: the primary reviewer (unless the stage has none)
# plus security-reviewer when the stage is security-flagged.
reviewers_for() {
  local stage="$1" primary
  primary=$(primary_reviewer_for "$stage")
  [[ -n "$primary" ]] && echo "$primary"
  is_security_stage "$stage" && echo "security-reviewer"
}

# Extracts the APPROVE/REVISE/ESCALATE token from a reviewer's ### Verdict section.
# Reviewer roles emit a fixed "### Verdict\n<TOKEN> — ..." footer (see scaffold/roles/reviewer.md
# §Output format); an empty result means the reviewer failed to produce a parseable verdict.
#
# Takes the LAST such token, not the first: every reviewer role file's own prompt contains a
# literal "### Verdict\nAPPROVE — ..." example inside its Output format template, and a model
# restating that template earlier in its response (e.g. while explaining what it's about to do)
# would otherwise be parsed as the real, final verdict — silently turning a real REVISE/ESCALATE
# into a false APPROVE with no fix pass. The model's actual verdict is expected to be the last
# "### Verdict" section in its output.
parse_verdict() {
  awk '/^### Verdict/{found=1; next} found && /^(APPROVE|REVISE|ESCALATE)/{v=$1; found=0} END{print v}' "$1"
}

# Merges verdicts worst-of (APPROVE < REVISE < ESCALATE); a missing/unparseable verdict is treated
# as ESCALATE, mirroring wf-minimal.js's "a reviewer failed to produce a verdict" fail-closed rule.
worst_verdict() {
  local worst="APPROVE" v rank_v rank_worst
  rank() { case "$1" in APPROVE) echo 0 ;; REVISE) echo 1 ;; *) echo 2 ;; esac; }
  for v in "$@"; do
    [[ -z "$v" ]] && v="ESCALATE"
    rank_v=$(rank "$v"); rank_worst=$(rank "$worst")
    (( rank_v > rank_worst )) && worst="$v"
  done
  echo "$worst"
}

run_review_round() {
  local stage="$1" report_file="$2" round="$3" reviewer review_file
  declare -a verdicts=()
  while IFS= read -r reviewer; do
    [[ -z "$reviewer" ]] && continue
    review_file="$work_dir/${stage}.${reviewer}.${round}.md"
    "$run_stage" "$reviewer" "$plan_file" --agent "$agent" --input "$report_file" > "$review_file"
    verdicts+=("$(parse_verdict "$review_file")")
  done < <(reviewers_for "$stage")
  # ${arr[@]+"${arr[@]}"}: bash 3.2 (stock macOS) treats an empty array as unset under `set -u`.
  # Unreachable today (run_one_stage only calls this when reviewers_for yields >=1 line), but
  # safe regardless if a future no-primary-reviewer, non-security stage reaches it.
  worst_verdict ${verdicts[@]+"${verdicts[@]}"}
}

run_one_stage() {
  local stage="$1" report_file verdict
  report_file="$work_dir/${stage}.generate.md"
  "$run_stage" "$stage" "$plan_file" --agent "$agent" > "$report_file"

  if [[ -z "$(primary_reviewer_for "$stage")" ]] && ! is_security_stage "$stage"; then
    echo "DONE"
    return
  fi

  verdict=$(run_review_round "$stage" "$report_file" "review-1")

  if [[ "$verdict" == "REVISE" ]]; then
    echo "  ${stage}: REVISE — running fix pass" >&2
    local findings_file="$work_dir/${stage}.findings.review-1.md"
    cat "$work_dir/${stage}".*.review-1.md > "$findings_file"
    "$run_stage" "$stage" "$plan_file" --agent "$agent" --input "$findings_file" > "$report_file"
    verdict=$(run_review_round "$stage" "$report_file" "review-2")
    [[ "$verdict" == "REVISE" ]] && verdict="ESCALATE"
  fi

  echo "$verdict"
}

for stage in "${stages[@]}"; do
  echo "== stage: $stage ==" >&2
  outcome=$(run_one_stage "$stage")
  echo "== $stage outcome: $outcome ==" >&2
  if [[ "$outcome" == "ESCALATE" ]]; then
    # $work_dir is deleted by the EXIT trap the moment this script exits — copy it to a durable
    # location first, or the human being handed this decision has no findings left to read.
    escalate_dir="${TMPDIR:-/tmp}/dataspoke-workflow-escalate-$(date +%Y%m%d-%H%M%S)-$$"
    cp -r "$work_dir" "$escalate_dir"
    echo "Halted at stage '$stage' — ESCALATE, needs human decision. Review findings under: $escalate_dir/${stage}.*.md" >&2
    exit 1
  fi
done

echo "All stages complete." >&2
