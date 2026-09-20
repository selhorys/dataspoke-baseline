"""Regression coverage for PRauto's isolated Codex JSONL adapter.

These tests deliberately source the shell libraries in a temporary state tree and
replace ``codex`` with a fixture-backed executable.  They never contact Codex or
GitHub.

spec: spec/AI_PRAUTO.md §Quota-pause and resume; §Agent execution adapters
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
PRAUTO = ROOT / ".prauto"
THREAD_ID = "8f0c2ea7-7f0e-4bc8-bc77-5d5d71f23d49"
READY_TIMESTAMP = "2026-08-25T00:00:00Z"


def _run_bash(
    script: str, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a hermetic bash snippet that sources the PRauto libraries."""
    run_env = os.environ.copy()
    run_env.update(env or {})
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        check=False,
        text=True,
        env=run_env,
    )


def _source_libraries(tmp_path: Path) -> str:
    state_root = tmp_path / "prauto"
    quoted = shlex.quote
    return "\n".join(
        [
            f"PRAUTO_DIR={quoted(str(state_root))}",
            f"source {quoted(str(PRAUTO / 'lib/helpers.sh'))}",
            f"source {quoted(str(PRAUTO / 'lib/state.sh'))}",
            f"source {quoted(str(PRAUTO / 'lib/quota.sh'))}",
            f"source {quoted(str(PRAUTO / 'lib/agent.sh'))}",
        ]
    )


def _source_phase_libraries(tmp_path: Path) -> str:
    return _source_libraries(tmp_path) + f"\nsource {shlex.quote(str(PRAUTO / 'lib/phases.sh'))}"


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def test_codex_jsonl_extracts_native_thread_and_multiline_final_output(tmp_path: Path) -> None:
    """Codex persists its emitted UUID and preserves raw multiline agent output.

    spec: spec/AI_PRAUTO.md §Quota-pause and resume (Codex identity),
    §Agent execution adapters (JSONL thread.started and terminal result).
    """
    fixture = tmp_path / "codex.jsonl"
    expected_output = "PRAUTO_WORKFLOW_OUTCOME: ESCALATED\nreviewer finding retained"
    _write_jsonl(
        fixture,
        [
            {"type": "thread.started", "thread_id": THREAD_ID},
            {"type": "item.completed", "item": {"type": "agent_message", "text": expected_output}},
        ],
    )

    result = _run_bash(
        "\n".join(
            [
                _source_libraries(tmp_path),
                f"thread=$(codex_thread_id {shlex.quote(str(fixture))})",
                f"output=$(codex_final_output {shlex.quote(str(fixture))})",
                'jq -n --arg thread "$thread" --arg output "$output" '
                "'{thread: $thread, output: $output}'",
            ]
        )
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"thread": THREAD_ID, "output": expected_output}


@pytest.mark.parametrize(
    ("records", "expected"),
    [
        ([{"type": "error", "error": {"code": "usage_limit_exceeded"}}], True),
        ([{"type": "turn.failed", "payload": {"error": {"reason": "rate_limited"}}}], True),
        (
            [
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "usage_limit_exceeded"},
                }
            ],
            False,
        ),
        (
            [
                {
                    "type": "thread.started",
                    "thread_id": THREAD_ID,
                    "rate_limit": {"code": "rate_limit"},
                }
            ],
            False,
        ),
        ([{"type": "error", "error": {"code": "ordinary_failure"}}], False),
        (
            [
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "quota reached"},
                },
                {"type": "turn.completed", "rate_limit": {"code": "rate_limit_exceeded"}},
            ],
            False,
        ),
    ],
)
def test_codex_quota_requires_a_terminal_failure_with_structured_code(
    tmp_path: Path, records: list[dict[str, object]], expected: bool
) -> None:
    """Quota-like text or intermediate/success events cannot create a pause.

    spec: spec/AI_PRAUTO.md §Agent execution adapters (JSONL protocol boundary)
    and §Quota-pause and resume (only rate/session-limit exits pause).
    """
    fixture = tmp_path / "codex.jsonl"
    _write_jsonl(fixture, records)
    result = _run_bash(
        "\n".join(
            [
                _source_libraries(tmp_path),
                f"codex_jsonl_has_quota_signal {shlex.quote(str(fixture))}",
            ]
        )
    )

    assert (result.returncode == 0) is expected, result.stderr


