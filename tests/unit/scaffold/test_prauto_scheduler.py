"""Regression coverage for the agent-supervised Hermes scheduler binding.

Exercises the scheduler detach primitive (`.prauto/scheduler/daemonize.py`), the
launcher (`.prauto/scheduler/launch.sh`), and the monitor (`.prauto/scheduler/monitor.sh`)
against clean, repo-shaped fixtures with harmless heartbeat/monitor stubs — no agent,
GitHub, or network. Covers:

  * daemonize.py detaches into its own session (survives the caller's process group)
  * launch.sh dispatches the executor and monitor on a clean checkout, reporting numeric PIDs
  * launch.sh treats an already-running executor (live PID lock) as a no-op
  * launch.sh clears a stale executor lock (dead PID) and proceeds
  * launch.sh reports a monitor-detach failure as nonzero (MONITOR_FAILED), not a silent success
  * launch.sh reports a monitor that exits immediately as nonzero (MONITOR_EXITED_IMMEDIATELY)
  * monitor.sh posts a final result and removes its lock (dry-run, no Slack)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).parents[3]
SCHEDULER = ROOT / ".prauto/scheduler"

# A PID outside macOS's valid range (max ~4.19M); `kill -0` returns ESRCH for it, so it is a
# guaranteed-dead PID for exercising the stale-lock branch.
DEAD_PID = "999999999"


def _scheduler_fixture(tmp_path: Path, *script_names: str) -> Path:
    """Copy the real scheduler scripts into a clean repo-shaped fixture.

    Only the named scripts are copied from the real scheduler dir; the test supplies stubs for the
    rest so launch.sh's own references resolve against the fixture, not the developer checkout.
    """
    repo = tmp_path / "repo"
    dst = repo / ".prauto/scheduler"
    dst.mkdir(parents=True)
    for name in script_names:
        shutil.copy2(SCHEDULER / name, dst / name)
    return repo


def _write_stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\nset -uo pipefail\n" + body)
    path.chmod(0o755)


def _stub_executor(repo: Path) -> None:
    """A heartbeat stub that proves it started (marker) and stays alive for launch.sh's verify."""
    _write_stub(
        repo / ".prauto/heartbeat.sh",
        'printf "started\\n" > "$PRAUTO_TEST_MARKER"\nsleep 30\n',
    )


def _stub_monitor_alive(repo: Path) -> None:
    """A monitor stub that proves it started and stays alive for launch.sh's verify."""
    _write_stub(
        repo / ".prauto/scheduler/monitor.sh",
        'printf "monitor-started\\n" > "$PRAUTO_TEST_MONITOR_MARKER"\nsleep 30\n',
    )


def _stub_monitor_dies(repo: Path) -> None:
    """A monitor stub that exits immediately — the daemonized monitor is already dead by verify."""
    _write_stub(repo / ".prauto/scheduler/monitor.sh", "exit 0\n")


