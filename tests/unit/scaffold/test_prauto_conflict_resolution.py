"""Hermetic tests for prauto's base-branch conflict resolution and head-bound squash.

spec: spec/AI_PRAUTO.md §Base-branch conflicts and §Squash-finalize. The invariants asserted:

* The executor merges ``origin/<base>`` (a merge, never a rebase) and starts only from
  origin's head: a local branch carrying commits origin does not have is refused.
* When git cannot merge on its own a worker resolves the conflicted paths. The executor accepts
  only a result in which the merge is still in progress on an unmoved ``HEAD``, no path is
  unmerged, every non-conflicted index entry is exactly git's own merge result, no resolved blob
  carries a conflict marker absent from both parents, and git's control surface (effective
  configuration with every included file, attributes, hooks) did not change.
* On acceptance it commits the merge, pushes it, and returns the PR to ``prauto:wip`` for the
  same post-PR readiness gate as a feedback pass.
* A rejected result restores the branch, records the ``{PR head, base head}`` pair (origin's head)
  in local state, and asks a human on the PR. Quota ends only restore; a session error is
  retried once for the same pair and then recorded.
* A failed push after the merge commit restores the branch and the prior labels (label-only).
* Squash-finalize is bound to the approved head: a moved branch is skipped, the force-push lease
  is taken on the approved head, and only reviewers who approved that commit are credited.

Each scenario drives the REAL ``.prauto/lib`` shell functions against a bare ``remote.git``,
clones, and a LINKED worktree in ``tmp_path`` (the executor's shape). The worker session, gh and
the regression/readiness functions are stubbed shell functions; nothing reaches claude, GitHub
or the network, and nothing touches the real ``.prauto/state``.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.unit.scaffold.prauto_isolation import ISOLATE_REAL_STATE_SHELL

ROOT = Path(__file__).parents[3]
PRAUTO = ROOT / ".prauto"
BRANCH = "prauto/I-1"
ISSUE = "1"
PR = "7"
NAMES = ("shared1.txt", "shared2.txt", "shared3.txt")

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


def _commit_files(repo: Path, files: dict[str, str], message: str) -> None:
    for name, content in files.items():
        (repo / name).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


@dataclass
class Scenario:
    """origin/dev moved on after the PR branch forked; the PR branch is in a linked worktree."""

    tmp: Path
    remote: Path
    repo: Path
    other: Path
    worktree: Path
    prauto_dir: Path
    state_dir: Path
    conflicted: tuple[str, ...]
    orig_head: str  # the PR branch head on origin (== local head at the start)
    base_sha: str  # origin/dev head

    @property
    def remote_branch_sha(self) -> str:
        return _git(self.remote, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip()

    @property
    def remote_base_sha(self) -> str:
        return _git(self.remote, "rev-parse", "refs/heads/dev").stdout.strip()

    @property
    def guard_file(self) -> Path:
        return self.state_dir / f"conflict-attempt-{ISSUE}.json"

    @property
    def events_file(self) -> Path:
        return self.tmp / "events.log"

    @property
    def gh_log(self) -> Path:
        return self.tmp / "gh.log"

    @property
    def comments_file(self) -> Path:
        return self.tmp / "comments.log"

    @property
    def agent_args(self) -> Path:
        return self.tmp / "agent.args"


def build(
    tmp_path: Path,
    *,
    conflicts: tuple[str, ...] = NAMES[:2],
    branch_extra: str = "",
    base_extra: str = "",
) -> Scenario:
    """Fork the PR branch from dev, then move dev on from a second clone.

    Each name in ``conflicts`` is edited on the same line on both sides (a textual conflict).
    The branch also adds ``work.txt``; dev also adds ``dev_only.txt`` (both merge cleanly), and
    ``untouched.txt`` is identical on every side. ``branch_extra`` / ``base_extra`` are appended
    to the branch's / dev's version of the first conflicted file.
    """
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    worktree = tmp_path / "wt"
    _git(tmp_path, "init", "--bare", "-b", "dev", str(remote))
    _git(tmp_path, "init", "-b", "dev", str(repo))
    # Absolute URL: a relative one breaks fetches run from inside a worktree.
    _git(repo, "remote", "add", "origin", str(remote))
    _commit_files(
        repo,
        {"README.md": "readme\n", "untouched.txt": "untouched\n"}
        | {name: "base\n" for name in conflicts},
        "chore: base",
    )
    _git(repo, "push", "origin", "dev")
    _git(repo, "checkout", "-b", BRANCH)
    branch_files = {"work.txt": "branch work\n"}
    for i, name in enumerate(conflicts):
        branch_files[name] = f"branch {name}\n" + (branch_extra if i == 0 else "")
    _commit_files(repo, branch_files, "feat: branch work")
    _git(repo, "push", "-u", "origin", BRANCH)
    # The branch must not be checked out in the main clone, as in the executor.
    _git(repo, "checkout", "dev")
    _git(tmp_path, "clone", str(remote), str(other))
    dev_files = {"dev_only.txt": "dev work\n"} | {
        name: f"dev {name}\n" + (base_extra if i == 0 else "") for i, name in enumerate(conflicts)
    }
    _commit_files(other, dev_files, "chore: dev moved")
    _git(other, "push", "origin", "dev")

    _git(repo, "worktree", "add", str(worktree), BRANCH)
    sc = Scenario(
        tmp=tmp_path,
        remote=remote,
        repo=repo,
        other=other,
        worktree=worktree,
        prauto_dir=tmp_path / "prauto-dir",
        state_dir=tmp_path / "state",
        conflicted=conflicts,
        orig_head=_sha(worktree, "HEAD"),
        base_sha=_git(remote, "rev-parse", "refs/heads/dev").stdout.strip(),
    )
    # Backstops: the scenario really is a linked worktree on origin's head, and the base
    # really is ahead of the branch (otherwise resolve_pr_conflicts would have nothing to do).
    assert (worktree / ".git").is_file()
    assert sc.orig_head == sc.remote_branch_sha
    behind = _git(remote, "merge-base", "--is-ancestor", sc.base_sha, sc.orig_head, check=False)
    assert behind.returncode == 1
    return sc


# --- shell harness ------------------------------------------------------------------------


def _prelude(sc: Scenario) -> str:
    libs = (
        "helpers.sh",
        "state.sh",
        "quota.sh",
        "agent.sh",
        "git-ops.sh",
        "pr.sh",
        "issues.sh",
        "phases.sh",
    )
    return "\n".join(
        [
            "set -euo pipefail",
            f"PRAUTO_DIR={shlex.quote(str(sc.prauto_dir))}",
            f"REPO_DIR={shlex.quote(str(sc.repo))}",
            "PRAUTO_GITHUB_REPO=owner/repo",
            "PRAUTO_WORKER_ID=test",
            "PRAUTO_GITHUB_ACTOR=bot",
            "PRAUTO_BRANCH_PREFIX=prauto/",
            "PRAUTO_BASE_BRANCH=dev",
            "PRAUTO_GITHUB_LABEL_WIP=prauto:wip",
            "PRAUTO_GITHUB_LABEL_REVIEW=prauto:review",
            "PRAUTO_GITHUB_LABEL_PLAN_REVIEW=prauto:plan-review",
            "PRAUTO_GITHUB_LABEL_FAILED=prauto:failed",
            "PRAUTO_GITHUB_LABEL_DONE=prauto:done",
            "PRAUTO_GIT_AUTHOR_NAME=prauto",
            "PRAUTO_GIT_AUTHOR_EMAIL=prauto@example.invalid",
            *(f"source {shlex.quote(str(PRAUTO / 'lib' / lib))}" for lib in libs),
            ISOLATE_REAL_STATE_SHELL,
            # After sourcing state.sh: the guard file must resolve under tmp_path.
            f"STATE_DIR={shlex.quote(str(sc.state_dir))}",
            'mkdir -p "$STATE_DIR"',
        ]
    )


_STUBS = r"""
EVENTS="$TMP_RUN/events.log"; GH_LOG="$TMP_RUN/gh.log"; COMMENTS="$TMP_RUN/comments.log"
AGENT_ARGS="$TMP_RUN/agent.args"
ev() {
  local sha
  sha=$(git --git-dir="$REMOTE" rev-parse refs/heads/prauto/I-1)
  printf '%s remote=%s\n' "$1" "$sha" >> "$EVENTS"
}
gh() {
  printf '%s\n' "$*" >> "$GH_LOG"
  if [[ "$1" == api && "$*" == *"pulls?head="* ]]; then
    printf '[{"number": 7, "head": {"repo": {"full_name": "owner/repo"}}}]'
    return 0
  fi
  if [[ "$1" == pr && "$2" == comment ]]; then
    local prev="" a
    for a in "$@"; do
      [[ "$prev" == --body ]] && printf '%s\n=====END=====\n' "$a" >> "$COMMENTS"
      prev="$a"
    done
    return 0
  fi
  return 0
}
regression_set_wip() { ev regression_set_wip; return "${SET_WIP_RC:-0}"; }
link_branch_to_issue() { ev link_branch_to_issue; }
publish_commit_checkpoints() { ev publish_commit_checkpoints; }
create_or_update_pr() { ev create_or_update_pr; }
run_post_pr_regression() {
  ev run_post_pr_regression
  printf '#porcelain:%s\n' "$(git status --porcelain | tr '\n' '|')" >> "$EVENTS"
}
regression_ready() { ev regression_ready; }
complete_job() { ev complete_job; }
run_conflict_resolution() {
  printf '%s' "$3" > "$AGENT_ARGS"
  AGENT_STATUS=ok
  CONFLICT_RESOLUTION_SUMMARY="resolution summary from the worker"
  agent_body
}
"""


def _run_script(
    sc: Scenario, script: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        check=False,
        text=True,
        env=_env({"TMP_RUN": str(sc.tmp), "REMOTE": str(sc.remote), **(extra_env or {})}),
    )


@dataclass
class Outcome:
    result: subprocess.CompletedProcess[str]
    sc: Scenario

    @property
    def ok(self) -> bool:
        # heartbeat.sh calls the function inside an `if`; exactly one verdict line must print,
        # or the function ended the shell instead of returning.
        verdicts = [ln for ln in self.result.stdout.splitlines() if ln.startswith("RESOLVE_")]
        assert verdicts in (["RESOLVE_OK"], ["RESOLVE_FAIL"]), (
            self.result.stdout + self.result.stderr
        )
        return verdicts == ["RESOLVE_OK"]

    @property
    def _log_lines(self) -> list[str]:
        f = self.sc.events_file
        return f.read_text().splitlines() if f.exists() else []

    @property
    def events(self) -> list[str]:
        return [ln for ln in self._log_lines if not ln.startswith("#porcelain:")]

    @property
    def post_regression_status(self) -> str | None:
        """`git status --porcelain` (newlines as `|`) as seen by run_post_pr_regression."""
        seen = [ln for ln in self._log_lines if ln.startswith("#porcelain:")]
        assert len(seen) <= 1
        return seen[0].removeprefix("#porcelain:") if seen else None

    @property
    def event_names(self) -> list[str]:
        return [e.split()[0] for e in self.events]

    @property
    def comments(self) -> str:
        f = self.sc.comments_file
        return f.read_text() if f.exists() else ""

    @property
    def gh_calls(self) -> str:
        f = self.sc.gh_log
        return f.read_text() if f.exists() else ""

    @property
    def agent_called(self) -> bool:
        return self.sc.agent_args.exists()


def resolve(sc: Scenario, agent_body: str = ":", env: dict[str, str] | None = None) -> Outcome:
    """Run resolve_pr_conflicts in the linked worktree under `set -euo pipefail`, called inside
    an `if` as heartbeat.sh does."""
    for f in (sc.events_file, sc.gh_log, sc.comments_file, sc.agent_args):
        f.unlink(missing_ok=True)
    script = "\n".join(
        [
            _prelude(sc),
            _STUBS,
            f"agent_body() {{\n{agent_body}\n}}",
            f"cd {shlex.quote(str(sc.worktree))}",
            f"if resolve_pr_conflicts {PR} {BRANCH} {ISSUE} 'PR title' 'approved plan'; "
            "then echo RESOLVE_OK; else echo RESOLVE_FAIL; fi",
        ]
    )
    return Outcome(_run_script(sc, script, env), sc)


def resolve_all(sc: Scenario, names: tuple[str, ...] | None = None) -> str:
    """Agent body: write a resolution for each conflicted file and stage it."""
    files = " ".join(names if names is not None else sc.conflicted)
    return f'for f in {files}; do printf "resolved %s\\n" "$f" > "$f"; git add -- "$f"; done'


# --- shared assertions ----------------------------------------------------------------------


def _merge_in_progress(sc: Scenario) -> bool:
    return (
        _git(sc.worktree, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0
    )


def _assert_restored(sc: Scenario, *, local_head: str | None = None) -> None:
    """Remote branch untouched; local HEAD back where it was, clean tree, no merge pending."""
    assert sc.remote_branch_sha == sc.orig_head
    assert _sha(sc.worktree, "HEAD") == (local_head or sc.orig_head)
    assert _git(sc.worktree, "status", "--porcelain").stdout == ""
    assert not _merge_in_progress(sc)


def _assert_rejected(
    sc: Scenario, out: Outcome, reason: str, *, local_head: str | None = None
) -> None:
    """Every rejection: restore, record origin's {head, base} guard, ask a human, push nothing."""
    assert out.ok is False, out.result.stdout + out.result.stderr
    _assert_restored(sc, local_head=local_head)
    assert sc.guard_file.exists()
    guard = json.loads(sc.guard_file.read_text())
    assert guard["head_sha"] == sc.orig_head  # origin's head, not a local-only commit
    assert guard["base_sha"] == sc.base_sha
    assert isinstance(guard["failed_at"], str) and guard["failed_at"]
    assert "needs a human" in out.comments
    assert reason in out.comments, out.comments
    assert out.comments.count("=====END=====") == 1  # one failure comment, no resolution comment
    assert out.event_names == []  # no WIP flip, no regression, nothing declared ready
    assert sc.remote_base_sha == sc.base_sha