def test_codex_anchor_requires_exact_current_lifecycle_and_strict_uuid(tmp_path: Path) -> None:
    """Only a locally recorded native UUID may resume the current issue lifecycle.

    spec: spec/AI_PRAUTO.md §Quota-pause and resume (same-session, same-agent
    resume; native-less exits restart) and §Agent execution adapters.
    """
    result = _run_bash(
        "\n".join(
            [
                _source_libraries(tmp_path),
                f"record_codex_native_session 192 {READY_TIMESTAMP} {THREAD_ID}",
                f"codex_native_session_anchor_matches 192 {READY_TIMESTAMP} {THREAD_ID}",
                f"! codex_native_session_anchor_matches 192 2026-08-26T00:00:00Z {THREAD_ID}",
                f"! codex_native_session_anchor_matches 193 {READY_TIMESTAMP} {THREAD_ID}",
                f"! codex_native_session_anchor_matches 192 {READY_TIMESTAMP} named-thread",
                f"! record_codex_native_session 192 {READY_TIMESTAMP} named-thread",
            ]
        )
    )

    assert result.returncode == 0, result.stderr
    anchor = tmp_path / "prauto/state/native-sessions/issue-192.json"
    assert json.loads(anchor.read_text()) == {
        "issue_number": "192",
        "ready_timestamp": READY_TIMESTAMP,
        "agent": "codex",
        "thread_id": THREAD_ID,
    }


def test_codex_adapter_uses_default_fresh_and_native_resume_arguments(tmp_path: Path) -> None:
    """Fresh and resume invocations remain distinct Codex-native CLI adapters.

    spec: spec/AI_PRAUTO.md §Agent execution adapters (fresh workspace-write
    with account-default model; resume only JSON/id/prompt) and §Quota-pause
    and resume (trusted resume).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fixture = tmp_path / "codex.jsonl"
    args_file = tmp_path / "codex-args.json"
    _write_jsonl(
        fixture,
        [
            {"type": "thread.started", "thread_id": THREAD_ID},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "complete"}},
        ],
    )
    codex_stub = bin_dir / "codex"
    codex_stub.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" > \"$CODEX_ARGS_FILE\"\n"
        "cat \"$CODEX_FIXTURE\"\n"
    )
    codex_stub.chmod(0o755)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    script = "\n".join(
        [
            _source_libraries(tmp_path),
            f"CUR_SESSION_DIR={shlex.quote(str(session_dir))}",
            "CUR_ISSUE_NUMBER=192",
            f"READY_LABEL_TIMESTAMP={READY_TIMESTAMP}",
            "ACTIVE_AGENT=codex",
            'invoke_agent "-fresh positional prompt" "Claude-only tools" 99 1.23',
            'cp "$CODEX_ARGS_FILE" "$CODEX_ARGS_FILE.fresh"',
            f"PAUSED_SESSION_ID={THREAD_ID}",
            "PAUSED_AGENT=codex",
            "PAUSED_MARKER_AUTHOR=worker",
            "PRAUTO_GITHUB_ACTOR=worker",
            (
                'resume_agent "-resume positional prompt" "Claude-only tools" '
                '99 "$PAUSED_SESSION_ID" 1.23'
            ),
        ]
    )
    result = _run_bash(
        script,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CODEX_ARGS_FILE": str(args_file),
            "CODEX_FIXTURE": str(fixture),
        },
    )

    assert result.returncode == 0, result.stderr
    assert Path(f"{args_file}.fresh").read_text().splitlines() == [
        "exec",
        "--json",
        "--sandbox",
        "workspace-write",
        "--",
        "-fresh positional prompt",
    ]
    assert args_file.read_text().splitlines() == [
        "exec",
        "resume",
        "--json",
        "--",
        THREAD_ID,
        "-resume positional prompt",
    ]
    assert "--max-turns" not in args_file.read_text()
    assert "--max-budget-usd" not in args_file.read_text()
    assert "-m" not in Path(f"{args_file}.fresh").read_text()
    assert "-c" not in Path(f"{args_file}.fresh").read_text()


def test_codex_committed_config_uses_account_default_without_model_flags(tmp_path: Path) -> None:
    """The committed config must not silently turn on a Codex override.

    This sources the actual shared PRauto config rather than relying only on
    an unset test environment, so a future default assignment cannot re-add
    ``-m`` or ``-c`` to ChatGPT-authenticated Codex invocations unnoticed.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fixture = tmp_path / "codex.jsonl"
    args_file = tmp_path / "codex-args.json"
    _write_jsonl(fixture, [{"type": "thread.started", "thread_id": THREAD_ID}])
    codex_stub = bin_dir / "codex"
    codex_stub.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" > \"$CODEX_ARGS_FILE\"\n"
        "cat \"$CODEX_FIXTURE\"\n"
    )
    codex_stub.chmod(0o755)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    result = _run_bash(
        "\n".join(
            [
                "unset PRAUTO_CODEX_MODEL PRAUTO_CODEX_EFFORT",
                f"source {shlex.quote(str(PRAUTO / 'config.env'))}",
                _source_libraries(tmp_path),
                f"CUR_SESSION_DIR={shlex.quote(str(session_dir))}",
                "CUR_ISSUE_NUMBER=192",
                f"READY_LABEL_TIMESTAMP={READY_TIMESTAMP}",
                "ACTIVE_AGENT=codex",
                'invoke_agent "config default prompt" "" 1',
            ]
        ),
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CODEX_ARGS_FILE": str(args_file),
            "CODEX_FIXTURE": str(fixture),
        },
    )

    assert result.returncode == 0, result.stderr
    assert args_file.read_text().splitlines() == [
        "exec", "--json", "--sandbox", "workspace-write", "--", "config default prompt"
    ]


