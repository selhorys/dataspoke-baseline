"""Hermetic tests for PRauto branch checkpoint publication."""

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


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def _make_repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "init", "-b", "dev", str(repo))
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "chore: base")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "origin", "dev")
    _git(repo, "checkout", "-b", "prauto/I-176")
    (repo / "README.md").write_text("checkpoint\n")
    _git(repo, "commit", "-am", "fix: checkpoint work")
    return repo


def _make_gh_stub(tmp_path: Path) -> tuple[Path, Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "gh.calls"
    comments = tmp_path / "gh.comments"
    linked = tmp_path / "gh.linked"
    stub = bin_dir / "gh"
    stub.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$GH_CALLS"
if [[ "$1" == issue && "$2" == view ]]; then
  if [[ "$*" == *"--jq .comments[] "* ]]; then
    if [[ -s "$GH_COMMENTS" ]]; then
      printf 'comment-id\n'
    fi
  else
    if [[ -s "$GH_COMMENTS" ]]; then
      jq -Rsc 'split("\n")[:-1] | map({createdAt: "2026-09-06T00:00:00Z", body: .})' "$GH_COMMENTS"
    else
      printf '[]\n'
    fi
  fi
elif [[ "$1" == issue && "$2" == comment ]]; then
  body=""
  while (($#)); do
    if [[ "$1" == --body ]]; then body="$2"; shift 2; else shift; fi
  done
  printf '%s\\n' "$body" >> "$GH_COMMENTS"
elif [[ "$1" == api ]]; then
  if [[ " $* " == *" --method POST "* ]]; then
    : > "$GH_LINKED"
  else
    printf '[]\n'
  fi
fi
"""
    )
    stub.chmod(0o755)
    return bin_dir, calls, linked


def test_checkpoint_comments_are_linked_and_idempotent(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    bin_dir, calls, linked = _make_gh_stub(tmp_path)
    source = "\n".join(
        [
            f"PRAUTO_DIR={shlex.quote(str(PRAUTO))}",
            f"WORKTREE_DIR={shlex.quote(str(repo))}",
            "PRAUTO_BASE_BRANCH=dev",
            "PRAUTO_GITHUB_REPO=owner/repo",
            "PRAUTO_WORKER_ID=worker",
            "READY_LABEL_TIMESTAMP=",
            f"source {shlex.quote(str(PRAUTO / 'lib/helpers.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/git-ops.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/issues.sh'))}",
            "cd \"$WORKTREE_DIR\"",
            "push_checkpoint_branch prauto/I-176",
            "publish_commit_checkpoints 176 prauto/I-176",
            "publish_commit_checkpoints 176 prauto/I-176",
        ]
    )
    result = _run(
        source,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GH_CALLS": str(calls),
            "GH_COMMENTS": str(tmp_path / "gh.comments"),
        },
    )

    assert result.returncode == 0, result.stderr
    body = (tmp_path / "gh.comments").read_text()
    assert body.count("Checkpoint commit") == 1
    assert "/commit/" in body
    assert "fix: checkpoint work" in body
    assert "--body" in calls.read_text()
    remote_branch = subprocess.run(
        ["git", "-C", str(repo), "ls-remote", "--heads", "origin", "prauto/I-176"],
        capture_output=True,
        check=False,
        text=True,
    )
    assert remote_branch.returncode == 0
    assert "refs/heads/prauto/I-176" in remote_branch.stdout
    assert not linked.exists()


def test_existing_branch_link_is_requested_via_issue_api(tmp_path: Path) -> None:
    bin_dir, calls, linked = _make_gh_stub(tmp_path)
    source = "\n".join(
        [
            f"PRAUTO_DIR={shlex.quote(str(PRAUTO))}",
            "PRAUTO_GITHUB_REPO=owner/repo",
            f"source {shlex.quote(str(PRAUTO / 'lib/helpers.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/git-ops.sh'))}",
            "link_branch_to_issue 176 prauto/I-176",
        ]
    )
    result = _run(
        source,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GH_CALLS": str(calls),
            "GH_COMMENTS": str(tmp_path / "gh.comments"),
            "GH_LINKED": str(linked),
        },
    )

    assert result.returncode == 0, result.stderr
    assert linked.exists()
    assert "--method POST" in calls.read_text()
    assert "branch=prauto/I-176" in calls.read_text()
