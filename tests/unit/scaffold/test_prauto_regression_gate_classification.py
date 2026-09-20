"""Hermetic tests for prauto's post-PR regression gate: infrastructure-blocked
health aborts, the deterministic environmental-flake classifier, deployed-artifact
binding for recorded passes, the post-stage probe, the failed-test listing, and
public-comment secret scrubbing.

Traceability is to `.prauto/lib/phases.sh` / `.prauto/lib/pr.sh` and
`spec/AI_PRAUTO.md`'s "Stage 5 -- Full regression and targeted-retry readiness gate
(post-PR)" and "Deterministic environmental-flake exception" sections.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
PHASES = ROOT / ".prauto/lib/phases.sh"
PR = ROOT / ".prauto/lib/pr.sh"
HELPERS = ROOT / ".prauto/lib/helpers.sh"
HEARTBEAT = ROOT / ".prauto/heartbeat.sh"

# The two session-start health-gate abort shapes is_cluster_health_abort() matches
# (spec: AI_PRAUTO.md §Deterministic environmental-flake exception, "aborts before
# any test runs is likewise infrastructure-blocked").
HEALTH_ABORT_EXIT_1 = (
    "E   Failed: helm-charts/bin/health-check.sh failed (exit 1). Integration "
    "tests would fail misleadingly against a broken cluster."
)
HEALTH_ABORT_TIMEOUT = (
    "FAILED tests/integration/api_wired/test_a.py::test_x - "
    "Failed: helm-charts/bin/health-check.sh did not finish within 300s"
)

# A single connection-refused pytest failure line, reused across the failed-test
# listing tests (item 6) that don't care about the flake classifier itself.
CONNECT_ERROR_PING_FAILURE = (
    "FAILED tests/integration/spot/test_health.py::test_ping - "
    "httpx.ConnectError: All connection attempts failed"
)


def _run(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    run_env.update(env or {})
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        check=False,
        text=True,
        env=run_env,
        cwd=ROOT,
    )


def _source_phases() -> str:
    """helpers.sh + phases.sh only, matching test_prauto_targeted_regression.py."""
    return "\n".join(
        [
            f"PRAUTO_DIR={shlex.quote(str(ROOT / '.prauto'))}",
            f"source {shlex.quote(str(HELPERS))}",
            f"source {shlex.quote(str(PHASES))}",
        ]
    )


def _source_all() -> str:
    """helpers.sh + pr.sh + phases.sh, needed by anything touching scrub_secrets,
    extract_failed_tests, or post_post_pr_regression_comment."""
    return "\n".join(
        [
            f"PRAUTO_DIR={shlex.quote(str(ROOT / '.prauto'))}",
            f"source {shlex.quote(str(HELPERS))}",
            f"source {shlex.quote(str(PR))}",
            f"source {shlex.quote(str(PHASES))}",
        ]
    )


def _init_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo_dir, check=True)


def _commit_all(repo_dir: Path, message: str) -> str:
    subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo_dir, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True
    ).stdout.strip()


# ---------------------------------------------------------------------------
# 2. Health-gate abort -> infrastructure-blocked
# ---------------------------------------------------------------------------


def test_is_cluster_health_abort_handles_large_output_under_pipefail(tmp_path: Path) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception -- 'A spot or
    api-wired stage whose integration session-start health gate (require_server in
    tests/integration/conftest.py) aborts before any test runs is likewise
    infrastructure-blocked.' Direct call, under `set -euo pipefail` (heartbeat.sh's
    own mode), on a >1MB variable with the abort marker at the start: the guard
    comment on `is_cluster_health_abort` claims a here-string (not a pipe) avoids a
    pipefail/SIGPIPE false negative on a large writer; this only proves anything
    when pipefail is actually active."""
    output_file = tmp_path / "big.out"
    output_file.write_text(HEALTH_ABORT_EXIT_1 + "\n" + ("x" * (1024 * 1024 + 10)))
    script = (
        "set -euo pipefail\n"
        + _source_phases()
        + f"""
    OUTPUT=$(cat {shlex.quote(str(output_file))})
    is_cluster_health_abort "$OUTPUT"
    printf 'rc=%s\\n' "$?"
    """
    )
    result = _run(script)
    assert result.returncode == 0, result.stderr
    assert "rc=0" in result.stdout


def test_full_cluster_regression_spot_health_abort_is_infrastructure_blocked(
    tmp_path: Path,
) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception -- 'A spot or
    api-wired stage whose integration session-start health gate (require_server in
    tests/integration/conftest.py) aborts before any test runs is likewise
    infrastructure-blocked -- never branch-attributable or a flake.' Uses a >1MB
    output with the abort marker at the very start, run under `set -euo pipefail`
    (matching heartbeat.sh's own execution mode) so the here-string (not piped)
    grep is actually exercised under the pipefail setting it is meant to guard
    against, rather than under a lenient default shell mode."""
    abort_file = tmp_path / "spot_abort.out"
    abort_file.write_text(HEALTH_ABORT_EXIT_1 + "\n" + ("x" * (1024 * 1024 + 10)))
    events = tmp_path / "events"

    script = (
        "set -euo pipefail\n"
        + _source_phases()
        + f"""
    POST_PR_REGRESSION_SUMMARY_MODE=true
    deploy_branch_api() {{ echo deploy-api >> "$EVENTS"; return 0; }}
    deploy_branch_frontend() {{ echo deploy-frontend >> "$EVENTS"; return 0; }}
    acquire_required_dev_lock() {{
      echo acquire >> "$EVENTS"
      REQUIRED_LOCK_OWNER=prauto-test
      DEV_ENV_FILE=/dev/null
      DEV_LOCK_URL=http://example.invalid
      return 0
    }}
    release_required_dev_lock() {{ echo release >> "$EVENTS"; REQUIRED_LOCK_OWNER=""; return 0; }}
    regression_blocked() {{ echo "blocked:$2" >> "$EVENTS"; return 0; }}
    with_dev_env() {{
      shift
      case "$*" in
        *tests/integration/spot*) cat {shlex.quote(str(abort_file))}; return 1 ;;
        *) return 1 ;;
      esac
    }}
    run_full_cluster_regression 179 prauto/I-179
    printf 'CLUSTER_REGRESSION_EXIT=%s\\n' "$CLUSTER_REGRESSION_EXIT" >> "$EVENTS"
    """
    )
    result = _run(script, env={"EVENTS": str(events)})

    assert result.returncode == 0, result.stderr
    lines = events.read_text().splitlines()
    assert lines[:3] == ["acquire", "deploy-api", "release"]
    assert any(
        line.startswith("blocked:") and "Integration (spot)" in line for line in lines
    )
    assert "CLUSTER_REGRESSION_EXIT=2" in lines
    # No frontend deploy or E2E stage is reached once the spot stage aborts.
    assert "deploy-frontend" not in lines


def test_full_cluster_regression_api_wired_health_abort_is_infrastructure_blocked(
    tmp_path: Path,
) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception -- same
    infrastructure-blocked guarantee when the *second* integration group (api-wired,
    after spot already passed) hits the health-gate abort."""
    events = tmp_path / "events"
    script = _source_phases() + """
    POST_PR_REGRESSION_SUMMARY_MODE=true
    deploy_branch_api() { echo deploy-api >> "$EVENTS"; return 0; }
    acquire_required_dev_lock() {
      echo acquire >> "$EVENTS"
      REQUIRED_LOCK_OWNER=prauto-test
      DEV_LOCK_URL=http://example.invalid
      return 0
    }
    release_required_dev_lock() { echo release >> "$EVENTS"; REQUIRED_LOCK_OWNER=""; return 0; }
    regression_blocked() { echo "blocked:$2" >> "$EVENTS"; return 0; }
    with_dev_env() {
      shift
      case "$*" in
        *tests/integration/spot*) printf '1 passed in 0.01s\\n'; return 0 ;;
        *tests/integration/api_wired*) printf '%s\\n' "$ABORT_MSG"; return 1 ;;
        *) return 1 ;;
      esac
    }
    run_full_cluster_regression 179 prauto/I-179
    printf 'CLUSTER_REGRESSION_EXIT=%s\\n' "$CLUSTER_REGRESSION_EXIT" >> "$EVENTS"
    """
    result = _run(script, env={"EVENTS": str(events), "ABORT_MSG": HEALTH_ABORT_TIMEOUT})

    assert result.returncode == 0, result.stderr
    lines = events.read_text().splitlines()
    assert lines[:3] == ["acquire", "deploy-api", "release"]
    assert any(
        line.startswith("blocked:") and "Integration (api-wired)" in line for line in lines
    )
    assert "CLUSTER_REGRESSION_EXIT=2" in lines


