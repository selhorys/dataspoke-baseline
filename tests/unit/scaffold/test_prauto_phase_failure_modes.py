"""Regression coverage for three silent failure modes in the PRauto phase library.

Each one failed invisibly: the wake ended early with no error attributable to it,
a lock stayed held by a dead process, or a cluster stayed provisioned with nothing
recording that it existed. The shell snippets stub every external command, so
nothing reaches GitHub, a cluster, or a lock service.

spec: spec/AI_PRAUTO.md §Executor Cycle; §The plan gate is evidence-based;
§Provisioning; §Dev Cluster and Deploys
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
PRAUTO = ROOT / ".prauto"


def _run_bash(
    script: str, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a phase-library snippet in its own process."""
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        check=False,
        text=True,
        env=os.environ | (env or {}),
    )


def _source_phases(tmp_path: Path, *, errexit: bool = False) -> str:
    """Source the phase libraries against an isolated state tree."""
    lines = []
    if errexit:
        # heartbeat.sh runs under `set -euo pipefail`; a library function that
        # returns non-zero on an ordinary path ends the whole wake there.
        lines.append("set -euo pipefail")
    lines += [
        f"PRAUTO_DIR={shlex.quote(str(tmp_path / 'prauto'))}",
        f"REPO_DIR={shlex.quote(str(tmp_path / 'repo'))}",
        'PRAUTO_GITHUB_REPO="owner/repo"',
        'PRAUTO_WORKER_ID="test"',
        'PRAUTO_GITHUB_LABEL_WIP="prauto:wip"',
        'PRAUTO_GITHUB_LABEL_PLAN_REVIEW="prauto:plan-review"',
        'PRAUTO_GITHUB_LABEL_FAILED="prauto:failed"',
        f"source {shlex.quote(str(PRAUTO / 'lib/helpers.sh'))}",
        f"source {shlex.quote(str(PRAUTO / 'lib/state.sh'))}",
        f"source {shlex.quote(str(PRAUTO / 'lib/quota.sh'))}",
        f"source {shlex.quote(str(PRAUTO / 'lib/agent.sh'))}",
        f"source {shlex.quote(str(PRAUTO / 'lib/git-ops.sh'))}",
        f"source {shlex.quote(str(PRAUTO / 'lib/pr.sh'))}",
        f"source {shlex.quote(str(PRAUTO / 'lib/issues.sh'))}",
        f"source {shlex.quote(str(PRAUTO / 'lib/phases.sh'))}",
    ]
    return "\n".join(lines)


