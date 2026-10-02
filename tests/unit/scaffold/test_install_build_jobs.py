"""Hermetic tests for `install.sh --build-jobs` and the docker-daemon-death hint (#149).

Everything here is bash-driven and cluster-free. The harnesses source the real
`helm-charts/bin/lib/helpers.sh`, slice the real function/argument-parsing text out of
`helm-charts/bin/install.sh`, stub `info`/`warn`/`error` onto one stream (stderr) and run
under `/bin/bash` (3.2 on macOS) so a bash-4-only construct fails here, not on an operator's
laptop. Nothing launches kubectl, helm or docker; the few runs of the real `install.sh`
only use inputs that are rejected before any cluster work, with PATH stubs that record any
accidental call.

Assertions derive from `spec/feature/HELM_CHART.md` (Flags row `--build-jobs`, §Image Builds
"Build concurrency bound", §Troubleshooting "Docker daemon dies during image builds") and the
approved plan for #149, not from incidental implementation behaviour.
"""

from __future__ import annotations

import os
import re
import shlex
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
INSTALL = ROOT / "helm-charts/bin/install.sh"
HELPERS = ROOT / "helm-charts/bin/lib/helpers.sh"
HELM_CHART_SPEC = ROOT / "spec/feature/HELM_CHART.md"

# The macOS system bash (3.2) is the platform that rules out `wait -n`; prefer it when present.
BASH = "/bin/bash" if Path("/bin/bash").exists() else "bash"

# Wording of the single hint, from the plan's design decision 4.
HINT_MARK = "Docker daemon appears to have died"
DAEMON_SIGNATURE = "write unix /var/run/docker.sock: broken pipe"
# Printed by install.sh only after the BUILD_JOBS resolution, so its absence proves the run
# was rejected before any install work started.
INSTALL_BANNER = "=== DataSpoke installation"
ORDINARY_FAILURE = "The command '/bin/sh -c pip install x' returned a non-zero code: 1"

# One shell word as printf %q renders it: backslash-escaped characters or plain characters.
_SHELL_WORD = r"(?:\\.|[^\s\\])+"

# Stub build task: records start/end markers so tests can derive concurrency.
# usage: stub.sh <events-file> <name> <hold-seconds> <exit-code> [log-text]
_STUB = """#!/bin/bash
echo "start $2" >> "$1"
sleep "$3"
if [ -n "${5:-}" ]; then printf '%s\\n' "$5"; fi
echo "end $2" >> "$1"
exit "$4"
"""


# --------------------------------------------------------------------------------------
# Source slicing
# --------------------------------------------------------------------------------------


def _install_text() -> str:
    return INSTALL.read_text()


def _function_text(text: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert match, f"function {name} not found"
    return match.group(0)


def _code_lines(text: str) -> list[str]:
    """Non-comment lines (comments legitimately mention `wait -n` as the rejected idiom)."""
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


def _task_functions_slice() -> str:
    """`PIDS=()` through the end of `_wait_all` (above the Secret management section)."""
    text = _install_text()
    start = text.index("PIDS=()\nLABELS=()\n")
    end = text.index("# Secret management helpers")
    block = text[start:end]
    for marker in ("_run_bg_build() {", "_wait_all() {", "_docker_daemon_death_hint() {"):
        assert marker in block, f"task-function slice is missing {marker}"
    return block


def _parse_case_slice() -> str:
    """The `--build-jobs)` case arm of the argument parser."""
    match = re.search(
        r"^    --build-jobs\)\n.*?(?=^    --[a-z-]+\))", _install_text(), re.S | re.M
    )
    assert match, "--build-jobs case arm not found"
    return match.group(0)


def _resolution_slice() -> str:
    """`source "$ENV_FILE"` through the BUILD_JOBS resolution/clamp (before namespace checks)."""
    text = _install_text()
    start = text.index('\nsource "$ENV_FILE"\n')
    end = text.index("\n# Every *_NAMESPACE var below")
    block = text[start:end]
    assert "assert_build_jobs" in block, "resolution block not sliced correctly"
    return block


# --------------------------------------------------------------------------------------
# Runners
# --------------------------------------------------------------------------------------


def _clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("DATASPOKE_") and k not in {"BUILD_JOBS", "ENV_FILE", "IMAGE_TAG"}
    }
    if extra:
        env.update(extra)
    return env


def _bash(
    script: str,
    *args: str,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, "-c", script, "bash", *args],
        capture_output=True,
        text=True,
        check=False,
        env=_clean_env(env),
        cwd=cwd,
        timeout=90,
    )


_STUBS = """
info()  { echo "INFO: $*" >&2; }
warn()  { echo "WARN: $*" >&2; }
error() { echo "ERROR: $*" >&2; exit 1; }
"""

# `_wait_all` hard-codes `sleep 5`; shrink only that so the tests stay fast. Other sleeps
# (the build-slot poll, the stub tasks) keep their real argument.
_FAST_SLEEP = """
sleep() { if [ "$1" = 5 ]; then command sleep 0.05; else command sleep "$@"; fi; }
"""


def _task_harness(body: str) -> str:
    return "\n".join(
        [
            "set -euo pipefail",
            f"source {shlex.quote(str(HELPERS))}",
            _STUBS,
            "START_TIME=$SECONDS",
            'BUILD_JOBS="${BUILD_JOBS:-}"',
            "trap 'echo ENV_FILE_AFTER=$ENV_FILE' EXIT",
            _task_functions_slice(),
            "_BUILD_SLOT_POLL_SECS=0.05",
            _FAST_SLEEP,
            body,
        ]
    )