def test_targeted_retry_integration_health_abort_is_infrastructure_blocked(
    tmp_path: Path,
) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception -- the same
    infrastructure-block rule applies to the targeted retry's own re-run of a
    recorded failed cluster stage, not only the initial full regression."""
    events = tmp_path / "events"
    script = _source_phases() + """
    acquire_required_dev_lock() {
      echo acquire >> "$EVENTS"
      REQUIRED_LOCK_OWNER=prauto-test
      DEV_LOCK_URL=http://example.invalid
      return 0
    }
    release_required_dev_lock() { echo release >> "$EVENTS"; REQUIRED_LOCK_OWNER=""; return 0; }
    regression_blocked() { echo "blocked:$2" >> "$EVENTS"; return 0; }
    deploy_branch_api() { echo deploy-api >> "$EVENTS"; return 0; }
    require_pushed_head() { return 0; }
    with_dev_env() {
      shift
      case "$*" in
        *tests/integration/spot*) printf '%s\\n' "$ABORT_MSG"; return 1 ;;
        *) return 1 ;;
      esac
    }
    POST_PR_FAILED_STAGES="Integration (spot)"
    run_targeted_post_pr_regression 179 prauto/I-179
    printf 'TARGETED_REGRESSION_EXIT=%s\\n' "$TARGETED_REGRESSION_EXIT" >> "$EVENTS"
    """
    result = _run(script, env={"EVENTS": str(events), "ABORT_MSG": HEALTH_ABORT_EXIT_1})

    assert result.returncode == 0, result.stderr
    lines = events.read_text().splitlines()
    assert lines[:3] == ["acquire", "deploy-api", "release"]
    assert any(
        line.startswith("blocked:") and "Integration (spot)" in line for line in lines
    )
    assert "TARGETED_REGRESSION_EXIT=2" in lines


def test_post_pr_regression_health_abort_never_dispatches_fix_session_or_comment(
    tmp_path: Path,
) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception -- a
    session-start health-gate abort 'dispatches no coding agent.' Drives the whole
    run_post_pr_regression entry point (not just run_full_cluster_regression) so the
    wiring between the two is covered too."""
    abort_file = tmp_path / "abort.out"
    abort_file.write_text(HEALTH_ABORT_EXIT_1 + "\n")
    events = tmp_path / "events"
    script = _source_phases() + f"""
    run_static_and_unit_regression() {{ echo static >> "$EVENTS"; LOCAL_REGRESSION_EXIT=0; }}
    deploy_branch_api() {{ echo deploy-api >> "$EVENTS"; return 0; }}
    deploy_branch_frontend() {{ echo deploy-frontend >> "$EVENTS"; return 0; }}
    acquire_required_dev_lock() {{
      echo acquire >> "$EVENTS"
      REQUIRED_LOCK_OWNER=prauto-test
      DEV_LOCK_URL=http://example.invalid
      return 0
    }}
    release_required_dev_lock() {{ echo release >> "$EVENTS"; REQUIRED_LOCK_OWNER=""; return 0; }}
    regression_blocked() {{ echo "blocked:$2" >> "$EVENTS"; return 0; }}
    post_post_pr_regression_comment() {{ echo "comment:$2" >> "$EVENTS"; return 0; }}
    run_integration_fix_session() {{
      echo agent-dispatched >> "$EVENTS"
      AGENT_STATUS=ok
      AGENT_OUTPUT=attested
    }}
    with_dev_env() {{
      shift
      case "$*" in
        *tests/integration/spot*) cat {shlex.quote(str(abort_file))}; return 1 ;;
        *) return 1 ;;
      esac
    }}
    run_post_pr_regression 179 prauto/I-179
    printf 'rc=%s\\n' "$?" >> "$EVENTS"
    """
    result = _run(script, env={"EVENTS": str(events)})

    assert result.returncode == 0, result.stderr
    lines = events.read_text().splitlines()
    assert "static" in lines
    assert any(line.startswith("blocked:") for line in lines)
    assert "release" in lines
    assert "rc=1" in lines
    assert not any(line.startswith("agent") for line in lines)
    assert not any(line.startswith("comment:") for line in lines)


# ---------------------------------------------------------------------------
# 3. Zero recorded failed stages
# ---------------------------------------------------------------------------


def test_targeted_regression_with_zero_recorded_failed_stages_is_blocked(
    tmp_path: Path,
) -> None:
    """spec: AI_PRAUTO.md §Stage 5 -- Full regression and targeted-retry readiness
    gate (post-PR) -- 'A targeted retry with no recorded failed stages is
    infrastructure-blocked, is never readiness success, and never applies
    prauto:review.'"""
    events = tmp_path / "events"
    script = _source_phases() + """
    regression_blocked() { echo "blocked:$2" >> "$EVENTS"; return 0; }
    require_pushed_head() { echo should-not-run >> "$EVENTS"; return 0; }
    post_post_pr_regression_comment() { echo "comment:$2" >> "$EVENTS"; return 0; }
    POST_PR_FAILED_STAGES=""
    run_targeted_post_pr_regression 179 prauto/I-179
    printf 'rc=%s exit=%s\\n' "$?" "$TARGETED_REGRESSION_EXIT" >> "$EVENTS"
    """
    result = _run(script, env={"EVENTS": str(events)})

    assert result.returncode == 0, result.stderr
    lines = events.read_text().splitlines()
    assert any(
        line.startswith("blocked:") and "no recorded failed stages" in line for line in lines
    )
    assert "rc=0 exit=2" in lines
    assert "should-not-run" not in lines
    assert not any(line.startswith("comment:") for line in lines)


def test_validate_targeted_verification_rejects_empty_expected_stage_set() -> None:
    """spec: AI_PRAUTO.md §Stage 5 -- Full regression and targeted-retry readiness
    gate (post-PR) -- a retry is only meaningful against recorded failed stages; a
    well-formed zero-stage attestation still verifies nothing."""
    revision = "a" * 40
    attestation = 'PRAUTO_TARGETED_VERIFICATION_JSON: {"revision":"' + revision + '","stages":[]}'
    script = (
        _source_phases()
        + "\nPOST_PR_FAILED_STAGES=''"
        + f"\nvalidate_targeted_verification {shlex.quote(attestation)} {shlex.quote(revision)}"
    )
    result = _run(script)

    assert result.returncode == 1, result.stderr


# ---------------------------------------------------------------------------
# 4. Flake classification (spec conditions 2, 4, 5)
# ---------------------------------------------------------------------------


def _flake_classification_script(
    *, head: str, recorded_sha: str | None, recorded_stage: str = "Integration (spot)"
) -> str:
    lines = [_source_phases(), f"POST_PR_REGRESSION_HEAD={shlex.quote(head)}"]
    if recorded_sha is not None:
        lines.append(
            f"record_heartbeat_stage_pass {shlex.quote(recorded_stage)} {shlex.quote(recorded_sha)}"
        )
    lines.append('OUTPUT=$(cat "$OUTPUT_FILE")')
    lines.append(f'record_post_pr_flake_classification {shlex.quote(recorded_stage)} "$OUTPUT"')
    lines.append(
        'printf "rc=%s flake=[%s] nonflake=[%s]\\n" "$?" '
        '"$POST_PR_FLAKE_STAGES" "$POST_PR_NON_FLAKE_STAGES"'
    )
    return "\n".join(lines)


def _run_flake_case(
    tmp_path: Path,
    *,
    output: str,
    head: str,
    recorded_sha: str | None,
    stage: str = "Integration (spot)",
) -> str:
    output_file = tmp_path / "output.txt"
    output_file.write_text(output)
    script = _flake_classification_script(
        head=head, recorded_sha=recorded_sha, recorded_stage=stage
    )
    result = _run(script, env={"OUTPUT_FILE": str(output_file)})
    assert result.returncode == 0, result.stderr
    return result.stdout


HEAD_SHA = "a" * 40
OTHER_SHA = "b" * 40


