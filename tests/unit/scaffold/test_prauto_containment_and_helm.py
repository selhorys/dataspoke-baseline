"""Hermetic regressions for PRauto containment and Helm dependency handling."""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[3]
PRAUTO = ROOT / ".prauto"
HELPERS = ROOT / "helm-charts/bin/lib/helpers.sh"


def _run(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False, env=run_env
    )


def _source_helpers() -> str:
    return f"source {shlex.quote(str(HELPERS))}"


def test_heartbeat_kills_verified_group_before_waiting_for_former_wrapper() -> None:
    """The signal window must not block on a wrapper that already exec'd install.sh."""
    source = (PRAUTO / "heartbeat.sh").read_text()
    cleanup = source[source.index("cleanup() {") : source.index("handle_signal() {")]
    assert 'kill -TERM -"$PROVISION_PGID"' in cleanup
    # Group termination must precede any wait on the leader: by then the leader
    # may already have exec'd install.sh and be wedged.
    assert cleanup.index('kill -TERM -"$PROVISION_PGID"') < cleanup.index(
        "$PROVISION_LEADER_PID"
    )
    # Waiting for the FIFO-only wrapper belongs exclusively to the unverified branch.
    assert 'elif [[ -n "${CONTAINMENT_GATE_WRAPPER_PID:-}" ]]' in cleanup


def test_heartbeat_cleanup_never_waits_unbounded() -> None:
    """The last teardown in the process must not be the one that can hang forever.

    spec: spec/AI_PRAUTO.md §Provisioning — the EXIT trap is what stops a
    provisioning tree the heartbeat can no longer supervise. It runs after every
    other wall-clock backstop has gone, so a bare `wait` on a leader stuck in
    uninterruptible sleep holds the heartbeat lock and the worktree indefinitely —
    a state no later wake can recover from, because the lock looks live.
    """
    source = (PRAUTO / "heartbeat.sh").read_text()
    cleanup = source[source.index("cleanup() {") : source.index("handle_signal() {")]

    bare_waits = [
        line.strip()
        for line in cleanup.splitlines()
        if line.strip().startswith("wait ") and "wait_for_pid_bounded" not in line
    ]
    assert not bare_waits, f"unbounded wait in the EXIT trap: {bare_waits}"
    assert "wait_for_pid_bounded" in cleanup


def test_heartbeat_group_kill_is_not_gated_on_the_leader_pid() -> None:
    """A recorded group must be terminated even if the leader PID is missing.

    spec: spec/AI_PRAUTO.md §Provisioning — PROVISION_PGID is recorded only once a
    private process group is verified, so its presence alone means a real tree is
    running. Requiring the leader PID as well lets any path that sets one without
    the other skip the entire teardown silently, leaving the tree alive.
    """
    source = (PRAUTO / "heartbeat.sh").read_text()
    cleanup = source[source.index("cleanup() {") : source.index("handle_signal() {")]

    guard = next(
        line for line in cleanup.splitlines() if 'if [[ -n "${PROVISION_PGID:-}"' in line
    )
    assert "PROVISION_LEADER_PID" not in guard, (
        "the group kill is gated on the leader PID as well; a PGID recorded "
        f"without one would skip teardown entirely: {guard.strip()}"
    )


def test_heartbeat_cleanup_can_be_retried_after_an_interrupted_run() -> None:
    """A cleanup cut short partway must not be permanently disabled.

    spec: spec/AI_PRAUTO.md §Executor Cycle — the trap owns teardown of the
    worktree, the dev-env lock and any provisioning tree. handle_signal clears
    every trap before calling it, so a second signal kills the process outright;
    marking the work done on entry means whatever had not run yet never does.
    """
    source = (PRAUTO / "heartbeat.sh").read_text()
    cleanup = source[source.index("cleanup() {") : source.index("handle_signal() {")]

    assert "CLEANUP_IN_PROGRESS" in cleanup, "no re-entrancy guard distinct from completion"
    done_at = cleanup.index("CLEANUP_DONE=true")
    release_at = cleanup.index("release_lock")
    assert done_at > release_at, (
        "CLEANUP_DONE is set before the teardown finishes, so an interrupted "
        "cleanup can never be retried"
    )


