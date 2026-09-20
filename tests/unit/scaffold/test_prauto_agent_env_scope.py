"""Regression coverage for per-phase Claude CLI environment scoping.

The snippets stub ``claude`` with a script that reports the environment it was
given, so nothing contacts a real agent CLI, GitHub, or a cluster.

spec: spec/AI_PRAUTO.md §Worker Agent Invocation; §Security Model
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[3]
PRAUTO = ROOT / ".prauto"


def _stub_claude(tmp_path: Path) -> Path:
    """A claude stub whose JSON result echoes the two knobs under test."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "claude"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'{"result":"WF=%s CEIL=%s","is_error":false}\' '
        '"${CLAUDE_CODE_WORKFLOWS:-unset}" '
        '"${CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS:-unset}"\n'
    )
    stub.chmod(0o755)
    return bin_dir


def _invoke(tmp_path: Path, invocation: str) -> str:
    """Source the agent library against the stub and return AGENT_RESULT."""
    bin_dir = _stub_claude(tmp_path)
    session_dir = tmp_path / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "sys").write_text("system prompt")

    script = "\n".join(
        [
            f"PRAUTO_DIR={shlex.quote(str(PRAUTO))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/helpers.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/state.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/quota.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/agent.sh'))}",
            "ACTIVE_AGENT=claude",
            f"CUR_SESSION_DIR={shlex.quote(str(session_dir))}",
            # Keep the real system-prompt assembly out of this unit.
            f'prepare_system_prompt() {{ printf "%s" {shlex.quote(str(session_dir / "sys"))}; }}',
            invocation,
            'printf "<<<%s>>>" "$AGENT_RESULT"',
        ]
    )
    # Scrub the four names this file is about from the child environment. Both
    # the CLAUDE_CODE_* pair (which the stub reports) and the PRAUTO_* pair (which
    # agent.sh resolves its defaults from) are ambient-config risks: a developer
    # shell exporting either would make these tests pass vacuously or fail on a
    # configuration config.local.env.example documents as supported.
    child_env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "CLAUDE_CODE_WORKFLOWS",
            "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS",
            "PRAUTO_CLAUDE_WORKFLOWS",
            "PRAUTO_CLAUDE_PRINT_BG_WAIT_CEILING_MS",
        }
    }
    child_env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        check=False,
        text=True,
        env=child_env,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.split("<<<", 1)[1].rsplit(">>>", 1)[0]


def test_implementation_invocation_receives_its_claude_environment(tmp_path: Path) -> None:
    """The implementation phase gets the dynamic-workflow opt-in and a wait ceiling.

    spec: spec/AI_PRAUTO.md §Worker Agent Invocation — the implementation phase
    drives .claude/workflows/wf-minimal.js through the CLI's Workflow tool, which
    the CLI registers only on this opt-in; --allowedTools cannot re-enable a tool
    that was never registered.
    """
    reported = _invoke(
        tmp_path,
        'invoke_agent "prompt" "Read" 5 "" "$DENY_TOOLS" "${IMPLEMENTATION_CLAUDE_ENV[@]}"',
    )

    assert "WF=1" in reported
    assert "CEIL=unset" not in reported


def test_other_phases_do_not_inherit_the_implementation_environment(tmp_path: Path) -> None:
    """Narrower phases keep the CLI's own defaults.

    spec: spec/AI_PRAUTO.md §Security Model — the accepted consequence that the
    delegation tools void DENY_TOOLS for delegated work is stated as a property of
    the implementation phase. A process-wide export would silently extend it to
    analysis and pr-review, and would also lift the background-wait ceiling on the
    two fix sessions, which run while this worker holds the dev-env lock.
    """
    reported = _invoke(tmp_path, 'invoke_agent "prompt" "Read" 5')

    assert "WF=unset" in reported
    assert "CEIL=unset" in reported


def test_resumed_implementation_keeps_the_same_claude_environment(tmp_path: Path) -> None:
    """A quota-resumed implementation must not lose the dynamic-workflow opt-in.

    spec: spec/AI_PRAUTO.md §Quota-pause and resume — a resume continues the same
    session for the same phase. Handing it a narrower environment than the fresh
    dispatch got would leave the resumed session unable to satisfy the phase's
    wf-minimal binding, so it would escalate having done nothing.
    """
    reported = _invoke(
        tmp_path,
        'PAUSED_SESSION_ID="s1"; '
        'resume_agent "prompt" "Read" 5 "s1" "" "$DENY_TOOLS" '
        '"${IMPLEMENTATION_CLAUDE_ENV[@]}"',
    )

    assert "WF=1" in reported
    assert "CEIL=unset" not in reported