def test_flake_all_connect_errors_with_matching_recorded_pass_is_a_flake(tmp_path: Path) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 2
    ('httpx ConnectError, ... ConnectionRefusedError, ... All connection attempts
    failed') and condition 4 ('that same stage passed earlier in the same
    heartbeat')."""
    output = (
        "FAILED tests/integration/spot/test_infra.py::test_health - "
        "httpx.ConnectError: All connection attempts failed\n"
        "FAILED tests/integration/spot/test_infra.py::test_ping - "
        "ConnectionRefusedError: [Errno 61] Connect call failed ('127.0.0.1', 1)\n"
        "=================== 2 failed in 0.23s ===================="
    )
    out = _run_flake_case(tmp_path, output=output, head=HEAD_SHA, recorded_sha=HEAD_SHA)
    assert "rc=0 flake=[Integration (spot)] nonflake=[]" in out


def test_flake_connecterror_plus_unlisted_broken_pipe_is_not_a_flake(tmp_path: Path) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception -- 'The
    allowlist is exhaustive ... any failure outside these conditions remains
    blocking.' Every extracted failure's own reason must match the allowlist
    (`stage_failures_are_transport_flakes` checks each one); one allowlisted
    ConnectError failure does not rescue a second failure ('broken pipe') whose own
    reason carries no allowlisted or disqualifying term, even with a matching
    recorded pass."""
    output = (
        "FAILED tests/integration/spot/test_x.py::test_a - "
        "httpx.ConnectError: All connection attempts failed\n"
        "FAILED tests/integration/spot/test_x.py::test_b - broken pipe\n"
        "=================== 2 failed in 0.10s ===================="
    )
    out = _run_flake_case(tmp_path, output=output, head=HEAD_SHA, recorded_sha=HEAD_SHA)
    assert "rc=1 flake=[] nonflake=[Integration (spot)]" in out


def test_flake_connecterror_plus_no_reason_entry_is_not_a_flake(tmp_path: Path) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception -- every
    extracted failure needs a matching allowlisted reason; a FAILED line with no
    ` - <reason>` suffix parses to an empty reason, which `stage_failures_are_
    transport_flakes` treats as unclassifiable and therefore blocking, even
    alongside an otherwise-allowlisted ConnectError failure and a matching
    recorded pass."""
    output = (
        "FAILED tests/integration/spot/test_x.py::test_a - "
        "httpx.ConnectError: All connection attempts failed\n"
        "FAILED tests/integration/spot/test_x.py::test_c\n"
        "=================== 2 failed in 0.10s ===================="
    )
    out = _run_flake_case(tmp_path, output=output, head=HEAD_SHA, recorded_sha=HEAD_SHA)
    assert "rc=1 flake=[] nonflake=[Integration (spot)]" in out


def test_flake_same_output_without_recorded_pass_is_not_a_flake(tmp_path: Path) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 4
    -- without an earlier same-heartbeat pass at the tested head, an initial full
    regression's condition 4 cannot be satisfied."""
    output = (
        "FAILED tests/integration/spot/test_infra.py::test_health - "
        "httpx.ConnectError: All connection attempts failed\n"
        "=================== 1 failed in 0.23s ===================="
    )
    out = _run_flake_case(tmp_path, output=output, head=HEAD_SHA, recorded_sha=None)
    assert "rc=1 flake=[] nonflake=[Integration (spot)]" in out


def test_flake_recorded_pass_at_different_sha_is_not_a_flake(tmp_path: Path) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 4
    -- a recorded pass is only evidence for the artifact/commit it actually tested."""
    output = (
        "FAILED tests/integration/spot/test_infra.py::test_health - "
        "httpx.ConnectError: All connection attempts failed\n"
        "=================== 1 failed in 0.23s ===================="
    )
    out = _run_flake_case(tmp_path, output=output, head=HEAD_SHA, recorded_sha=OTHER_SHA)
    assert "rc=1 flake=[] nonflake=[Integration (spot)]" in out


def test_flake_assertion_failure_alongside_connect_error_is_not_a_flake(tmp_path: Path) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 5
    -- 'The output contains no test assertion failure ... A first-run api-wired or
    E2E assertion failure is always blocking and is never auto-ignored.'"""
    output = (
        "FAILED tests/integration/spot/test_x.py::test_a - "
        "httpx.ConnectError: All connection attempts failed\n"
        "FAILED tests/integration/spot/test_x.py::test_b - AssertionError: assert 503 == 200\n"
        "=================== 2 failed in 0.10s ===================="
    )
    out = _run_flake_case(tmp_path, output=output, head=HEAD_SHA, recorded_sha=HEAD_SHA)
    assert "rc=1 flake=[] nonflake=[Integration (spot)]" in out


def test_flake_plain_assert_reason_disqualifies_on_its_own(tmp_path: Path) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 5
    -- 'The output contains no test assertion failure.' A plain `assert` embedded in
    the pytest short-summary reason itself (not a separate AssertionError/`E   assert`
    line) is still a test assertion failure and disqualifies the stage, even with a
    matching recorded pass. The reason names an otherwise-allowlisted client
    transport phrase ('Connection refused') and carries no "failed" word and no
    other disqualifying term, so only the ` - assert ` shape itself can be
    responsible for the non-flake result -- removing just that disqualifier
    alternative from the pattern flips this case to a flake."""
    output = (
        "FAILED tests/integration/spot/test_x.py::test_a - "
        "assert 'Connection refused' not in resp.text\n"
        "=================== 1 failed in 0.10s ===================="
    )
    out = _run_flake_case(tmp_path, output=output, head=HEAD_SHA, recorded_sha=HEAD_SHA)
    assert "rc=1 flake=[] nonflake=[Integration (spot)]" in out


def test_flake_traceback_assert_line_alongside_connect_error_summary_is_not_a_flake(
    tmp_path: Path,
) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 5
    -- 'The output contains no test assertion failure.' An `E   assert` traceback
    line disqualifies the whole stage output even when the pytest short-summary
    FAILED line itself names an allowlisted transport error, and even with a
    matching recorded pass."""
    output = (
        "FAILED tests/integration/spot/test_x.py::test_a - "
        "httpx.ConnectError: All connection attempts failed\n"
        "=================== FAILURES ===================\n"
        "tests/integration/spot/test_x.py:10: in test_a\n"
        "    resp = httpx.get(url)\n"
        "E   assert resp.status == 200\n"
        "=================== 1 failed in 0.10s ===================="
    )
    out = _run_flake_case(tmp_path, output=output, head=HEAD_SHA, recorded_sha=HEAD_SHA)
    assert "rc=1 flake=[] nonflake=[Integration (spot)]" in out


def test_flake_bare_503_without_ingress_or_control_plane_source_is_not_a_flake(
    tmp_path: Path,
) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 2
    -- '... an ingress 502, 503, or 504 ...' -- and 'The allowlist is exhaustive: an
    unrecognized message, an ambiguous source for a transport status, or any failure
    outside these conditions remains blocking.' A gateway status with no
    ingress/control-plane source attributed to it in the same reason is ambiguous
    and stays blocking."""
    output = (
        "FAILED tests/integration/spot/test_x.py::test_a - Service unavailable: 503\n"
        "=================== 1 failed in 0.10s ===================="
    )
    out = _run_flake_case(tmp_path, output=output, head=HEAD_SHA, recorded_sha=HEAD_SHA)
    assert "rc=1 flake=[] nonflake=[Integration (spot)]" in out


def test_flake_connecterror_with_bare_503_in_the_same_reason_is_not_a_flake(
    tmp_path: Path,
) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 2
    ('httpx ConnectError ... All connection attempts failed') -- and 'The allowlist
    is exhaustive: an unrecognized message, an ambiguous source for a transport
    status ... remains blocking.' An otherwise-allowlisted client transport phrase
    in the *same* reason as a bare, unsourced gateway status is still blocking: the
    ambiguous status is not rescued by the allowlisted phrase alongside it."""
    output = (
        "FAILED tests/integration/spot/test_x.py::test_a - httpx.ConnectError: "
        "All connection attempts failed after 503 Service Unavailable\n"
        "=================== 1 failed in 0.10s ===================="
    )
    out = _run_flake_case(tmp_path, output=output, head=HEAD_SHA, recorded_sha=HEAD_SHA)
    assert "rc=1 flake=[] nonflake=[Integration (spot)]" in out


def test_flake_ingress_source_in_a_different_failures_reason_does_not_rescue_a_bare_503(
    tmp_path: Path,
) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 2
    ('the configured ingress or DNS client reports ... an ingress 502, 503, or
    504'); and 'The allowlist is exhaustive: an unrecognized message, an ambiguous
    source for a transport status, or any failure outside these conditions remains
    blocking.' plan: item 4 (every extracted failure's own reason must match the
    allowlist). test_a's reason alone (`Bad Gateway from ingress-nginx: 502`)
    carries both an allowlisted gateway status and its ingress source together and
    would, on its own, qualify as a flake; test_b's reason
    (`httpx.ConnectError: All connection attempts failed after 503`) carries an
    otherwise-allowlisted client transport phrase alongside a bare, unsourced 503
    -- ambiguous on its own. The ingress source in test_a's reason does not
    attribute test_b's bare 503 in a *different* failure's reason, so the whole
    stage stays blocking even though one of its two failures would independently
    qualify."""
    output = (
        "FAILED tests/integration/spot/test_x.py::test_a - "
        "Bad Gateway from ingress-nginx: 502\n"
        "FAILED tests/integration/spot/test_x.py::test_b - "
        "httpx.ConnectError: All connection attempts failed after 503\n"
        "=================== 2 failed in 0.10s ===================="
    )
    out = _run_flake_case(tmp_path, output=output, head=HEAD_SHA, recorded_sha=HEAD_SHA)
    assert "rc=1 flake=[] nonflake=[Integration (spot)]" in out


def test_flake_nginx_sourced_502_with_recorded_pass_is_a_flake(tmp_path: Path) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 2
    -- 'the configured ingress or DNS client reports ... an ingress 502, 503, or
    504.'"""
    output = (
        "FAILED tests/integration/spot/test_x.py::test_a - Bad Gateway from ingress-nginx: 502\n"
        "=================== 1 failed in 0.10s ===================="
    )
    out = _run_flake_case(tmp_path, output=output, head=HEAD_SHA, recorded_sha=HEAD_SHA)
    assert "rc=0 flake=[Integration (spot)] nonflake=[]" in out


def test_flake_status_code_404_anywhere_is_not_a_flake(tmp_path: Path) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 5
    -- an 'API contract/schema mismatch, or application response failure' is
    disqualifying regardless of an accompanying transport failure elsewhere in the
    same stage output."""
    output = (
        "FAILED tests/integration/spot/test_x.py::test_a - "
        "httpx.ConnectError: All connection attempts failed\n"
        "FAILED tests/integration/spot/test_x.py::test_b - status_code 404\n"
        "=================== 2 failed in 0.10s ===================="
    )
    out = _run_flake_case(tmp_path, output=output, head=HEAD_SHA, recorded_sha=HEAD_SHA)
    assert "rc=1 flake=[] nonflake=[Integration (spot)]" in out


def test_flake_zero_extracted_failures_is_not_a_flake(tmp_path: Path) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception -- the
    allowlist exception applies to a 'failure in a cluster-dependent stage'; an
    output with no extractable failure entries has nothing to classify as a flake."""
    out = _run_flake_case(
        tmp_path, output="no failures parsed here", head=HEAD_SHA, recorded_sha=HEAD_SHA
    )
    assert "rc=1 flake=[] nonflake=[Integration (spot)]" in out


def test_flake_spec_unlisted_terms_are_not_a_flake(tmp_path: Path) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception -- 'The
    allowlist is exhaustive: an unrecognized message ... remains blocking.'
    'broken pipe' is not among the allowlisted signatures."""
    output = (
        "FAILED tests/integration/spot/test_x.py::test_a - broken pipe\n"
        "=================== 1 failed in 0.10s ===================="
    )
    out = _run_flake_case(tmp_path, output=output, head=HEAD_SHA, recorded_sha=HEAD_SHA)
    assert "rc=1 flake=[] nonflake=[Integration (spot)]" in out


@pytest.mark.parametrize(
    ("stage", "output", "expected"),
    [
        pytest.param(
            "Unit (Python)",
            "FAILED tests/unit/test_x.py::test_a - "
            "httpx.ConnectError: All connection attempts failed\n"
            "=================== 1 failed in 0.10s ====================",
            "rc=1 flake=[] nonflake=[Unit (Python)]",
            id="unit-python-not-cluster-dependent",
        ),
        pytest.param(
            "E2E",
            "  1) [chromium] › some.spec.ts:1:1 › some test ───\n"
            "   Error: connect ECONNREFUSED 10.0.0.1:443\n"
            "1 failed",
            "rc=0 flake=[E2E] nonflake=[]",
            id="e2e-playwright-econnrefused",
        ),
        pytest.param(
            "Integration (api-wired)",
            "FAILED tests/integration/api_wired/test_x.py::test_a - "
            "httpx.ConnectError: All connection attempts failed\n"
            "=================== 1 failed in 0.10s ====================",
            "rc=0 flake=[Integration (api-wired)] nonflake=[]",
            id="integration-api-wired",
        ),
    ],
)
def test_flake_classification_across_stages(
    tmp_path: Path, stage: str, output: str, expected: str
) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 1
    ('The failure is in a cluster-dependent stage: spot integration, api-wired
    integration, or E2E') -- `Unit (Python)` is not cluster-dependent so it is never
    a flake regardless of message content; `E2E` and `Integration (api-wired)` are,
    and each recognizes the allowlisted shapes documented for condition 2."""
    out = _run_flake_case(
        tmp_path, output=output, head=HEAD_SHA, recorded_sha=HEAD_SHA, stage=stage
    )
    assert expected in out


# ---------------------------------------------------------------------------
# 4 (cont'd). Recording is bound to the deployed artifact
# ---------------------------------------------------------------------------


def test_executor_test_head_reflects_a_real_git_repo(tmp_path: Path) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 4
    ('that same stage passed earlier in the same heartbeat'); plan: item 4 (pass
    bound to the clean tested commit and deployed artifact); source:
    .prauto/lib/phases.sh executor_test_head. `executor_test_head` is empty
    whenever the worktree carries any modified or untracked file, since the sha
    would then not describe what actually ran."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / "a.txt").write_text("hi\n")
    sha = _commit_all(repo, "init")

    script = _source_phases() + "\nexecutor_test_head"
    clean_result = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, check=False
    )
    assert clean_result.stdout == sha

    (repo / "untracked.txt").write_text("scratch\n")
    dirty_result = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, check=False
    )
    assert dirty_result.stdout == ""