def test_provisioning_keeps_command_output_private_and_reports_only_safe_status(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env.dev"
    env_file.write_text(
        "DATASPOKE_KUBE_CLUSTER=test\nDATASPOKE_DEV_LOCK_URL=http://127.0.0.1:9221\n"
    )
    install = tmp_path / "helm-charts/bin/install.sh"
    install.parent.mkdir(parents=True)
    install.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'PRIVATE_TOKEN=do-not-leak\\n'\n"
        "printf 'STDERR_TOKEN=stderr-must-not-leak\\n' >&2\n"
        "head -c 20000 /dev/zero | tr '\\0' X\n"
        "exit 0\n"
    )
    install.chmod(0o755)
    state_dir = tmp_path / "state"
    script = "\n".join(
        [
            f"PRAUTO_DIR={shlex.quote(str(state_dir))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/helpers.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/pr.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/phases.sh'))}",
            f"REPO_DIR={shlex.quote(str(tmp_path))}",
            f"PRAUTO_DEV_ENV_FILE={shlex.quote(str(env_file))}",
            "DATASPOKE_PROVISION_TIMEOUT_SECS=5",
            "resolve_dev_env",
            f"provision_dev_env {shlex.quote(str(env_file))}",
        ]
    )
    result = _run(script)
    assert result.returncode == 0, result.stderr
    assert "PRIVATE_TOKEN" not in result.stdout
    assert "do-not-leak" not in result.stdout
    assert "STDERR_TOKEN" not in result.stdout
    assert "STDERR_TOKEN" not in result.stderr
    logs = list((state_dir / "state").glob("provision-*"))
    assert logs
    assert any("PRIVATE_TOKEN=do-not-leak" in path.read_text() for path in logs)
    assert any("STDERR_TOKEN=stderr-must-not-leak" in path.read_text() for path in logs)
    assert all(path.stat().st_size <= 12000 for path in logs)
    assert all(path.stat().st_mode & 0o077 == 0 for path in logs)


def test_provisioning_retains_short_output_byte_for_byte(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.dev"
    env_file.write_text(
        "DATASPOKE_KUBE_CLUSTER=test\nDATASPOKE_DEV_LOCK_URL=http://127.0.0.1:9221\n"
    )
    install = tmp_path / "helm-charts/bin/install.sh"
    install.parent.mkdir(parents=True)
    install.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'short stdout\\n'\n"
        "printf 'short stderr\\n' >&2\n"
        "printf 'final bytes'\n"
    )
    install.chmod(0o755)
    state_dir = tmp_path / "state"
    script = "\n".join(
        [
            f"PRAUTO_DIR={shlex.quote(str(state_dir))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/helpers.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/pr.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/phases.sh'))}",
            f"REPO_DIR={shlex.quote(str(tmp_path))}",
            f"PRAUTO_DEV_ENV_FILE={shlex.quote(str(env_file))}",
            "DATASPOKE_PROVISION_TIMEOUT_SECS=5",
            "resolve_dev_env",
            f"provision_dev_env {shlex.quote(str(env_file))}",
        ]
    )
    result = _run(script)
    assert result.returncode == 0, result.stderr
    logs = list((state_dir / "state").glob("provision-*"))
    assert len(logs) == 1
    assert logs[0].read_bytes() == b"short stdout\nshort stderr\nfinal bytes"


def test_provisioning_retains_bounded_head_and_tail_of_unterminated_output(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env.dev"
    env_file.write_text(
        "DATASPOKE_KUBE_CLUSTER=test\nDATASPOKE_DEV_LOCK_URL=http://127.0.0.1:9221\n"
    )
    install = tmp_path / "helm-charts/bin/install.sh"
    install.parent.mkdir(parents=True)
    install.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'FIRST_MARKER:'\n"
        "head -c 7000 /dev/zero | tr '\\0' A\n"
        "head -c 7000 /dev/zero | tr '\\0' Z\n"
        "printf 'LAST_MARKER:'\n"
    )
    install.chmod(0o755)
    state_dir = tmp_path / "state"
    script = "\n".join(
        [
            f"PRAUTO_DIR={shlex.quote(str(state_dir))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/helpers.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/pr.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/phases.sh'))}",
            f"REPO_DIR={shlex.quote(str(tmp_path))}",
            f"PRAUTO_DEV_ENV_FILE={shlex.quote(str(env_file))}",
            "DATASPOKE_PROVISION_TIMEOUT_SECS=5",
            "resolve_dev_env",
            f"provision_dev_env {shlex.quote(str(env_file))}",
        ]
    )
    result = _run(script)
    assert result.returncode == 0, result.stderr
    logs = list((state_dir / "state").glob("provision-*"))
    assert len(logs) == 1
    retained = logs[0].read_bytes()
    assert len(retained) == 12000
    assert retained.startswith(b"FIRST_MARKER:")
    assert b"LAST_MARKER:" in retained
    assert retained.endswith(b"LAST_MARKER:")


