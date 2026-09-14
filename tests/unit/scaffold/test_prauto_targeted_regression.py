"""Hermetic tests for PRauto's one-full-run, targeted-retry regression gate.

Traceability is to the scaffold-internal contract in `.prauto/lib/phases.sh`
(`run_post_pr_regression`, `validate_targeted_verification`, and
`require_pushed_head`) and `spec/AI_PRAUTO.md`'s post-PR regression policy.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
PHASES = ROOT / ".prauto/lib/phases.sh"
HELPERS = ROOT / ".prauto/lib/helpers.sh"


def _run(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    run_env.update(env or {})
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        check=False,
        text=True,
        env=run_env,
    )


def _source_phases() -> str:
    return "\n".join(
        [
            f"PRAUTO_DIR={shlex.quote(str(ROOT / '.prauto'))}",
            f"source {shlex.quote(str(HELPERS))}",
            f"source {shlex.quote(str(PHASES))}",
        ]
    )


def _control_flow_harness(*, initial_failure: bool, targeted_exit: int = 0) -> str:
    """Replace effects with event logging while retaining the gate's branching."""
    local_exit = 1 if initial_failure else 0
    initial_state = (
        'POST_PR_FAILED_STAGES="Static (ruff)"; POST_PR_FAILURE_EVIDENCE="ruff output"'
        if initial_failure
        else ":"
    )
    # targeted_exit == 2 mirrors run_targeted_post_pr_regression's own
    # infrastructure-block path (e.g. "no recorded failed stages", a pushed-head
    # mismatch, or an E2E setup failure): the real function calls
    # regression_blocked itself and never records a targeted pass/failure.
    targeted_body = (
        'regression_blocked "$1" "targeted regression could not be verified" "$2"'
        if targeted_exit == 2
        else 'POST_PR_TARGETED_PASSES="Static (ruff)"; POST_PR_TARGETED_FAILURES="Static (ruff)"'
    )
    return _source_phases() + f"""
    branch_is_code_affecting() {{ return 0; }}
    run_static_and_unit_regression() {{
      echo static >> "$EVENTS"
      LOCAL_REGRESSION_EXIT={local_exit}
      {initial_state}
    }}
    run_full_cluster_regression() {{ echo full-cluster >> "$EVENTS"; CLUSTER_REGRESSION_EXIT=0; }}
    post_post_pr_regression_comment() {{ printf 'comment:%s\\n' "$2" >> "$EVENTS"; return 0; }}
    git() {{ [[ "$1" == status ]] && return 0; command git "$@"; }}
    run_integration_fix_session() {{
      echo agent:$3 >> "$EVENTS"
      AGENT_STATUS=ok
      AGENT_OUTPUT=attested
    }}
    validate_targeted_verification() {{ [[ "$1" == attested ]]; }}
    checkpoint_branch() {{ echo checkpoint >> "$EVENTS"; }}
    push_branch() {{ echo push >> "$EVENTS"; }}
    create_or_update_pr() {{ echo update-pr >> "$EVENTS"; }}
    require_pushed_head() {{ echo exact-head >> "$EVENTS"; return 0; }}
    run_targeted_post_pr_regression() {{
      echo targeted >> "$EVENTS"
      TARGETED_REGRESSION_EXIT={targeted_exit}
      {targeted_body}
    }}
    regression_set_wip() {{ echo wip >> "$EVENTS"; return 0; }}
    regression_blocked() {{ echo blocked:$2 >> "$EVENTS"; return 0; }}
    regression_ready() {{ echo ready >> "$EVENTS"; return 0; }}
    set_pr_review_label() {{ echo review-label >> "$EVENTS"; return 0; }}
    run_post_pr_regression 179 prauto/I-179
    """


def test_direct_full_regression_pass_skips_agent_and_targeted_retry(tmp_path: Path) -> None:
    events = tmp_path / "events"
    result = _run(_control_flow_harness(initial_failure=False), env={"EVENTS": str(events)})

    assert result.returncode == 0, result.stderr
    assert events.read_text().splitlines() == [
        "static",
        "full-cluster",
        "comment:Full post-PR regression passed for the current pushed PR head.",
    ]


def test_initial_failure_uses_one_targeted_retry_without_second_full_regression(
    tmp_path: Path,
) -> None:
    events = tmp_path / "events"
    result = _run(_control_flow_harness(initial_failure=True), env={"EVENTS": str(events)})

    assert result.returncode == 0, result.stderr
    lines = events.read_text().splitlines()
    assert lines.count("full-cluster") == 1
    assert "agent:Static (ruff)" in lines
    assert "targeted" in lines
    assert "exact-head" in lines
    assert any("Initial full regression failures: Static (ruff)." in line for line in lines)
    assert any(
        "Targeted retry passed on the current pushed PR head: Static (ruff)." in line
        for line in lines
    )
    assert any("No second full regression was run." in line for line in lines)