def _integration_fix_script(*, deployed_api_sha: str, run_integration_groups_body: str) -> str:
    return _source_phases() + f"""
    resolve_dev_env() {{
      DEV_ENV_FILE=/dev/null
      DEV_LOCK_URL=http://example.invalid/lock
      return 0
    }}
    dev_env_healthy() {{ return 0; }}
    curl() {{
      case "$*" in
        *"/status"*) return 0 ;;
        *"/acquire"*) printf '200' ;;
        *) return 0 ;;
      esac
    }}
    gh() {{ return 0; }}
    run_integration_groups() {{
      {run_integration_groups_body}
      INTEG_SPOT_EXIT=0; INTEG_SPOT_OUTPUT=""
      INTEG_API_WIRED_EXIT=0; INTEG_API_WIRED_OUTPUT=""
      INTEG_EXIT=0; INTEG_OUTPUT=""
    }}
    DEPLOYED_API_SHA={shlex.quote(deployed_api_sha)}
    run_integration_test_fix 179 prauto/I-179
    printf 'passes=[%s]\\n' "$HEARTBEAT_STAGE_PASSES"
    """


def test_integration_fix_records_pass_only_when_tested_head_matches_deployed_api_sha(
    tmp_path: Path,
) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 4
    ('that same stage passed earlier in the same heartbeat'); plan: item 4 (pass
    bound to the clean tested commit and deployed artifact); source:
    .prauto/lib/phases.sh run_integration_test_fix. Records a pass only when the
    exact tested commit is the deployed branch API."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / "tests" / "integration" / "spot").mkdir(parents=True)
    (repo / "tests" / "integration" / "api_wired").mkdir(parents=True)
    (repo / "tests" / "integration" / "spot" / ".keep").write_text("")
    (repo / "tests" / "integration" / "api_wired" / ".keep").write_text("")
    sha = _commit_all(repo, "init")

    script = _integration_fix_script(deployed_api_sha=sha, run_integration_groups_body=":")
    result = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert f"Integration (spot)\t{sha}" in result.stdout
    assert f"Integration (api-wired)\t{sha}" in result.stdout


def test_integration_fix_skips_record_when_deployed_api_sha_is_different(
    tmp_path: Path,
) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 4;
    plan: item 4 (pass bound to the clean tested commit and deployed artifact);
    source: .prauto/lib/phases.sh run_integration_test_fix. A clean tree alone is
    not sufficient evidence: the tested commit must also equal the *deployed*
    artifact's commit, not merely be clean."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / "tests" / "integration" / "spot").mkdir(parents=True)
    (repo / "tests" / "integration" / "api_wired").mkdir(parents=True)
    (repo / "tests" / "integration" / "spot" / ".keep").write_text("")
    (repo / "tests" / "integration" / "api_wired" / ".keep").write_text("")
    _commit_all(repo, "init")

    script = _integration_fix_script(
        deployed_api_sha="0" * 40, run_integration_groups_body=":"
    )
    result = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "passes=[]" in result.stdout


def test_integration_fix_skips_record_when_untracked_file_empties_tested_head(
    tmp_path: Path,
) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 4;
    plan: item 4 (pass bound to the clean tested commit and deployed artifact);
    source: .prauto/lib/phases.sh executor_test_head. An untracked file in the
    worktree makes `executor_test_head` empty, so no recorded pass is possible even
    when `DEPLOYED_API_SHA` matches HEAD."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / "tests" / "integration" / "spot").mkdir(parents=True)
    (repo / "tests" / "integration" / "api_wired").mkdir(parents=True)
    (repo / "tests" / "integration" / "spot" / ".keep").write_text("")
    (repo / "tests" / "integration" / "api_wired" / ".keep").write_text("")
    sha = _commit_all(repo, "init")
    (repo / "scratch.txt").write_text("untracked\n")

    script = _integration_fix_script(deployed_api_sha=sha, run_integration_groups_body=":")
    result = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "passes=[]" in result.stdout


def test_integration_fix_skips_record_when_head_moves_during_the_run(tmp_path: Path) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 4;
    plan: item 4 (pass bound to the clean tested commit and deployed artifact);
    source: .prauto/lib/phases.sh executor_test_head/current_head. A recorded pass
    binds to a clean tested commit; if HEAD moves while the integration groups run,
    the commit that was actually tested is no longer current HEAD, so no pass is
    recorded even though DEPLOYED_API_SHA still matches the (now stale)
    tested_head."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / "tests" / "integration" / "spot").mkdir(parents=True)
    (repo / "tests" / "integration" / "api_wired").mkdir(parents=True)
    (repo / "tests" / "integration" / "spot" / ".keep").write_text("")
    (repo / "tests" / "integration" / "api_wired" / ".keep").write_text("")
    sha = _commit_all(repo, "init")

    script = _integration_fix_script(
        deployed_api_sha=sha,
        run_integration_groups_body='git commit -q --allow-empty -m "moved"',
    )
    result = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "passes=[]" in result.stdout


def _stub_install_script(repo: Path, exit_code: int = 0) -> None:
    """Committed (not untracked) install.sh so a clean-tree assertion is honest."""
    install = repo / "helm-charts" / "bin" / "install.sh"
    install.parent.mkdir(parents=True, exist_ok=True)
    install.write_text(f"#!/usr/bin/env bash\nexit {exit_code}\n")
    install.chmod(0o755)