def _tracked_files(sc: Scenario, ref: str) -> list[str]:
    return _git(sc.worktree, "ls-tree", "-r", "--name-only", ref).stdout.split()


# --- the good path --------------------------------------------------------------------------


def test_good_resolution_commits_a_merge_and_pushes_it_through_the_readiness_gate(
    tmp_path: Path,
) -> None:
    sc = build(tmp_path)
    # A stale guard from an earlier failed attempt on a different pair must be cleared.
    sc.state_dir.mkdir()
    sc.guard_file.write_text(
        json.dumps(
            {"issue_number": 1, "head_sha": "0" * 40, "base_sha": "1" * 40, "failed_at": "t"}
        )
    )

    out = resolve(sc, resolve_all(sc))

    assert out.ok is True, out.result.stdout + out.result.stderr
    assert sc.agent_args.read_text() == "\n".join(sc.conflicted)  # the worker got exactly the set
    head = _sha(sc.worktree, "HEAD")
    assert head != sc.orig_head
    # A merge commit: old head first, base second (history kept, never rebased).
    parents = _git(sc.worktree, "rev-list", "--parents", "-n1", "HEAD").stdout.split()[1:]
    assert parents == [sc.orig_head, sc.base_sha]
    assert sc.remote_branch_sha == head  # pushed
    assert (
        _git(
            sc.worktree, "grep", "-nE", "^(<<<<<<<|=======|>>>>>>>)( |$)", "HEAD", check=False
        ).returncode
        == 1
    )
    for name in sc.conflicted:
        assert _git(sc.worktree, "show", f"HEAD:{name}").stdout == f"resolved {name}\n"
    tree = _tracked_files(sc, "HEAD")
    assert "dev_only.txt" in tree and "work.txt" in tree  # both sides' unrelated work survives
    assert _git(sc.worktree, "status", "--porcelain").stdout == ""
    assert not sc.guard_file.exists()  # guard cleared on success

    # Ordering: WIP flip -> push -> regression -> ready -> complete.
    names = out.event_names
    required = ["regression_set_wip", "run_post_pr_regression", "regression_ready", "complete_job"]
    positions = [names.index(n) for n in required]
    assert positions == sorted(positions), names
    by_name = dict(e.split(" ", 1) for e in out.events)
    assert by_name["regression_set_wip"] == f"remote={sc.orig_head}"  # not pushed yet
    assert by_name["run_post_pr_regression"] == f"remote={head}"  # already pushed
    assert by_name["complete_job"] == f"remote={head}"
    assert out.post_regression_status == ""  # the regression saw exactly the pushed head

    assert "Base branch merged" in out.comments
    for name in sc.conflicted:
        assert name in out.comments
    assert "fresh approval" in out.comments  # approval is bound to the head commit
    assert "needs a human" not in out.comments


