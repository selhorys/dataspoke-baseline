"""Hermetic tests for prauto PR creation, PR lookup, and reviewer assignment.

Traceability is to the reviewed source contract in `.prauto/lib/pr.sh`
(`create_or_update_pr`, `get_pr_number_for_branch`) and `.prauto/lib/phases.sh`
(`regression_ready`) — this is scaffold-internal tooling, not a `spec/` feature,
so there is no `spec/*.md` citation to make.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[3]
PRAUTO = ROOT / ".prauto"


def _run(script: str, *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    run_env.update(env)
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        check=False,
        text=True,
        env=run_env,
    )


def _source_pr(*, with_phases: bool = False, with_issues: bool = False) -> str:
    lines = [
        f"PRAUTO_DIR={shlex.quote(str(PRAUTO))}",
        f"source {shlex.quote(str(PRAUTO / 'lib/helpers.sh'))}",
        f"source {shlex.quote(str(PRAUTO / 'lib/pr.sh'))}",
    ]
    if with_issues:
        lines.append(f"source {shlex.quote(str(PRAUTO / 'lib/issues.sh'))}")
    if with_phases:
        lines.append(f"source {shlex.quote(str(PRAUTO / 'lib/phases.sh'))}")
    return "\n".join(lines)


def _base_env(bin_dir: Path) -> dict[str, str]:
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def _make_gh_stub(tmp_path: Path) -> tuple[Path, Path]:
    """A gh stub branching on subcommand, driven entirely by env-var knobs.

    Reused across scenarios (mirrors _make_gh_stub in test_prauto_checkpoint.py):
    every gh invocation's argv is appended to $GH_CALLS. `pulls?head=` queries
    are answered from $GH_PULLS_RESPONSES, one JSON payload per line, advancing
    through it call-by-call (so the pre-create existing-PR check and a later
    post-failure recovery check can return different results).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "gh.calls"
    stub = bin_dir / "gh"
    stub.write_text(
        """#!/usr/bin/env bash
set -uo pipefail
printf '%s\\n' "$*" >> "$GH_CALLS"

if [[ "$1" == pr && "$2" == create ]]; then
  printf '%s\\n' "${GH_PR_CREATE_URL:-https://github.com/${PRAUTO_GITHUB_REPO:-owner/repo}/pull/999}"
  exit "${GH_PR_CREATE_EXIT:-0}"
fi

if [[ "$1" == api ]] && [[ "$*" == *"pulls?head="* ]]; then
  idx=0
  [[ -f "$GH_PULLS_CALL_INDEX" ]] && idx=$(cat "$GH_PULLS_CALL_INDEX")
  line=$(sed -n "$((idx + 1))p" "$GH_PULLS_RESPONSES")
  [[ -z "$line" ]] && line=$(tail -1 "$GH_PULLS_RESPONSES")
  printf '%s\\n' "$((idx + 1))" > "$GH_PULLS_CALL_INDEX"
  printf '%s' "$line"
  exit "${GH_PULLS_EXIT:-0}"
fi

if [[ "$1" == pr && "$2" == edit ]]; then
  if [[ "$*" == *"--add-reviewer"* ]]; then
    exit "${GH_REVIEWER_EDIT_EXIT:-0}"
  fi
  exit "${GH_PR_EDIT_EXIT:-0}"
fi

if [[ "$1" == issue && "$2" == edit ]]; then
  exit "${GH_ISSUE_EDIT_EXIT:-0}"
fi

# check_review_pr/derive_phase_from_github read issue labels/comments and PR
# review-comment threads past the initial pulls?head= lookup. Answering these
# with a clean empty JSON array (rather than falling through to the silent,
# no-output catch-all below) keeps their downstream jq pipelines from choking
# on an empty stdin (e.g. "Cannot iterate over null") when a test only cares
# about the pulls?head= resolution itself.
if [[ "$1" == issue && "$2" == view ]]; then
  printf '[]'
  exit 0
fi

if [[ "$1" == api ]] && [[ "$*" == *"/comments"* ]]; then
  printf '[]'
  exit 0
fi

exit 0
"""
    )
    stub.chmod(0o755)
    return bin_dir, calls


# --- create_or_update_pr ----------------------------------------------------