def _run_launch(repo: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        ["bash", str(repo / ".prauto/scheduler/launch.sh")],
        capture_output=True,
        check=False,
        cwd=repo,
        env=run_env,
        text=True,
    )


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _wait_for(path: Path, *, timeout_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert path.exists(), f"{path} did not appear within {timeout_seconds}s"


def test_daemonize_detaches_into_its_own_session_and_survives_caller(tmp_path: Path) -> None:
    """daemonize.py prints a numeric PID and the child outlives the caller, in its own session."""
    marker = tmp_path / "daemon-started"
    log = tmp_path / "daemon.log"
    worker = tmp_path / "worker.sh"
    _write_stub(worker, 'printf "ran\\n" > "$PRAUTO_TEST_MARKER"\nsleep 30\n')

    result = subprocess.run(
        ["python3", str(SCHEDULER / "daemonize.py"), str(log), "--", "bash", str(worker)],
        capture_output=True,
        check=False,
        text=True,
        env={**os.environ, "PRAUTO_TEST_MARKER": str(marker)},
    )

    assert result.returncode == 0, result.stderr
    pid = int(result.stdout.strip())
    assert pid > 0

    # The caller has already returned; the daemon is reparented and in its own session/group.
    _wait_for(marker)
    assert marker.read_text() == "ran\n"
    assert os.getpgid(pid) != os.getpgid(os.getpid()), "daemon did not detach into its own group"
    _kill(pid)


def test_launch_dispatches_executor_and_monitor_on_clean_checkout(tmp_path: Path) -> None:
    repo = _scheduler_fixture(tmp_path, "launch.sh", "daemonize.py")
    _stub_executor(repo)
    _stub_monitor_alive(repo)
    marker = tmp_path / "executor-started"
    monitor_marker = tmp_path / "monitor-started"

    result = _run_launch(
        repo,
        {
            "PRAUTO_TEST_MARKER": str(marker),
            "PRAUTO_TEST_MONITOR_MARKER": str(monitor_marker),
        },
    )

    assert result.returncode == 0, result.stderr
    m = re.search(r"STARTED pid=(\d+) monitor_pid=(\d+)", result.stdout)
    assert m is not None, result.stdout
    exec_pid, monitor_pid = int(m.group(1)), int(m.group(2))
    try:
        _wait_for(marker)
        _wait_for(monitor_marker)
        assert marker.read_text() == "started\n"
        assert monitor_marker.read_text() == "monitor-started\n"
    finally:
        _kill(exec_pid)
        _kill(monitor_pid)


def test_launch_already_running_executor_is_noop(tmp_path: Path) -> None:
    repo = _scheduler_fixture(tmp_path, "launch.sh", "daemonize.py")
    marker = tmp_path / "executor-started"

    # A live executor: hold the PID lock with a real, running process.
    holder = subprocess.Popen(["sleep", "60"])
    lock = repo / ".prauto/state/heartbeat.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(holder.pid))

    try:
        result = _run_launch(repo, {"PRAUTO_TEST_MARKER": str(marker)})
        assert result.returncode == 0, result.stderr
        assert re.search(rf"ALREADY_RUNNING pid={holder.pid}", result.stdout), result.stdout
        assert not marker.exists(), "a no-op tick must not launch a second executor"
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_launch_clears_stale_executor_lock_and_proceeds(tmp_path: Path) -> None:
    repo = _scheduler_fixture(tmp_path, "launch.sh", "daemonize.py")
    _stub_executor(repo)
    _stub_monitor_alive(repo)
    marker = tmp_path / "executor-started"

    lock = repo / ".prauto/state/heartbeat.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(DEAD_PID)

    result = _run_launch(
        repo,
        {
            "PRAUTO_TEST_MARKER": str(marker),
            "PRAUTO_TEST_MONITOR_MARKER": str(tmp_path / "monitor-started"),
        },
    )

    assert result.returncode == 0, result.stderr
    m = re.search(r"STARTED pid=(\d+) monitor_pid=(\d+)", result.stdout)
    assert m is not None, result.stdout
    exec_pid, monitor_pid = int(m.group(1)), int(m.group(2))
    try:
        _wait_for(marker)
    finally:
        _kill(exec_pid)
        _kill(monitor_pid)


def test_launch_monitor_detach_failure_is_nonzero(tmp_path: Path) -> None:
    """A monitor whose log cannot be opened must surface as MONITOR_FAILED, not a silent STARTED."""
    repo = _scheduler_fixture(tmp_path, "launch.sh", "daemonize.py")
    _stub_executor(repo)
    _stub_monitor_alive(repo)
    marker = tmp_path / "executor-started"

    # Force daemonize.py's os.open(monitor.log, O_WRONLY) to fail (EISDIR): the monitor's log
    # path is a directory, so the monitor detach produces no PID while the executor detach works.
    state = repo / ".prauto/state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "monitor.log").mkdir()

    result = _run_launch(repo, {"PRAUTO_TEST_MARKER": str(marker)})

    assert result.returncode != 0, result.stdout
    m = re.search(r"MONITOR_FAILED pid=(\d+)", result.stdout)
    assert m is not None, result.stdout
    exec_pid = int(m.group(1))
    _kill(exec_pid)


def test_launch_monitor_exited_immediately_is_nonzero(tmp_path: Path) -> None:
    """A monitor that daemonizes but dies at once must surface as MONITOR_EXITED_IMMEDIATELY."""
    repo = _scheduler_fixture(tmp_path, "launch.sh", "daemonize.py")
    _stub_executor(repo)
    _stub_monitor_dies(repo)
    marker = tmp_path / "executor-started"

    result = _run_launch(repo, {"PRAUTO_TEST_MARKER": str(marker)})

    assert result.returncode != 0, result.stdout
    m = re.search(r"MONITOR_EXITED_IMMEDIATELY pid=(\d+) monitor_pid=(\d+)", result.stdout)
    assert m is not None, result.stdout
    exec_pid = int(m.group(1))
    _kill(exec_pid)


def test_monitor_reports_final_state_and_cleans_lock(tmp_path: Path) -> None:
    """monitor.sh (dry-run) posts a final result and removes its lock, without touching Slack."""
    repo = _scheduler_fixture(tmp_path, "monitor.sh")
    state = repo / ".prauto/state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "heartbeat_cron.log").write_text("Heartbeat complete\n")

    # A dead executor PID: the monitor reports "no live executor" immediately.
    result = subprocess.run(
        ["bash", str(repo / ".prauto/scheduler/monitor.sh"), DEAD_PID],
        capture_output=True,
        check=False,
        text=True,
        cwd=repo,
        env={**os.environ, "PRAUTO_MONITOR_DRY_RUN": "1"},
    )

    assert result.returncode == 0, result.stderr
    # A dead executor is the monitor's "attached but nothing to watch" path: it reports the
    # classified final state (done, from the "Heartbeat complete" marker) and exits immediately.
    assert "monitor attached but no live executor" in result.stdout, result.stdout
    assert "Final state: done" in result.stdout, result.stdout
    assert not (state / "monitor.lock").exists(), "monitor must remove its lock on exit"


# --- reporting-channel preflight (Slack target resolution) ---------------------
#
# `hermes send` resolves the target from ONE Hermes profile home. The monitor must fail loudly
# when that home cannot resolve the target, instead of reporting a run's progress into a void.


def _stub_hermes(
    bin_dir: Path, *, list_body: str = "", list_rc: int = 0, send_body: str = "", send_rc: int = 0
) -> Path:
    """A fake `hermes` on PATH: `send --list` and `send --to` behave as scripted."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "hermes"
    stub.write_text(
        "#!/usr/bin/env bash\nset -uo pipefail\n"
        'if [[ " $* " == *" --list "* ]]; then\n'
        f'  printf "%b\\n" {json.dumps(list_body)}\n'
        f"  exit {list_rc}\n"
        "fi\n"
        f"{send_body}"
        f"exit {send_rc}\n"
    )
    stub.chmod(0o755)
    return bin_dir


def _monitor_env(tmp_path: Path, bin_dir: Path, **extra: str) -> dict[str, str]:
    """A monitor environment whose `hermes` is the stub: HOME without one, stub dir on PATH."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        **extra,
    }