def test_background_wait_ceiling_default_is_finite(tmp_path: Path) -> None:
    """A wedged background task must eventually be terminated, not waited on forever.

    spec: spec/AI_PRAUTO.md §Retry tracking — a session the CLI terminated on its
    wait ceiling is classified as a harness fault and refunds its attempt. A
    ceiling of 0 waits indefinitely, which both holds the executor's PID lock for
    the full PRAUTO_AGENT_TIMEOUT_SECS and makes that classification unreachable.
    """
    reported = _invoke(
        tmp_path,
        'invoke_agent "prompt" "Read" 5 "" "$DENY_TOOLS" "${IMPLEMENTATION_CLAUDE_ENV[@]}"',
    )

    ceiling = reported.split("CEIL=", 1)[1].strip()
    assert ceiling.isdigit(), reported
    assert int(ceiling) > 0
    # The shipped default specifically, not whatever an operator override supplies:
    # spec/AI_PRAUTO.md names 4 hours, and _invoke scrubs the override from the env.
    assert int(ceiling) == 14400000


def _record_argv(tmp_path: Path, script_lines: list[str]) -> str:
    """Run a snippet that records an invoke/resume argv, and return the recording."""
    bin_dir = _stub_claude(tmp_path)
    session_dir = tmp_path / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "sys").write_text("system prompt")
    script = "\n".join(
        [
            f"PRAUTO_DIR={shlex.quote(str(PRAUTO))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/helpers.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/state.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/quota.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/agent.sh'))}",
            "ACTIVE_AGENT=claude",
            f"CUR_SESSION_DIR={shlex.quote(str(session_dir))}",
            *script_lines,
        ]
    )
    child_env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "CLAUDE_CODE_WORKFLOWS",
            "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS",
            "PRAUTO_CLAUDE_WORKFLOWS",
            "PRAUTO_CLAUDE_PRINT_BG_WAIT_CEILING_MS",
        }
    }
    child_env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        check=False,
        text=True,
        env=child_env,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_run_implementation_passes_the_implementation_environment(tmp_path: Path) -> None:
    """The fresh implementation dispatch must actually hand the knobs over.

    spec: spec/AI_PRAUTO.md §Worker Agent Invocation — the implementation phase
    drives wf-minimal through the CLI's Workflow tool, registered only on the
    opt-in. Asserting the array's contents without asserting that the dispatch
    passes it leaves the wiring unpinned, and dropping it reproduces the original
    regression exactly: no Workflow tool, an unsatisfiable binding, escalate with
    zero commits.
    """
    recorded = _record_argv(
        tmp_path,
        [
            'render_prompt() { printf "prompt"; }',
            # The capture itself is covered by test_prauto_evaluator_authority.py;
            # here it only has to succeed so the dispatch is reached.
            "capture_evaluator_authority() { return 0; }",
            "invoke_agent() { printf 'ARGV:%s\\n' \"$*\"; AGENT_STATUS=ok; }",
            "run_implementation 182 branch plan",
        ],
    )

    assert "CLAUDE_CODE_WORKFLOWS=1" in recorded, recorded
    assert "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=14400000" in recorded, recorded


def test_invoke_agent_separates_the_result_stderr_and_merged_planes(
    tmp_path: Path,
) -> None:
    """Which plane a value lands in is the whole basis of the trust split.

    spec: spec/AI_PRAUTO.md §Retry tracking — the sentinel is read from what the
    agent said, while diagnostics keep the merged text. Assembling those planes by
    hand in a test proves nothing about what invoke_agent populates, so this drives
    the real function against a stub that writes to both stdout and stderr.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "claude"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'printf "DISTINCTIVE_STDERR_LINE\\n" >&2\n'
        'printf \'{"result":"THE_AGENTS_ANSWER","is_error":false}\'\n'
    )
    stub.chmod(0o755)
    session_dir = tmp_path / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "sys").write_text("system prompt")

    script = "\n".join(
        [
            f"PRAUTO_DIR={shlex.quote(str(PRAUTO))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/helpers.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/state.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/quota.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/agent.sh'))}",
            "ACTIVE_AGENT=claude",
            f"CUR_SESSION_DIR={shlex.quote(str(session_dir))}",
            f'prepare_system_prompt() {{ printf "%s" {shlex.quote(str(session_dir / "sys"))}; }}',
            'invoke_agent "prompt" "Read" 5',
            'printf "RESULT<<%s>>\\n" "$AGENT_RESULT"',
            'printf "STDERR<<%s>>\\n" "$AGENT_STDERR"',
            'printf "MERGED<<%s>>\\n" "$AGENT_OUTPUT"',
        ]
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        check=False,
        text=True,
        env=os.environ | {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout

    # The agent's answer alone — no stderr appended, or a sentinel would be displaced.
    assert "RESULT<<THE_AGENTS_ANSWER>>" in out, out
    # The CLI's stderr on its own plane.
    assert "STDERR<<DISTINCTIVE_STDERR_LINE>>" in out, out
    # The merged plane carries both, answer first.
    assert "THE_AGENTS_ANSWER" in out.split("MERGED<<", 1)[1]
    assert "DISTINCTIVE_STDERR_LINE" in out.split("MERGED<<", 1)[1]