def _stub_cluster_tools(bin_dir: Path) -> None:
    """kubectl/helm/docker stubs outside the repo (never on the git tree)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    for tool in ("kubectl", "helm", "docker"):
        script = bin_dir / tool
        script.write_text("#!/usr/bin/env bash\nexit 0\n")
        script.chmod(0o755)


def test_deploy_branch_api_sets_deployed_api_sha_to_head_on_a_clean_tree(
    tmp_path: Path,
) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 4;
    plan: item 4 (pass bound to the clean tested commit and deployed artifact);
    source: .prauto/lib/phases.sh deploy_branch_api. Hermetic: a temp git repo as
    WORKTREE_DIR with a committed stub install.sh and stubbed kubectl/helm/docker on
    PATH. A successful deploy from a clean tree binds DEPLOYED_API_SHA to HEAD."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _stub_install_script(repo, exit_code=0)
    sha = _commit_all(repo, "init")
    bin_dir = tmp_path / "bin"
    _stub_cluster_tools(bin_dir)

    script = _source_phases() + f"""
    WORKTREE_DIR={shlex.quote(str(repo))}
    deploy_branch_api /dev/null
    printf 'rc=%s api_sha=[%s] fe_sha=[%s]\\n' "$?" "$DEPLOYED_API_SHA" "$DEPLOYED_FRONTEND_SHA"
    """
    run_env = os.environ.copy()
    run_env["PATH"] = f"{bin_dir}{os.pathsep}{run_env.get('PATH', '')}"
    result = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, env=run_env, check=False
    )
    assert result.returncode == 0, result.stderr
    assert f"api_sha=[{sha}]" in result.stdout


def test_deploy_branch_api_leaves_deployed_api_sha_empty_on_a_dirty_tree(
    tmp_path: Path,
) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 4;
    plan: item 4 (pass bound to the clean tested commit and deployed artifact);
    source: .prauto/lib/phases.sh deploy_branch_api. A dirty tree still lets the
    stub deploy 'succeed', but `executor_test_head` is empty, so the binding stays
    empty rather than naming a sha that does not describe what was actually built."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _stub_install_script(repo, exit_code=0)
    _commit_all(repo, "init")
    (repo / "untracked.txt").write_text("scratch\n")
    bin_dir = tmp_path / "bin"
    _stub_cluster_tools(bin_dir)

    script = _source_phases() + f"""
    WORKTREE_DIR={shlex.quote(str(repo))}
    deploy_branch_api /dev/null
    printf 'rc=%s api_sha=[%s]\\n' "$?" "$DEPLOYED_API_SHA"
    """
    run_env = os.environ.copy()
    run_env["PATH"] = f"{bin_dir}{os.pathsep}{run_env.get('PATH', '')}"
    result = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, env=run_env, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "api_sha=[]" in result.stdout


def test_deploy_branch_frontend_failure_clears_deployed_api_sha(tmp_path: Path) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 4;
    plan: item 4 (pass bound to the clean tested commit and deployed artifact);
    source: .prauto/lib/phases.sh deploy_branch_frontend ('The umbrella upgrade also
    rolls the API pod, so a failed frontend deploy clears the API binding as
    well.'). A failing frontend deploy clears a previously-bound DEPLOYED_API_SHA."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _stub_install_script(repo, exit_code=1)
    _commit_all(repo, "init")
    bin_dir = tmp_path / "bin"
    _stub_cluster_tools(bin_dir)

    script = _source_phases() + f"""
    WORKTREE_DIR={shlex.quote(str(repo))}
    DEPLOYED_API_SHA="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    deploy_branch_frontend /dev/null
    printf 'rc=%s api_sha=[%s]\\n' "$?" "$DEPLOYED_API_SHA"
    """
    run_env = os.environ.copy()
    run_env["PATH"] = f"{bin_dir}{os.pathsep}{run_env.get('PATH', '')}"
    result = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, env=run_env, check=False
    )
    assert "api_sha=[]" in result.stdout


def test_deploy_branch_api_clears_deployed_frontend_sha(tmp_path: Path) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 4;
    plan: item 4 (pass bound to the clean tested commit and deployed artifact);
    source: .prauto/lib/phases.sh deploy_branch_api ('An API upgrade removes the
    cluster frontend, so the frontend binding is always cleared.'). Any call to
    deploy_branch_api clears a previously-bound DEPLOYED_FRONTEND_SHA, even on a
    successful API deploy."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _stub_install_script(repo, exit_code=0)
    _commit_all(repo, "init")
    bin_dir = tmp_path / "bin"
    _stub_cluster_tools(bin_dir)

    script = _source_phases() + f"""
    WORKTREE_DIR={shlex.quote(str(repo))}
    DEPLOYED_FRONTEND_SHA="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    deploy_branch_api /dev/null
    printf 'rc=%s fe_sha=[%s]\\n' "$?" "$DEPLOYED_FRONTEND_SHA"
    """
    run_env = os.environ.copy()
    run_env["PATH"] = f"{bin_dir}{os.pathsep}{run_env.get('PATH', '')}"
    result = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, env=run_env, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "fe_sha=[]" in result.stdout


def _e2e_fix_script(
    *, deployed_api_sha: str, deployed_frontend_sha: str, pnpm_test_extra: str = ":"
) -> str:
    return _source_phases() + f"""
    resolve_dev_env() {{
      DEV_ENV_FILE=/dev/null
      DEV_LOCK_URL=http://example.invalid/lock
      return 0
    }}
    dev_env_healthy() {{ return 0; }}
    diff_touches() {{ return 0; }}
    deploy_branch_frontend() {{ return 0; }}
    pnpm() {{
      if [[ "$3" == "test" ]]; then
        {pnpm_test_extra}
      fi
      return 0
    }}
    get_pr_number_for_branch() {{ BRANCH_PR_NUMBER=""; }}
    curl() {{
      case "$*" in
        *"/status"*) return 0 ;;
        *"/acquire"*) printf '200' ;;
        *) return 0 ;;
      esac
    }}
    gh() {{ return 0; }}
    DEPLOYED_API_SHA={shlex.quote(deployed_api_sha)}
    DEPLOYED_FRONTEND_SHA={shlex.quote(deployed_frontend_sha)}
    run_e2e_test_fix 179 prauto/I-179
    printf 'passes=[%s]\\n' "$HEARTBEAT_STAGE_PASSES"
    """


def test_e2e_fix_records_pass_only_when_both_frontend_and_api_sha_match(tmp_path: Path) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 4;
    plan: item 4 (pass bound to the clean tested commit and deployed artifact);
    source: .prauto/lib/phases.sh run_e2e_test_fix. E2E exercises both the deployed
    frontend and the API it calls, so its recorded pass requires the tested commit
    to equal *both* DEPLOYED_FRONTEND_SHA and DEPLOYED_API_SHA."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / "tests" / "e2e").mkdir(parents=True)
    (repo / "tests" / "e2e" / ".keep").write_text("")
    sha = _commit_all(repo, "init")

    script = _e2e_fix_script(deployed_api_sha=sha, deployed_frontend_sha=sha)
    result = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert f"E2E\t{sha}" in result.stdout


def test_e2e_fix_skips_record_when_only_frontend_sha_matches(tmp_path: Path) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 4;
    plan: item 4 (pass bound to the clean tested commit and deployed artifact);
    source: .prauto/lib/phases.sh run_e2e_test_fix. The frontend binding alone is
    not sufficient evidence; an API redeploy (or one that never happened at this
    head) leaves DEPLOYED_API_SHA unmatched and no E2E pass is recorded."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / "tests" / "e2e").mkdir(parents=True)
    (repo / "tests" / "e2e" / ".keep").write_text("")
    sha = _commit_all(repo, "init")

    script = _e2e_fix_script(deployed_api_sha="0" * 40, deployed_frontend_sha=sha)
    result = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "passes=[]" in result.stdout


def test_e2e_fix_skips_record_when_only_api_sha_matches(tmp_path: Path) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 4;
    plan: item 4 (pass bound to the clean tested commit and deployed artifact);
    source: .prauto/lib/phases.sh run_e2e_test_fix. The complementary case: the API
    binding alone is not sufficient evidence either -- a mismatched
    DEPLOYED_FRONTEND_SHA also blocks the recorded pass."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / "tests" / "e2e").mkdir(parents=True)
    (repo / "tests" / "e2e" / ".keep").write_text("")
    sha = _commit_all(repo, "init")

    script = _e2e_fix_script(deployed_api_sha=sha, deployed_frontend_sha="0" * 40)
    result = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "passes=[]" in result.stdout


def test_e2e_fix_skips_record_when_head_moves_during_the_run(tmp_path: Path) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 4;
    plan: item 4 (pass bound to the clean tested commit and deployed artifact);
    source: .prauto/lib/phases.sh run_e2e_test_fix (executor_test_head/current_head).
    The stubbed `pnpm ... test` invocation itself moves HEAD (an empty commit)
    before returning, so the commit actually tested is no longer current HEAD when
    the pass would be recorded -- no pass is recorded even though DEPLOYED_API_SHA
    and DEPLOYED_FRONTEND_SHA both matched the (now stale) tested_head."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / "tests" / "e2e").mkdir(parents=True)
    (repo / "tests" / "e2e" / ".keep").write_text("")
    sha = _commit_all(repo, "init")

    script = _e2e_fix_script(
        deployed_api_sha=sha,
        deployed_frontend_sha=sha,
        pnpm_test_extra='git commit -q --allow-empty -m "moved" >/dev/null 2>&1 || true',
    )
    result = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "passes=[]" in result.stdout


# ---------------------------------------------------------------------------
# 5. Post-stage probe never provisions
# ---------------------------------------------------------------------------


def test_dev_env_probe_healthy_fails_without_repo_dir() -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 3
    -- 'This post-stage check is a probe only -- it never provisions or reinstalls
    the cluster; a failing post-stage probe makes the failure non-flake.' A flake
    needs positive evidence, so a missing checkout fails closed."""
    script = _source_phases() + "\nunset REPO_DIR\ndev_env_probe_healthy /dev/null"
    result = _run(script)
    assert result.returncode == 1, result.stderr