def test_codex_adapter_passes_valid_explicit_model_and_effort_together(tmp_path: Path) -> None:
    """An opt-in override passes its validated formal model and effort exactly."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fixture = tmp_path / "codex.jsonl"
    args_file = tmp_path / "codex-args.json"
    _write_jsonl(fixture, [{"type": "thread.started", "thread_id": THREAD_ID}])
    codex_stub = bin_dir / "codex"
    codex_stub.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" > \"$CODEX_ARGS_FILE\"\n"
        "cat \"$CODEX_FIXTURE\"\n"
    )
    codex_stub.chmod(0o755)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    result = _run_bash(
        "\n".join(
            [
                _source_libraries(tmp_path),
                f"CUR_SESSION_DIR={shlex.quote(str(session_dir))}",
                "CUR_ISSUE_NUMBER=192",
                f"READY_LABEL_TIMESTAMP={READY_TIMESTAMP}",
                "ACTIVE_AGENT=codex",
                'PRAUTO_CODEX_MODEL="gpt-5.6-terra"',
                'PRAUTO_CODEX_EFFORT="xhigh"',
                'invoke_agent "override prompt" "" 1',
            ]
        ),
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CODEX_ARGS_FILE": str(args_file),
            "CODEX_FIXTURE": str(fixture),
        },
    )

    assert result.returncode == 0, result.stderr
    assert args_file.read_text().splitlines() == [
        "exec", "--json", "--sandbox", "workspace-write",
        "-m", "gpt-5.6-terra", "-c", "model_reasoning_effort=xhigh",
        "--", "override prompt",
    ]


@pytest.mark.parametrize(
    ("configured_model", "configured_effort", "case"),
    [
        ("terra", "medium", "bare-alias"),
        ("not-a-codex-model", "medium", "unknown-model"),
        ("gpt-5.6", "unsupported", "invalid-effort"),
        ("", "medium", "effort-only"),
    ],
)
def test_codex_invalid_override_fails_before_invoking_cli(
    tmp_path: Path, configured_model: str, configured_effort: str, case: str
) -> None:
    """Invalid or partial Codex overrides are fail-closed harness errors.

    spec: spec/AI_PRAUTO.md §Agent execution adapters (paired, validated
    Codex model/reasoning-effort override).  No external CLI invocation is
    allowed for an invalid model, invalid effort, or effort-only setting.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    invocation_marker = tmp_path / "codex-invoked"
    codex_stub = bin_dir / "codex"
    codex_stub.write_text(
        '#!/usr/bin/env bash\n'
        'touch "$CODEX_INVOCATION_MARKER"\n'
        'exit 99\n'
    )
    codex_stub.chmod(0o755)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    result = _run_bash(
        "\n".join(
            [
                _source_libraries(tmp_path),
                f"CUR_SESSION_DIR={shlex.quote(str(session_dir))}",
                "CUR_ISSUE_NUMBER=192",
                f"READY_LABEL_TIMESTAMP={READY_TIMESTAMP}",
                "ACTIVE_AGENT=codex",
                f"PRAUTO_CODEX_MODEL={shlex.quote(configured_model)}",
                f"PRAUTO_CODEX_EFFORT={shlex.quote(configured_effort)}",
                'invoke_agent "invalid model prompt" "" 1',
                'jq -cn --arg session "$AGENT_SESSION_ID" --arg status "$AGENT_STATUS" '
                "'{session: $session, status: $status}'",
            ]
        ),
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CODEX_INVOCATION_MARKER": str(invocation_marker),
        },
    )

    assert result.returncode == 0, result.stderr
    assert not invocation_marker.exists(), case
    assert json.loads(result.stdout.splitlines()[-1]) == {"session": "", "status": "error"}