def test_clean_merge_needs_no_agent_and_is_pushed(tmp_path: Path) -> None:
    sc = build(tmp_path, conflicts=())

    out = resolve(sc, 'touch "$TMP_RUN/agent-was-called"')

    assert out.ok is True, out.result.stdout + out.result.stderr
    assert not out.agent_called
    assert not (tmp_path / "agent-was-called").exists()
    head = _sha(sc.worktree, "HEAD")
    parents = _git(sc.worktree, "rev-list", "--parents", "-n1", "HEAD").stdout.split()[1:]
    assert parents == [sc.orig_head, sc.base_sha]
    assert sc.remote_branch_sha == head
    names = out.event_names
    assert names.index("regression_set_wip") < names.index("run_post_pr_regression")
    assert names[-2:] == ["regression_ready", "complete_job"]
    assert "without conflicts" in out.comments
    assert not sc.guard_file.exists()


@pytest.mark.parametrize("side", ["branch", "base"])
def test_marker_like_line_already_on_one_side_is_not_rejected(tmp_path: Path, side: str) -> None:
    # A line like `>>>>>>> example` already in either parent's blob (a git tutorial, a fixture)
    # is not something the resolution introduced: spec "no resolved blob carries a conflict
    # marker absent from both parents".
    extra = (
        {"branch_extra": ">>>>>>> example\n"}
        if side == "branch"
        else {"base_extra": ">>>>>>> example\n"}
    )
    sc = build(tmp_path, **extra)
    body = (
        'printf "resolved shared1.txt\\n>>>>>>> example\\n" > shared1.txt\n'
        "git add -- shared1.txt\n"
        'printf "resolved shared2.txt\\n" > shared2.txt\n'
        "git add -- shared2.txt"
    )

    out = resolve(sc, body)

    assert out.ok is True, out.result.stdout + out.result.stderr
    assert sc.remote_branch_sha == _sha(sc.worktree, "HEAD") != sc.orig_head
    assert ">>>>>>> example" in _git(sc.worktree, "show", "HEAD:shared1.txt").stdout
    assert out.event_names[-1] == "complete_job"