def test_dev_env_probe_healthy_fails_when_health_check_script_is_missing(tmp_path: Path) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 3
    -- same fail-closed guarantee when REPO_DIR is set but health-check.sh itself
    does not exist there."""
    script = (
        _source_phases()
        + f"\nREPO_DIR={shlex.quote(str(tmp_path))}\ndev_env_probe_healthy /dev/null"
    )
    result = _run(script)
    assert result.returncode == 1, result.stderr


def test_dev_env_probe_healthy_never_provisions_on_failure(tmp_path: Path) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 3
    -- an unhealthy post-stage probe must not provision or reinstall the cluster."""
    health_check = tmp_path / "helm-charts" / "bin" / "health-check.sh"
    health_check.parent.mkdir(parents=True)
    health_check.write_text("#!/usr/bin/env bash\nexit 1\n")
    health_check.chmod(0o755)
    events = tmp_path / "events"

    script = _source_phases() + f"""
    REPO_DIR={shlex.quote(str(tmp_path))}
    provision_dev_env() {{ echo provision-called >> "$EVENTS"; return 0; }}
    dev_env_probe_healthy /dev/null
    printf 'rc=%s\\n' "$?"
    """
    result = _run(script, env={"EVENTS": str(events)})
    assert "rc=1" in result.stdout
    assert not events.exists() or "provision-called" not in events.read_text()


def test_post_pr_regression_skips_flake_comment_when_post_stage_probe_fails(
    tmp_path: Path,
) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception, condition 3
    -- a failing post-stage probe makes the failure non-flake, so
    run_post_pr_regression must fall through to the normal failure/fix-loop path
    instead of posting the environmental-flake notice."""
    events = tmp_path / "events"
    script = _source_phases() + """
    run_static_and_unit_regression() { LOCAL_REGRESSION_EXIT=0; }
    run_full_cluster_regression() {
      CLUSTER_REGRESSION_EXIT=1
      POST_PR_FAILED_STAGES="Integration (spot)"
      POST_PR_FLAKE_STAGES="Integration (spot)"
      POST_PR_NON_FLAKE_STAGES=""
      POST_PR_FLAKE_HEALTH_BEFORE=true
      POST_PR_FLAKE_CATEGORIES="Integration (spot): client connection refused/reset/timeout"
    }
    require_pushed_head() { return 0; }
    dev_env_probe_healthy() { echo probe-called >> "$EVENTS"; return 1; }
    checkpoint_branch() { echo checkpoint >> "$EVENTS"; }
    post_post_pr_regression_comment() { echo "comment:$2" >> "$EVENTS"; return 0; }
    run_integration_fix_session() {
      echo agent-dispatched >> "$EVENTS"
      AGENT_STATUS=quota
      ACTIVE_AGENT=claude
    }
    run_post_pr_regression 179 prauto/I-179
    printf 'rc=%s\\n' "$?" >> "$EVENTS"
    """
    result = _run(script, env={"EVENTS": str(events)})

    assert result.returncode == 0, result.stderr
    lines = events.read_text().splitlines()
    assert "probe-called" in lines
    assert any(line.startswith("comment:") for line in lines)
    assert not any("environmental flake" in line for line in lines)
    assert any("Initial full post-PR regression failed" in line for line in lines)
    assert "agent-dispatched" in lines
    assert "rc=1" in lines


# ---------------------------------------------------------------------------
# 6. Failed-test listing (spec Stage 5)
# ---------------------------------------------------------------------------


def _extract(stage: str, output: str, with_reasons: str = "true") -> str:
    script = (
        _source_all()
        + f"\nextract_failed_tests {shlex.quote(stage)} {shlex.quote(output)} {with_reasons}"
    )
    result = _run(script)
    assert result.returncode == 0, result.stderr
    return result.stdout.rstrip("\n")


def _render_block(stage: str, exit_code: int, output: str) -> str:
    script = (
        _source_all()
        + f"\nrender_failed_tests_block {shlex.quote(stage)} {exit_code} "
        + f"{shlex.quote(output)} true"
    )
    result = _run(script)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_extract_failed_tests_pytest_node_ids_with_one_line_reasons() -> None:
    """spec: AI_PRAUTO.md §Stage 5 -- 'the failing test identifiers -- pytest node
    IDs, ... each with a one-line reason.'"""
    output = (
        "FAILED tests/integration/spot/test_health.py::test_ping - "
        "httpx.ConnectError: All connection attempts failed\n"
        "ERROR tests/integration/spot/test_health.py::test_setup - "
        "Failed: fixture teardown boom\n"
        "=================== 1 failed, 1 error in 0.12s ===================="
    )
    block = _extract("Integration (spot)", output)
    assert (
        "`tests/integration/spot/test_health.py::test_ping` "
        "— `httpx.ConnectError: All connection attempts failed`"
    ) in block
    assert (
        "`tests/integration/spot/test_health.py::test_setup` — `Failed: fixture teardown boom`"
        in block
    )


def test_extract_failed_tests_playwright_header_and_first_error_line() -> None:
    """spec: AI_PRAUTO.md §Stage 5 -- 'Playwright test titles.'"""
    output = (
        "  1) [chromium] › uc1-01-ingest.spec.ts:12:3 › UC1 ingestion happy path ───\n"
        "   Error: expect(received).toBe(expected)\n"
        "1 failed"
    )
    block = _extract("E2E", output)
    assert "`[chromium] › uc1-01-ingest.spec.ts:12:3 › UC1 ingestion happy path`" in block
    assert "Error: expect(received).toBe(expected)" in block


def test_extract_failed_tests_static_diagnostics_as_path_line_entries() -> None:
    """spec: AI_PRAUTO.md §Stage 5 -- 'static-check diagnostics as path:line
    entries.'"""
    ruff_block = _extract(
        "Static (ruff)", "src/backend/foo.py:10:5: F401 [*] `os` imported but unused"
    )
    assert "`src/backend/foo.py:10:5`" in ruff_block

    mypy_block = _extract(
        "Static (mypy)",
        'src/backend/foo.py:22: error: Incompatible types in assignment '
        '(expression has type "str", variable has type "int")  [assignment]',
    )
    assert "`src/backend/foo.py:22`" in mypy_block

    tsc_block = _extract(
        "Static (frontend typecheck)",
        "src/frontend/app/page.tsx(15,3): error TS2322: "
        "Type 'string' is not assignable to type 'number'.",
    )
    assert "`src/frontend/app/page.tsx:15:3`" in tsc_block


def test_extract_failed_tests_deploy_stage_names_stage_only() -> None:
    """spec: AI_PRAUTO.md §Stage 5 -- 'A deploy-stage failure names the stage only,
    since it carries no test identifiers to extract.'"""
    block = _extract("Deploy (API)", "some irrelevant build log")
    assert block == "- (deploy stage; no test identifiers)"


def test_extract_failed_tests_unparseable_output_says_so_explicitly() -> None:
    """spec: AI_PRAUTO.md §Stage 5 -- 'When no identifiers can be extracted from a
    failed stage's output, the comment says so for that stage rather than omitting
    it.'"""
    block = _extract("Unit (Python)", "garbage nonsense text with no matches")
    assert block == "- No failing test identifiers could be extracted from this stage's output."


def _read_bash_constant(name: str) -> int:
    """Read a numeric constant straight from the sourced script instead of
    hardcoding its current value, so this test tracks the real cap."""
    script = _source_phases() + f'\nprintf "%s" "${name}"'
    result = _run(script)
    assert result.returncode == 0, result.stderr
    return int(result.stdout)


def test_extract_failed_tests_bounds_entries_reason_and_summaries() -> None:
    """spec: AI_PRAUTO.md §Stage 5 -- the listing is 'bounded in size.' Caps are
    read from the sourced script (`FAILED_TESTS_MAX_ENTRIES`,
    `FAILED_TESTS_MAX_SUMMARIES`) and from the reason-clip length embedded in
    `sanitize_failed_test_entries`, rather than hardcoded, so this test tracks the
    real bound instead of pinning a copy of it."""
    max_entries = _read_bash_constant("FAILED_TESTS_MAX_ENTRIES")
    max_summaries = _read_bash_constant("FAILED_TESTS_MAX_SUMMARIES")
    reason_clip_match = re.search(r"length\(\$s\) > (\d+)", PHASES.read_text())
    assert reason_clip_match is not None
    reason_cap = int(reason_clip_match.group(1))

    total_failures = max_entries + 10
    many_failures = "\n".join(
        f"FAILED tests/integration/spot/test_x.py::test_{i} - boom {i}"
        for i in range(total_failures)
    )
    block = _extract("Integration (spot)", many_failures)
    assert block.count("\n- `") + (1 if block.startswith("- `") else 0) == max_entries
    assert f"… and {total_failures - max_entries} more" in block

    long_reason_output = "FAILED tests/integration/spot/test_x.py::test_a - " + (
        "B" * (reason_cap + 100)
    )
    reason_block = _extract("Integration (spot)", long_reason_output)
    match = re.search(r"— `([^`]*)`", reason_block)
    assert match is not None
    assert len(match.group(1)) == reason_cap

    total_summaries = max_summaries + 1
    many_summaries = "\n".join(
        f"=================== run {i} passed in 0.0{i}s ===================="
        for i in range(total_summaries)
    )
    summary_block = _extract("Integration (spot)", many_summaries)
    assert summary_block.count("- Summary:") == max_summaries