def test_create_recovers_via_lookup_when_create_reports_failure(tmp_path: Path) -> None:
    # pr.sh `create_or_update_pr`: if `gh pr create` exits non-zero, it calls
    # get_pr_number_for_branch; when a PR is found it logs [WARN], best-effort
    # adds the assignee, and returns 0 rather than treating this as a hard error.
    bin_dir, calls = _make_gh_stub(tmp_path)
    pulls_responses = tmp_path / "pulls.jsonl"
    pulls_responses.write_text(
        '[]\n'
        '[{"number": 203, "head": {"repo": {"full_name": "owner/repo"}}}]\n'
    )

    result = _run(
        _source_pr()
        + "\nPRAUTO_GITHUB_REPO=owner/repo"
        + "\nPRAUTO_BASE_BRANCH=dev"
        + "\nPRAUTO_WORKER_ID=worker"
        + "\nPRAUTO_GITHUB_ACTOR=alice"
        + "\nPRAUTO_GITHUB_LABEL_WIP=prauto:wip"
        + '\ncreate_or_update_pr 42 "Add feature" prauto/I-42',
        env={
            **_base_env(bin_dir),
            "GH_CALLS": str(calls),
            "GH_PULLS_RESPONSES": str(pulls_responses),
            "GH_PULLS_CALL_INDEX": str(tmp_path / "pulls.idx"),
            "GH_PR_CREATE_EXIT": "1",
        },
    )

    assert result.returncode == 0, result.stderr
    combined_output = result.stdout + result.stderr
    assert "[WARN]" in combined_output
    calls_text = calls.read_text()
    create_line = next(line for line in calls_text.splitlines() if line.startswith("pr create"))
    assert "--reviewer" not in create_line
    assert "pr edit 203" in calls_text
    assert "--add-assignee" in calls_text


def test_create_fails_hard_when_no_recovery_pr_exists(tmp_path: Path) -> None:
    # pr.sh `create_or_update_pr`: when `gh pr create` fails AND the recovery
    # lookup finds nothing, it calls error() ("Failed to create PR"), which
    # exits 1 — this must never be silently swallowed as a success.
    bin_dir, calls = _make_gh_stub(tmp_path)
    pulls_responses = tmp_path / "pulls.jsonl"
    pulls_responses.write_text("[]\n[]\n")

    result = _run(
        _source_pr()
        + "\nPRAUTO_GITHUB_REPO=owner/repo"
        + "\nPRAUTO_BASE_BRANCH=dev"
        + "\nPRAUTO_WORKER_ID=worker"
        + "\nPRAUTO_GITHUB_ACTOR=alice"
        + "\nPRAUTO_GITHUB_LABEL_WIP=prauto:wip"
        + '\ncreate_or_update_pr 42 "Add feature" prauto/I-42',
        env={
            **_base_env(bin_dir),
            "GH_CALLS": str(calls),
            "GH_PULLS_RESPONSES": str(pulls_responses),
            "GH_PULLS_CALL_INDEX": str(tmp_path / "pulls.idx"),
            "GH_PR_CREATE_EXIT": "1",
        },
    )

    assert result.returncode == 1
    assert "Failed to create PR" in result.stderr


def test_create_success_path_has_no_recovery_lookup_or_reviewer_flag(tmp_path: Path) -> None:
    # pr.sh `create_or_update_pr`: a successful `gh pr create` returns
    # immediately — no recovery get_pr_number_for_branch call — and never
    # passes --reviewer (the flag was removed from the create invocation).
    bin_dir, calls = _make_gh_stub(tmp_path)
    pulls_responses = tmp_path / "pulls.jsonl"
    pulls_responses.write_text("[]\n")

    result = _run(
        _source_pr()
        + "\nPRAUTO_GITHUB_REPO=owner/repo"
        + "\nPRAUTO_BASE_BRANCH=dev"
        + "\nPRAUTO_WORKER_ID=worker"
        + "\nPRAUTO_GITHUB_ACTOR=alice"
        + "\nPRAUTO_GITHUB_LABEL_WIP=prauto:wip"
        + '\ncreate_or_update_pr 42 "Add feature" prauto/I-42',
        env={
            **_base_env(bin_dir),
            "GH_CALLS": str(calls),
            "GH_PULLS_RESPONSES": str(pulls_responses),
            "GH_PULLS_CALL_INDEX": str(tmp_path / "pulls.idx"),
        },
    )

    assert result.returncode == 0, result.stderr
    calls_text = calls.read_text()
    assert calls_text.count("pulls?head=") == 1  # only the initial existing-PR check
    create_line = next(line for line in calls_text.splitlines() if line.startswith("pr create"))
    assert "--reviewer" not in create_line


# --- get_pr_number_for_branch ------------------------------------------------