def test_new_marker_in_one_of_two_files_is_rejected_even_if_the_other_is_clean(
    tmp_path: Path,
) -> None:
    sc = build(tmp_path)
    body = (
        'printf "resolved shared1.txt\\n" > shared1.txt\n'
        "git add -- shared1.txt\n"
        'printf "resolved shared2.txt\\n<<<<<<< x\\n" > shared2.txt\n'
        "git add -- shared2.txt"
    )

    out = resolve(sc, body)

    assert out.agent_called
    _assert_rejected(sc, out, "conflict markers remain")
    assert "shared2.txt: <<<<<<< x" in out.comments  # names the offending file and line
    assert "shared1.txt: <<<<<<<" not in out.comments


def test_worker_leftovers_are_dropped_before_the_regression_sees_the_head(
    tmp_path: Path,
) -> None:
    sc = build(tmp_path)
    body = (
        resolve_all(sc)
        + '\necho "unstaged scribble" >> shared1.txt'  # unstaged edit to a staged tracked file
        + '\necho "unstaged scribble" >> README.md'  # unstaged edit to another tracked file
        + '\necho "stray" > scratch.tmp'  # untracked file
    )

    out = resolve(sc, body)

    assert out.ok is True, out.result.stdout + out.result.stderr
    head = _sha(sc.worktree, "HEAD")
    assert sc.remote_branch_sha == head != sc.orig_head
    # The pushed blob is the staged resolution, without the unstaged scribble.
    assert _git(sc.worktree, "show", "HEAD:shared1.txt").stdout == "resolved shared1.txt\n"
    assert "scratch.tmp" not in _tracked_files(sc, "HEAD")
    # Proof that the post-commit reset/clean ran: the regression stub saw a pristine worktree.
    assert out.post_regression_status == ""
    assert not (sc.worktree / "scratch.tmp").exists()
    assert (sc.worktree / "README.md").read_text() == "readme\n"


def test_regression_set_wip_failure_restores_the_branch_and_stops_before_any_push(
    tmp_path: Path,
) -> None:
    sc = build(tmp_path)

    out = resolve(sc, resolve_all(sc), env={"SET_WIP_RC": "1"})

    assert out.agent_called
    assert out.ok is False
    _assert_restored(sc)  # remote branch untouched, HEAD back, clean tree
    assert out.event_names == ["regression_set_wip"]  # backstop: reached, and nothing after it
    for later in ("run_post_pr_regression", "regression_ready", "complete_job"):
        assert later not in out.event_names
    assert not sc.guard_file.exists()  # a label failure is not a verdict on the resolution


def _arm_hooks_and_signing(sc: Scenario) -> list[Path]:
    """Baseline (set before the attempt): hooks that leave sentinels and mandatory signing
    with a signing program that always fails. Returns the sentinel paths."""
    common = Path(
        _git(sc.worktree, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    )
    hooks = common / "hooks"
    hooks.mkdir(exist_ok=True)
    sentinels = [sc.tmp / "sentinel-post-commit", sc.tmp / "sentinel-post-merge"]
    for hook, sentinel in zip(("post-commit", "post-merge"), sentinels, strict=True):
        script = hooks / hook
        script.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(sentinel))}\n")
        script.chmod(0o755)
    _git(sc.repo, "config", "--local", "commit.gpgSign", "true")
    _git(sc.repo, "config", "--local", "gpg.program", "false")

    # Sanity: plain git on a copy of this very baseline either fails on signing or runs the
    # hooks, so a pass below is due to the executor's neutralisation and not an inert setup.
    probe = sc.tmp / "probe"
    shutil.copytree(sc.repo, probe, symlinks=True)
    signed = _git(probe, "commit", "--allow-empty", "-m", "probe", check=False)
    assert signed.returncode != 0, "plain git must fail to sign"
    _git(probe, "fetch", "origin", "dev")
    _git(probe, "-c", "commit.gpgSign=false", "merge", "origin/dev")
    _git(probe, "-c", "commit.gpgSign=false", "commit", "--allow-empty", "-m", "probe")
    assert all(s.exists() for s in sentinels), "plain git must run both hooks"
    for sentinel in sentinels:
        sentinel.unlink()
    return sentinels


