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


def test_codex_adapter_uses_only_native_fresh_and_resume_arguments(tmp_path: Path) -> None:
    """Fresh and resume invocations remain distinct Codex-native CLI adapters.

    spec: spec/AI_PRAUTO.md §Agent execution adapters (fresh workspace-write;
    resume only JSON/id/prompt) and §Quota-pause and resume (trusted resume).
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
            'invoke_agent "fresh prompt" "Claude-only tools" 99 1.23',
            'cp "$CODEX_ARGS_FILE" "$CODEX_ARGS_FILE.fresh"',
            f"PAUSED_SESSION_ID={THREAD_ID}",
            "PAUSED_AGENT=codex",
            "PAUSED_MARKER_AUTHOR=worker",
            "PRAUTO_GITHUB_ACTOR=worker",
            'resume_agent "resume prompt" "Claude-only tools" 99 "$PAUSED_SESSION_ID" 1.23',
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
        "fresh prompt",
    ]
    assert args_file.read_text().splitlines() == [
        "exec",
        "resume",
        "--json",
        THREAD_ID,
        "resume prompt",
    ]
    assert "--max-turns" not in args_file.read_text()
    assert "--max-budget-usd" not in args_file.read_text()


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
                'invoke_agent "quota prompt" "" 1',
                'jq -cn --arg session "$AGENT_SESSION_ID" --arg status "$AGENT_STATUS" '
                "'{session: $session, status: $status}'",
            ]
        ),
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "CODEX_FIXTURE": str(fixture)},
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {"session": "", "status": "error"}