def _run_monitor(
    repo: Path, env: dict[str, str], pid: str = DEAD_PID
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(repo / ".prauto/scheduler/monitor.sh"), pid],
        capture_output=True,
        check=False,
        text=True,
        cwd=repo,
        env=env,
    )


def test_monitor_preflight_fails_loudly_when_target_unresolved(tmp_path: Path) -> None:
    """An unresolvable Slack target is a nonzero exit with a recorded reason — never silent."""
    repo = _scheduler_fixture(tmp_path, "monitor.sh")
    state = repo / ".prauto/state"
    state.mkdir(parents=True, exist_ok=True)
    (repo / ".prauto/config.local.env").write_text(
        'PRAUTO_SCHEDULER_HERMES_HOME="/tmp/other-profile"\n'
        'PRAUTO_SLACK_TARGET="slack:hermes-dev"\n'
    )
    bin_dir = _stub_hermes(
        tmp_path / "bin",
        list_body="hermes send: Could not resolve 'hermes-dev' on slack.",
        list_rc=1,
    )

    result = _run_monitor(repo, _monitor_env(tmp_path, bin_dir))

    assert result.returncode != 0, result.stdout
    assert "cannot resolve Slack target 'slack:hermes-dev'" in result.stderr, result.stderr
    marker = state / "monitor-slack-unresolved"
    assert marker.exists(), "the reason must be recorded for post-mortem"
    assert "/tmp/other-profile" in marker.read_text(), marker.read_text()
    assert not (state / "monitor.lock").exists(), "a failed preflight must not take the lock"