def test_append_failed_tests_block_accumulator_cap_keeps_details_tags_balanced() -> None:
    """spec: AI_PRAUTO.md §Stage 5 -- the failed-tests listing stays 'bounded in
    size'; the accumulator across multiple stage blocks must never leave an
    unbalanced <details>/</details> pair in the PR comment body."""
    script = _source_all() + """
    BIG_ENTRY="- \\`tests/integration/spot/test_x.py::test_a\\` — \\`boom\\`"
    BODY=""
    for _ in $(seq 1 2000); do BODY="${BODY}${BIG_ENTRY}
"; done
    BIGBLOCK="<details><summary>Integration (spot) — failed (exit 1)</summary>

${BODY}
</details>"
    append_failed_tests_block ACC "$BIGBLOCK"
    SECOND="<details><summary>Integration (api-wired) — failed (exit 1)</summary>

- \\`tests/integration/api_wired/test_y.py::test_b\\` — \\`boom2\\`

</details>"
    append_failed_tests_block ACC "$SECOND"
    printf '%s' "$ACC"
    """
    result = _run(script)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert out.count("<details>") == out.count("</details>") == 1
    assert "Further failed-stage listings omitted (size limit)." in out
    assert "tests/integration/api_wired/test_y.py::test_b" not in out


def test_render_failed_tests_block_excludes_raw_traceback_body() -> None:
    """spec: AI_PRAUTO.md §Stage 5 -- the initial failure comment lists 'the failing
    test identifiers ... each with a one-line reason' and posts 'no raw per-stage
    output.'"""
    traceback_output = (
        CONNECT_ERROR_PING_FAILURE + "\n"
        "=================== FAILURES ===================\n"
        "tests/integration/spot/test_health.py:10: in test_ping\n"
        "    resp = httpx.get(url)\n"
        '  File "/app/.venv/lib/python3.13/site-packages/httpx/_api.py", line 195, in get\n'
        "    return request(\n"
        "E   httpx.ConnectError: All connection attempts failed\n"
        "=================== 1 failed in 0.23s ===================="
    )
    block = _render_block("Integration (spot)", 1, traceback_output)
    assert 'File "' not in block
    assert not any(line.strip().startswith("E   httpx") for line in block.splitlines())
    assert "`tests/integration/spot/test_health.py::test_ping`" in block


def test_record_targeted_result_populates_targeted_failed_test_details() -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception -- 'A
    targeted-retry failure posts a bounded, secret-scrubbed executor-evidence block
    ... carrying the same per-stage failed-test listing described above for the
    stages that still fail after the retry.'"""
    output = (
        CONNECT_ERROR_PING_FAILURE + "\n"
        "=================== 1 failed in 0.12s ===================="
    )
    script = (
        _source_all()
        + f'\nrecord_targeted_result {shlex.quote("Integration (spot)")} 1 {shlex.quote(output)}'
        + '\nprintf "%s" "$POST_PR_TARGETED_FAILED_TEST_DETAILS"'
    )
    result = _run(script)
    assert result.returncode == 0, result.stderr
    assert "`tests/integration/spot/test_health.py::test_ping`" in result.stdout


def test_extract_failed_tests_flake_notice_lists_identifiers_without_reasons() -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception -- the flake
    notice 'may name the failing test identifiers, bounded and secret-scrubbed,
    without per-test reasons.'"""
    output = (
        CONNECT_ERROR_PING_FAILURE + "\n"
        "=================== 1 failed in 0.12s ===================="
    )
    block = _extract("Integration (spot)", output, with_reasons="false")
    assert "`tests/integration/spot/test_health.py::test_ping`" in block
    assert " — `" not in block


# ---------------------------------------------------------------------------
# 7. Public comment safety (scrubbing, neutralization, --body-file)
# ---------------------------------------------------------------------------


def test_render_failed_tests_block_neutralizes_hostile_reason_text() -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception -- the
    comment 'excludes raw logs, credentials, URLs, tokens, dataset values, and
    other environment-sensitive output.' A reason carrying markdown mentions,
    links, an image, an embedded closing `</details>` tag, a carriage return, and a
    zero-width space must render inert."""
    reason = "@org/team [x](http://e) ![](http://i) </details>\r\u200b"
    output = (
        f"FAILED tests/integration/spot/test_link.py::test_a - {reason}\n"
        "=================== 1 failed in 0.05s ===================="
    )
    block = _render_block("Integration (spot)", 1, output)

    assert "\r" not in block
    assert "\u200b" not in block
    assert "@org/team [x](http://e) ![](http://i) </details>" in block

    stripped = re.sub(r"`[^`]*`", "", block)
    assert stripped.count("<details>") == 1
    assert stripped.count("</details>") == 1


def test_render_failed_tests_block_neutralizes_a_backtick_embedded_in_the_reason() -> None:
    """spec: AI_PRAUTO.md \u00a7Deterministic environmental-flake exception -- the
    comment 'excludes raw logs, credentials, URLs, tokens, dataset values, and
    other environment-sensitive output.' A reason that itself embeds a backtick
    (e.g. to try to break out of its own code span early) must not let any raw
    backtick from the input survive, and stripping the resulting code spans must
    still leave balanced `<details>`/`</details>` tags with no `@org` or
    `</details>` outside a code span."""
    reason = "x` @org/team </details> `y"
    output = (
        f"FAILED tests/integration/spot/test_link.py::test_a - {reason}\n"
        "=================== 1 failed in 0.05s ===================="
    )
    block = _render_block("Integration (spot)", 1, output)

    # sanitize_failed_test_entries transliterates every backtick in a reason to a
    # single quote, so none of the input's own backticks survive as raw backticks.
    assert "x`" not in block
    assert "`y" not in block

    stripped = re.sub(r"`[^`]*`", "", block)
    assert "@org" not in stripped
    assert stripped.count("<details>") == 1
    assert stripped.count("</details>") == 1


def test_extract_failed_tests_neutralizes_disallowed_identifier_characters() -> None:
    """spec: AI_PRAUTO.md \u00a7Deterministic environmental-flake exception -- the
    comment 'excludes raw logs, credentials, URLs, tokens, dataset values, and
    other environment-sensitive output.' plan: item 7, "conservative charset" rule
    (identifiers, reasons, and summaries each sit in their own code span, so
    mentions, links, images, issue references, and HTML are inert); source:
    .prauto/lib/phases.sh sanitize_failed_test_entries. A pytest node id carrying a
    backtick, a `<`, and another disallowed character (`%`) is neutralized to `?`
    per character; `@` is itself an allowed identifier character so it is not
    asserted on here."""
    output = (
        "FAILED tests/x.py::test_a`<%weird - boom\n"
        "=================== 1 failed in 0.05s ===================="
    )
    block = _extract("Integration (spot)", output)
    assert "`tests/x.py::test_a???weird`" in block
    assert "test_a`<%weird" not in block


def test_scrub_secrets_redacts_env_file_values_and_credential_shapes(tmp_path: Path) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception -- 'The
    comment excludes raw logs, credentials, URLs, tokens, dataset values, and other
    environment-sensitive output.'"""
    env_file = tmp_path / "env.dev"
    env_file.write_text(
        "SOME_API_TOKEN=verysecretvalue123!   # rotate quarterly\n"
        "SOME_DB_PASS='it'\\''s-a-pass'\n"
        "SOME_BUILD_TAG=0123456789abcdef0123\n"
        "SOME_NAMESPACE=dataspoke-01\n"
        "SOME_MODEL=gemini-3.5-flash\n"
    )
    text = (
        "leaked config dump: token=verysecretvalue123! db=it's-a-pass "
        "conn=redis://:hunter2pass@dbhost:6379/0 "
        "Authorization: Bearer abcdefghijklmnop "
        "worker key dsk_AbC-123_xyz "
        "build 0123456789abcdef0123 "
        "namespace dataspoke-01 model gemini-3.5-flash"
    )
    script = _source_all() + f"""
    DEV_ENV_FILE={shlex.quote(str(env_file))}
    unset REPO_DIR PRAUTO_DEV_ENV_FILE
    scrub_secrets {shlex.quote(text)}
    """
    result = _run(script)
    scrubbed = result.stdout

    assert "verysecretvalue123" not in scrubbed
    assert "it's-a-pass" not in scrubbed
    assert "hunter2pass" not in scrubbed
    assert "redis://***REDACTED***@dbhost:6379/0" in scrubbed
    assert "Authorization: ***REDACTED***" in scrubbed
    assert "abcdefghijklmnop" not in scrubbed
    assert "dsk_AbC-123_xyz" not in scrubbed
    assert "0123456789abcdef0123" not in scrubbed
    # Plain configuration values stay legible.
    assert "dataspoke-01" in scrubbed
    assert "gemini-3.5-flash" in scrubbed