class _Tasks:
    """Per-test stub-task fixture: INSTALL_TMPDIR, events file and stub script in tmp_path."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.install_tmp = tmp_path / "install-tmp"
        self.install_tmp.mkdir()
        self.events = tmp_path / "events.log"
        self.events.write_text("")
        self.stub = tmp_path / "stub.sh"
        self.stub.write_text(_STUB)
        self.stub.chmod(self.stub.stat().st_mode | stat.S_IXUSR)
        self.script_dir = tmp_path / "bin"
        self.script_dir.mkdir()
        self.env_file = tmp_path / ".env.prod"
        self.env_file.write_text("")

    def launch(self, label: str, *, hold: float = 0.1, rc: int = 0, log: str = "") -> str:
        """Shell line launching one stub task the way install.sh would for that label."""
        runner = "_run_bg_build" if label.startswith("build-") else "_run_bg"
        cmd = [
            runner,
            label,
            BASH,
            str(self.stub),
            str(self.events),
            label,
            str(hold),
            str(rc),
            log,
        ]
        return " ".join(shlex.quote(c) for c in cmd)

    def run(
        self,
        lines: list[str],
        *,
        build_jobs: str | None = None,
        frontend_mode: str = "cluster",
        image_tag: str = "dev",
        env_file: str | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        body = "\n".join([*lines, "_wait_all"])
        env = {
            "INSTALL_TMPDIR": str(self.install_tmp),
            "ENV_FILE": env_file if env_file is not None else str(self.env_file),
            "IMAGE_TAG": image_tag,
            "FRONTEND_MODE": frontend_mode,
            "SCRIPT_DIR": str(self.script_dir),
        }
        if build_jobs is not None:
            env["BUILD_JOBS"] = build_jobs
        return _bash(_task_harness(body), env=env, cwd=cwd)

    def timeline(self) -> list[tuple[str, str]]:
        return [
            (parts[0], parts[1])
            for ln in self.events.read_text().splitlines()
            if (parts := ln.split()) and len(parts) == 2
        ]

    def peak_concurrency(self, prefix: str = "") -> int:
        live = peak = 0
        for kind, name in self.timeline():
            if not name.startswith(prefix):
                continue
            live += 1 if kind == "start" else -1
            peak = max(peak, live)
        return peak


def _hint_line(stderr: str) -> str:
    lines = [ln for ln in stderr.splitlines() if HINT_MARK in ln]
    assert len(lines) == 1, f"expected exactly one hint line, got {len(lines)}:\n{stderr}"
    return lines[0]


# --------------------------------------------------------------------------------------
# assert_build_jobs
# --------------------------------------------------------------------------------------


def _assert_build_jobs(value: str) -> subprocess.CompletedProcess[str]:
    script = (
        f"source {shlex.quote(str(HELPERS))}\n"
        'assert_build_jobs "--build-jobs" "$VALUE"\n'
        "echo ACCEPTED"
    )
    return _bash(script, env={"VALUE": value})


@pytest.mark.parametrize("value", ["1", "4", "16", "999999", "10"])
def test_assert_build_jobs_accepts_positive_integers(value: str) -> None:
    """spec: HELM_CHART.md Flags `--build-jobs` — `<n>` is a positive integer (`^[1-9][0-9]*$`)."""
    result = _assert_build_jobs(value)
    assert result.returncode == 0, result.stderr
    assert "ACCEPTED" in result.stdout


@pytest.mark.parametrize(
    "value",
    [
        "0",
        "-1",
        "abc",
        "1.5",
        "",
        "2;rm",
        "$(id)",
        " 4",
        "4 ",
        "\t4",
        "4\n",
        "4\n5",
        "01",
        "+4",
        "0x10",
        "4a",
    ],
    ids=repr,
)
def test_assert_build_jobs_rejects_everything_else(value: str) -> None:
    """spec: HELM_CHART.md Flags `--build-jobs` — anything else is a hard error."""
    result = _assert_build_jobs(value)
    assert result.returncode != 0, f"{value!r} was accepted"
    assert "ACCEPTED" not in result.stdout
    assert "ERROR" in result.stderr
    # The error points at the knob the value came from.
    assert "--build-jobs" in result.stderr
    # A rejected value must never be evaluated as shell/arithmetic.
    assert "uid=" not in result.stderr + result.stdout


def test_assert_build_jobs_error_names_the_source_knob() -> None:
    script = f'source {shlex.quote(str(HELPERS))}\nassert_build_jobs "DATASPOKE_BUILD_JOBS" "abc"'
    result = _bash(script)
    assert result.returncode != 0
    assert "DATASPOKE_BUILD_JOBS" in result.stderr


# --------------------------------------------------------------------------------------
# Flag / env-var resolution (sliced from install.sh)
# --------------------------------------------------------------------------------------


def _resolve(
    tmp_path: Path,
    argv: list[str],
    *,
    env_file_text: str = "",
    shell_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env_file = tmp_path / ".env.dev"
    env_file.write_text(env_file_text)
    script = "\n".join(
        [
            "set -euo pipefail",
            f"source {shlex.quote(str(HELPERS))}",
            _STUBS,
            'BUILD_JOBS_FLAG=""',
            "BUILD_JOBS_FLAG_SET=false",
            'while [[ $# -gt 0 ]]; do',
            '  case "$1" in',
            _parse_case_slice(),
            '    *) error "Unknown option: $1" ;;',
            "  esac",
            "done",
            f"ENV_FILE={shlex.quote(str(env_file))}",
            _resolution_slice(),
            'echo "BUILD_JOBS=[${BUILD_JOBS}]"',
        ]
    )
    return _bash(script, *argv, env=shell_env)


def _resolved(result: subprocess.CompletedProcess[str]) -> str:
    assert result.returncode == 0, result.stderr
    match = re.search(r"^BUILD_JOBS=\[(.*)\]$", result.stdout, re.M)
    assert match, result.stdout
    return match.group(1)


def test_resolution_unset_means_unbounded(tmp_path: Path) -> None:
    assert _resolved(_resolve(tmp_path, [])) == ""


def test_resolution_flag_alone(tmp_path: Path) -> None:
    assert _resolved(_resolve(tmp_path, ["--build-jobs", "3"])) == "3"


def test_resolution_env_var_alone_is_honoured(tmp_path: Path) -> None:
    result = _resolve(tmp_path, [], shell_env={"DATASPOKE_BUILD_JOBS": "5"})
    assert _resolved(result) == "5"


def test_resolution_flag_wins_over_env_var(tmp_path: Path) -> None:
    result = _resolve(tmp_path, ["--build-jobs", "2"], shell_env={"DATASPOKE_BUILD_JOBS": "5"})
    assert _resolved(result) == "2"


def test_resolution_blank_env_var_counts_as_unset(tmp_path: Path) -> None:
    result = _resolve(tmp_path, [], shell_env={"DATASPOKE_BUILD_JOBS": ""})
    assert _resolved(result) == ""


def test_resolution_blank_env_var_does_not_mask_the_flag(tmp_path: Path) -> None:
    result = _resolve(tmp_path, ["--build-jobs", "2"], shell_env={"DATASPOKE_BUILD_JOBS": ""})
    assert _resolved(result) == "2"


def test_resolution_blank_flag_value_is_an_error_even_with_valid_env_var(tmp_path: Path) -> None:
    result = _resolve(tmp_path, ["--build-jobs", ""], shell_env={"DATASPOKE_BUILD_JOBS": "5"})
    assert result.returncode != 0
    assert "ERROR" in result.stderr
    assert "BUILD_JOBS=[" not in result.stdout


def test_resolution_flag_without_value_is_an_error(tmp_path: Path) -> None:
    result = _resolve(tmp_path, ["--build-jobs"])
    assert result.returncode != 0
    assert "ERROR" in result.stderr
    assert "--build-jobs" in result.stderr
    assert "BUILD_JOBS=[" not in result.stdout


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "1.5", "01"])
def test_resolution_invalid_flag_value_is_an_error(tmp_path: Path, bad: str) -> None:
    result = _resolve(tmp_path, ["--build-jobs", bad])
    assert result.returncode != 0
    assert "ERROR" in result.stderr
    assert "BUILD_JOBS=[" not in result.stdout


def test_resolution_invalid_env_var_is_an_error_naming_the_variable(tmp_path: Path) -> None:
    result = _resolve(tmp_path, [], shell_env={"DATASPOKE_BUILD_JOBS": "abc"})
    assert result.returncode != 0
    assert "DATASPOKE_BUILD_JOBS" in result.stderr


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("123456", "123456"),
        ("999999", "999999"),
        ("1000000", "999999"),
        ("1234567", "999999"),
        ("99999999999999999999999", "999999"),
    ],
)
def test_resolution_clamps_values_longer_than_six_digits(
    tmp_path: Path, given: str, expected: str
) -> None:
    """spec: HELM_CHART.md Flags — values longer than 6 digits are clamped to `999999`."""
    assert _resolved(_resolve(tmp_path, ["--build-jobs", given])) == expected
    assert (
        _resolved(_resolve(tmp_path, [], shell_env={"DATASPOKE_BUILD_JOBS": given})) == expected
    )


def test_resolution_env_file_assignment_overrides_shell_export(tmp_path: Path) -> None:
    """spec: HELM_CHART.md Flags — an env-file assignment overrides a shell-exported value."""
    result = _resolve(
        tmp_path,
        [],
        env_file_text="DATASPOKE_BUILD_JOBS=3\n",
        shell_env={"DATASPOKE_BUILD_JOBS": "5"},
    )
    assert _resolved(result) == "3"


def test_resolution_blank_env_file_assignment_overrides_shell_export(tmp_path: Path) -> None:
    """spec: HELM_CHART.md Flags — "even a blank one", so a blanked line means unbounded."""
    result = _resolve(
        tmp_path,
        [],
        env_file_text="DATASPOKE_BUILD_JOBS=\n",
        shell_env={"DATASPOKE_BUILD_JOBS": "5"},
    )
    assert _resolved(result) == ""


def test_resolution_flag_wins_over_env_file_assignment(tmp_path: Path) -> None:
    result = _resolve(
        tmp_path, ["--build-jobs", "2"], env_file_text="DATASPOKE_BUILD_JOBS=3\n"
    )
    assert _resolved(result) == "2"


# --------------------------------------------------------------------------------------
# The real install.sh: only inputs rejected before any cluster work
# --------------------------------------------------------------------------------------


def _real_install(
    tmp_path: Path, args: list[str], env_file_text: str = ""
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the real install.sh with PATH stubs that record any kubectl/helm/docker call."""
    stubs = tmp_path / "stub-path"
    stubs.mkdir()
    marker = tmp_path / "cluster-tool-called"
    for tool in ("kubectl", "helm", "docker", "gcloud", "aws"):
        path = stubs / tool
        path.write_text(f'#!/bin/sh\necho "{tool} $*" >> {shlex.quote(str(marker))}\nexit 99\n')
        path.chmod(0o755)
    env_file = tmp_path / ".env.dev"
    env_file.write_text(env_file_text)
    result = subprocess.run(
        [BASH, str(INSTALL), "--profile", "dev", "--env-file", str(env_file), *args],
        capture_output=True,
        text=True,
        check=False,
        env=_clean_env({"PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}"}),
        cwd=tmp_path,
        timeout=60,
    )
    return result, marker


