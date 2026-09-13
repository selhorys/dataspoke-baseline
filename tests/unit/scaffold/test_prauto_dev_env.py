"""Hermetic tests for prauto dev-environment propagation and cleanup."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[3]
PRAUTO = ROOT / ".prauto"


def _run(script: str, *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    run_env.update(env)
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
            f"PRAUTO_DIR={shlex.quote(str(PRAUTO))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/helpers.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/phases.sh'))}",
        ]
    )


def _source_phases_with_state_dir(state_dir: Path) -> str:
    # phases.sh computes DEV_ENV_STATE_FILE from $PRAUTO_DIR at source time (a
    # top-level assignment, not inside a function), so PRAUTO_DIR must already
    # point at a scratch directory *before* the source line runs — setting it
    # afterward has no effect on that already-evaluated path. This keeps a
    # provisioning test that never calls teardown_provisioned_dev_env from
    # writing its marker into the real repo's .prauto/state/.
    return "\n".join(
        [
            f"PRAUTO_DIR={shlex.quote(str(state_dir))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/helpers.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/pr.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/phases.sh'))}",
        ]
    )


def test_integration_commands_receive_the_resolved_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.dev"
    env_file.write_text("DATASPOKE_TEST_VALUE=from-env-file\n")
    probe = tmp_path / "uv"
    probe.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s|%s\\n' \"$ENV_FILE\" \"$DATASPOKE_TEST_VALUE\" >> \"$PROBE_OUTPUT\"\n"
    )
    probe.chmod(0o755)
    (tmp_path / "tests/integration/spot").mkdir(parents=True)
    (tmp_path / "tests/integration/api_wired").mkdir(parents=True)
    output = tmp_path / "probe.out"

    result = _run(
        _source_phases()
        + f"\ncd {shlex.quote(str(tmp_path))}"
        + f"\nrun_integration_groups {shlex.quote(str(env_file))}",
        env={
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "PROBE_OUTPUT": str(output),
        },
    )

    assert result.returncode == 0, result.stderr
    assert output.read_text().splitlines() == [
        f"{env_file}|from-env-file",
        f"{env_file}|from-env-file",
    ]


def test_successful_provision_is_torn_down_with_full_delete(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.dev"
    env_file.write_text("DATASPOKE_KUBE_CLUSTER=test\n")
    install = tmp_path / "helm-charts/bin/install.sh"
    uninstall = tmp_path / "helm-charts/bin/uninstall.sh"
    install.parent.mkdir(parents=True)
    install.write_text("#!/usr/bin/env bash\nexit 0\n")
    uninstall.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" > \"$UNINSTALL_OUTPUT\"\n"
    )
    install.chmod(0o755)
    uninstall.chmod(0o755)
    output = tmp_path / "uninstall.out"

    result = _run(
        _source_phases()
        + f"\nREPO_DIR={shlex.quote(str(tmp_path))}"
        + f"\nprovision_dev_env {shlex.quote(str(env_file))}"
        + "\nteardown_provisioned_dev_env",
        env={"UNINSTALL_OUTPUT": str(output)},
    )

    assert result.returncode == 0, result.stderr
    assert output.read_text().strip() == (
        f"--profile dev --env-file {env_file} --no-question --delete-all"
    )


def test_preexisting_dev_environment_is_not_torn_down(tmp_path: Path) -> None:
    uninstall = tmp_path / "helm-charts/bin/uninstall.sh"
    uninstall.parent.mkdir(parents=True)
    uninstall.write_text("#!/usr/bin/env bash\nexit 99\n")
    uninstall.chmod(0o755)

    result = _run(
        _source_phases()
        + f"\nREPO_DIR={shlex.quote(str(tmp_path))}"
        + "\nteardown_provisioned_dev_env",
        env={},
    )

    assert result.returncode == 0, result.stderr


def _write_install_stub_rewriting_lock_url(install_path: Path, *, new_url: str) -> None:
    """A stub install.sh that mimics the real script's in-place env-file rewrite."""
    install_path.parent.mkdir(parents=True, exist_ok=True)
    install_path.write_text(
        "#!/usr/bin/env bash\n"
        "env_file=\"\"\n"
        "while (( $# )); do\n"
        "  case \"$1\" in\n"
        "    --env-file) env_file=\"$2\"; shift 2 ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        f"printf 'DATASPOKE_DEV_LOCK_URL={new_url}\\n' > \"$env_file\"\n"
        "exit 0\n"
    )
    install_path.chmod(0o755)