@pytest.mark.parametrize("conflicts", [NAMES[:2], ()], ids=["agent-resolved", "clean-merge"])
def test_commit_runs_without_hooks_or_signing(tmp_path: Path, conflicts: tuple[str, ...]) -> None:
    # spec §Base-branch conflicts: the executor "commits the index with hooks, fsmonitor,
    # signing, credential helpers and the ext:: transport disabled". The baseline demands
    # signing (with a program that always fails) and has post-commit/post-merge hooks.
    sc = build(tmp_path, conflicts=conflicts)
    sentinels = _arm_hooks_and_signing(sc)

    out = resolve(sc, resolve_all(sc))

    assert out.ok is True, out.result.stdout + out.result.stderr
    assert out.agent_called == bool(conflicts)  # backstop: both paths really ran
    head = _sha(sc.worktree, "HEAD")
    parents = _git(sc.worktree, "rev-list", "--parents", "-n1", "HEAD").stdout.split()[1:]
    assert parents == [sc.orig_head, sc.base_sha]
    assert sc.remote_branch_sha == head
    assert [s.name for s in sentinels if s.exists()] == []
    assert out.event_names[-1] == "complete_job"


# --- the executor rejects what it cannot verify --------------------------------------------


def test_file_left_unmerged_is_rejected(tmp_path: Path) -> None:
    sc = build(tmp_path)
    # One file resolved and staged, the other left conflicted, plus an untracked stray file.
    body = resolve_all(sc, sc.conflicted[:1]) + '\necho stray > "$PWD/stray.tmp"'

    out = resolve(sc, body)

    assert out.agent_called
    _assert_rejected(sc, out, "still unmerged")
    assert sc.conflicted[1] in out.comments
    assert not (sc.worktree / "stray.tmp").exists()


def test_staged_conflict_marker_is_rejected(tmp_path: Path) -> None:
    sc = build(tmp_path)
    # `git add` of the conflicted files as they are: nothing is unmerged, markers are staged.
    body = "git add -- " + " ".join(sc.conflicted)

    out = resolve(sc, body)

    assert out.agent_called
    _assert_rejected(sc, out, "conflict markers remain")
    assert "<<<<<<<" in out.comments


@pytest.mark.parametrize("n_conflicts", [1, 2, 3])
@pytest.mark.parametrize("edit", ["modify", "add"])
def test_edit_outside_the_conflicted_set_is_rejected(
    tmp_path: Path, n_conflicts: int, edit: str
) -> None:
    # Regression test for the awk newline bug: with two or more conflicted paths the
    # excluded-path list contains a newline, which BSD awk rejects in `-v`; both snapshots then
    # came out empty and an out-of-set edit was accepted.
    sc = build(tmp_path, conflicts=NAMES[:n_conflicts])
    extra = (
        'printf "tampered\\n" > work.txt\ngit add -- work.txt'
        if edit == "modify"
        else 'printf "sneaky\\n" > brand_new.txt\ngit add -- brand_new.txt'
    )

    out = resolve(sc, resolve_all(sc) + "\n" + extra)

    assert out.agent_called
    _assert_rejected(sc, out, "outside the conflicted set")


def test_agent_that_commits_on_its_own_is_rejected(tmp_path: Path) -> None:
    sc = build(tmp_path)

    out = resolve(sc, resolve_all(sc) + "\ngit commit --no-edit -m 'agent merge'")

    assert out.agent_called
    _assert_rejected(sc, out, "committed, aborted, or redirected the merge")


def test_agent_that_aborts_the_merge_is_rejected(tmp_path: Path) -> None:
    sc = build(tmp_path)

    out = resolve(sc, "git merge --abort")

    assert out.agent_called
    _assert_rejected(sc, out, "committed, aborted, or redirected the merge")


_BOT_INCLUDE = "bot.gitconfig"


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param("git config --local alias.zz status", id="git-config-local"),
        pytest.param(
            f'printf "[core]\\n\\tsshCommand = evil\\n" >> "$TMP_RUN/{_BOT_INCLUDE}"',
            id="include-path-target",
        ),
        pytest.param(
            'c=$(git rev-parse --path-format=absolute --git-common-dir); mkdir -p "$c/hooks"; '
            'printf "#!/bin/sh\\nexit 0\\n" > "$c/hooks/post-commit"',
            id="hooks-directory",
        ),
        pytest.param(
            'c=$(git rev-parse --path-format=absolute --git-common-dir); mkdir -p "$c/info"; '
            'printf "*.nothing -diff\\n" > "$c/info/attributes"',
            id="info-attributes",
        ),
    ],
)
def test_git_control_surface_tampering_is_rejected(tmp_path: Path, tamper: str) -> None:
    sc = build(tmp_path)
    # Baseline: an include file (as the bot's core.sshCommand lives in one) already configured.
    include = tmp_path / _BOT_INCLUDE
    include.write_text("[core]\n\tabbrev = 7\n")
    _git(sc.repo, "config", "--local", "include.path", str(include))

    out = resolve(sc, resolve_all(sc) + "\n" + tamper)

    assert out.agent_called
    _assert_rejected(sc, out, "git configuration, attributes, or hooks changed")


def test_local_branch_ahead_of_origin_is_refused_before_any_agent_call(tmp_path: Path) -> None:
    sc = build(tmp_path)
    _commit_files(sc.worktree, {"local_only.txt": "unpushed checkpoint\n"}, "wip: local checkpoint")
    local_head = _sha(sc.worktree, "HEAD")
    assert local_head != sc.orig_head  # backstop: the local branch really is ahead

    out = resolve(sc, resolve_all(sc))

    assert not out.agent_called
    _assert_rejected(sc, out, f"commits origin/{BRANCH} does not", local_head=local_head)


# --- agent quota and error ends ------------------------------------------------------------


