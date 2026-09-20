"""Regression coverage for the pinned evaluator authority the executor captures.

The snapshot is what makes a stage reviewer independent of the branch it judges.
These snippets read the real scaffold/ sources through the library and write into
a temporary tree; nothing contacts an agent, GitHub, or a cluster.

spec: spec/AI_PRAUTO.md §The implementation phase runs the AGENTS.md workflow;
§Executor-owned review gate; spec/AI_SCAFFOLD.md (pinned evaluator authority)
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[3]
PRAUTO = ROOT / ".prauto"
EVALUATORS = ("reviewer", "test-reviewer", "spec-reviewer", "security-reviewer")


def _run_bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        check=False,
        text=True,
        env=os.environ.copy(),
    )


def _setup(tmp_path: Path, repo_dir: Path = ROOT, prauto_dir: Path | None = None) -> str:
    return "\n".join(
        [
            f"PRAUTO_DIR={shlex.quote(str(prauto_dir or (tmp_path / 'prauto')))}",
            f"REPO_DIR={shlex.quote(str(repo_dir))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/helpers.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/state.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/quota.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/agent.sh'))}",
        ]
    )


def test_authority_is_captured_for_every_evaluator_type(tmp_path: Path) -> None:
    """Each reviewer binding wf-minimal can dispatch must have its authority on disk.

    spec: spec/AI_SCAFFOLD.md — reviewers fail closed on absent authority, so a
    type the run turns out to need but the capture skipped escalates the whole
    stage rather than degrading.
    """
    dest = tmp_path / "prauto" / "authority" / "I-182"
    result = _run_bash(_setup(tmp_path) + f"\ncapture_evaluator_authority {shlex.quote(str(dest))}")

    assert result.returncode == 0, result.stderr
    for evaluator in EVALUATORS:
        assert (dest / f"{evaluator}.md").is_file(), f"missing authority for {evaluator}"


def test_a_captured_snapshot_is_complete_and_untruncated(tmp_path: Path) -> None:
    """The whole role, schema and memory must be present, not a summary.

    spec: spec/AI_SCAFFOLD.md — the snapshot IS the reviewer's instructions. The
    previous inline-argument path exceeded the workflow tool's script limit, so
    callers truncated memory notes to fit; passing a file removes that pressure,
    and this asserts the content is genuinely whole.
    """
    dest = tmp_path / "prauto" / "authority" / "I-182"
    result = _run_bash(_setup(tmp_path) + f"\ncapture_evaluator_authority {shlex.quote(str(dest))}")
    assert result.returncode == 0, result.stderr

    captured = (dest / "reviewer.md").read_text()
    role = (ROOT / "scaffold/roles/reviewer.md").read_text()
    schema = (ROOT / "scaffold/contracts/reviewer-verdict.schema.json").read_text()

    assert role in captured, "the reviewer role is not present verbatim"
    assert schema in captured, "the verdict schema is not present verbatim"

    memory_dir = ROOT / "scaffold/memory/reviewer"
    notes = sorted(p for p in memory_dir.iterdir() if p.is_file())
    assert notes, "expected the reviewer to have evaluator memory"
    for note in notes:
        assert note.read_text() in captured, f"memory note truncated or missing: {note.name}"


def test_the_snapshot_records_the_commit_it_was_taken_from(tmp_path: Path) -> None:
    """Provenance has to travel with the snapshot, or it cannot be audited.

    spec: spec/AI_PRAUTO.md §The implementation phase runs the AGENTS.md workflow —
    the authority is pinned from trusted pre-generation state. The snapshot is
    overwritten each attempt, so the header is what lets a later reader reproduce
    exactly what a given run's reviewers were told.
    """
    dest = tmp_path / "prauto" / "authority" / "I-182"
    result = _run_bash(_setup(tmp_path) + f"\ncapture_evaluator_authority {shlex.quote(str(dest))}")
    assert result.returncode == 0, result.stderr

    header = (dest / "reviewer.md").read_text().split("=====", 1)[0]
    assert "PINNED EVALUATOR AUTHORITY SNAPSHOT" in header
    assert "reviewer-binding: reviewer" in header
    assert "captured-from:" in header
    assert str(ROOT) in header


def test_capture_fails_closed_when_a_role_is_missing(tmp_path: Path) -> None:
    """A partial capture must not reach dispatch.

    spec: spec/AI_SCAFFOLD.md — a reviewer without authority escalates. Detecting
    that here costs nothing; detecting it after dispatch costs an attempt and a
    worker session to reach a verdict the executor could already predict.
    """
    fake_repo = tmp_path / "repo"
    (fake_repo / "scaffold/roles").mkdir(parents=True)
    (fake_repo / "scaffold/contracts").mkdir(parents=True)
    (fake_repo / "scaffold/contracts/reviewer-verdict.schema.json").write_text("{}")
    # Only one of the four roles exists.
    (fake_repo / "scaffold/roles/reviewer.md").write_text("role")

    dest = tmp_path / "prauto" / "authority" / "I-182"
    result = _run_bash(
        _setup(tmp_path, repo_dir=fake_repo)
        + f"\ncapture_evaluator_authority {shlex.quote(str(dest))}"
    )

    assert result.returncode != 0, "a missing evaluator role must fail the capture"


def test_capture_fails_closed_when_the_verdict_schema_is_missing(tmp_path: Path) -> None:
    """Without the schema a reviewer has no contract to emit against.

    spec: spec/AI_SCAFFOLD.md — the parent validates every verdict against the
    pinned shared schema; a reviewer that never received it cannot produce one
    the parent can validate, and an unvalidatable verdict is an ESCALATE.
    """
    fake_repo = tmp_path / "repo"
    (fake_repo / "scaffold/roles").mkdir(parents=True)
    for evaluator in EVALUATORS:
        (fake_repo / f"scaffold/roles/{evaluator}.md").write_text("role")

    dest = tmp_path / "prauto" / "authority" / "I-182"
    result = _run_bash(
        _setup(tmp_path, repo_dir=fake_repo)
        + f"\ncapture_evaluator_authority {shlex.quote(str(dest))}"
    )

    assert result.returncode != 0, "a missing verdict schema must fail the capture"


def test_a_stale_snapshot_does_not_survive_a_recapture(tmp_path: Path) -> None:
    """Each attempt reviews against its own snapshot, not a previous run's.

    spec: spec/AI_PRAUTO.md §Retry tracking — attempts are independent. The
    directory is reused across attempts, so a file left by an earlier capture
    that a later one no longer produces would silently supply stale authority.
    """
    dest = tmp_path / "prauto" / "authority" / "I-182"
    dest.mkdir(parents=True)
    stale = dest / "leftover-from-an-earlier-run.md"
    stale.write_text("stale authority")

    result = _run_bash(_setup(tmp_path) + f"\ncapture_evaluator_authority {shlex.quote(str(dest))}")

    assert result.returncode == 0, result.stderr
    assert not stale.exists(), "a stale authority file survived the recapture"


def test_the_authority_directory_is_absolute_and_outside_any_worktree() -> None:
    """Reviewers must read a snapshot the branch under review cannot be.

    spec: spec/AI_PRAUTO.md §Pinned evaluator authority capture — the snapshot
    lives outside every worktree, so branch content cannot reach it. It is
    deliberately not under the state tree: the reviewers' ability to read their
    own authority must not depend on whether a parent's --disallowedTools rule is
    inherited by subagents, since that would wedge every run if it were.
    """
    agent_lib = (PRAUTO / "lib/agent.sh").read_text()

    assert 'authority_dir="${PRAUTO_DIR}/authority/' in agent_lib, (
        "the authority directory moved; it must stay outside every worktree"
    )
    assert "worktrees" not in agent_lib.split('authority_dir="')[1].split("\n")[0]



def test_the_implementation_is_not_dispatched_without_authority(tmp_path: Path) -> None:
    """A failed capture stops before the worker starts, not after.

    spec: spec/AI_SCAFFOLD.md — reviewers fail closed on absent authority, so a
    worker dispatched without it can only reach an ESCALATE. Spending an attempt
    and a session to arrive at a verdict the executor already knows is waste, and
    it burns retry budget the job needs for real work.
    """
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()  # no scaffold/ at all

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    result = _run_bash(
        "\n".join(
            [
                _setup(tmp_path, repo_dir=fake_repo),
                f"CUR_SESSION_DIR={shlex.quote(str(session_dir))}",
                'render_prompt() { printf "prompt"; }',
                'invoke_agent() { printf "DISPATCHED\\n"; }',
                "run_implementation 182 branch plan",
                'printf "status=%s\\n" "$AGENT_STATUS"',
            ]
        )
    )

    assert result.returncode == 0, result.stderr
    assert "DISPATCHED" not in result.stdout, "the worker was dispatched without authority"
    assert "status=error" in result.stdout, result.stdout


def test_wf_minimal_rejects_a_relative_authority_path() -> None:
    """A relative path would resolve against the worktree generators write.

    spec: spec/AI_SCAFFOLD.md — the authority must come from state no generator
    can reach. `authority/reviewer.md` relative to the worktree is exactly such a
    path, so the workflow has to refuse it rather than review against whatever
    the branch happens to contain there.
    """
    workflow = (ROOT / ".claude/workflows/wf-minimal.js").read_text()

    assert "startsWith('/')" in workflow, (
        "wf-minimal no longer checks that authority paths are absolute"
    )
    assert "IS NOT ABSOLUTE" in workflow
    # And a missing entry must still escalate rather than silently proceed.
    assert "AUTHORITY NOT SUPPLIED" in workflow


def test_capture_refuses_a_destination_outside_its_own_authority_root(
    tmp_path: Path,
) -> None:
    """The capture clears its destination, so it must own that destination.

    spec: spec/AI_PRAUTO.md §Pinned evaluator authority capture — the snapshot
    directory is recreated per attempt. A removal is only safe where the path is
    known to be the one this function manages; an empty PRAUTO_DIR or issue
    number would otherwise expand to something like `/authority/I-`.
    """
    victim = tmp_path / "not-authority"
    victim.mkdir()
    (victim / "keep.txt").write_text("important")

    result = _run_bash(
        _setup(tmp_path) + f"\ncapture_evaluator_authority {shlex.quote(str(victim))}"
    )

    assert result.returncode != 0, "capture accepted a destination it does not own"
    assert (victim / "keep.txt").exists(), "capture removed a directory outside its root"


def test_capture_refuses_a_traversing_destination(tmp_path: Path) -> None:
    """A path that escapes the authority root by traversal is refused.

    spec: spec/AI_PRAUTO.md §Pinned evaluator authority capture — the same
    ownership requirement, against a path that satisfies the prefix check
    textually while resolving elsewhere.
    """
    escaping = str(tmp_path / "prauto" / "authority" / ".." / ".." / "elsewhere")

    result = _run_bash(_setup(tmp_path) + f"\ncapture_evaluator_authority {shlex.quote(escaping)}")

    assert result.returncode != 0, "capture accepted a traversing destination"


def test_capture_refuses_a_relative_prauto_dir(tmp_path: Path) -> None:
    """Without an absolute PRAUTO_DIR the destination cannot be reasoned about.

    spec: spec/AI_PRAUTO.md §Executor Cycle — the executor resolves PRAUTO_DIR to
    an absolute path at startup; anything else means the caller is not the
    executor, and the removal below must not proceed on that assumption.
    """
    result = _run_bash(
        "\n".join(
            [
                "PRAUTO_DIR=relative/path",
                f"REPO_DIR={shlex.quote(str(ROOT))}",
                f"source {shlex.quote(str(PRAUTO / 'lib/helpers.sh'))}",
                f"source {shlex.quote(str(PRAUTO / 'lib/state.sh'))}",
                f"source {shlex.quote(str(PRAUTO / 'lib/quota.sh'))}",
                f"source {shlex.quote(str(PRAUTO / 'lib/agent.sh'))}",
                "capture_evaluator_authority relative/path/authority/I-1",
            ]
        )
    )

    assert result.returncode != 0, "capture proceeded with a relative PRAUTO_DIR"


def test_an_uncapturable_authority_refunds_the_attempt(tmp_path: Path) -> None:
    """An executor-side capture failure must not charge the job an attempt.

    spec: spec/AI_PRAUTO.md §Retry tracking — only genuine attempt starts consume
    the retry budget. The heartbeat increments the counter before this point, so
    a deterministic harness fault that dispatches no worker at all would otherwise
    burn an attempt on every wake and abandon the job at the retry limit without
    a single line of work attempted.
    """
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()  # no scaffold/ at all

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    result = _run_bash(
        "\n".join(
            [
                _setup(tmp_path, repo_dir=fake_repo),
                f"CUR_SESSION_DIR={shlex.quote(str(session_dir))}",
                'render_prompt() { printf "prompt"; }',
                'invoke_agent() { printf "DISPATCHED\\n"; }',
                'refund_retry_count() { printf "REFUNDED\\n"; }',
                "run_implementation 182 branch plan",
            ]
        )
    )

    assert result.returncode == 0, result.stderr
    assert "DISPATCHED" not in result.stdout
    assert "REFUNDED" in result.stdout, result.stdout


def test_a_non_numeric_issue_number_is_refused_before_any_delete(
    tmp_path: Path,
) -> None:
    """The issue number names a directory the capture recursively clears.

    spec: spec/AI_PRAUTO.md §Pinned evaluator authority capture — the snapshot
    directory is recreated per attempt. Its name embeds the issue number, so a
    value that is not a plain integer must be refused before it reaches the path.
    """
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    result = _run_bash(
        "\n".join(
            [
                _setup(tmp_path),
                f"CUR_SESSION_DIR={shlex.quote(str(session_dir))}",
                'render_prompt() { printf "prompt"; }',
                'invoke_agent() { printf "DISPATCHED\\n"; }',
                'capture_evaluator_authority() { printf "CAPTURED\\n"; }',
                'run_implementation "../../escape" branch plan',
                'printf "status=%s\\n" "$AGENT_STATUS"',
            ]
        )
    )

    assert result.returncode == 0, result.stderr
    assert "CAPTURED" not in result.stdout, "capture ran for a non-numeric issue"
    assert "DISPATCHED" not in result.stdout
    assert "status=error" in result.stdout


def test_reviewers_can_read_their_authority_without_a_deny_exception() -> None:
    """A reviewer must not have to out-argue the parent's tool denials to work.

    spec: spec/AI_PRAUTO.md §Pinned evaluator authority capture — the reviewer
    subagents read the snapshot directly. Placing it under a path DENY_TOOLS
    blocks would make every pass depend on subagent tool inheritance; if that
    resolved the other way, every reviewer's first action fails and the phase
    wedges for every issue rather than degrading.
    """
    agent_lib = (PRAUTO / "lib/agent.sh").read_text()
    deny = next(
        line for line in agent_lib.splitlines() if line.startswith("DENY_TOOLS=")
    )
    authority_line = next(
        line for line in agent_lib.splitlines() if 'authority_dir="' in line
    )

    assert "${PRAUTO_DIR}/state" in deny, "expected the state tree to stay denied"
    assert "/state/" not in authority_line, (
        "the authority snapshot sits under a denied path; reviewers would need "
        "the parent's deny not to reach them, which is unmeasured"
    )