def test_provision_dev_env_reresolves_lock_url_after_install_rewrite(tmp_path: Path) -> None:
    # phases.sh `provision_dev_env`: after a successful install, it re-runs
    # resolve_dev_env so DEV_LOCK_URL reflects DATASPOKE_DEV_LOCK_URL as
    # rewritten by install.sh (e.g. a fresh LB IP), rather than the stale value
    # read before the install ran.
    env_file = tmp_path / ".env.dev"
    env_file.write_text("DATASPOKE_DEV_LOCK_URL=http://old:9221\n")
    _write_install_stub_rewriting_lock_url(
        tmp_path / "helm-charts/bin/install.sh", new_url="http://new:9221"
    )
    state_dir = tmp_path / "prauto-state"

    result = _run(
        _source_phases_with_state_dir(state_dir)
        + f"\nREPO_DIR={shlex.quote(str(tmp_path))}"
        + f"\nPRAUTO_DEV_ENV_FILE={shlex.quote(str(env_file))}"
        + "\nresolve_dev_env"
        + f"\nprovision_dev_env {shlex.quote(str(env_file))}"
        + '\nprintf \'%s\' "$DEV_LOCK_URL"',
        env={},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "http://new:9221/lock"
    assert env_file.read_text() == "DATASPOKE_DEV_LOCK_URL=http://new:9221\n"


def test_provision_dev_env_fails_when_reresolve_fails_but_leaves_provisioned_true(
    tmp_path: Path,
) -> None:
    # phases.sh `provision_dev_env`: it returns 1 if the post-install re-resolve
    # fails (e.g. install.sh deleted the env file), but the teardown marker
    # (DEV_ENV_PROVISIONED) is still set before that point — a failed
    # re-resolve must not suppress the durable-teardown obligation.
    env_file = tmp_path / ".env.dev"
    env_file.write_text("DATASPOKE_DEV_LOCK_URL=http://old:9221\n")
    install = tmp_path / "helm-charts/bin/install.sh"
    install.parent.mkdir(parents=True)
    install.write_text(
        "#!/usr/bin/env bash\n"
        "env_file=\"\"\n"
        "while (( $# )); do\n"
        "  case \"$1\" in\n"
        "    --env-file) env_file=\"$2\"; shift 2 ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        "rm -f \"$env_file\"\n"
        "exit 0\n"
    )
    install.chmod(0o755)
    state_dir = tmp_path / "prauto-state"

    result = _run(
        _source_phases_with_state_dir(state_dir)
        + f"\nREPO_DIR={shlex.quote(str(tmp_path))}"
        + f"\nPRAUTO_DEV_ENV_FILE={shlex.quote(str(env_file))}"
        + "\nresolve_dev_env"
        + f"\nprovision_dev_env {shlex.quote(str(env_file))}; rc=$?"
        + '\nprintf \'rc=%s provisioned=%s\' "$rc" "$DEV_ENV_PROVISIONED"',
        env={},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "rc=1 provisioned=true"
    assert not env_file.exists()


def _write_flaky_health_check_stub(health_check_path: Path) -> None:
    """A health-check.sh stub that fails its first invocation (pre-provision,
    red) and passes every invocation after (post-provision), matching
    dev_env_healthy's own red -> provision -> re-check flow. Progress is
    tracked via $HEALTH_CHECK_COUNT_FILE, since dev_env_healthy invokes the
    same script twice (before and after a provisioning attempt)."""
    health_check_path.parent.mkdir(parents=True, exist_ok=True)
    health_check_path.write_text(
        "#!/usr/bin/env bash\n"
        "count=0\n"
        "[[ -f \"$HEALTH_CHECK_COUNT_FILE\" ]] && count=$(cat \"$HEALTH_CHECK_COUNT_FILE\")\n"
        "count=$((count + 1))\n"
        "printf '%s' \"$count\" > \"$HEALTH_CHECK_COUNT_FILE\"\n"
        "[[ \"$count\" -eq 1 ]] && exit 1\n"
        "exit 0\n"
    )
    health_check_path.chmod(0o755)


def _write_host_gated_curl_stub(curl_path: Path, *, allowed_host: str) -> None:
    """A curl stub that records every invocation's argv to $CURL_CALLS and
    only "succeeds" (reachable) when the URL contains `allowed_host` — a call
    against any other host (a stale pre-provision LB address) simulates a
    connection failure. Also emits the `-w "%{http_code}"` format value
    ("200") so a lock-acquire call's HTTP-code capture succeeds."""
    curl_path.parent.mkdir(parents=True, exist_ok=True)
    curl_path.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$CURL_CALLS\"\n"
        "url=\"\"\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in http*) url=\"$arg\" ;; esac\n"
        "done\n"
        f'case "$url" in\n'
        f"  *{allowed_host}*) : ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n"
        "[[ \" $* \" == *\" -w \"* ]] && printf '200'\n"
        "exit 0\n"
    )
    curl_path.chmod(0o755)


def _write_noop_gh_stub(gh_path: Path, *, calls_env_var: str = "GH_CALLS") -> None:
    gh_path.parent.mkdir(parents=True, exist_ok=True)
    gh_path.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$*" >> "${calls_env_var}"\n'
        "exit 0\n"
    )
    gh_path.chmod(0o755)