def test_quota_end_restores_without_a_guard_or_a_comment(tmp_path: Path) -> None:
    sc = build(tmp_path)

    out = resolve(sc, resolve_all(sc) + "\nAGENT_STATUS=quota")

    assert out.agent_called
    assert out.ok is False
    _assert_restored(sc)
    assert not sc.guard_file.exists()
    assert out.comments == ""
    assert out.event_names == []


def test_first_session_error_retries_and_a_second_on_the_same_pair_is_recorded(
    tmp_path: Path,
) -> None:
    sc = build(tmp_path)

    first = resolve(sc, resolve_all(sc) + "\nAGENT_STATUS=error")

    assert first.agent_called
    assert first.ok is False
    _assert_restored(sc)
    assert first.comments == ""
    assert first.event_names == []
    counter = json.loads(sc.guard_file.read_text())  # backstop: the error was counted
    assert counter["session_errors"] == 1 and "failed_at" not in counter

    second = resolve(sc, resolve_all(sc) + "\nAGENT_STATUS=error")

    assert second.agent_called
    _assert_rejected(sc, second, "status error on 2 attempts")


def test_session_error_count_restarts_when_the_base_moves(tmp_path: Path) -> None:
    sc = build(tmp_path)
    first = resolve(sc, "AGENT_STATUS=error")
    assert first.ok is False
    # The base branch moves; the second error is the first for the new pair.
    _commit_files(sc.other, {"later.txt": "later\n"}, "chore: base moved again")
    _git(sc.other, "push", "origin", "dev")

    second = resolve(sc, "AGENT_STATUS=error")

    assert second.agent_called
    assert second.ok is False
    _assert_restored(sc)
    assert second.comments == ""
    record = json.loads(sc.guard_file.read_text())
    assert record["session_errors"] == 1 and "failed_at" not in record
    assert record["base_sha"] == sc.remote_base_sha != sc.base_sha


# --- push failure ----------------------------------------------------------------------------


def test_push_failure_restores_the_branch_and_labels_without_a_reviewer_request(
    tmp_path: Path,
) -> None:
    sc = build(tmp_path)
    # Fetches (which use the fetch URL) still work; the push cannot.
    _git(sc.repo, "remote", "set-url", "--push", "origin", "/nonexistent/remote.git")

    out = resolve(sc, resolve_all(sc))

    assert out.agent_called
    assert out.ok is False
    _assert_restored(sc)
    # Backstop: the merge was verified and committed, and the run got as far as the push.
    assert out.event_names == ["regression_set_wip"]
    # Label-only restore of the review state the PR had; no reviewer request, no readiness gate.
    assert (
        "pr edit 7 -R owner/repo --remove-label prauto:wip --add-label prauto:review"
        in out.gh_calls
    )
    assert (
        f"issue edit {ISSUE} -R owner/repo --remove-label prauto:wip --add-label prauto:review"
        in out.gh_calls
    )
    assert "--add-reviewer" not in out.gh_calls
    assert "regression_ready" not in out.event_names
    # A transient push failure is not a verdict on the resolution: no guard, no human ping.
    assert not sc.guard_file.exists()
    assert out.comments == ""


# --- regression: linked worktree under errexit/pipefail ------------------------------------


def _fingerprint_script(sc: Scenario, cwd: Path) -> str:
    return "\n".join(
        [
            _prelude(sc),
            f"cd {shlex.quote(str(cwd))}",
            'fp1=$(git_control_fingerprint); echo "FP1=$fp1"',
            'fp2=$(git_control_fingerprint); echo "FP2=$fp2"',
        ]
    )


def test_control_fingerprint_succeeds_in_a_linked_worktree_under_errexit_and_pipefail(
    tmp_path: Path,
) -> None:
    # A reviewer found that a linked worktree has no per-worktree hooks directory, which made
    # `find` fail under pipefail and the fingerprint return 1: the smoke tests in a main
    # checkout never saw it, and every real wake would then refuse to resolve anything.
    sc = build(tmp_path)
    git_dir = _git(sc.worktree, "rev-parse", "--path-format=absolute", "--git-dir").stdout.strip()
    common = _git(
        sc.worktree, "rev-parse", "--path-format=absolute", "--git-common-dir"
    ).stdout.strip()
    assert git_dir != common and "/worktrees/" in git_dir  # backstop: truly a linked worktree
    assert not (Path(git_dir) / "hooks").exists()  # the shape that broke the old code

    result = _run_script(sc, _fingerprint_script(sc, sc.worktree))

    assert result.returncode == 0, result.stdout + result.stderr
    values = dict(ln.split("=", 1) for ln in result.stdout.splitlines() if ln.startswith("FP"))
    assert re.fullmatch(r"[0-9a-f]{40,64}", values["FP1"])
    assert values["FP1"] == values["FP2"]  # deterministic: no spurious rejection


