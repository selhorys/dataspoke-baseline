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