def test_monitor_preflight_requires_a_whole_entry_name(tmp_path: Path) -> None:
    """`slack:hermes-dev` must not be satisfied by a longer entry such as `slack:hermes-dev-2`."""
    repo = _scheduler_fixture(tmp_path, "monitor.sh")
    (repo / ".prauto/state").mkdir(parents=True, exist_ok=True)
    (repo / ".prauto/config.local.env").write_text('PRAUTO_SLACK_TARGET="slack:hermes-dev"\n')
    bin_dir = _stub_hermes(
        tmp_path / "bin", list_body="slack:\n  slack:hermes-dev-2  [C0BS779DV4L]"
    )

    result = _run_monitor(repo, _monitor_env(tmp_path, bin_dir))

    assert result.returncode != 0, result.stdout
    assert "cannot resolve Slack target" in result.stderr, result.stderr


def test_monitor_pins_the_profile_home_over_an_inherited_one(tmp_path: Path) -> None:
    """The pinned profile home (not the plain shell's) is what `hermes` sees on a real send."""
    repo = _scheduler_fixture(tmp_path, "monitor.sh")
    state = repo / ".prauto/state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "heartbeat_cron.log").write_text("Heartbeat complete\n")
    (repo / ".prauto/config.local.env").write_text(
        'PRAUTO_SCHEDULER_HERMES_HOME="/tmp/pinned-profile"\n'
        'PRAUTO_SLACK_TARGET="slack:hermes-dev"\n'
    )
    seen = tmp_path / "hermes-home-seen"
    bin_dir = _stub_hermes(
        tmp_path / "bin",
        list_body="slack:\n  slack:hermes-dev  [C0BS779DV4L]",
        send_body=f'printf "%s" "$HERMES_HOME" > "{seen}"\n',
    )

    result = _run_monitor(repo, _monitor_env(tmp_path, bin_dir, HERMES_HOME="/tmp/inherited-wrong"))

    assert result.returncode == 0, result.stderr
    assert seen.exists(), "the monitor must have posted its final note"
    # The instance override wins over an inherited (wrong) HERMES_HOME.
    assert seen.read_text() == "/tmp/pinned-profile", seen.read_text()
    assert not (state / "monitor.lock").exists()


def test_monitor_dry_run_leaves_a_real_unresolved_marker_alone(tmp_path: Path) -> None:
    """A dry run verifies nothing, so it must not clear a genuine reporting breadcrumb."""
    repo = _scheduler_fixture(tmp_path, "monitor.sh")
    state = repo / ".prauto/state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "heartbeat_cron.log").write_text("Heartbeat complete\n")
    marker = state / "monitor-slack-unresolved"
    marker.write_text("monitor: cannot resolve Slack target 'slack:hermes-dev'\n")
    bin_dir = _stub_hermes(tmp_path / "bin", list_body="slack:\n  slack:hermes-dev")

    result = _run_monitor(repo, _monitor_env(tmp_path, bin_dir, PRAUTO_MONITOR_DRY_RUN="1"))

    assert result.returncode == 0, result.stderr
    assert marker.exists(), "dry-run must not clear a marker it never verified"
    assert marker.read_text().startswith("monitor: cannot resolve Slack target"), marker.read_text()


def test_monitor_clears_a_stale_marker_once_the_preflight_passes(tmp_path: Path) -> None:
    """A verified preflight is the only thing that removes an earlier run's breadcrumb."""
    repo = _scheduler_fixture(tmp_path, "monitor.sh")
    state = repo / ".prauto/state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "heartbeat_cron.log").write_text("Heartbeat complete\n")
    marker = state / "monitor-slack-unresolved"
    marker.write_text("monitor: cannot resolve Slack target 'slack:hermes-dev'\n")
    (repo / ".prauto/config.local.env").write_text('PRAUTO_SLACK_TARGET="slack:hermes-dev"\n')
    bin_dir = _stub_hermes(tmp_path / "bin", list_body="slack:\n  slack:hermes-dev  [C0BS779DV4L]")

    result = _run_monitor(repo, _monitor_env(tmp_path, bin_dir))

    assert result.returncode == 0, result.stderr
    assert not marker.exists(), "a resolving preflight must clear the stale breadcrumb"