def test_control_fingerprint_changes_with_each_part_of_the_control_surface(
    tmp_path: Path,
) -> None:
    sc = build(tmp_path)
    include = tmp_path / "bot.gitconfig"
    include.write_text("[core]\n\tabbrev = 7\n")
    _git(sc.repo, "config", "--local", "include.path", str(include))

    def fingerprint() -> str:
        out = _run_script(sc, _fingerprint_script(sc, sc.worktree))
        assert out.returncode == 0, out.stdout + out.stderr
        return next(ln for ln in out.stdout.splitlines() if ln.startswith("FP1=")).split("=", 1)[1]

    seen = [fingerprint()]
    _git(sc.worktree, "config", "--local", "alias.zz", "status")
    seen.append(fingerprint())
    include.write_text("[core]\n\tabbrev = 7\n\tsshCommand = evil\n")
    seen.append(fingerprint())
    common = Path(
        _git(sc.worktree, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    )
    (common / "hooks").mkdir(exist_ok=True)
    (common / "hooks" / "post-commit").write_text("#!/bin/sh\n")
    seen.append(fingerprint())
    (common / "info").mkdir(exist_ok=True)
    (common / "info" / "attributes").write_text("*.nothing -diff\n")
    seen.append(fingerprint())

    assert len(set(seen)) == len(seen), seen


def test_good_path_resolution_in_a_linked_worktree_under_errexit_and_pipefail(
    tmp_path: Path,
) -> None:
    # The same full good path as above, spelled out as the regression the reviewer reported:
    # `set -euo pipefail`, a linked worktree, and the call inside an `if` as heartbeat.sh does.
    sc = build(tmp_path)
    assert (sc.worktree / ".git").is_file()

    out = resolve(sc, resolve_all(sc))

    assert out.ok is True, out.result.stdout + out.result.stderr
    assert "cannot fingerprint git configuration" not in out.result.stdout
    assert sc.remote_branch_sha == _sha(sc.worktree, "HEAD") != sc.orig_head
    assert out.event_names[-1] == "complete_job"


# --- squash-finalize bound to the approved head ----------------------------------------------


def _squash(
    sc: Scenario, approved_sha: str, reviews: list[dict] | None = None
) -> subprocess.CompletedProcess[str]:
    script = "\n".join(
        [
            _prelude(sc),
            r"""
generate_squash_commit_message() { SQUASH_COMMIT_MESSAGE="feat: squashed"; }
link_branch_to_issue() { :; }
publish_commit_checkpoints() { :; }
gh() {
  if [[ "$1" == pr && "$2" == view ]]; then printf '%s' "$GH_REVIEWS_JSON"; return 0; fi
  if [[ "$1" == api && "$2" == users/* ]]; then
    [[ "$*" == *".email"* ]] && return 0
    printf '%s' "${2#users/}"
    return 0
  fi
  return 0
}
""",
            f"cd {shlex.quote(str(sc.worktree))}",
            f"if squash_and_finalize_pr {PR} {BRANCH} title body {ISSUE} "
            f"{shlex.quote(approved_sha)}; "
            "then echo SQUASH_OK; else echo SQUASH_FAIL; fi",
        ]
    )
    env = _env({"GH_REVIEWS_JSON": json.dumps({"reviews": reviews or []})})
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, check=False, text=True, env=env
    )


def test_squash_refuses_when_the_checked_out_head_is_not_the_approved_head(
    tmp_path: Path,
) -> None:
    sc = build(tmp_path, conflicts=())
    not_approved = _sha(sc.worktree, "HEAD~1")
    assert not_approved != sc.orig_head

    result = _squash(sc, not_approved)

    assert result.stdout.count("SQUASH_FAIL") == 1, result.stdout + result.stderr
    assert "is not the approved head" in result.stdout
    assert sc.remote_branch_sha == sc.orig_head  # nothing pushed
    assert _sha(sc.worktree, "HEAD") == sc.orig_head  # not even the base merge happened
    assert _git(sc.worktree, "status", "--porcelain").stdout == ""
    assert not _merge_in_progress(sc)


def test_squash_on_the_approved_head_credits_only_reviewers_approved_on_that_head(
    tmp_path: Path,
) -> None:
    sc = build(tmp_path, conflicts=())
    older = _sha(sc.worktree, "HEAD~1")
    reviews = [
        {"author": {"login": "alice"}, "state": "APPROVED", "commit": {"oid": sc.orig_head}},
        {"author": {"login": "bob"}, "state": "APPROVED", "commit": {"oid": older}},
    ]

    result = _squash(sc, sc.orig_head, reviews)

    assert result.stdout.count("SQUASH_OK") == 1, result.stdout + result.stderr
    pushed = sc.remote_branch_sha
    assert pushed != sc.orig_head  # backstop: the squash really ran and was pushed
    assert _git(sc.remote, "rev-parse", f"{pushed}^").stdout.strip() == sc.base_sha
    message = _git(sc.remote, "log", "-1", "--format=%B", pushed).stdout
    assert "Co-Authored-By: alice" in message
    assert "bob" not in message


def test_squash_force_push_lease_is_taken_on_the_approved_head(tmp_path: Path) -> None:
    sc = build(tmp_path, conflicts=())
    # A human pushes after approval, and the executor's tracking ref learns about it. A lease
    # taken on the tracking ref would match and overwrite that commit; the approved head must not.
    _git(sc.other, "checkout", BRANCH)
    _commit_files(sc.other, {"human.txt": "late human commit\n"}, "fix: human push after approval")
    _git(sc.other, "push", "origin", BRANCH)
    human = _sha(sc.other, "HEAD")
    _git(sc.repo, "fetch", "origin")
    assert _sha(sc.repo, f"refs/remotes/origin/{BRANCH}") == human  # backstop

    result = _squash(sc, sc.orig_head)

    assert result.stdout.count("SQUASH_FAIL") == 1, result.stdout + result.stderr
    assert "force-push failed" in result.stdout  # the lease refused, not an earlier step
    assert sc.remote_branch_sha == human  # the human commit survives
    assert _sha(sc.worktree, "HEAD") == sc.orig_head  # restored for the next wake


# --- the worker grant and the heartbeat wiring -------------------------------------------------