@pytest.mark.parametrize(
    ("attestation", "revision", "expected"),
    [
        ("agent prose only", "a" * 40, 1),
        (
            'PRAUTO_TARGETED_VERIFICATION_JSON: {"revision":"' + "b" * 40
            + '","stages":[{"name":"Static (ruff)","outcome":"pass","evidence_ref":"ruff"}]}',
            "a" * 40,
            1,
        ),
        (
            'PRAUTO_TARGETED_VERIFICATION_JSON: {"revision":"' + "a" * 40
            + '","stages":[{"name":"Static (ruff)","outcome":"fail","evidence_ref":"ruff"}]}',
            "a" * 40,
            1,
        ),
        (
            'PRAUTO_TARGETED_VERIFICATION_JSON: {"revision":"' + "a" * 40
            + '","stages":[{"name":"Static (ruff)","outcome":"pass","evidence_ref":"ruff"}]}',
            "a" * 40,
            0,
        ),
    ],
)
def test_targeted_verification_rejects_missing_invalid_or_wrong_head_attestation(
    attestation: str, revision: str, expected: int
) -> None:
    script = _source_phases() + "\nPOST_PR_FAILED_STAGES='Static (ruff)'" + (
        f"\nvalidate_targeted_verification {shlex.quote(attestation)} {shlex.quote(revision)}"
    )
    result = _run(script)

    assert result.returncode == expected, result.stderr


def test_require_pushed_head_rejects_remote_head_mismatch(tmp_path: Path) -> None:
    git = tmp_path / "git"
    git.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ $1 == rev-parse ]]; then\n"
        "  printf '%040d\\n' 1\n"
        "else\n"
        "  printf '%040d refs/heads/prauto/I-179\\n' 2\n"
        "fi\n"
    )
    git.chmod(0o755)
    result = _run(
        _source_phases() + "\nrequire_pushed_head prauto/I-179",
        env={"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
    )

    assert result.returncode == 1
    assert "local HEAD and origin/prauto/I-179 do not match" in result.stdout


def test_targeted_failure_keeps_pr_wip_with_code_failure_report(tmp_path: Path) -> None:
    """spec: AI_PRAUTO.md §Stage 5 -- Full regression and targeted-retry readiness gate
    (post-PR) -- 'If executor-targeted verification exposes a new branch-attributable
    failing stage, it reports that failure explicitly and leaves the PR in prauto:wip.'"""
    events = tmp_path / "events"
    result = _run(
        _control_flow_harness(initial_failure=True, targeted_exit=1),
        env={"EVENTS": str(events)},
    )

    assert result.returncode == 1
    lines = events.read_text().splitlines()
    assert "wip" in lines
    assert lines.count("full-cluster") == 1
    assert any("Targeted retry still failed: Static (ruff)." in line for line in lines)
    assert any("Sanitized executor evidence" in line for line in lines)


def test_targeted_infrastructure_block_keeps_pr_wip_without_code_failure_report(
    tmp_path: Path,
) -> None:
    """spec: AI_PRAUTO.md §Stage 5 -- Full regression and targeted-retry readiness
    gate (post-PR) -- 'A targeted retry with no recorded failed stages is
    infrastructure-blocked, is never readiness success, and never applies
    prauto:review.' (See also the infrastructure-blocked paragraph in
    §Deterministic environmental-flake exception: 'A provisioning, health-check,
    lock, or local setup failure is infrastructure-blocked: the executor posts a
    distinct brief blocked comment, leaves the issue and PR in prauto:wip, and
    retries the blocked regression or targeted stage on a later heartbeat.') A
    blocked targeted retry is never reported as a "still failed" code result, and
    posts no success comment either."""
    events = tmp_path / "events"
    result = _run(
        _control_flow_harness(initial_failure=True, targeted_exit=2),
        env={"EVENTS": str(events)},
    )

    assert result.returncode == 1
    lines = events.read_text().splitlines()
    assert lines.count("full-cluster") == 1
    assert any(line.startswith("blocked:") for line in lines)
    # An infrastructure-blocked targeted retry is "never readiness success, and
    # never applies prauto:review" (spec §Stage 5): regression_ready (which gates
    # set_pr_review_label) must never run on this path, nor may the "still failed"
    # code-failure report or a targeted-retry success comment.
    assert "ready" not in lines
    assert "review-label" not in lines
    assert not any("Targeted retry" in line for line in lines)
    assert not any("Full post-PR regression passed" in line for line in lines)
    assert any(line.startswith("comment:") for line in lines)