def _remote_chart_fixture(tmp_path: Path) -> tuple[Path, Path]:
    chart = tmp_path / "chart"
    (chart / "charts").mkdir(parents=True)
    archive = chart / "charts/remote-1.2.3.tgz"
    archive.write_bytes(b"correct archive")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (chart / "Chart.lock").write_text(
        "dependencies:\n- name: remote\n  version: 1.2.3\n  repository: https://charts.example.test\n"
    )
    manifest = chart / "dependency-integrity.yaml"
    manifest.write_text(
        "dependencies:\n- name: remote\n  version: 1.2.3\n"
        "  repository: https://charts.example.test\n  sha256: "
        + digest
        + "\n"
    )
    return chart, archive


def test_remote_dependency_cache_rejects_absent_integrity_manifest(tmp_path: Path) -> None:
    chart, _ = _remote_chart_fixture(tmp_path)
    (chart / "dependency-integrity.yaml").unlink()
    result = _run(_source_helpers() + f"\n_remote_dependency_cache_verified {chart}")
    assert result.returncode != 0


def test_remote_dependency_cache_rejects_manifest_tuple_mismatch(tmp_path: Path) -> None:
    chart, _ = _remote_chart_fixture(tmp_path)
    manifest = chart / "dependency-integrity.yaml"
    manifest.write_text(manifest.read_text().replace("name: remote", "name: other"))
    result = _run(_source_helpers() + f"\n_remote_dependency_cache_verified {chart}")
    assert result.returncode != 0


def test_remote_dependency_cache_requires_matching_manifest_digest(tmp_path: Path) -> None:
    chart, archive = _remote_chart_fixture(tmp_path)
    ok = _run(
        _source_helpers()
        + f"\n_remote_dependency_cache_verified {shlex.quote(str(chart))}"
    )
    assert ok.returncode == 0

    archive.write_bytes(b"tampered archive")
    bad = _run(
        _source_helpers()
        + f"\n_remote_dependency_cache_verified {shlex.quote(str(chart))}"
    )
    assert bad.returncode != 0


def test_dependency_reacquisition_does_not_accept_unverifiable_archive(tmp_path: Path) -> None:
    chart, archive = _remote_chart_fixture(tmp_path)
    archive.write_bytes(b"preexisting bad bytes")
    fake_helm = tmp_path / "helm"
    fake_helm.write_text(
        "#!/usr/bin/env bash\n"
        "mkdir -p \"$3/charts\"\n"
        "printf wrong-bytes > \"$3/charts/remote-1.2.3.tgz\"\n"
    )
    fake_helm.chmod(0o755)
    result = _run(
        _source_helpers()
        + "\nsleep() { :; }"
        + f"\nDATASPOKE_CHART_DEPS_TIMEOUT_SECS=1 _build_chart_deps {shlex.quote(str(chart))}",
        env={"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode != 0
    assert not archive.exists()


def test_local_subchart_is_repackaged_on_every_dependency_build(tmp_path: Path) -> None:
    chart = tmp_path / "chart"
    local = chart / "subcharts/local"
    (chart / "charts").mkdir(parents=True)
    local.mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: root\nversion: 0.1.0\ndependencies:\n"
        "  - name: local\n    repository: file://subcharts/local\n    version: 0.1.0\n"
    )
    (local / "Chart.yaml").write_text("apiVersion: v2\nname: local\nversion: 0.1.0\n")
    archive = chart / "charts/local-0.1.0.tgz"
    archive.write_text("stale-package")
    fake_helm = tmp_path / "helm"
    fake_helm.write_text(
        "#!/usr/bin/env bash\n"
        "count=0; [[ -f \"$HELM_COUNT\" ]] && count=$(cat \"$HELM_COUNT\")\n"
        "count=$((count + 1)); printf '%s' \"$count\" > \"$HELM_COUNT\"\n"
        "printf '%s\\n' \"$*\" >> \"$HELM_CALLS\"\n"
        "mkdir -p \"$4\"\n"
        "printf 'packaged-%s' \"$count\" > \"$4/local-0.1.0.tgz\"\n"
    )
    fake_helm.chmod(0o755)
    calls = tmp_path / "helm.calls"
    helm_count = tmp_path / "helm.count"
    first = _run(
        _source_helpers()
        + f"\n_package_local_chart_deps {shlex.quote(str(chart))}",
        env={
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "HELM_CALLS": str(calls),
            "HELM_COUNT": str(helm_count),
        },
    )
    assert first.returncode == 0, first.stderr
    assert archive.read_text() == "packaged-1"
    (local / "Chart.yaml").write_text(
        "apiVersion: v2\nname: local\nversion: 0.1.0\ndescription: changed\n"
    )
    second = _run(
        _source_helpers()
        + f"\n_package_local_chart_deps {shlex.quote(str(chart))}",
        env={
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "HELM_CALLS": str(calls),
            "HELM_COUNT": str(helm_count),
        },
    )
    assert second.returncode == 0, second.stderr
    assert archive.read_text() == "packaged-2"
    assert "package" in calls.read_text()