def _write_git_diff_stub(git_path: Path, *, touched_path: str) -> None:
    """A stub whose `diff` subcommand always reports one changed path, so
    diff_touches() sees a match without needing a real git repository or
    origin remote."""
    git_path.parent.mkdir(parents=True, exist_ok=True)
    git_path.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == diff ]]; then\n'
        f"  printf '%s\\n' {shlex.quote(touched_path)}\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    git_path.chmod(0o755)


def test_integration_fix_loop_probes_only_the_reresolved_lock_host(tmp_path: Path) -> None:
    # End-to-end check that run_integration_test_fix reads DEV_LOCK_URL *after*
    # dev_env_healthy (which provisions on a red health check), so a stale LB
    # host from before provisioning is never probed. The health check fails on
    # its first run (pre-provision) and passes on its second (post-provision),
    # matching dev_env_healthy's own red -> provision -> re-check flow.
    env_file = tmp_path / ".env.dev"
    env_file.write_text("DATASPOKE_DEV_LOCK_URL=http://old:9221\n")
    _write_install_stub_rewriting_lock_url(
        tmp_path / "helm-charts/bin/install.sh", new_url="http://new:9221"
    )
    _write_flaky_health_check_stub(tmp_path / "helm-charts/bin/health-check.sh")
    health_count_file = tmp_path / "health-check.count"

    curl_calls = tmp_path / "curl.calls"
    _write_host_gated_curl_stub(tmp_path / "bin/curl", allowed_host="new")
    _write_noop_gh_stub(tmp_path / "bin/gh")

    (tmp_path / "cwd/tests/integration").mkdir(parents=True)
    state_dir = tmp_path / "prauto-state"

    result = _run(
        _source_phases_with_state_dir(state_dir)
        + f"\ncd {shlex.quote(str(tmp_path / 'cwd'))}"
        + f"\nREPO_DIR={shlex.quote(str(tmp_path))}"
        + f"\nPRAUTO_DEV_ENV_FILE={shlex.quote(str(env_file))}"
        + "\nPRAUTO_GITHUB_REPO=acme/dataspoke"
        + "\nPRAUTO_WORKER_ID=worker"
        + '\nrun_integration_test_fix 999 "prauto/I-999"'
        + '\nprintf \'DEV_LOCK_URL=%s\' "$DEV_LOCK_URL"',
        env={
            "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "CURL_CALLS": str(curl_calls),
            "HEALTH_CHECK_COUNT_FILE": str(health_count_file),
            "GH_CALLS": str(tmp_path / "gh.calls"),
        },
    )

    assert result.returncode == 0, result.stderr
    assert "DEV_LOCK_URL=http://new:9221/lock" in result.stdout
    calls_text = curl_calls.read_text()
    assert "old:9221" not in calls_text
    assert calls_text.count("new:9221") >= 2  # status probe + acquire, at minimum


