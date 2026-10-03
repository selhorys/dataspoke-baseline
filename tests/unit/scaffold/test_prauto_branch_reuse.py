"""Hermetic tests for PRauto branch-reuse reconciliation.

spec: spec/AI_PRAUTO.md "Branch-reuse reconciliation" (under "Branch-based continuity").
Before a reused ``prauto/I-<n>`` branch, or an existing PR branch, gets a worktree, the
local ref is reconciled with ``origin/<branch>``:

    local missing, origin present       -> local created at origin
    origin missing                      -> local kept
    every local commit is known to
    origin (current head or reflog)     -> local moved to origin (fast-forward, or a
                                           rebase + force-push done elsewhere)
    origin is an ancestor of local      -> local kept, with a warning (unpushed checkpoint)
    otherwise (diverged)                -> fail closed, no worktree, local ref untouched

The squash path restores the local branch to its pre-squash head on any failure, so
the next wake does not mistake its own half-finished rewrite for a diverged branch.

Each test drives the REAL ``.prauto/lib`` shell functions against a bare ``remote.git``
and clones in ``tmp_path``. Nothing here touches ``.prauto/state``.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
PRAUTO = ROOT / ".prauto"
BRANCH = "prauto/I-1"
ISSUE = "1"

# Hermetic git: no user/system config (signing, includeIf, hooks), identity from env.
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.update(GIT_ENV)
    env.update(extra or {})
    return env


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
        text=True,
        env=_env(),
    )
    if check:
        assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result


def _sha(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", ref).stdout.strip()


def _commit(repo: Path, filename: str, message: str) -> None:
    (repo / filename).write_text(message + "\n")
    _git(repo, "add", filename)
    _git(repo, "commit", "-m", message)


@dataclass
class Fixture:
    """A bare remote, the executor's clone (``repo``) and a second human clone."""

    remote: Path
    repo: Path
    other: Path
    prauto_dir: Path
    gh_bin: Path

    @property
    def local_sha(self) -> str:
        return _sha(self.repo, f"refs/heads/{BRANCH}")

    @property
    def origin_sha(self) -> str:
        """The remote's actual branch head (not the possibly stale tracking ref)."""
        return _git(self.remote, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip()


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    """origin/dev has a base commit; origin/prauto/I-1 has one commit; local == origin."""
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    _git(tmp_path, "init", "--bare", "-b", "dev", str(remote))
    _git(tmp_path, "init", "-b", "dev", str(repo))
    # Absolute URL: a relative one breaks fetches run from inside a worktree.
    _git(repo, "remote", "add", "origin", str(remote))
    _commit(repo, "README.md", "chore: base")
    _git(repo, "push", "origin", "dev")
    _git(repo, "checkout", "-b", BRANCH)
    _commit(repo, "work.txt", "feat: pushed work")
    _git(repo, "push", "-u", "origin", BRANCH)
    # The branch must not be checked out in the main clone, as in the executor.
    _git(repo, "checkout", "dev")
    _git(tmp_path, "clone", str(remote), str(other))
    _git(other, "checkout", BRANCH)

    gh_bin = tmp_path / "bin"
    gh_bin.mkdir()
    gh = gh_bin / "gh"
    gh.write_text("#!/bin/sh\nexit 0\n")
    gh.chmod(0o755)
    return Fixture(remote, repo, other, tmp_path / "prauto-dir", gh_bin)


# --- ways origin and local can relate -------------------------------------------------


def _human_rebases_and_force_pushes(fx: Fixture) -> None:
    """A second clone rebases the branch onto a new base commit and force-pushes."""
    old_head = fx.local_sha
    _git(fx.other, "checkout", "dev")
    _commit(fx.other, "base2.txt", "chore: new base")
    _git(fx.other, "push", "origin", "dev")
    _git(fx.other, "checkout", BRANCH)
    _git(fx.other, "rebase", "dev")
    _git(fx.other, "push", "--force", "origin", BRANCH)
    _git(fx.repo, "fetch", "origin")
    # Realism guard: the old local head is NOT an ancestor of the rewritten origin.
    assert fx.origin_sha != old_head
    ancestor = _git(fx.repo, "merge-base", "--is-ancestor", old_head, fx.origin_sha, check=False)
    assert ancestor.returncode != 0


def _local_behind(fx: Fixture) -> None:
    _commit(fx.other, "more.txt", "feat: pushed elsewhere")
    _git(fx.other, "push", "origin", BRANCH)
    _git(fx.repo, "fetch", "origin")


def _local_ahead(fx: Fixture) -> None:
    _git(fx.repo, "checkout", BRANCH)
    _commit(fx.repo, "checkpoint.txt", "wip: unpushed checkpoint")
    _git(fx.repo, "checkout", "dev")


def _diverged(fx: Fixture) -> None:
    _local_ahead(fx)
    _human_rebases_and_force_pushes(fx)


def _local_missing(fx: Fixture) -> None:
    _git(fx.repo, "branch", "-D", BRANCH)


def _origin_missing(fx: Fixture) -> None:
    _local_ahead(fx)  # local-only work, so "kept" is distinguishable from "reset"
    _git(fx.repo, "push", "origin", "--delete", BRANCH)
    _git(fx.repo, "fetch", "--prune", "origin")


# --- shell harness ----------------------------------------------------------------------


def _script(fx: Fixture, body: str, *, with_pr: bool = False) -> str:
    libs = ["helpers.sh", "git-ops.sh"] + (["pr.sh"] if with_pr else [])
    return "\n".join(
        [
            f"REPO_DIR={shlex.quote(str(fx.repo))}",
            f"PRAUTO_DIR={shlex.quote(str(fx.prauto_dir))}",
            "PRAUTO_BRANCH_PREFIX=prauto/",
            "PRAUTO_BASE_BRANCH=dev",
            *(f"source {shlex.quote(str(PRAUTO / 'lib' / lib))}" for lib in libs),
            body,
        ]
    )


def _run(fx: Fixture, body: str, *, with_pr: bool = False) -> subprocess.CompletedProcess[str]:
    env = _env({"PATH": f"{fx.gh_bin}{os.pathsep}{os.environ['PATH']}"})
    return subprocess.run(
        ["bash", "-c", _script(fx, body, with_pr=with_pr)],
        capture_output=True,
        check=False,
        text=True,
        env=env,
    )


# (entry point shell command, worktree dir name)
ENTRY_POINTS = {
    "create_branch": (f"create_branch {ISSUE}", f"I-{ISSUE}"),
    "checkout_branch_worktree": (f"checkout_branch_worktree {BRANCH}", BRANCH.replace("/", "-")),
}
entry_points = pytest.mark.parametrize("entry", list(ENTRY_POINTS))


def _reuse(fx: Fixture, entry: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    command, dirname = ENTRY_POINTS[entry]
    return _run(fx, command), fx.prauto_dir / "worktrees" / dirname


def _worktree_head(worktree: Path) -> str:
    return _sha(worktree, "HEAD")


# --- the reconciliation rule ------------------------------------------------------------


@entry_points
def test_rebased_and_force_pushed_origin_replaces_stale_local(fx: Fixture, entry: str) -> None:
    _human_rebases_and_force_pushes(fx)
    stale_local = fx.local_sha

    result, worktree = _reuse(fx, entry)

    assert result.returncode == 0, result.stderr
    assert _worktree_head(worktree) == fx.origin_sha
    assert fx.local_sha == fx.origin_sha != stale_local
    assert f"Moved {BRANCH} from {stale_local[:12]} to origin/{BRANCH}" in result.stdout


@entry_points
def test_local_strictly_behind_origin_is_fast_forwarded(fx: Fixture, entry: str) -> None:
    _local_behind(fx)
    stale_local = fx.local_sha
    assert stale_local != fx.origin_sha

    result, worktree = _reuse(fx, entry)

    assert result.returncode == 0, result.stderr
    assert _worktree_head(worktree) == fx.origin_sha
    assert fx.local_sha == fx.origin_sha
    assert f"Moved {BRANCH} from {stale_local[:12]}" in result.stdout


@entry_points
def test_local_ahead_of_origin_is_kept_with_a_warning(fx: Fixture, entry: str) -> None:
    _local_ahead(fx)
    local_before = fx.local_sha
    assert local_before != fx.origin_sha

    result, worktree = _reuse(fx, entry)

    assert result.returncode == 0, result.stderr
    assert fx.local_sha == local_before
    assert _worktree_head(worktree) == local_before
    assert "[WARN]" in result.stdout
    assert f"unpushed commit(s) on top of origin/{BRANCH}" in result.stdout


@entry_points
def test_diverged_branch_fails_closed(fx: Fixture, entry: str) -> None:
    _diverged(fx)
    local_before = fx.local_sha
    assert local_before != fx.origin_sha

    result, worktree = _reuse(fx, entry)

    assert result.returncode != 0
    assert "[ERROR]" in result.stderr
    assert f"Cannot safely reuse {BRANCH}" in result.stderr
    assert "diverged" in result.stdout  # the reconcile warning ran, not some other failure
    assert not worktree.exists()
    assert fx.local_sha == local_before


@entry_points
def test_missing_local_branch_is_created_at_origin(fx: Fixture, entry: str) -> None:
    _local_missing(fx)
    origin = fx.origin_sha

    result, worktree = _reuse(fx, entry)

    assert result.returncode == 0, result.stderr
    assert _worktree_head(worktree) == origin
    assert fx.local_sha == origin
    assert f"Created {BRANCH} at origin/{BRANCH}" in result.stdout


@entry_points
def test_missing_origin_keeps_the_local_branch(fx: Fixture, entry: str) -> None:
    _origin_missing(fx)
    local_before = fx.local_sha
    remote_heads = _git(fx.remote, "branch", "--list", BRANCH).stdout
    assert remote_heads.strip() == ""  # backstop: origin really has no such branch

    result, worktree = _reuse(fx, entry)

    assert result.returncode == 0, result.stderr
    assert fx.local_sha == local_before
    assert _worktree_head(worktree) == local_before


@entry_points
def test_stale_prauto_worktree_is_removed_before_the_branch_is_moved(
    fx: Fixture, entry: str
) -> None:
    """Reconcile must run after the stale worktree is gone.

    spec: spec/AI_PRAUTO.md "Branch-reuse reconciliation" — the local ref moves to origin
    even when a leftover prauto worktree from an earlier wake still has the branch checked
    out, because `git branch -f` refuses a branch checked out in any worktree.
    """
    _, dirname = ENTRY_POINTS[entry]
    stale = fx.prauto_dir / "worktrees" / dirname
    _git(fx.repo, "worktree", "add", str(stale), BRANCH)
    assert _sha(stale, "HEAD") == fx.local_sha  # backstop: the branch is checked out there
    _human_rebases_and_force_pushes(fx)
    stale_local = fx.local_sha

    result, worktree = _reuse(fx, entry)

    assert result.returncode == 0, result.stderr
    assert "Removing stale worktree" in result.stdout
    assert worktree == stale
    assert _worktree_head(worktree) == fx.origin_sha
    assert fx.local_sha == fx.origin_sha != stale_local


@entry_points
def test_branch_checked_out_in_another_worktree_fails_closed(
    fx: Fixture, entry: str, tmp_path: Path
) -> None:
    """spec: spec/AI_PRAUTO.md "Branch-reuse reconciliation" — fails closed when the move
    is refused because the branch is still checked out in another worktree."""
    elsewhere = tmp_path / "elsewhere"
    _git(fx.repo, "worktree", "add", str(elsewhere), BRANCH)
    _human_rebases_and_force_pushes(fx)
    local_before = fx.local_sha
    assert local_before != fx.origin_sha

    result, worktree = _reuse(fx, entry)

    assert result.returncode != 0
    assert "[ERROR]" in result.stderr
    assert f"Cannot safely reuse {BRANCH}" in result.stderr
    assert "checked out in another worktree" in result.stdout  # the refused move, not a diverge
    assert fx.local_sha == local_before
    assert not worktree.exists()
    assert _worktree_head(elsewhere) == local_before


@entry_points
def test_failed_fetch_warns_and_uses_the_last_known_origin_ref(fx: Fixture, entry: str) -> None:
    """spec: spec/AI_PRAUTO.md "Branch-reuse reconciliation" — a failed fetch only warns and
    the rule runs against the last-known origin ref."""
    _local_behind(fx)  # successful fetch: tracking ref is ahead of the stale local ref
    tracking = _sha(fx.repo, f"refs/remotes/origin/{BRANCH}")
    assert tracking != fx.local_sha
    # The remote advances further, unseen, and every later fetch fails.
    _commit(fx.other, "unseen.txt", "feat: pushed after the last fetch")
    _git(fx.other, "push", "origin", BRANCH)
    assert fx.origin_sha != tracking
    _git(fx.repo, "remote", "set-url", "origin", "/nonexistent/remote.git")

    result, worktree = _reuse(fx, entry)

    assert result.returncode == 0, result.stderr
    assert "[WARN]" in result.stdout
    assert "fetch failed" in result.stdout
    assert _worktree_head(worktree) == tracking
    assert fx.local_sha == tracking


# --- squash retry -----------------------------------------------------------------------


def test_failed_squash_restores_head_so_the_next_wake_reuses_the_branch(fx: Fixture) -> None:
    """A failed force-push must not leave a rewritten local ref behind.

    spec: spec/AI_PRAUTO.md "Branch-reuse reconciliation" — a rewritten local head with
    never-pushed commits reads as diverged and fails closed. The squash therefore restores
    the pre-squash head on failure, and the next wake checks the branch out and retries.
    """
    # More than one commit on the branch, and a base that moved, so the squash path
    # really rebases and squashes before the push fails.
    _git(fx.repo, "checkout", BRANCH)
    _commit(fx.repo, "more-work.txt", "feat: more pushed work")
    _git(fx.repo, "push", "origin", BRANCH)
    _git(fx.repo, "checkout", "dev")
    _git(fx.other, "checkout", "dev")
    _commit(fx.other, "base2.txt", "chore: base moved")
    _git(fx.other, "push", "origin", "dev")
    # The push fails; fetches (which use the fetch URL) still work.
    _git(fx.repo, "remote", "set-url", "--push", "origin", "/nonexistent/remote.git")
    pre_squash = fx.local_sha

    body = f"""
generate_squash_commit_message() {{ SQUASH_COMMIT_MESSAGE=squashed; }}
PRAUTO_GITHUB_REPO=owner/repo
PRAUTO_GIT_AUTHOR_NAME=prauto
PRAUTO_GIT_AUTHOR_EMAIL=prauto@example.invalid
wake() {{
  checkout_branch_worktree {BRANCH}
  echo "PRE_$1=$(git -C "$REPO_DIR" rev-parse refs/heads/{BRANCH})"
  ( cd "$WORKTREE_DIR" && squash_and_finalize_pr 7 {BRANCH} title body {ISSUE} )
  echo "SQUASH_RC_$1=$?"
  echo "POST_$1=$(git -C "$REPO_DIR" rev-parse refs/heads/{BRANCH})"
}}
wake 1
wake 2
"""
    result = _run(fx, body, with_pr=True)

    assert result.returncode == 0, result.stderr
    assert "Cannot safely reuse" not in result.stderr
    # Backstop: both wakes reached the push (after rebase + squash) and it failed there.
    assert result.stdout.count("force-push failed") == 2
    assert "skipping rebase" not in result.stdout
    markers = ("PRE_", "POST_", "SQUASH_RC_")
    values = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if line.startswith(markers)
    )
    for wake in ("1", "2"):
        assert values[f"SQUASH_RC_{wake}"] != "0"
        assert values[f"PRE_{wake}"] == pre_squash
        assert values[f"POST_{wake}"] == pre_squash
    assert fx.local_sha == pre_squash
    assert fx.origin_sha == pre_squash