def test_conflict_session_is_denied_ref_moving_and_configuration_commands(
    tmp_path: Path,
) -> None:
    # spec §Base-branch conflicts: the grant denies `git push` and additionally ref-moving,
    # merge-ending, worktree-rewriting and git-configuration commands, and (fix-session denial)
    # delegation tools.
    sc = build(tmp_path, conflicts=())
    script = "\n".join(
        [
            _prelude(sc),
            'invoke_agent() { printf "ALLOWED=%s\\nTURNS=%s\\nBUDGET=%s\\nDENY=%s\\nPROMPT=%s\\n" '
            '"$2" "$3" "$4" "$5" "$1"; AGENT_RESULT=summary; }',
            "PRAUTO_CLAUDE_MAX_BUDGET_IMPLEMENTATION=9",
            "unset PRAUTO_CLAUDE_MAX_TURNS_CONFLICT_FIX PRAUTO_CLAUDE_MAX_BUDGET_CONFLICT_FIX",
            f"PRAUTO_DIR={shlex.quote(str(PRAUTO))}",
            f"run_conflict_resolution {ISSUE} {BRANCH} $'a.txt\\nb.txt' 'PR title' 'the plan'",
            'echo "SUMMARY=$CONFLICT_RESOLUTION_SUMMARY"',
        ]
    )

    result = _run_script(sc, script)

    assert result.returncode == 0, result.stderr
    lines = {
        ln.split("=", 1)[0]: ln.split("=", 1)[1] for ln in result.stdout.splitlines() if "=" in ln
    }
    deny = lines["DENY"].split(",")
    for needed in (
        "Bash(git push *)",  # the standing denial
        "Agent",
        "Workflow",
        "Task",  # fix-session delegation denial
        "Bash(git commit *)",
        "Bash(git merge *)",
        "Bash(git rebase *)",
        "Bash(git reset *)",
        "Bash(git checkout *)",
        "Bash(git update-ref *)",
        "Bash(git config *)",
        "Bash(git clean *)",
        "Write(.git/**)",
        "Edit(.git/**)",
    ):
        assert needed in deny, needed
    assert lines["TURNS"] == "100"  # PRAUTO_CLAUDE_MAX_TURNS_CONFLICT_FIX default
    assert lines["BUDGET"] == "9"  # falls back to the implementation budget
    assert "a.txt" in result.stdout and "{conflicted_files}" not in result.stdout
    assert "SUMMARY=summary" in result.stdout


def test_heartbeat_routes_conflicts_and_binds_squash_to_the_checked_head() -> None:
    # Structural check of the prauto:review dispatch (heartbeat.sh is a script, not a library).
    text = (PRAUTO / "heartbeat.sh").read_text()
    arm = text[text.index("conflict_resolution)") :]
    arm = arm[: arm.index(";;")]
    order = [
        "fetch_approved_plan",
        "checkout_branch_worktree",
        "cd ",
        "resolve_pr_conflicts",
        "cleanup_worktree",
    ]
    positions = [arm.index(token) for token in order]
    assert positions == sorted(positions)
    assert '"$REVIEW_PR_NUMBER" "$REVIEW_PR_BRANCH" "$CUR_ISSUE_NUMBER"' in arm
    squash = text[text.index("squash_ready)") :]
    squash = squash[: squash.index(";;")]
    assert '"$CUR_ISSUE_NUMBER" "$REVIEW_PR_HEAD_SHA"' in squash


# --- conflict-attempt state helpers --------------------------------------------------------------


def _state_shell(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    state_dir = tmp_path / "state"
    script = "\n".join(
        [
            f"PRAUTO_DIR={shlex.quote(str(tmp_path / 'prauto-dir'))}",
            f"REPO_DIR={shlex.quote(str(tmp_path / 'repo'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/helpers.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/state.sh'))}",
            ISOLATE_REAL_STATE_SHELL,
            f"STATE_DIR={shlex.quote(str(state_dir))}",
            'mkdir -p "$STATE_DIR"',
            body,
        ]
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, check=False, text=True, env=_env()
    )


def test_conflict_attempt_file_is_per_issue_under_the_state_dir(tmp_path: Path) -> None:
    result = _state_shell(
        tmp_path,
        'conflict_attempt_file 42; echo; conflict_attempt_file "4;2" && echo BAD || echo REJECTED',
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        str(tmp_path / "state" / "conflict-attempt-42.json"),
        "REJECTED",
    ]


def test_only_a_recorded_failure_for_the_exact_pair_suppresses(tmp_path: Path) -> None:
    h1, h2, b1, b2 = "1" * 40, "2" * 40, "3" * 40, "4" * 40
    result = _state_shell(
        tmp_path,
        f"""
check() {{ conflict_attempt_is_unchanged 5 "$1" "$2" && echo "$3=suppressed" || echo "$3=armed"; }}
check {h1} {b1} none
record_conflict_attempt 5 {h1} {b1}
check {h1} {b1} same
check {h2} {b1} head-moved
check {h1} {b2} base-moved
check "" {b1} empty-head
check {h1} "" empty-base
clear_conflict_attempt 5
check {h1} {b1} cleared
printf 'not json' > "$(conflict_attempt_file 5)"
check {h1} {b1} malformed
""",
    )

    assert result.returncode == 0, result.stderr
    assert dict(ln.split("=") for ln in result.stdout.splitlines()) == {
        "none": "armed",
        "same": "suppressed",
        "head-moved": "armed",
        "base-moved": "armed",
        "empty-head": "armed",
        "empty-base": "armed",
        "cleared": "armed",
        "malformed": "armed",
    }


def test_session_error_counter_counts_per_pair_and_never_suppresses(tmp_path: Path) -> None:
    h1, h2, b1 = "1" * 40, "2" * 40, "3" * 40
    result = _state_shell(
        tmp_path,
        f"""
echo "n=$(note_conflict_session_error 5 {h1} {b1})"
echo "n=$(note_conflict_session_error 5 {h1} {b1})"
conflict_attempt_is_unchanged 5 {h1} {b1} && echo suppressed || echo armed
echo "n=$(note_conflict_session_error 5 {h2} {b1})"
""",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["n=1", "n=2", "armed", "n=1"]


def test_conflict_attempt_survives_reset_ephemeral_state(tmp_path: Path) -> None:
    (tmp_path / "prauto-dir" / "worktrees").mkdir(parents=True)
    h1, b1 = "1" * 40, "3" * 40
    result = _state_shell(
        tmp_path,
        f"""
record_conflict_attempt 5 {h1} {b1}
reset_ephemeral_state
conflict_attempt_is_unchanged 5 {h1} {b1} && echo kept || echo lost
""",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "kept"
