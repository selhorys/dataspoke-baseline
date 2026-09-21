"""Regression coverage for which issues occupy a PRauto open-issue slot.

The shell snippets stub ``gh`` with a fixture-backed executable and make no
network or GitHub calls.

spec: spec/AI_PRAUTO.md §Executor Cycle (claim new work if under
PRAUTO_OPEN_ISSUE_LIMIT); §Label lifecycle
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[3]
PRAUTO = ROOT / ".prauto"


def _run_bash(
    script: str, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run an issues-library snippet against a stubbed gh, in its own process."""
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        check=False,
        text=True,
        env=os.environ | (env or {}),
    )


def _stub_gh(tmp_path: Path, issues: list[dict[str, object]]) -> tuple[Path, Path]:
    """A gh stub returning `issues`, and recording the argv it was called with."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    payload = tmp_path / "issues.json"
    payload.write_text(json.dumps(issues))
    argv_log = tmp_path / "gh-argv.txt"
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {shlex.quote(str(argv_log))}\n'
        f"cat {shlex.quote(str(payload))}\n"
    )
    gh.chmod(0o755)
    return bin_dir, argv_log


def _issue(number: int, labels: list[str]) -> dict[str, object]:
    return {
        "number": number,
        "title": f"issue {number}",
        "labels": [{"name": name} for name in labels],
    }


def _setup(tmp_path: Path) -> str:
    return "\n".join(
        [
            f"PRAUTO_DIR={shlex.quote(str(tmp_path / 'prauto'))}",
            'PRAUTO_GITHUB_REPO="owner/repo"',
            'PRAUTO_GITHUB_ACTOR="prauto-bot"',
            'PRAUTO_GITHUB_LABEL_READY="prauto:ready"',
            'PRAUTO_GITHUB_LABEL_WIP="prauto:wip"',
            'PRAUTO_GITHUB_LABEL_FAILED="prauto:failed"',
            'PRAUTO_GITHUB_LABEL_DONE="prauto:done"',
            f"source {shlex.quote(str(PRAUTO / 'lib/helpers.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/issues.sh'))}",
        ]
    )


def _claimed_numbers(tmp_path: Path, issues: list[dict[str, object]]) -> list[int]:
    bin_dir, _ = _stub_gh(tmp_path, issues)
    # find_all_claimed_issues logs to stdout, so fence the payload rather than
    # parsing whatever the library happened to print alongside it.
    result = _run_bash(
        _setup(tmp_path)
        + "\nfind_all_claimed_issues || true"
        + "\nprintf '<<<%s>>>' \"$ALL_CLAIMED_ISSUES\"",
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stderr
    payload = result.stdout.split("<<<", 1)[1].rsplit(">>>", 1)[0]
    return [entry["number"] for entry in json.loads(payload or "[]")]


def test_terminal_issues_do_not_occupy_an_open_issue_slot(tmp_path: Path) -> None:
    """A finished issue must not hold the slot that gates new pickup.

    spec: spec/AI_PRAUTO.md §Executor Cycle — prauto claims new work while under
    PRAUTO_OPEN_ISSUE_LIMIT. The heartbeat's processing loop treats prauto:failed
    and prauto:done as terminal and does no work on them, so counting them as
    claimed holds a slot nothing can release: every later wake becomes a no-op
    until a human edits the labels.
    """
    assert _claimed_numbers(
        tmp_path,
        [
            _issue(182, ["prauto:failed"]),
            _issue(183, ["prauto:done"]),
        ],
    ) == []


def test_active_issues_still_occupy_an_open_issue_slot(tmp_path: Path) -> None:
    """Work actually in flight must still gate new pickup.

    spec: spec/AI_PRAUTO.md §Executor Cycle — the limit exists to keep one worker
    from claiming more issues than it can carry.
    """
    assert _claimed_numbers(
        tmp_path,
        [
            _issue(190, ["prauto:wip"]),
            _issue(191, ["prauto:review"]),
            _issue(192, ["prauto:plan-review"]),
        ],
    ) == [190, 191, 192]


def test_a_restarted_issue_carrying_only_ready_is_not_claimed(tmp_path: Path) -> None:
    """A re-queued issue is unclaimed work, not work in flight.

    spec: spec/AI_PRAUTO.md §Issue restart protocol — a restart leaves a fresh
    prauto:ready label, which re-enters the discovery path rather than the
    claimed-issue path.
    """
    assert _claimed_numbers(tmp_path, [_issue(200, ["prauto:ready"])]) == []


def test_a_failed_issue_alongside_active_work_leaves_only_the_active_one(
    tmp_path: Path,
) -> None:
    """Mixed state resolves per-issue, so one failure cannot wedge the worker.

    spec: spec/AI_PRAUTO.md §Label lifecycle — on failure prauto:wip is replaced
    with prauto:failed, which ends this worker's involvement with that issue.
    """
    assert _claimed_numbers(
        tmp_path,
        [
            _issue(182, ["prauto:failed"]),
            _issue(190, ["prauto:wip"]),
        ],
    ) == [190]


def test_the_claimed_query_is_scoped_to_this_worker_and_repo(tmp_path: Path) -> None:
    """The claimed set is this worker's open issues in this repo, not everyone's.

    spec: spec/AI_PRAUTO.md §Executor Cycle — the slot count is over "open issues
    assigned to this worker". The label filtering happens in jq, so without this
    the query flags are unpinned: dropping --assignee would make one worker count
    another worker's issues against its own PRAUTO_OPEN_ISSUE_LIMIT, and dropping
    --state open would count closed ones.
    """
    bin_dir, argv_log = _stub_gh(tmp_path, [_issue(190, ["prauto:wip"])])
    result = _run_bash(
        _setup(tmp_path) + "\nfind_all_claimed_issues || true",
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stderr

    argv = argv_log.read_text()
    assert "--assignee prauto-bot" in argv
    assert "--state open" in argv
    assert "-R owner/repo" in argv
    assert "--json number,title,labels" in argv