@pytest.mark.parametrize(
    ("anchor_timestamp", "paused_session_id"),
    [
        ("2026-08-24T00:00:00Z", THREAD_ID),
        (READY_TIMESTAMP, "183915b9-9195-4d1a-a748-8712ed7b7b12"),
    ],
    ids=["stale-lifecycle", "mismatched-native-session"],
)
def test_codex_resume_refuses_untrusted_anchor_without_invoking_cli(
    tmp_path: Path, anchor_timestamp: str, paused_session_id: str
) -> None:
    """Stale or mismatched local proof must block a Codex resume before dispatch.

    spec: spec/AI_PRAUTO.md §Quota-pause and resume (same-session native
    identity bound to the current lifecycle) and §Agent execution adapters.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    invocation_marker = tmp_path / "codex-invoked"
    codex_stub = bin_dir / "codex"
    codex_stub.write_text('#!/usr/bin/env bash\ntouch "$CODEX_INVOCATION_MARKER"\nexit 99\n')
    codex_stub.chmod(0o755)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    result = _run_bash(
        "\n".join(
            [
                _source_libraries(tmp_path),
                f"CUR_SESSION_DIR={shlex.quote(str(session_dir))}",
                "CUR_ISSUE_NUMBER=192",
                f"READY_LABEL_TIMESTAMP={READY_TIMESTAMP}",
                f"record_codex_native_session 192 {anchor_timestamp} {THREAD_ID}",
                "ACTIVE_AGENT=codex",
                "PAUSED_AGENT=codex",
                "PAUSED_MARKER_AUTHOR=worker",
                "PRAUTO_GITHUB_ACTOR=worker",
                f"PAUSED_SESSION_ID={paused_session_id}",
                'resume_agent "resume prompt" "" 1 "$PAUSED_SESSION_ID"',
                'jq -cn --arg session "$AGENT_SESSION_ID" --arg status "$AGENT_STATUS" '
                "'{session: $session, status: $status}'",
            ]
        ),
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CODEX_INVOCATION_MARKER": str(invocation_marker),
        },
    )

    assert result.returncode == 0, result.stderr
    assert not invocation_marker.exists()
    assert json.loads(result.stdout.splitlines()[-1]) == {"session": "", "status": "error"}


def test_codex_quota_exit_without_thread_started_is_nonresumable(tmp_path: Path) -> None:
    """A quota failure before Codex emits a native id is restarted, never resumed.

    spec: spec/AI_PRAUTO.md §Quota-pause and resume (native-less Codex exit
    restarts) and §Agent execution adapters (never fabricate an id).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fixture = tmp_path / "quota.jsonl"
    _write_jsonl(fixture, [{"type": "error", "error": {"code": "usage_limit_exceeded"}}])
    codex_stub = bin_dir / "codex"
    codex_stub.write_text('#!/usr/bin/env bash\ncat "$CODEX_FIXTURE"\nexit 1\n')
    codex_stub.chmod(0o755)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    result = _run_bash(
        "\n".join(
            [
                _source_libraries(tmp_path),
                f"CUR_SESSION_DIR={shlex.quote(str(session_dir))}",
                "CUR_ISSUE_NUMBER=192",
                f"READY_LABEL_TIMESTAMP={READY_TIMESTAMP}",
                "ACTIVE_AGENT=codex",
                'PRAUTO_CODEX_MODEL="gpt-5.6"',
                'PRAUTO_CODEX_EFFORT="medium"',
                'invoke_agent "quota prompt" "" 1',
                'jq -cn --arg session "$AGENT_SESSION_ID" --arg status "$AGENT_STATUS" '
                "'{session: $session, status: $status}'",
            ]
        ),
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "CODEX_FIXTURE": str(fixture)},
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {"session": "", "status": "error"}