def test_get_pr_number_ignores_cross_repository_fork_entry(tmp_path: Path) -> None:
    # pr.sh `get_pr_number_for_branch`: filters the head-query response down to
    # entries whose head.repo.full_name matches $PRAUTO_GITHUB_REPO, so a same-
    # named branch opened from a fork is never mistaken for this repo's PR.
    bin_dir, calls = _make_gh_stub(tmp_path)
    pulls_responses = tmp_path / "pulls.jsonl"
    pulls_responses.write_text(
        '[{"number": 10, "head": {"repo": {"full_name": "forker/repo"}}}, '
        '{"number": 20, "head": {"repo": {"full_name": "owner/repo"}}}]\n'
    )

    result = _run(
        _source_pr()
        + "\nPRAUTO_GITHUB_REPO=owner/repo"
        + '\nget_pr_number_for_branch prauto/I-42'
        + '\nprintf \'%s\' "$BRANCH_PR_NUMBER"',
        env={
            **_base_env(bin_dir),
            "GH_CALLS": str(calls),
            "GH_PULLS_RESPONSES": str(pulls_responses),
            "GH_PULLS_CALL_INDEX": str(tmp_path / "pulls.idx"),
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "20"
    assert (
        "api repos/owner/repo/pulls?head=owner:prauto/I-42&state=open" in calls.read_text()
    )


def test_get_pr_number_returns_empty_on_api_error_object_without_jq_error(
    tmp_path: Path,
) -> None:
    # pr.sh `get_pr_number_for_branch`: an API error response (a JSON object,
    # not an array, alongside a non-zero gh exit) must resolve to an empty
    # BRANCH_PR_NUMBER — and since the jq filter's `if type == "array"`
    # guard handles a non-array input gracefully, this must never surface a
    # `jq: error` diagnostic on stderr.
    bin_dir, calls = _make_gh_stub(tmp_path)
    pulls_responses = tmp_path / "pulls.jsonl"
    pulls_responses.write_text('{"message": "Not Found"}\n')

    result = _run(
        _source_pr()
        + "\nPRAUTO_GITHUB_REPO=owner/repo"
        + '\nget_pr_number_for_branch prauto/I-42'
        + '\nprintf \'[%s]\' "$BRANCH_PR_NUMBER"',
        env={
            **_base_env(bin_dir),
            "GH_CALLS": str(calls),
            "GH_PULLS_RESPONSES": str(pulls_responses),
            "GH_PULLS_CALL_INDEX": str(tmp_path / "pulls.idx"),
            "GH_PULLS_EXIT": "1",
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "[]"
    assert "jq: error" not in result.stderr


# --- regression_ready ---------------------------------------------------------


def _regression_ready_env(
    tmp_path: Path, bin_dir: Path, calls: Path, **extra: str
) -> dict[str, str]:
    pulls_responses = tmp_path / "pulls.jsonl"
    pulls_responses.write_text(
        '[{"number": 55, "head": {"repo": {"full_name": "owner/repo"}}}]\n'
    )
    env = {
        **_base_env(bin_dir),
        "GH_CALLS": str(calls),
        "GH_PULLS_RESPONSES": str(pulls_responses),
        "GH_PULLS_CALL_INDEX": str(tmp_path / "pulls.idx"),
    }
    env.update(extra)
    return env


def _source_regression_ready(*, reviewer: str) -> str:
    return (
        _source_pr(with_phases=True)
        + "\nPRAUTO_GITHUB_REPO=owner/repo"
        + "\nPRAUTO_GITHUB_LABEL_WIP=prauto:wip"
        + "\nPRAUTO_GITHUB_LABEL_REVIEW=prauto:review"
        + "\nPRAUTO_GITHUB_LABEL_PLAN_REVIEW=prauto:plan-review"
        + f"\nPRAUTO_REVIEWER={shlex.quote(reviewer)}"
    )


def test_regression_ready_requests_reviewer_after_label_edits_succeed(tmp_path: Path) -> None:
    # phases.sh `regression_ready`: with PRAUTO_REVIEWER set and both label
    # edits (PR review label, issue review label) succeeding, it returns 0
    # and issues `gh pr edit <n> --add-reviewer <reviewer>` — ordered after the
    # label edits, since the reviewer request only makes sense on a PR that is
    # already marked ready for review.
    bin_dir, calls = _make_gh_stub(tmp_path)

    result = _run(
        _source_regression_ready(reviewer="ep1804")
        + '\nregression_ready 42 "prauto/I-42"; printf \'rc=%s\' "$?"',
        env=_regression_ready_env(tmp_path, bin_dir, calls),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "rc=0"
    call_lines = calls.read_text().splitlines()
    reviewer_idx = next(i for i, line in enumerate(call_lines) if "--add-reviewer ep1804" in line)
    label_idx = next(i for i, line in enumerate(call_lines) if "--add-label prauto:review" in line)
    assert reviewer_idx > label_idx


def test_regression_ready_survives_reviewer_edit_failure(tmp_path: Path) -> None:
    # phases.sh `regression_ready`: the reviewer request is explicitly
    # best-effort — a failing `gh pr edit --add-reviewer` still returns 0 and
    # only logs [WARN]; it must never flip an otherwise-ready PR back to
    # unready.
    bin_dir, calls = _make_gh_stub(tmp_path)

    result = _run(
        _source_regression_ready(reviewer="ep1804")
        + '\nregression_ready 42 "prauto/I-42"; printf \'rc=%s\' "$?"',
        env=_regression_ready_env(tmp_path, bin_dir, calls, GH_REVIEWER_EDIT_EXIT="1"),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "rc=0"
    combined_output = result.stdout + result.stderr
    assert "[WARN]" in combined_output


def test_regression_ready_skips_reviewer_request_when_unconfigured(tmp_path: Path) -> None:
    # phases.sh `regression_ready`: the reviewer request is gated on
    # PRAUTO_REVIEWER being non-empty — an empty value must never call
    # --add-reviewer at all.
    bin_dir, calls = _make_gh_stub(tmp_path)

    result = _run(
        _source_regression_ready(reviewer="")
        + '\nregression_ready 42 "prauto/I-42"; printf \'rc=%s\' "$?"',
        env=_regression_ready_env(tmp_path, bin_dir, calls),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "rc=0"
    assert "--add-reviewer" not in calls.read_text()


def test_regression_ready_fails_and_skips_reviewer_when_issue_label_edit_fails(
    tmp_path: Path,
) -> None:
    # phases.sh `regression_ready`: if the issue-label transition to review
    # fails, the function returns 1 (restoring the PR to WIP is the caller's
    # job via its own best-effort revert) and never proceeds to request a
    # reviewer on a PR whose issue side never reached "review".
    bin_dir, calls = _make_gh_stub(tmp_path)

    result = _run(
        _source_regression_ready(reviewer="ep1804")
        + '\nregression_ready 42 "prauto/I-42"; printf \'rc=%s\' "$?"',
        env=_regression_ready_env(tmp_path, bin_dir, calls, GH_ISSUE_EDIT_EXIT="1"),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "rc=1"
    assert "--add-reviewer" not in calls.read_text()


def test_regression_ready_fails_and_skips_reviewer_when_pr_label_edit_fails(
    tmp_path: Path,
) -> None:
    # phases.sh `regression_ready`: it moves the PR to prauto:review first (via
    # set_pr_review_label); if that label edit itself fails, the function
    # returns 1 immediately — before the issue-label edit and, in particular,
    # before ever requesting a reviewer.
    bin_dir, calls = _make_gh_stub(tmp_path)

    result = _run(
        _source_regression_ready(reviewer="ep1804")
        + '\nregression_ready 42 "prauto/I-42"; printf \'rc=%s\' "$?"',
        env=_regression_ready_env(tmp_path, bin_dir, calls, GH_PR_EDIT_EXIT="1"),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "rc=1"
    assert "--add-reviewer" not in calls.read_text()


# --- check_review_pr ----------------------------------------------------------


def test_check_review_pr_ignores_cross_repository_fork_entry(tmp_path: Path) -> None:
    # pr.sh `check_review_pr` runs its own inline head-query + same-repo
    # filter (a separate jq expression from get_pr_number_for_branch, not a
    # shared helper), so it needs its own coverage of the same fork-exclusion
    # contract: a fork entry ahead of the same-repo entry must not leak
    # through as REVIEW_PR_NUMBER.
    bin_dir, calls = _make_gh_stub(tmp_path)
    pulls_responses = tmp_path / "pulls.jsonl"
    pulls_responses.write_text(
        '[{"number": 10, "head": {"repo": {"full_name": "forker/repo"}}}, '
        '{"number": 20, "head": {"repo": {"full_name": "owner/repo"}}}]\n'
    )

    result = _run(
        _source_pr()
        + "\nPRAUTO_GITHUB_REPO=owner/repo"
        + "\nPRAUTO_BRANCH_PREFIX=prauto/"
        + "\nPRAUTO_WORKER_ID=worker"
        + "\nPRAUTO_GITHUB_ACTOR=alice"
        + "\ncheck_review_pr 42"
        + '\nprintf \'%s\' "$REVIEW_PR_NUMBER"',
        env={
            **_base_env(bin_dir),
            "GH_CALLS": str(calls),
            "GH_PULLS_RESPONSES": str(pulls_responses),
            "GH_PULLS_CALL_INDEX": str(tmp_path / "pulls.idx"),
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "20"
    calls_text = calls.read_text()
    assert "api repos/owner/repo/pulls?head=owner:prauto/I-42&state=open" in calls_text
    assert "pr list" not in calls_text


def test_check_review_pr_returns_waiting_on_api_error_object_without_jq_error(
    tmp_path: Path,
) -> None:
    # pr.sh `check_review_pr`: a non-array API-error response (alongside a
    # non-zero gh exit) must resolve to "no PR found" (return 1) without ever
    # surfacing a `jq: error` diagnostic — same contract as
    # get_pr_number_for_branch, verified here against check_review_pr's own
    # separate inline filter.
    bin_dir, calls = _make_gh_stub(tmp_path)
    pulls_responses = tmp_path / "pulls.jsonl"
    pulls_responses.write_text('{"message": "Not Found"}\n')

    result = _run(
        _source_pr()
        + "\nPRAUTO_GITHUB_REPO=owner/repo"
        + "\nPRAUTO_BRANCH_PREFIX=prauto/"
        + '\ncheck_review_pr 42; printf \'rc=%s\' "$?"',
        env={
            **_base_env(bin_dir),
            "GH_CALLS": str(calls),
            "GH_PULLS_RESPONSES": str(pulls_responses),
            "GH_PULLS_CALL_INDEX": str(tmp_path / "pulls.idx"),
            "GH_PULLS_EXIT": "1",
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "rc=1"
    assert "jq: error" not in result.stderr
    calls_text = calls.read_text()
    assert "api repos/owner/repo/pulls?head=owner:prauto/I-42&state=open" in calls_text
    assert "pr list" not in calls_text


# --- derive_phase_from_github --------------------------------------------------


def test_derive_phase_from_github_reports_pr_for_same_repo_entry(tmp_path: Path) -> None:
    # issues.sh `derive_phase_from_github`: an open PR for the branch (via
    # get_pr_number_for_branch, filtered to same-repo entries) always wins —
    # DERIVED_PHASE is "pr" regardless of any issue-label/comment state.
    bin_dir, calls = _make_gh_stub(tmp_path)
    pulls_responses = tmp_path / "pulls.jsonl"
    pulls_responses.write_text(
        '[{"number": 20, "head": {"repo": {"full_name": "owner/repo"}}}]\n'
    )

    result = _run(
        _source_pr(with_issues=True)
        + "\nPRAUTO_GITHUB_REPO=owner/repo"
        + '\nderive_phase_from_github 42 "prauto/I-42"'
        + '\nprintf \'%s\' "$DERIVED_PHASE"',
        env={
            **_base_env(bin_dir),
            "GH_CALLS": str(calls),
            "GH_PULLS_RESPONSES": str(pulls_responses),
            "GH_PULLS_CALL_INDEX": str(tmp_path / "pulls.idx"),
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "pr"
    calls_text = calls.read_text()
    assert "api repos/owner/repo/pulls?head=owner:prauto/I-42&state=open" in calls_text
    assert "pr list" not in calls_text


def test_derive_phase_from_github_does_not_report_pr_for_fork_only_entry(
    tmp_path: Path,
) -> None:
    # issues.sh `derive_phase_from_github`: when the only head-query match is
    # a fork entry (filtered out by get_pr_number_for_branch), BRANCH_PR_NUMBER
    # is empty and the function must fall through to the label/comment-based
    # phases instead of reporting "pr". The exact fallback phase depends on
    # issue state not under test here; only "not pr" is asserted.
    bin_dir, calls = _make_gh_stub(tmp_path)
    pulls_responses = tmp_path / "pulls.jsonl"
    pulls_responses.write_text(
        '[{"number": 10, "head": {"repo": {"full_name": "forker/repo"}}}]\n'
    )

    result = _run(
        _source_pr(with_issues=True)
        + "\nPRAUTO_GITHUB_REPO=owner/repo"
        + "\nPRAUTO_GITHUB_LABEL_PLAN_REVIEW=prauto:plan-review"
        + "\nPRAUTO_WORKER_ID=worker"
        + '\nderive_phase_from_github 42 "prauto/I-42"'
        + '\nprintf \'%s\' "$DERIVED_PHASE"',
        env={
            **_base_env(bin_dir),
            "GH_CALLS": str(calls),
            "GH_PULLS_RESPONSES": str(pulls_responses),
            "GH_PULLS_CALL_INDEX": str(tmp_path / "pulls.idx"),
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout != "pr"
    assert result.stdout != ""
    calls_text = calls.read_text()
    assert "api repos/owner/repo/pulls?head=owner:prauto/I-42&state=open" in calls_text
    assert "pr list" not in calls_text