def test_a_non_minor_plan_approval_does_not_end_the_wake(tmp_path: Path) -> None:
    """Posting a plan that needs approval must not abort the heartbeat.

    spec: spec/AI_PRAUTO.md §The plan gate is evidence-based — a non-minor change
    posts its plan and waits for a human; that is the ordinary path, not an error.
    §Executor Cycle has the wake go on to process every remaining claimed issue.

    heartbeat.sh calls this function bare under `set -euo pipefail`, so a non-zero
    return ends the entire wake: the worktree is never cleaned up and every other
    claimed issue is skipped, with nothing in the log attributing it to the plan
    path. The regression is a `[[ cond ]] && call` tail, whose exit status is the
    condition's when the condition is false.
    """
    script = "\n".join(
        [
            _source_phases(tmp_path, errexit=True),
            # Reach the post-plan tail with a change size that needs approval.
            'plan_approved_on_issue() { return 1; }',
            'plan_comment_exists() { return 1; }',
            'gh() { printf "{}"; }',
            'fetch_issue_body() { printf "body"; }',
            'run_analysis() { ANALYSIS_OUTPUT="plan text"; AGENT_STATUS=ok; }',
            'resolve_change_size() { printf "medium"; }',
            'post_plan_comment() { :; }',
            'has_quota_paused_comment() { return 1; }',
            "handle_phase_plan_approval 182 'title' branch",
            # Only reached if the function returned 0 under set -e.
            'printf "WAKE_CONTINUED\\n"',
        ]
    )
    result = _run_bash(script)

    assert "WAKE_CONTINUED" in result.stdout, (
        f"the wake ended inside handle_phase_plan_approval\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert result.returncode == 0, result.stderr


def test_provision_fails_closed_when_the_state_marker_cannot_be_written(
    tmp_path: Path,
) -> None:
    """No cluster may be provisioned without durable evidence that it exists.

    spec: spec/AI_PRAUTO.md §Provisioning — the marker is what a later wake's
    recovery reads to tear down a cluster this worker started. Provisioning
    without it leaves a cluster that no wake can discover and nothing tears down,
    which bills indefinitely and fails silently. Not provisioning is the safe side.
    """
    repo = tmp_path / "repo"
    (repo / "helm-charts" / "bin").mkdir(parents=True)
    install = repo / "helm-charts" / "bin" / "install.sh"
    install.write_text("#!/usr/bin/env bash\nprintf 'INSTALL_RAN\\n'\n")
    install.chmod(0o755)
    env_file = tmp_path / "env.dev"
    env_file.write_text("X=1\n")

    script = "\n".join(
        [
            _source_phases(tmp_path),
            "ensure_state_dirs",
            # The one failure under test.
            "write_dev_env_state_marker() { return 1; }",
            f"provision_dev_env {shlex.quote(str(env_file))} && rc=0 || rc=$?",
            'printf "rc=%s provisioned=%s\\n" "$rc" "${DEV_ENV_PROVISIONED:-unset}"',
        ]
    )
    result = _run_bash(script)

    assert "rc=1" in result.stdout, result.stdout + result.stderr
    # The globals must not claim a cluster either — teardown keys off them.
    assert "provisioned=false" in result.stdout, result.stdout + result.stderr
    # The gated wrapper must never have been authorized to exec the installer.
    assert "INSTALL_RAN" not in result.stdout


def test_release_required_dev_lock_releases_the_registered_owner_once(
    tmp_path: Path,
) -> None:
    """The release names the registered owner, and only releases once.

    spec: spec/AI_PRAUTO.md §Dev Cluster and Deploys — the heartbeat's EXIT trap
    releases through release_required_dev_lock, which acts only on
    REQUIRED_LOCK_OWNER. This covers the release half; that each acquire site opts
    in is covered separately.
    """
    script = "\n".join(
        [
            _source_phases(tmp_path),
            'DEV_LOCK_URL="http://lock.invalid"',
            'PRAUTO_WORKER_ID="w1"',
            'REQUIRED_LOCK_OWNER=""',
            # Record what the release actually asks for.
            f'RELEASE_LOG={shlex.quote(str(tmp_path / "release.txt"))}',
            'curl() { printf "%s\\n" "$*" >> "$RELEASE_LOG"; }',
            # Simulate the registration the fix loops perform after a 200 acquire.
            'lock_owner="prauto-${PRAUTO_WORKER_ID}"',
            'REQUIRED_LOCK_OWNER="$lock_owner"',
            "release_required_dev_lock",
            'printf "owner_after=[%s]\\n" "${REQUIRED_LOCK_OWNER}"',
        ]
    )
    result = _run_bash(script)

    assert result.returncode == 0, result.stderr
    released = (tmp_path / "release.txt").read_text()
    # The release must name the same owner the acquire registered...
    assert "prauto-w1" in released
    assert "/release" in released
    # ...and the helper must be idempotent, so the EXIT trap's later call no-ops.
    assert "owner_after=[]" in result.stdout


def test_release_required_dev_lock_is_a_noop_without_a_registered_owner(
    tmp_path: Path,
) -> None:
    """The trap must never release a lock this worker does not hold.

    spec: spec/AI_PRAUTO.md §Dev Cluster and Deploys — the lock is shared with
    humans and other workers, so a blind release would hand another holder's lock
    away mid-run.
    """
    script = "\n".join(
        [
            _source_phases(tmp_path),
            'DEV_LOCK_URL="http://lock.invalid"',
            'REQUIRED_LOCK_OWNER=""',
            f'RELEASE_LOG={shlex.quote(str(tmp_path / "release.txt"))}',
            'curl() { printf "%s\\n" "$*" >> "$RELEASE_LOG"; }',
            "release_required_dev_lock",
            'printf "DONE\\n"',
        ]
    )
    result = _run_bash(script)

    assert "DONE" in result.stdout, result.stderr
    assert not (tmp_path / "release.txt").exists()


def test_every_inline_dev_lock_acquire_registers_the_shared_owner() -> None:
    """Backstop: an acquire that skips registration leaks the lock on a TERM.

    spec: spec/AI_PRAUTO.md §Dev Cluster and Deploys — the EXIT trap releases via
    REQUIRED_LOCK_OWNER, so registration is what makes a held lock recoverable.

    A source-shape backstop for the two long fix loops, whose acquires sit deep
    inside cluster-dependent control flow. It matches the identifier rather than an
    exact assignment, and treats "no inline acquires" as nothing to guard rather
    than a failure — folding them into acquire_required_dev_lock, which registers
    the owner itself, is a strictly better implementation this must not block.
    """
    phases = (PRAUTO / "lib/phases.sh").read_text().splitlines()
    inline_acquires = [
        i for i, line in enumerate(phases) if '"${lock_url}/acquire"' in line
    ]
    if not inline_acquires:
        pytest.skip("no inline acquires left; the shared helper registers the owner")

    for idx in inline_acquires:
        window = "\n".join(phases[idx : idx + 20])
        assert "REQUIRED_LOCK_OWNER" in window, (
            f"the dev-lock acquire at phases.sh:{idx + 1} does not register "
            "REQUIRED_LOCK_OWNER, so the heartbeat EXIT trap cannot release it"
        )


def test_the_retry_consumed_flag_is_set_and_reset_inside_the_issue_loop() -> None:
    """Backstop: both halves of the refund gate must exist, and per issue.

    spec: spec/AI_PRAUTO.md §Retry tracking — only the dispatch that consumed an
    attempt may refund one. Deleting the set silently turns the refund rule into
    dead code; hoisting the reset out of the per-issue loop lets one issue refund
    an attempt a previous issue in the same wake consumed.

    A source-shape backstop: it checks the reset is indented inside the claimed-issue
    loop rather than at file scope, which is the part a single-issue behavioural
    test cannot distinguish.
    """
    lines = (PRAUTO / "heartbeat.sh").read_text().splitlines()

    sets = [i for i, line in enumerate(lines) if line.strip() == "RETRY_COUNT_CONSUMED=true"]
    resets = [i for i, line in enumerate(lines) if line.strip() == "RETRY_COUNT_CONSUMED=false"]
    assert sets, "nothing marks an attempt consumed, so refund_retry_count never refunds"
    assert resets, "the per-issue reset is gone: a refund could take another issue's attempt"

    # Inside the loop body, so it re-runs per claimed issue.
    for idx in resets:
        indent = len(lines[idx]) - len(lines[idx].lstrip())
        assert indent > 0, (
            "RETRY_COUNT_CONSUMED is reset at file scope, so it is not reset per "
            "claimed issue and can carry over within a wake"
        )

    # The mark must follow the increment it reports.
    increment = next(
        i for i, line in enumerate(lines) if line.strip().startswith("if ! increment_retry_count")
    )
    assert min(sets) > increment, "expected the consumed mark after the increment"