def test_run_with_timeout_force_kills_term_ignoring_child(tmp_path: Path) -> None:
    """The agent backstop must terminate a child that ignores SIGTERM."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            _source_libraries(tmp_path)
            + "\nrun_with_timeout 1 bash -c 'trap \"\" TERM; while :; do sleep 0.1; done'",
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=6,
    )

    assert result.returncode == 124, result.stderr


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("PRAUTO_WORKFLOW_OUTCOME: COMPLETE\n", True),
        ("report\nPRAUTO_WORKFLOW_OUTCOME: COMPLETE\n", True),
        ("PRAUTO_WORKFLOW_OUTCOME: COMPLETE\ntrailing text\n", False),
        ("PRAUTO_WORKFLOW_OUTCOME: ESCALATED\n", False),
        ("PRAUTO_WORKFLOW_OUTCOME: COMPLETE|ESCALATED\n", False),
        ("", False),
    ],
)
def test_implementation_requires_exact_complete_sentinel(
    tmp_path: Path, output: str, expected: bool
) -> None:
    """A successful exit is not enough to authorize integration or PR creation."""
    result = _run_bash(
        _source_phase_libraries(tmp_path)
        + f"\nimplementation_complete {shlex.quote(output)}",
    )

    assert (result.returncode == 0) is expected, result.stderr


def test_claude_is_error_detection_survives_separate_stderr(tmp_path: Path) -> None:
    """Claude diagnostics must not corrupt the JSON object used for classification."""
    output_file = tmp_path / "claude.json"
    stderr_file = tmp_path / "claude.stderr"
    output_file.write_text(json.dumps({"is_error": True, "terminal_reason": "api_error"}))
    stderr_file.write_text("transient diagnostic\n")
    result = _run_bash(
        "\n".join(
            [
                _source_libraries(tmp_path),
                f"classify_exit {shlex.quote(str(output_file))} 0 claude "
                f"{shlex.quote(str(stderr_file))}",
                'printf "%s" "$AGENT_STATUS"',
            ]
        )
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "error"



@pytest.mark.parametrize(
    ("elapsed_secs", "ceiling_ms", "expected"),
    [
        # At or past the ceiling: the CLI must have terminated the session.
        ("14400", "14400000", True),
        ("20000", "14400000", True),
        ("600", "600000", True),
        # Short of it: a session that ended on its own.
        ("14399", "14400000", False),
        ("5", "14400000", False),
        # 0 means wait indefinitely, so no truncation is possible.
        ("99999", "0", False),
        # A malformed ceiling must not be read as "always truncated".
        ("99999", "4h", False),
    ],
)
def test_cli_truncation_is_classified_from_the_executors_own_clock(
    tmp_path: Path, elapsed_secs: str, ceiling_ms: str, expected: bool
) -> None:
    """A session the CLI killed on its wait ceiling is not a failed attempt.

    spec: spec/AI_PRAUTO.md §Retry tracking — only genuine attempt starts consume
    the retry budget. The CLI exits 0 after terminating a session whose background
    workflow was still running, leaving committed stage work but no sentinel, so
    exit status cannot tell the two apart; the wall-clock span can.
    """
    result = _run_bash(
        "\n".join(
            [
                _source_phase_libraries(tmp_path),
                f"PRAUTO_CLAUDE_PRINT_BG_WAIT_CEILING_MS={shlex.quote(ceiling_ms)}",
                f"implementation_truncated_by_agent_cli {shlex.quote(elapsed_secs)}",
            ]
        )
    )

    assert (result.returncode == 0) is expected, result.stderr


@pytest.mark.parametrize(
    "agent_authored",
    [
        "Background tasks still running after 600s; terminating.",
        "My report:\nBackground tasks still running after 600s; terminating.\nDone.",
        "  Background tasks still running after 600s; terminating.",
    ],
)
def test_agent_authored_text_cannot_claim_a_cli_truncation(
    tmp_path: Path, agent_authored: str
) -> None:
    """The worker must not be able to refund its own retry by what it writes.

    spec: spec/AI_PRAUTO.md §Retry tracking; §Prauto executes unreviewed branch
    code. The worker runs branch code as the executor's own OS user, so its report
    AND the stderr sidecar under the state tree are both reachable to it. A refund
    keyed on either nets every dispatch to zero: PRAUTO_MAX_RETRIES_PER_JOB is
    never reached and the issue loops forever holding an open-issue slot.

    Drives the real call site with a short elapsed span, so only a classification
    that reads the executor's own clock declines the refund.
    """
    result = _run_bash(
        "\n".join(
            [
                _source_phase_libraries(tmp_path),
                "PRAUTO_CLAUDE_PRINT_BG_WAIT_CEILING_MS=14400000",
                "has_quota_paused_comment() { return 1; }",
                "checkpoint_branch() { :; }",
                f"run_implementation() {{ IMPL_RESULT={shlex.quote(agent_authored)}; "
                f"IMPL_STDERR={shlex.quote(agent_authored)}; "
                'IMPL_OUTPUT="$IMPL_RESULT"; IMPL_ELAPSED_SECS=12; AGENT_STATUS=ok; }',
                'refund_retry_count() { printf "REFUNDED\\n"; }',
                "implement_and_finalize 182 branch plan title",
            ]
        )
    )

    assert result.returncode == 0, result.stderr
    assert "REFUNDED" not in result.stdout
    assert "no valid COMPLETE outcome" in result.stdout + result.stderr


def test_a_real_cli_truncation_refunds_the_attempt(tmp_path: Path) -> None:
    """A genuine wait-ceiling kill is classified as a harness fault and refunded.

    spec: spec/AI_PRAUTO.md §Retry tracking — the CLI can terminate its own session
    before the workflow emits its sentinel, even though the stages that already ran
    committed their work. That is not the worker's failure.

    Same call site as the adversarial case, differing only in the executor-measured
    elapsed span, so the two together pin which argument the call site passes.
    """
    result = _run_bash(
        "\n".join(
            [
                _source_phase_libraries(tmp_path),
                "PRAUTO_CLAUDE_PRINT_BG_WAIT_CEILING_MS=14400000",
                "has_quota_paused_comment() { return 1; }",
                "checkpoint_branch() { :; }",
                'run_implementation() { IMPL_RESULT="a partial report with no sentinel"; '
                'IMPL_STDERR=""; IMPL_OUTPUT="$IMPL_RESULT"; '
                "IMPL_ELAPSED_SECS=14400; AGENT_STATUS=ok; }",
                'refund_retry_count() { printf "REFUNDED\\n"; }',
                "implement_and_finalize 182 branch plan title",
            ]
        )
    )

    assert result.returncode == 0, result.stderr
    assert "REFUNDED" in result.stdout


def test_a_complete_sentinel_survives_a_benign_stderr_line(tmp_path: Path) -> None:
    """The sentinel is read from the agent's answer, not the stderr-merged output.

    spec: spec/AI_PRAUTO.md §The implementation phase runs the AGENTS.md workflow —
    the parent requires the exact COMPLETE sentinel before integration or PR
    finalization. AGENT_OUTPUT carries the session's stderr appended after the
    answer, so reading the merged text would score a genuinely complete workflow as
    a failure. Drives the call site, not just the predicate.
    """
    impl_result = "stage reports\nPRAUTO_WORKFLOW_OUTCOME: COMPLETE"
    impl_stderr = "a benign runtime diagnostic"
    result = _run_bash(
        "\n".join(
            [
                _source_phase_libraries(tmp_path),
                f"R={shlex.quote(impl_result)}",
                f"E={shlex.quote(impl_stderr)}",
                f"M={shlex.quote(impl_result + chr(10) + impl_stderr)}",
                "has_quota_paused_comment() { return 1; }",
                "checkpoint_branch() { :; }",
                'run_implementation() { IMPL_RESULT="$R"; IMPL_STDERR="$E"; '
                'IMPL_OUTPUT="$M"; IMPL_ELAPSED_SECS=12; AGENT_STATUS=ok; }',
                # Past the COMPLETE gate the next step is the clean-worktree check;
                # stub git so it reports clean, then mark the step after it.
                'git() { if [[ "$1" == status ]]; then printf ""; else command git "$@"; fi; }',
                'run_pre_pr_selected_verification() { printf "PAST_COMPLETE_GATE\\n"; return 1; }',
                "implement_and_finalize 182 branch plan title || true",
            ]
        )
    )

    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    assert "no valid COMPLETE outcome" not in combined, combined
    assert "PAST_COMPLETE_GATE" in result.stdout, combined