def test_e2e_test_fix_probes_only_the_reresolved_lock_host(tmp_path: Path) -> None:
    # Same ordering contract as above, exercised through run_e2e_test_fix:
    # it too reads DEV_LOCK_URL only after dev_env_healthy, so a stale
    # pre-provision host must never be probed here either.
    env_file = tmp_path / ".env.dev"
    env_file.write_text("DATASPOKE_DEV_LOCK_URL=http://old:9221\n")
    _write_install_stub_rewriting_lock_url(
        tmp_path / "helm-charts/bin/install.sh", new_url="http://new:9221"
    )
    _write_flaky_health_check_stub(tmp_path / "helm-charts/bin/health-check.sh")
    health_count_file = tmp_path / "health-check.count"

    curl_calls = tmp_path / "curl.calls"
    _write_host_gated_curl_stub(tmp_path / "bin/curl", allowed_host="new")
    _write_noop_gh_stub(tmp_path / "bin/gh")

    # diff_touches shells out to `git`; stub it to always report a touched
    # E2E path so run_e2e_test_fix doesn't skip out before reaching the probe.
    _write_git_diff_stub(tmp_path / "bin/git", touched_path="tests/e2e/example.spec.ts")

    pnpm_stub = tmp_path / "bin/pnpm"
    pnpm_stub.write_text("#!/usr/bin/env bash\nexit 0\n")
    pnpm_stub.chmod(0o755)

    # WORKTREE_DIR is deliberately left unset: deploy_branch_frontend's own
    # guard then fails fast (before touching kubectl/helm/docker), which is
    # fine here — the probe under test happens earlier in the function, and
    # the lock-release call after the loop still hits the resolved host.
    (tmp_path / "cwd/tests/e2e").mkdir(parents=True)
    state_dir = tmp_path / "prauto-state"

    result = _run(
        _source_phases_with_state_dir(state_dir)
        + f"\ncd {shlex.quote(str(tmp_path / 'cwd'))}"
        + f"\nREPO_DIR={shlex.quote(str(tmp_path))}"
        + f"\nPRAUTO_DEV_ENV_FILE={shlex.quote(str(env_file))}"
        + "\nPRAUTO_BASE_BRANCH=dev"
        + "\nPRAUTO_GITHUB_REPO=acme/dataspoke"
        + "\nPRAUTO_WORKER_ID=worker"
        + '\nrun_e2e_test_fix 999 "prauto/I-999"; rc=$?'
        + '\nprintf \'rc=%s DEV_LOCK_URL=%s\' "$rc" "$DEV_LOCK_URL"',
        env={
            "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "CURL_CALLS": str(curl_calls),
            "HEALTH_CHECK_COUNT_FILE": str(health_count_file),
            "GH_CALLS": str(tmp_path / "gh.calls"),
        },
    )

    assert result.returncode == 0, result.stderr
    assert "rc=0" in result.stdout
    assert "DEV_LOCK_URL=http://new:9221/lock" in result.stdout
    calls_text = curl_calls.read_text()
    assert "old:9221" not in calls_text
    assert calls_text.count("new:9221") >= 2  # status probe + acquire, at minimum


def test_integration_tests_with_protocol_probes_only_the_reresolved_lock_host(
    tmp_path: Path,
) -> None:
    # Same ordering contract again, exercised through
    # run_integration_tests_with_protocol: it reads DEV_LOCK_URL only after
    # dev_env_healthy too, so a stale pre-provision host must never be probed.
    env_file = tmp_path / ".env.dev"
    env_file.write_text("DATASPOKE_DEV_LOCK_URL=http://old:9221\n")
    _write_install_stub_rewriting_lock_url(
        tmp_path / "helm-charts/bin/install.sh", new_url="http://new:9221"
    )
    _write_flaky_health_check_stub(tmp_path / "helm-charts/bin/health-check.sh")
    health_count_file = tmp_path / "health-check.count"

    curl_calls = tmp_path / "curl.calls"
    _write_host_gated_curl_stub(tmp_path / "bin/curl", allowed_host="new")
    # post_test_results_comment (called twice, for spot and api-wired) posts
    # via `gh pr comment`; a no-op stub lets that best-effort call succeed.
    _write_noop_gh_stub(tmp_path / "bin/gh")

    (tmp_path / "cwd").mkdir()
    state_dir = tmp_path / "prauto-state"

    result = _run(
        _source_phases_with_state_dir(state_dir)
        + f"\ncd {shlex.quote(str(tmp_path / 'cwd'))}"
        + f"\nREPO_DIR={shlex.quote(str(tmp_path))}"
        + f"\nPRAUTO_DEV_ENV_FILE={shlex.quote(str(env_file))}"
        + "\nPRAUTO_GITHUB_REPO=acme/dataspoke"
        + "\nPRAUTO_WORKER_ID=worker"
        + "\nrun_integration_tests_with_protocol 77"
        + '\nprintf \'DEV_LOCK_URL=%s\' "$DEV_LOCK_URL"',
        env={
            "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "CURL_CALLS": str(curl_calls),
            "HEALTH_CHECK_COUNT_FILE": str(health_count_file),
            "GH_CALLS": str(tmp_path / "gh.calls"),
        },
    )

    assert result.returncode == 0, result.stderr
    assert "DEV_LOCK_URL=http://new:9221/lock" in result.stdout
    calls_text = curl_calls.read_text()
    assert "old:9221" not in calls_text
    assert calls_text.count("new:9221") >= 2  # status probe + acquire, at minimum
    gh_calls_text = (tmp_path / "gh.calls").read_text()
    assert gh_calls_text.count("pr comment 77") == 2  # spot + api-wired results