def test_targeted_failure_comment_evidence_never_posts_a_straddling_secret(
    tmp_path: Path,
) -> None:
    """spec: AI_PRAUTO.md §Deterministic environmental-flake exception -- 'A
    targeted-retry failure posts a bounded, secret-scrubbed executor-evidence
    block so reviewers can distinguish test, deployment, and transport conditions
    without accessing local artifacts' (the scrubbing guarantee only). plan: item 7
    (straddling-secret truncation safety); source: .prauto/lib/phases.sh
    targeted_failure_comment_evidence."""
    env_file = tmp_path / "env.dev"
    secret = "abcdefghij0123456789ABCDEFGHIJ9876543210"
    env_file.write_text(f"SOME_TOKEN={secret}\n")

    max_chars = 12000
    total_len = 20000
    boundary = total_len - max_chars
    start = boundary - 20
    tail_marker = "TAIL_MARKER_NOT_A_SECRET"
    filler_after = "y" * (total_len - start - len(secret) - len(tail_marker)) + tail_marker
    evidence = ("x" * start) + secret + filler_after
    assert len(evidence) == total_len
    evidence_file = tmp_path / "evidence.txt"
    evidence_file.write_text(evidence)

    script = _source_all() + f"""
    DEV_ENV_FILE={shlex.quote(str(env_file))}
    unset REPO_DIR PRAUTO_DEV_ENV_FILE
    POST_PR_TARGETED_EVIDENCE=$(cat {shlex.quote(str(evidence_file))})
    targeted_failure_comment_evidence
    """
    result = _run(script)
    out = result.stdout

    assert result.returncode == 0, result.stderr
    assert secret not in out
    assert secret[:20] not in out
    assert secret[20:] not in out
    assert "(truncated" in out
    assert tail_marker in out


def test_post_post_pr_regression_comment_uses_body_file_and_removes_temp_dir(
    tmp_path: Path,
) -> None:
    """plan: item 7 (comment body passed through a private temp file, not argv, so
    it is neither exposed in the process table nor limited by argument size);
    source: .prauto/lib/phases.sh post_post_pr_regression_comment."""
    gh_args_file = tmp_path / "gh_args.txt"
    gh_body_file = tmp_path / "gh_body.txt"
    gh_body_dir_file = tmp_path / "gh_body_dir.txt"

    script = _source_all() + f"""
    PRAUTO_WORKER_ID=tester
    PRAUTO_GITHUB_REPO=acme/repo
    get_pr_number_for_branch() {{ BRANCH_PR_NUMBER=42; }}
    gh() {{
      printf '%s\\n' "$*" >> {shlex.quote(str(gh_args_file))}
      local i=1
      while [[ $i -le $# ]]; do
        if [[ "${{!i}}" == "--body-file" ]]; then
          local next=$((i+1))
          cp "${{!next}}" {shlex.quote(str(gh_body_file))}
          dirname "${{!next}}" > {shlex.quote(str(gh_body_dir_file))}
        fi
        i=$((i+1))
      done
      return 0
    }}
    post_post_pr_regression_comment prauto/I-179 "hello body text"
    printf 'rc=%s\\n' "$?"
    """
    result = _run(script)

    assert "rc=0" in result.stdout
    args_text = gh_args_file.read_text()
    assert "--body-file" in args_text
    assert "hello body text" not in args_text
    assert gh_body_file.read_text() == "prauto(tester): hello body text"

    body_dir = gh_body_dir_file.read_text().strip()
    assert not Path(body_dir).exists()


# ---------------------------------------------------------------------------
# 8. Lock release
# ---------------------------------------------------------------------------


def test_release_required_dev_lock_is_idempotent(tmp_path: Path) -> None:
    """plan: item 8 (lock release is idempotent: the owner is cleared after one
    release attempt, so a later call -- e.g. from the heartbeat EXIT trap -- is a
    no-op); source: .prauto/lib/phases.sh release_required_dev_lock."""
    events = tmp_path / "events"
    script = _source_phases() + """
    curl() { echo curl-release >> "$EVENTS"; return 0; }
    DEV_LOCK_URL=http://example.invalid/lock
    REQUIRED_LOCK_OWNER=prauto-test
    release_required_dev_lock
    release_required_dev_lock
    printf 'owner=[%s]\\n' "$REQUIRED_LOCK_OWNER"
    """
    result = _run(script, env={"EVENTS": str(events)})

    assert "owner=[]" in result.stdout
    assert events.read_text().splitlines().count("curl-release") == 1


def test_release_is_a_no_op_after_a_failed_lock_acquire(tmp_path: Path) -> None:
    """plan: item 8 (REQUIRED_LOCK_OWNER is set only once the lock is actually
    held, so a release -- including the heartbeat EXIT trap's -- never targets a
    lock this worker does not own); source: .prauto/lib/phases.sh
    acquire_required_dev_lock."""
    events = tmp_path / "events"
    script = _source_phases() + """
    resolve_dev_env() {
      DEV_ENV_FILE=/dev/null
      DEV_LOCK_URL=http://example.invalid/lock
      return 0
    }
    dev_env_healthy() { return 0; }
    regression_blocked() { echo "blocked:$2" >> "$EVENTS"; return 0; }
    curl() {
      case "$*" in
        *"/status"*) return 0 ;;
        *"/acquire"*) printf '409' ;;
        *"/release"*) echo curl-release >> "$EVENTS"; return 0 ;;
        *) return 0 ;;
      esac
    }
    acquire_required_dev_lock 179 "test purpose"
    printf 'owner=[%s]\\n' "$REQUIRED_LOCK_OWNER"
    release_required_dev_lock
    """
    result = _run(script, env={"EVENTS": str(events)})

    assert "owner=[]" in result.stdout
    assert not events.exists() or "curl-release" not in events.read_text()


def test_heartbeat_cleanup_calls_release_required_dev_lock(tmp_path: Path) -> None:
    """plan: item 8 (a regression interrupted mid-stage must not strand the
    dev-env lock; the heartbeat EXIT trap releases it, idempotently); source:
    .prauto/heartbeat.sh cleanup()."""
    heartbeat_text = HEARTBEAT.read_text()
    match = re.search(r"^cleanup\(\) \{.*?\n\}\n", heartbeat_text, re.MULTILINE | re.DOTALL)
    assert match is not None, "cleanup() function not found in heartbeat.sh"
    cleanup_snippet = match.group(0)

    events = tmp_path / "events"
    script = (
        _source_phases()
        + f"""
    WORKTREE_DIR=""
    REPO_DIR={shlex.quote(str(tmp_path))}
    release_required_dev_lock() {{ echo release-called >> "$EVENTS"; }}
    release_lock() {{ echo release-lock >> "$EVENTS"; return 0; }}
    {cleanup_snippet}
    cleanup
    """
    )
    result = _run(script, env={"EVENTS": str(events)})

    assert result.returncode == 0, result.stderr
    lines = events.read_text().splitlines()
    assert "release-called" in lines


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        # The mechanism wins over the cluster that reported it: these co-occur,
        # and naming the cluster points an operator at the wrong subsystem.
        (
            "gke node pool: temporary failure in name resolution",
            "DNS resolution failure",
        ),
        (
            "kubernetes apiserver: pod was evicted",
            "pod eviction/preemption or node not ready",
        ),
        # An ingress-attributed gateway status is an ingress error...
        ("ingress-nginx returned 503 for the api host", "ingress gateway error"),
        # ...while the same status from the control plane is not.
        ("kube-apiserver returned 503", "control-plane request failure"),
        # Cluster named with no more specific mechanism.
        ("gke control-plane i/o timeout", "control-plane request failure"),
        ("econnreset from the api host", "client connection refused/reset/timeout"),
    ],
)
def test_flake_category_names_the_mechanism_not_the_cluster(
    reason: str, expected: str
) -> None:
    """A flake notice must point at the subsystem that actually failed.

    spec: spec/AI_PRAUTO.md §Deterministic environmental-flake exception — the
    notice is what an operator reads to decide whether to investigate. Cluster
    names appear in almost every cluster-sourced failure, so matching on them
    first files DNS failures, evictions and ingress errors alike as control-plane
    problems.
    """
    result = _run(
        _source_phases() + f"\ntransport_flake_category {shlex.quote(reason)}"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_an_unattributed_gateway_status_is_not_a_transport_flake() -> None:
    """A bare 502/503/504 keeps a failure blocking even if something else matches.

    spec: spec/AI_PRAUTO.md §Deterministic environmental-flake exception — the
    allowlist is closed and ambiguity stays blocking. A reason that mentions a
    gateway status with no ingress or control-plane source is a real failure to
    investigate; matching it through an unrelated allowlist alternative would
    retry a genuine regression as environmental.
    """
    blocking = "econnreset while polling; server later returned 503"
    flaky = "econnreset from the api host"

    result = _run(
        _source_phases()
        + f'\nif is_environmental_transport_failure {shlex.quote(blocking)}; then'
        ' printf "unattributed=flake\\n"; else printf "unattributed=blocking\\n"; fi'
        + f'\nif is_environmental_transport_failure {shlex.quote(flaky)}; then'
        ' printf "plain=flake\\n"; else printf "plain=blocking\\n"; fi'
    )

    assert result.returncode == 0, result.stderr
    assert "unattributed=blocking" in result.stdout
    # The same text without the gateway status is still recognised, so this is
    # the status gate doing the work rather than the allowlist simply missing.
    assert "plain=flake" in result.stdout