def test_monitor_records_undelivered_message_after_one_retry(tmp_path: Path) -> None:
    """A send that fails twice leaves a local trace instead of vanishing."""
    repo = _scheduler_fixture(tmp_path, "monitor.sh")
    state = repo / ".prauto/state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "heartbeat_cron.log").write_text("Heartbeat complete\n")
    (repo / ".prauto/config.local.env").write_text(
        'PRAUTO_SCHEDULER_HERMES_HOME="/tmp/pinned-profile"\n'
        'PRAUTO_SLACK_TARGET="slack:hermes-dev"\n'
        "PRAUTO_MONITOR_SEND_RETRY_SECS=0\n"
    )
    bin_dir = _stub_hermes(
        tmp_path / "bin",
        list_body="slack:\n  slack:hermes-dev  [C0BS779DV4L]",
        send_rc=1,
    )

    result = _run_monitor(repo, _monitor_env(tmp_path, bin_dir))

    assert "monitor: slack send failed" in result.stderr, result.stderr
    undelivered = (state / "monitor-undelivered.log").read_text()
    assert "monitor attached but no live executor" in undelivered, undelivered


def test_monitor_caps_the_undelivered_log(tmp_path: Path) -> None:
    """The undelivered log is size-bounded: past the cap only the newest entries are kept."""
    repo = _scheduler_fixture(tmp_path, "monitor.sh")
    state = repo / ".prauto/state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "heartbeat_cron.log").write_text("Heartbeat complete\n")
    (state / "monitor-undelivered.log").write_text("old entry\n" * 40000)
    (repo / ".prauto/config.local.env").write_text(
        'PRAUTO_SLACK_TARGET="slack:hermes-dev"\nPRAUTO_MONITOR_SEND_RETRY_SECS=0\n'
    )
    bin_dir = _stub_hermes(
        tmp_path / "bin", list_body="slack:\n  slack:hermes-dev  [C0BS779DV4L]", send_rc=1
    )

    _run_monitor(repo, _monitor_env(tmp_path, bin_dir))

    log = state / "monitor-undelivered.log"
    assert log.stat().st_size <= 262144, log.stat().st_size
    assert "monitor attached but no live executor" in log.read_text()


def test_launch_reports_the_monitor_failure_reason(tmp_path: Path) -> None:
    """A monitor that dies at once surfaces WHY in the launcher's status line (reason=...)."""
    repo = _scheduler_fixture(tmp_path, "launch.sh", "daemonize.py")
    _stub_executor(repo)
    _write_stub(
        repo / ".prauto/scheduler/monitor.sh",
        "reason=\"monitor: cannot resolve Slack target 'slack:hermes-dev' "
        'under HERMES_HOME=/tmp/x"\n'
        'printf "%s\\n" "$reason" >&2\n'
        "exit 2\n",
    )
    marker = tmp_path / "executor-started"

    result = _run_launch(repo, {"PRAUTO_TEST_MARKER": str(marker)})

    assert result.returncode != 0, result.stdout
    m = re.search(
        r"MONITOR_EXITED_IMMEDIATELY pid=(\d+) monitor_pid=(\d+) "
        r"reason=monitor: cannot resolve Slack target",
        result.stdout,
    )
    assert m is not None, result.stdout
    _kill(int(m.group(1)))