def test_install_help_documents_build_jobs() -> None:
    """spec: HELM_CHART.md Flags row `--build-jobs` / its env var, surfaced by --help."""
    result = subprocess.run(
        [BASH, str(INSTALL), "--help"],
        capture_output=True,
        text=True,
        check=False,
        env=_clean_env(),
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "--build-jobs" in result.stdout
    assert "DATASPOKE_BUILD_JOBS" in result.stdout


@pytest.mark.parametrize("args", [["--build-jobs", "0"], ["--build-jobs", ""], ["--build-jobs"]])
def test_real_install_rejects_bad_flag_before_any_cluster_work(
    tmp_path: Path, args: list[str]
) -> None:
    result, marker = _real_install(tmp_path, args)
    assert result.returncode != 0
    assert "ERROR" in result.stderr
    assert "--build-jobs" in result.stderr
    # Rejected before the install proper began (the banner is printed after resolution).
    assert INSTALL_BANNER not in result.stdout + result.stderr
    assert not marker.exists(), marker.read_text()


def test_real_install_rejects_bad_env_file_value_before_any_cluster_work(tmp_path: Path) -> None:
    result, marker = _real_install(tmp_path, [], env_file_text="DATASPOKE_BUILD_JOBS=abc\n")
    assert result.returncode != 0
    assert "DATASPOKE_BUILD_JOBS" in result.stderr
    assert INSTALL_BANNER not in result.stdout + result.stderr
    assert not marker.exists(), marker.read_text()


# --------------------------------------------------------------------------------------
# _run_bg_build: the concurrency bound
# --------------------------------------------------------------------------------------


def _four_builds(tasks: _Tasks, hold: float = 0.5) -> list[str]:
    names = ("api", "airflow", "postgres", "frontend")
    return [tasks.launch(f"build-{n}", hold=hold) for n in names]


@pytest.mark.parametrize(
    ("build_jobs", "expected_peak"),
    [("1", 1), ("2", 2), (None, 4), ("", 4)],
    ids=["serial", "two", "unset", "blank"],
)
def test_run_bg_build_bounds_concurrent_builds(
    tmp_path: Path, build_jobs: str | None, expected_peak: int
) -> None:
    """spec: HELM_CHART.md §Image Builds "Build concurrency bound" — caps builds in flight."""
    tasks = _Tasks(tmp_path)
    result = tasks.run(_four_builds(tasks), build_jobs=build_jobs)
    assert result.returncode == 0, result.stderr
    assert tasks.peak_concurrency() == expected_peak
    # Every build ran to completion and was reported.
    timeline = tasks.timeline()
    assert sorted(n for k, n in timeline if k == "end") == [
        "build-airflow",
        "build-api",
        "build-frontend",
        "build-postgres",
    ]
    assert result.stderr.count("[OK] build-") == 4


def test_run_bg_build_unset_never_announces_a_wait(tmp_path: Path) -> None:
    tasks = _Tasks(tmp_path)
    result = tasks.run(_four_builds(tasks, hold=0.2))
    assert result.returncode == 0, result.stderr
    assert "waiting for a build slot" not in result.stderr.lower()


def test_run_bg_build_announces_the_wait_when_bound(tmp_path: Path) -> None:
    """plan: a one-line "waiting for a build slot" note so the pause is not silent."""
    tasks = _Tasks(tmp_path)
    result = tasks.run(_four_builds(tasks, hold=0.3), build_jobs="2")
    assert result.returncode == 0, result.stderr
    waits = [ln for ln in result.stderr.splitlines() if "waiting for a build slot" in ln.lower()]
    # The first two builds find free slots; the third had to wait (the fourth may or may not,
    # depending on whether the first two finished together).
    assert 1 <= len(waits) <= 2, result.stderr
    assert all("2/2" in ln for ln in waits)
    assert "build-api" not in waits[0] and "build-airflow" not in waits[0]


def test_run_bg_build_serial_bound_orders_builds_without_overlap(tmp_path: Path) -> None:
    tasks = _Tasks(tmp_path)
    result = tasks.run(_four_builds(tasks, hold=0.3), build_jobs="1")
    assert result.returncode == 0, result.stderr
    timeline = tasks.timeline()
    # start/end strictly alternate: no build starts before the previous one has ended.
    assert [k for k, _ in timeline] == ["start", "end"] * 4


def test_bound_does_not_count_or_delay_peripherals(tmp_path: Path) -> None:
    """spec: §Build concurrency bound — peripherals are never counted or delayed.

    A long-running non-build task (`datahub`) is launched first via plain `_run_bg`; at
    BUILD_JOBS=1 the first build must still start at once, alongside it, and only the SECOND
    build waits (1 of 1 build slots taken -- the peripheral is not counted).
    """
    tasks = _Tasks(tmp_path)
    lines = [
        tasks.launch("datahub", hold=1.2),
        tasks.launch("build-a", hold=0.4),
        tasks.launch("build-b", hold=0.4),
    ]
    result = tasks.run(lines, build_jobs="1")
    assert result.returncode == 0, result.stderr
    timeline = tasks.timeline()
    index = {event: i for i, event in enumerate(timeline)}
    # The build started while the peripheral was still running ...
    # (Both starts race at millisecond scale, so only their order relative to the end matters.)
    assert index[("start", "datahub")] < index[("end", "datahub")]
    assert index[("start", "build-a")] < index[("end", "datahub")]
    # ... the builds themselves stayed serial ...
    assert index[("end", "build-a")] < index[("start", "build-b")]
    assert tasks.peak_concurrency("build-") == 1
    # ... and the peripheral overlapped a build (peak across all tasks is 2).
    assert tasks.peak_concurrency() == 2
    waits = [ln for ln in result.stderr.splitlines() if "waiting for a build slot" in ln.lower()]
    assert len(waits) == 1, result.stderr
    assert "1/1" in waits[0], waits[0]


def test_bound_leaves_plain_background_tasks_unthrottled(tmp_path: Path) -> None:
    tasks = _Tasks(tmp_path)
    lines = [
        tasks.launch(label, hold=0.5) for label in ("datahub", "langfuse", "dummy-data", "dev-lock")
    ]
    result = tasks.run(lines, build_jobs="1")
    assert result.returncode == 0, result.stderr
    assert tasks.peak_concurrency() == 4
    assert "waiting for a build slot" not in result.stderr.lower()


# --------------------------------------------------------------------------------------
# bash 3.2 compatibility
# --------------------------------------------------------------------------------------


def test_new_code_uses_no_wait_dash_n() -> None:
    """spec: §Build concurrency bound — a `kill -0` poll, not `wait -n` (macOS bash is 3.2)."""
    install = _install_text()
    helpers = HELPERS.read_text()
    new_code = [
        _function_text(install, name)
        for name in (
            "_run_bg_build",
            "_count_live_builds",
            "_build_parallelism_label",
            "_docker_daemon_death_hint",
            "_wait_all",
        )
    ] + [
        _function_text(helpers, "assert_build_jobs"),
        _function_text(helpers, "log_shows_docker_daemon_death"),
    ]
    for text in new_code:
        for line in _code_lines(text):
            assert not re.search(r"\bwait\s+(-\w*n|--)", line), line
    # The whole script has no `wait -n` either (its own comment is the only mention).
    for line in _code_lines(install):
        assert not re.search(r"\bwait\s+-\w*n\b", line), line


def test_new_code_uses_no_other_bash4_constructs() -> None:
    install = _install_text()
    helpers = HELPERS.read_text()
    new_code = "\n".join(
        _code_lines(
            "\n".join(
                [_function_text(install, n) for n in ("_run_bg_build", "_count_live_builds",
                                                      "_build_parallelism_label",
                                                      "_docker_daemon_death_hint", "_wait_all")]
                + [_function_text(helpers, n) for n in ("assert_build_jobs",
                                                        "log_shows_docker_daemon_death")]
            )
        )
    )
    for pattern in (r"declare\s+-A", r"\bmapfile\b", r"\breadarray\b", r"\$\{[A-Za-z_]+(,,|\^\^)\}",
                    r"\$\{[A-Za-z_]+@[QEPAa]\}", r"&>>", r"\|&"):
        assert not re.search(pattern, new_code), pattern


@pytest.mark.parametrize("path", [INSTALL, HELPERS], ids=lambda p: p.name)
def test_scripts_parse_under_the_system_bash(path: Path) -> None:
    result = subprocess.run(
        [BASH, "-n", str(path)], capture_output=True, text=True, check=False, timeout=60
    )
    assert result.returncode == 0, result.stderr


def test_task_harness_really_runs_under_the_chosen_bash() -> None:
    """Backstop: the harness shell is the one the tests claim, not a silent fallback."""
    result = _bash('echo "${BASH_VERSION}"')
    assert result.returncode == 0
    if BASH == "/bin/bash":
        expected = subprocess.run(
            ["/bin/bash", "-c", 'echo "${BASH_VERSION}"'],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert result.stdout == expected


# --------------------------------------------------------------------------------------
# Call-site wiring (static)
# --------------------------------------------------------------------------------------


def test_every_build_task_goes_through_the_bound() -> None:
    """spec: §Installation phases 2 — builds are bounded; peripherals are not."""
    code = "\n".join(_code_lines(_install_text()))
    assert not re.search(r'_run_bg\s+"build-', code), "a build task bypasses _run_bg_build"
    launched = re.findall(r'_run_bg_build\s+"(build-[a-z]+)"', code)
    # dev Phase 2 + prod Phase 2, each: api, airflow, postgres, frontend
    assert sorted(set(launched)) == [
        "build-airflow",
        "build-api",
        "build-frontend",
        "build-postgres",
    ]
    assert len(launched) == 8
    for peripheral in ("datahub", "langfuse", "dummy-data", "dev-lock"):
        assert re.search(rf'_run_bg\s+"{peripheral}"', code), peripheral
        assert not re.search(rf'_run_bg_build\s+"{peripheral}"', code), peripheral


def test_dev_phase_two_launches_peripherals_before_builds() -> None:
    """spec: §Installation dev phase 2 — peripheral installs launch first, then the builds."""
    text = _install_text()
    first_build = text.index('_run_bg_build "build-api"')
    assert text.index('_run_bg "datahub"') < first_build
    assert text.index('_run_bg "langfuse"') < first_build


# --------------------------------------------------------------------------------------
# log_shows_docker_daemon_death
# --------------------------------------------------------------------------------------


def _log_shows_death(path: Path) -> subprocess.CompletedProcess[str]:
    script = f'source {shlex.quote(str(HELPERS))}\nlog_shows_docker_daemon_death "$LOGFILE"'
    return _bash(script, env={"LOGFILE": str(path)})


@pytest.mark.parametrize(
    "log_text",
    [
        "write unix /var/run/docker.sock: broken pipe",
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock.",
        "Is the docker daemon running?",
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
        "Is the docker daemon running?",
        "write: broken pipe (talking to /var/run/docker.sock)",
        'error during connect: Post "http://docker.example/v1.41/build": '
        "write tcp 127.0.0.1:50000->127.0.0.1:2375: write: broken pipe",
        "Step 5/9 : RUN make\n#9 DONE 3.1s\nERROR: failed to solve: "
        "rpc error: write unix @->/run/docker.sock: write: broken pipe\nmore output\n",
    ],
    ids=[
        "docker-sock-broken-pipe",
        "cannot-connect-phrase",
        "daemon-running-phrase",
        "cannot-connect-full-line",
        "broken-pipe-before-docker-sock",
        "error-during-connect",
        "buried-in-output",
    ],
)
def test_daemon_death_signatures_are_recognised(tmp_path: Path, log_text: str) -> None:
    """spec: HELM_CHART.md §Troubleshooting "Docker daemon dies" — Symptom signatures."""
    log = tmp_path / "build-api.log"
    log.write_text(log_text + "\n")
    result = _log_shows_death(log)
    assert result.returncode == 0, result.stderr
    # Only a boolean leaves the function: log text is never echoed.
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize(
    "log_text",
    [
        ORDINARY_FAILURE,
        "read tcp 10.0.0.1:443: write: broken pipe",
        "",
        "Step 3/9 : RUN pip install\nSuccessfully built 1234\n",
    ],
    ids=["dockerfile-failure", "unrelated-broken-pipe", "empty", "healthy-build"],
)
def test_ordinary_logs_are_not_daemon_death(tmp_path: Path, log_text: str) -> None:
    log = tmp_path / "build-api.log"
    log.write_text(log_text + "\n")
    assert _log_shows_death(log).returncode != 0


def test_missing_log_is_not_daemon_death(tmp_path: Path) -> None:
    result = _log_shows_death(tmp_path / "does-not-exist.log")
    assert result.returncode != 0
    assert result.stderr == ""


def test_unreadable_log_is_not_daemon_death(tmp_path: Path) -> None:
    log = tmp_path / "build-api.log"
    log.write_text(DAEMON_SIGNATURE + "\n")
    log.chmod(0)
    try:
        if os.access(log, os.R_OK):
            pytest.skip("running as a user that can read mode-000 files (root)")
        assert _log_shows_death(log).returncode != 0
    finally:
        log.chmod(0o600)


def test_binary_noise_in_log_does_not_defeat_matching(tmp_path: Path) -> None:
    log = tmp_path / "build-api.log"
    log.write_bytes(b"\x00\x01\xffgarbage\n" + DAEMON_SIGNATURE.encode() + b"\n")
    result = _log_shows_death(log)
    assert result.returncode == 0
    assert "Binary file" not in result.stdout + result.stderr


# --------------------------------------------------------------------------------------
# _wait_all: the one hint
# --------------------------------------------------------------------------------------


def _four_failing(tasks: _Tasks, log_for) -> list[str]:
    return [
        tasks.launch(f"build-{n}", hold=0.1, rc=1, log=log_for(n))
        for n in ("api", "airflow", "postgres", "frontend")
    ]


def test_wait_all_prints_the_hint_exactly_once_between_logs_and_final_error(
    tmp_path: Path,
) -> None:
    """spec: §Build concurrency bound — a single hint after the failing logs, ahead of the error."""
    tasks = _Tasks(tmp_path)
    marker = "LOGMARKER_7f3c91"
    result = tasks.run(
        _four_failing(tasks, lambda n: f"{marker}_{n}\n{DAEMON_SIGNATURE}"),
        build_jobs=None,
    )
    assert result.returncode != 0
    err = result.stderr
    # Backstop: all four failing logs were dumped, so the ordering below is meaningful.
    assert err.count(marker) == 4
    assert err.count("[FAIL] build-") == 4
    assert err.count(HINT_MARK) == 1
    hint_at = err.index(HINT_MARK)
    error_at = err.index("ERROR:")
    assert err.rfind(marker) < hint_at < error_at
    assert "4 background task(s) failed" in err[error_at:]
    # The hint never echoes build output.
    hint = _hint_line(err)
    assert marker not in hint
    assert "broken pipe" not in hint.lower()
    assert "docker.sock" not in hint


def test_wait_all_hint_never_echoes_log_contents_even_for_hostile_logs(tmp_path: Path) -> None:
    tasks = _Tasks(tmp_path)
    hostile = "EVIL_$(touch pwned)_MARK `id` \x1b[31m"
    result = tasks.run(
        [tasks.launch("build-api", hold=0.1, rc=1, log=f"{hostile}\n{DAEMON_SIGNATURE}")],
        build_jobs="3",
    )
    assert result.returncode != 0
    hint = _hint_line(result.stderr)
    assert "EVIL_" not in hint
    assert "\x1b" not in hint
    assert not (tasks.tmp / "pwned").exists()


def test_wait_all_ordinary_build_failures_get_no_hint(tmp_path: Path) -> None:
    tasks = _Tasks(tmp_path)
    result = tasks.run(_four_failing(tasks, lambda n: ORDINARY_FAILURE))
    assert result.returncode != 0
    assert HINT_MARK not in result.stderr
    assert "--skip-build" not in result.stderr
    assert result.stderr.count("[FAIL] build-") == 4
    assert "4 background task(s) failed" in result.stderr


def test_wait_all_unrelated_broken_pipe_gets_no_hint(tmp_path: Path) -> None:
    tasks = _Tasks(tmp_path)
    result = tasks.run(
        [tasks.launch("build-api", hold=0.1, rc=1, log="read tcp 10.0.0.1:443: write: broken pipe")]
    )
    assert result.returncode != 0
    # Backstop: the failing build was reported and the final error fired.
    assert "[FAIL] build-api" in result.stderr
    assert "1 background task(s) failed" in result.stderr
    assert HINT_MARK not in result.stderr


def test_wait_all_signature_in_a_non_build_task_gets_no_hint(tmp_path: Path) -> None:
    """The hint is about the local build daemon; a failing peripheral never triggers it."""
    tasks = _Tasks(tmp_path)
    result = tasks.run([tasks.launch("datahub", hold=0.1, rc=1, log=DAEMON_SIGNATURE)])
    assert result.returncode != 0
    assert HINT_MARK not in result.stderr
    assert "1 background task(s) failed" in result.stderr


def test_wait_all_mixed_failures_print_one_hint(tmp_path: Path) -> None:
    tasks = _Tasks(tmp_path)
    logs = {
        "api": ORDINARY_FAILURE,
        "airflow": DAEMON_SIGNATURE,
        "postgres": ORDINARY_FAILURE,
        "frontend": DAEMON_SIGNATURE,
    }
    result = tasks.run(_four_failing(tasks, lambda n: logs[n]))
    assert result.returncode != 0
    assert result.stderr.count(HINT_MARK) == 1


def test_wait_all_success_prints_no_hint_and_exits_zero(tmp_path: Path) -> None:
    tasks = _Tasks(tmp_path)
    result = tasks.run(_four_builds(tasks, hold=0.1), build_jobs="1")
    assert result.returncode == 0, result.stderr
    assert HINT_MARK not in result.stderr
    assert "ERROR" not in result.stderr


# --------------------------------------------------------------------------------------
# Hint content
# --------------------------------------------------------------------------------------


def _hint_for(
    tasks: _Tasks,
    *,
    build_jobs: str | None = None,
    frontend_mode: str = "cluster",
    image_tag: str = "dev",
    env_file: str | None = None,
    cwd: Path | None = None,
) -> tuple[str, subprocess.CompletedProcess[str]]:
    result = tasks.run(
        [tasks.launch("build-api", hold=0.1, rc=1, log=DAEMON_SIGNATURE)],
        build_jobs=build_jobs,
        frontend_mode=frontend_mode,
        image_tag=image_tag,
        env_file=env_file,
        cwd=cwd,
    )
    assert result.returncode != 0
    return _hint_line(result.stderr), result


def _env_file_token(hint: str) -> str:
    match = re.search(rf"ENV_FILE=({_SHELL_WORD})", hint)
    assert match, f"no ENV_FILE= in hint: {hint}"
    return match.group(1)


def test_hint_carries_the_absolute_env_file_and_the_mandatory_prefix(tmp_path: Path) -> None:
    """spec: §Troubleshooting step 3 — `ENV_FILE=<env file> <build-image.sh> <name> <tag>`."""
    tasks = _Tasks(tmp_path)
    hint, _ = _hint_for(tasks, image_tag="v1.2.3-rc1")
    token = _env_file_token(hint)
    assert shlex.split(token) == [str(tasks.env_file)]
    assert token.startswith("/")
    script = f"{tasks.script_dir}/build-image.sh"
    assert re.search(
        rf"ENV_FILE={re.escape(token)} {re.escape(script)} <name> v1\.2\.3-rc1\b", hint
    ), hint
    # The mandatory-prefix warning and the keep-the-original-flags re-run advice.
    assert "mandatory" in hint
    assert "--skip-build" in hint
    assert "--image-tag v1.2.3-rc1" in hint
    assert "--env-file" in hint


def test_hint_makes_a_relative_env_file_absolute_and_leaves_the_global_unchanged(
    tmp_path: Path,
) -> None:
    """The hint must not carry a cwd-relative path: pasted elsewhere it could pick another file."""
    tasks = _Tasks(tmp_path)
    work = tmp_path / "work"
    (work / "conf").mkdir(parents=True)
    env_file = work / "conf" / ".env.prod"
    env_file.write_text("")
    hint, result = _hint_for(tasks, env_file="conf/.env.prod", cwd=work)
    token = _env_file_token(hint)
    assert token.startswith("/"), hint
    assert shlex.split(token)[0] in {str(env_file), str(env_file.resolve())}
    # The global ENV_FILE (consumed by child scripts) was not rewritten.
    assert "ENV_FILE_AFTER=conf/.env.prod" in result.stdout


def test_hint_shell_quotes_an_env_file_path_with_a_space(tmp_path: Path) -> None:
    """spec: copy-pasteable hint — the path is %q-quoted, so a space is escaped, not split."""
    tasks = _Tasks(tmp_path)
    spaced = tmp_path / "my dir"
    spaced.mkdir()
    env_file = spaced / ".env.prod"
    env_file.write_text("")
    hint, _ = _hint_for(tasks, env_file=str(env_file))
    token = _env_file_token(hint)
    assert "my\\ dir" in token
    assert shlex.split(token) == [str(env_file)]
    assert re.search(rf"ENV_FILE={re.escape(token)} \S+/build-image\.sh <name>", hint), hint


def test_hint_neutralises_shell_metacharacters_in_the_env_file_path(tmp_path: Path) -> None:
    tasks = _Tasks(tmp_path)
    odd = tmp_path / "a;b$c'd"
    odd.mkdir()
    env_file = odd / ".env.prod"
    env_file.write_text("")
    hint, _ = _hint_for(tasks, env_file=str(env_file))
    assert shlex.split(_env_file_token(hint)) == [str(env_file)]


@pytest.mark.parametrize(
    ("mode", "lists_frontend"), [("cluster", True), ("none", False), ("local", False)]
)
def test_hint_lists_frontend_only_for_frontend_cluster(
    tmp_path: Path, mode: str, lists_frontend: bool
) -> None:
    """spec: §Troubleshooting step 3 — `<name>` includes frontend when `--frontend cluster`."""
    tasks = _Tasks(tmp_path)
    hint, _ = _hint_for(tasks, frontend_mode=mode)
    listing = re.search(r"one per name: ([^)]*)\)", hint)
    assert listing, f"no image list in hint: {hint}"
    images = [n.strip() for n in listing.group(1).split(",")]
    assert images[:3] == ["api", "airflow", "postgres"], images
    assert ("frontend" in images) is lists_frontend
    assert images == (["api", "airflow", "postgres", "frontend"] if lists_frontend
                      else ["api", "airflow", "postgres"])


def test_hint_names_the_same_env_file_in_the_env_var_alternative(tmp_path: Path) -> None:
    """spec: the env-var alternative to `--build-jobs 1` names that same env file."""
    tasks = _Tasks(tmp_path)
    hint, _ = _hint_for(tasks)
    assert "retry with --build-jobs 1" in hint
    assert "DATASPOKE_BUILD_JOBS=1" in hint
    # The env file shows up in the prefix AND in the env-var alternative.
    assert hint.count(str(tasks.env_file)) >= 2


def test_hint_with_bound_already_one_omits_the_retry_serially_advice(tmp_path: Path) -> None:
    """spec: §Troubleshooting recovery 2 — at bound 1 the remaining lever is daemon memory."""
    tasks = _Tasks(tmp_path)
    hint, result = _hint_for(tasks, build_jobs="1")
    assert "retry with --build-jobs 1" not in hint
    assert "DATASPOKE_BUILD_JOBS=1" not in hint
    assert "memory" in hint.lower()
    # The manual recovery path is still offered, and the install still ends in the error.
    assert "ENV_FILE=" in hint
    assert "--skip-build" in hint
    assert "1 background task(s) failed" in result.stderr


def test_hint_with_a_larger_bound_still_advises_serial_retry(tmp_path: Path) -> None:
    tasks = _Tasks(tmp_path)
    hint, _ = _hint_for(tasks, build_jobs="4")
    assert "retry with --build-jobs 1" in hint


# --------------------------------------------------------------------------------------
# Spec guard
# --------------------------------------------------------------------------------------


def _troubleshooting_entry() -> str:
    text = HELM_CHART_SPEC.read_text()
    heading = "### Docker daemon dies during image builds"
    start = text.index(heading)
    nxt = text.find("\n### ", start + len(heading))
    return text[start : nxt if nxt != -1 else len(text)]


def test_spec_troubleshooting_entry_states_the_mandatory_env_file_prefix() -> None:
    """The spec entry the hint points at must carry the mandatory `ENV_FILE=` wording."""
    entry = _troubleshooting_entry()
    assert "ENV_FILE=" in entry
    assert re.search(r"`ENV_FILE=`\s+prefix\s+is\s+mandatory", entry), entry
    # Why: an unprefixed call defaults to .env.dev and pushes to the dev registry.
    assert ".env.dev" in entry
    assert "--skip-build" in entry
    assert "--build-jobs 1" in entry
    # Each vendor row of the affected-vendors table is present.
    assert re.search(r"\|\s*`AWS`\s*\|.*\|\s*yes\s*\|", entry)
    assert re.search(r"\|\s*`GCP`\s*\|.*\|\s*no\s*\|", entry)


def test_spec_flags_table_documents_build_jobs() -> None:
    text = HELM_CHART_SPEC.read_text()
    row = next(ln for ln in text.splitlines() if ln.startswith("| `--build-jobs <n>`"))
    assert "DATASPOKE_BUILD_JOBS" in row
    assert "^[1-9][0-9]*$" in row
    assert "999999" in row