def test_launch_ignores_a_stale_monitor_log_line_when_reporting_the_reason(tmp_path: Path) -> None:
    """monitor.log accumulates across runs; only this run's output may supply `reason=`."""
    repo = _scheduler_fixture(tmp_path, "launch.sh", "daemonize.py")
    _stub_executor(repo)
    state = repo / ".prauto/state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "monitor.log").write_text("monitor: slack send failed\n")
    _write_stub(repo / ".prauto/scheduler/monitor.sh", "exit 2\n")
    marker = tmp_path / "executor-started"

    result = _run_launch(repo, {"PRAUTO_TEST_MARKER": str(marker)})

    assert result.returncode != 0, result.stdout
    m = re.search(r"MONITOR_EXITED_IMMEDIATELY pid=(\d+) monitor_pid=\d+", result.stdout)
    assert m is not None, result.stdout
    assert "reason=" not in result.stdout, result.stdout
    assert "slack send failed" not in result.stdout, result.stdout
    _kill(int(m.group(1)))


def test_scheduler_reference_values_are_marked_as_not_read_by_code() -> None:
    """Config that nothing reads must say so, or it reads as a live knob.

    spec: spec/AI_PRAUTO.md §Executor and Scheduler — the Hermes binding is one
    reference example, and these fields describe the job an operator creates
    rather than anything the executor consults. Left unmarked, an operator
    editing the schedule here would reasonably expect a running worker's cadence
    to change, and nothing would tell them otherwise.
    """
    root = Path(__file__).parents[3]
    config = (root / ".prauto/config.env").read_text()

    declared = {
        line.split("=", 1)[0]
        for line in config.splitlines()
        if line.startswith("PRAUTO_SCHEDULER_HERMES_")
    }
    assert declared, "expected the reference scheduler values to be present"

    shell = "\n".join(
        p.read_text()
        for p in sorted((root / ".prauto").rglob("*.sh"))
    )
    unread = sorted(name for name in declared if name not in shell)

    # Whatever is genuinely unread has to be covered by the note above them.
    if unread:
        block_start = config.index("# Scheduler —")
        block_end = config.index("PRAUTO_SCHEDULER_HERMES_SCHEDULE")
        note = config[block_start:block_end]
        assert "no code in .prauto/ reads" in note, (
            f"these are declared but read by no code, and nothing says so: {unread}"
        )


def test_a_no_work_wake_is_not_reported_as_an_immediate_death() -> None:
    """The ordinary outcome must not look like a fault.

    spec: spec/AI_PRAUTO.md §Executor Cycle — a wake with no claimed issue to
    advance logs "Heartbeat complete." and exits. That finishes well inside
    launch.sh's five-second liveness probe, so probing by liveness alone reports
    the most common outcome as EXITED_IMMEDIATELY and trains an operator to
    ignore the status line that matters when the executor really does die.
    """
    launch = (Path(__file__).parents[3] / ".prauto/scheduler/launch.sh").read_text()

    assert "COMPLETED_NO_WORK" in launch, "a completed no-work wake has no distinct status"
    probe = launch[launch.index("# 3. Verify it survived"):launch.index("# 4. Detach the monitor")]
    assert "Heartbeat complete" in probe, (
        "the probe still distinguishes by liveness alone rather than by what "
        "the executor logged"
    )
    # Compare the emitting statements, not the names — the comment above them
    # mentions both.
    assert probe.index('echo "COMPLETED_NO_WORK') < probe.index('echo "EXITED_IMMEDIATELY'), (
        "the completion check must run before the death report, or the death "
        "report wins for every fast wake"
    )


def test_the_monitor_log_marks_run_boundaries_and_is_bounded() -> None:
    """A shared append-only log must say which run each line belongs to.

    spec: spec/AI_PRAUTO.md §Executor and Scheduler — the monitor posts progress
    until the coding agent finishes, and its log is the local record of that. The
    file is opened O_APPEND across runs, so without boundaries an operator
    reading it attributes an old run's "slack send failed" to the current one and
    concludes reporting is broken when it is not.
    """
    launch = (Path(__file__).parents[3] / ".prauto/scheduler/launch.sh").read_text()

    assert "--- monitor run" in launch, "no run banner is written to the monitor log"
    assert "MONITOR_LOG_MAX_BYTES" in launch, "the monitor log grows without bound"

    # The banner must precede the offset capture, or launch.sh's own "new lines
    # this run" reads would include it and report it as monitor output.
    assert launch.index("--- monitor run") < launch.index("monitor_log_start=0"), (
        "the run banner is written after the offset capture, so it would be "
        "read back as this run's monitor output"
    )
