"""Tests for the layered protected-branch commit approval hooks.

Requirements: AGENTS.md §Git Commit Convention.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[3]
CLASSIFIER = ROOT / "scaffold/hooks/protected-commit.py"


def _load_classifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("protected_commit", CLASSIFIER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def classifier() -> ModuleType:
    return _load_classifier()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", repository], check=True)
    subprocess.run(["git", "-C", repository, "checkout", "-q", "-b", "topic"], check=True)
    return repository


def _native(
    repo: Path, command: object, *, tool_name: str = "Bash"
) -> subprocess.CompletedProcess[str]:
    payload = {"tool_name": tool_name, "tool_input": {"command": command, "cwd": str(repo)}}
    return subprocess.run(
        [sys.executable, CLASSIFIER, "native"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("branch", ["dev", "master"])
def test_native_hook_requests_approval_for_protected_branch(repo: Path, branch: str) -> None:
    subprocess.run(["git", "-C", repo, "checkout", "-q", "-b", branch], check=True)

    result = _native(repo, "git commit -m 'approved message'")

    assert result.returncode == 0
    output = json.loads(result.stdout)
    decision = output["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "ask"
    assert branch in decision["permissionDecisionReason"]


@pytest.mark.parametrize("branch", ["topic", "main", None])
def test_native_hook_allows_unprotected_or_detached_head(repo: Path, branch: str | None) -> None:
    if branch == "main":
        subprocess.run(["git", "-C", repo, "checkout", "-q", "-b", branch], check=True)
    if branch is None:
        subprocess.run(["git", "-C", repo, "commit", "--allow-empty", "-qm", "seed"], check=True)
        subprocess.run(["git", "-C", repo, "checkout", "-q", "--detach", "HEAD"], check=True)

    result = _native(repo, "git commit -m message")

    assert result.returncode == 0
    assert result.stdout == ""


@pytest.mark.parametrize(
    "command",
    [
        "env FLAG=1 command git commit --allow-empty -m message",
        "git -c user.name=Test commit -m message",
        "sh -c 'git commit -m nested'",
        "printf done && git commit -m chained",
    ],
)
def test_native_hook_detects_supported_commit_variants(repo: Path, command: str) -> None:
    subprocess.run(["git", "-C", repo, "checkout", "-q", "-b", "dev"], check=True)

    result = _native(repo, command)

    assert result.returncode == 0
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git commitment -m nope",
        "printf 'git commit -m nope'",
        "python -c 'print(\"git commit\")'",
    ],
)
def test_native_hook_avoids_non_commit_false_positives(repo: Path, command: str) -> None:
    subprocess.run(["git", "-C", repo, "checkout", "-q", "-b", "master"], check=True)

    result = _native(repo, command)

    assert result.returncode == 0
    assert result.stdout == ""


def test_native_hook_honors_git_dash_c_repository(repo: Path, tmp_path: Path) -> None:
    subprocess.run(["git", "-C", repo, "checkout", "-q", "-b", "dev"], check=True)

    result = _native(tmp_path, f"git -C {repo} commit -m message")

    assert result.returncode == 0
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize(
    "raw_input",
    [
        "not-json",
        "[]",
        json.dumps({"tool_name": "Bash"}),
        json.dumps({"tool_name": "Bash", "tool_input": []}),
        json.dumps({"tool_name": "Bash", "tool_input": {"command": 1, "cwd": "/tmp"}}),
        json.dumps({"tool_name": "Bash", "tool_input": {"command": "git commit"}}),
        json.dumps({"tool_name": "Bash", "tool_input": {"command": "git commit", "cwd": 1}}),
    ],
)
def test_native_hook_fails_closed_on_malformed_input(raw_input: str) -> None:
    result = subprocess.run(
        [sys.executable, CLASSIFIER, "native"],
        input=raw_input,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "failed closed" in result.stderr


def test_native_hook_ignores_unrelated_tools(repo: Path) -> None:
    result = _native(repo, "git commit -m message", tool_name="Read")

    assert result.returncode == 0
    assert result.stdout == ""


def test_native_hook_distinguishes_detached_head_from_branch_probe_failure(
    repo: Path, tmp_path: Path
) -> None:
    subprocess.run(["git", "-C", repo, "commit", "--allow-empty", "-qm", "seed"], check=True)
    subprocess.run(["git", "-C", repo, "checkout", "-q", "--detach", "HEAD"], check=True)

    detached = _native(repo, "git commit -m detached")
    failed_probe = _native(tmp_path / "not-a-repository", "git commit -m unknown")

    assert detached.returncode == 0
    assert detached.stdout == ""
    assert failed_probe.returncode == 2
    assert "cannot resolve Git branch" in failed_probe.stderr


@pytest.mark.parametrize(("symbolic_ref_rc", "expected"), [(1, None), (2, "error")])
def test_branch_treats_only_symbolic_ref_one_as_detached(
    classifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    symbolic_ref_rc: int,
    expected: str | None,
) -> None:
    results = iter(
        [
            subprocess.CompletedProcess([], symbolic_ref_rc, stdout="", stderr="symbolic failure"),
            subprocess.CompletedProcess([], 0, stdout="true\n", stderr=""),
        ]
    )
    monkeypatch.setattr(classifier.subprocess, "run", lambda *_args, **_kwargs: next(results))

    if expected is None:
        assert classifier._branch(Path("/repository")) is None
    else:
        with pytest.raises(ValueError, match="cannot resolve Git branch"):
            classifier._branch(Path("/repository"))


def test_native_hook_fails_closed_for_unresolved_commit_capable_shell(repo: Path) -> None:
    result = _native(repo, "echo `git commit -m nested`")

    assert result.returncode == 0
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "ask"
    assert "unresolved shell branch" in decision["permissionDecisionReason"]


@pytest.mark.parametrize("separator", ["&&", "||", ";", "\n"])
def test_native_hook_tracks_cwd_across_shell_separators(
    repo: Path, tmp_path: Path, separator: str
) -> None:
    protected = tmp_path / "protected"
    protected.mkdir()
    subprocess.run(["git", "init", "-q", protected], check=True)
    subprocess.run(["git", "-C", protected, "checkout", "-q", "-b", "dev"], check=True)
    if separator == "||":
        subprocess.run(["git", "-C", repo, "checkout", "-q", "-b", "master"], check=True)
        target = tmp_path / "unprotected"
        target.mkdir()
        subprocess.run(["git", "init", "-q", target], check=True)
        subprocess.run(["git", "-C", target, "checkout", "-q", "-b", "topic"], check=True)
        command = f"cd -- {target} || git commit -m message"
    else:
        command = f"cd -- {protected} {separator} git commit -m message"

    result = _native(repo, command)

    assert result.returncode == 0
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_native_hook_keeps_successful_cd_path_across_or_then_sequence(
    repo: Path, tmp_path: Path
) -> None:
    protected = tmp_path / "protected-or"
    protected.mkdir()
    subprocess.run(["git", "init", "-q", protected], check=True)
    subprocess.run(["git", "-C", protected, "checkout", "-q", "-b", "dev"], check=True)

    result = _native(repo, f"cd -- {protected} || exit 1; git commit -m message")

    assert result.returncode == 0
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "ask"
    assert "dev" in decision["permissionDecisionReason"]


def test_native_hook_tracks_or_and_precedence_across_alternative_cd_paths(
    repo: Path, tmp_path: Path
) -> None:
    protected = tmp_path / "precedence-protected"
    protected.mkdir()
    subprocess.run(["git", "init", "-q", protected], check=True)
    subprocess.run(["git", "-C", protected, "checkout", "-q", "-b", "dev"], check=True)

    ambiguous = _native(repo, f"cd {protected} || cd {repo} && git commit -m potentially-protected")
    topic_only = _native(repo, f"cd {repo} && git commit -m topic-only")

    assert ambiguous.returncode == 0
    decision = json.loads(ambiguous.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "ask"
    assert "dev" in decision["permissionDecisionReason"]
    assert topic_only.returncode == 0
    assert topic_only.stdout == ""


@pytest.mark.parametrize("chdir_option", ["-C", "--chdir"])
def test_native_hook_tracks_env_chdir_for_protected_and_unprotected_targets(
    repo: Path, tmp_path: Path, chdir_option: str
) -> None:
    protected = tmp_path / f"protected-{chdir_option.lstrip('-')}"
    protected.mkdir()
    subprocess.run(["git", "init", "-q", protected], check=True)
    subprocess.run(["git", "-C", protected, "checkout", "-q", "-b", "master"], check=True)

    protected_result = _native(repo, f"env {chdir_option} {protected} git commit -m protected")
    unprotected_result = _native(repo, f"env {chdir_option} {repo} git commit -m topic")

    assert protected_result.returncode == 0
    protected_decision = json.loads(protected_result.stdout)["hookSpecificOutput"]
    assert protected_decision["permissionDecision"] == "ask"
    assert "master" in protected_decision["permissionDecisionReason"]
    assert unprotected_result.returncode == 0
    assert unprotected_result.stdout == ""


@pytest.mark.parametrize(
    ("env_command", "chdir_option"),
    [
        ("/usr/bin/env", "-C"),
        ("/usr/bin/env", "--chdir"),
        ("command env", "-C"),
        ("command /usr/bin/env", "--chdir"),
    ],
)
def test_native_hook_normalizes_env_wrappers_with_chdir(
    repo: Path, tmp_path: Path, env_command: str, chdir_option: str
) -> None:
    protected = tmp_path / f"wrapped-{len(env_command)}-{chdir_option.lstrip('-')}"
    protected.mkdir()
    subprocess.run(["git", "init", "-q", protected], check=True)
    subprocess.run(["git", "-C", protected, "checkout", "-q", "-b", "master"], check=True)

    protected_result = _native(
        repo, f"{env_command} {chdir_option} {protected} git commit -m protected"
    )
    topic_result = _native(repo, f"{env_command} {chdir_option} {repo} git commit -m topic")

    assert protected_result.returncode == 0
    decision = json.loads(protected_result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "ask"
    assert "master" in decision["permissionDecisionReason"]
    assert topic_result.returncode == 0
    assert topic_result.stdout == ""


class _Terminal:
    def __init__(self, response: str) -> None:
        self.response = io.StringIO(response)
        self.output = io.StringIO()

    def __enter__(self) -> _Terminal:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def write(self, value: str) -> int:
        return self.output.write(value)

    def flush(self) -> None:
        pass

    def readline(self) -> str:
        return self.response.readline()


class _TTYHandle:
    def __init__(
        self,
        *,
        response: str = "",
        events: list[str] | None = None,
        fail_read: bool = False,
        fail_write: bool = False,
        fail_flush: bool = False,
    ):
        self.response = io.StringIO(response)
        self.output = io.StringIO()
        self.events = events if events is not None else []
        self.fail_read = fail_read
        self.fail_write = fail_write
        self.fail_flush = fail_flush

    def __enter__(self) -> _TTYHandle:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def readline(self) -> str:
        self.events.append("read")
        if self.fail_read:
            raise OSError("tty read failed")
        return self.response.readline()

    def write(self, value: str) -> int:
        self.events.append("write")
        if self.fail_write:
            raise OSError("tty write failed")
        return self.output.write(value)

    def flush(self) -> None:
        self.events.append("flush")
        if self.fail_flush:
            raise OSError("tty flush failed")


@pytest.mark.parametrize(("response", "expected"), [("yes\n", 0), ("no\n", 1), ("YES\n", 1)])
def test_git_fallback_requires_exact_confirmation(
    classifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    response: str,
    expected: int,
) -> None:
    monkeypatch.setattr(classifier, "_branch", lambda _cwd: "dev")
    terminal = _Terminal(response)
    monkeypatch.setattr("builtins.open", lambda *_args, **_kwargs: terminal)

    assert classifier._git_hook() == expected


def test_git_fallback_uses_separate_nonseeking_tty_handles(
    classifier: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(classifier, "_branch", lambda _cwd: "dev")
    events: list[str] = []
    terminal_input = _TTYHandle(response="yes\n", events=events)
    terminal_output = _TTYHandle(events=events)
    calls: list[tuple[str, str]] = []

    def open_tty(path: str, mode: str = "r", **_kwargs: object) -> _TTYHandle:
        calls.append((path, mode))
        return terminal_input if mode == "r" else terminal_output

    monkeypatch.setattr("builtins.open", open_tty)

    assert classifier._git_hook() == 0
    assert calls == [("/dev/tty", "r"), ("/dev/tty", "w")]
    assert all("+" not in mode for _, mode in calls)
    assert "Type 'yes' to confirm" in terminal_output.output.getvalue()
    assert events == ["write", "flush", "read"]


@pytest.mark.parametrize("failure", ["input-open", "output-open", "read", "write", "flush"])
def test_git_fallback_fails_closed_for_each_tty_operation(
    classifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    monkeypatch.setattr(classifier, "_branch", lambda _cwd: "master")
    terminal_input = _TTYHandle(response="yes\n", fail_read=failure == "read")
    terminal_output = _TTYHandle(fail_write=failure == "write", fail_flush=failure == "flush")

    def open_tty(_path: str, mode: str = "r", **_kwargs: object) -> _TTYHandle:
        if failure == "input-open" and mode == "r":
            raise OSError("tty input open failed")
        if failure == "output-open" and mode == "w":
            raise OSError("tty output open failed")
        return terminal_input if mode == "r" else terminal_output

    monkeypatch.setattr("builtins.open", open_tty)

    assert classifier._git_hook() == 1
    assert "requires interactive confirmation" in capsys.readouterr().err


def test_git_fallback_blocks_without_tty(
    classifier: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(classifier, "_branch", lambda _cwd: "master")

    def no_terminal(*_args: object, **_kwargs: object) -> None:
        raise OSError("no controlling terminal")

    monkeypatch.setattr("builtins.open", no_terminal)

    assert classifier._git_hook() == 1
    assert "requires interactive confirmation" in capsys.readouterr().err


def test_git_fallback_allows_other_branches_without_opening_tty(
    classifier: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(classifier, "_branch", lambda _cwd: "topic")

    def unexpected_open(*_args: object, **_kwargs: object) -> None:
        pytest.fail("unprotected branches must not prompt")

    monkeypatch.setattr("builtins.open", unexpected_open)

    assert classifier._git_hook() == 0


def _installer_fixture(tmp_path: Path) -> Path:
    repository = tmp_path / "install-repo"
    (repository / "scaffold/bin").mkdir(parents=True)
    (repository / "scaffold/hooks").mkdir()
    (repository / ".githooks").mkdir()
    shutil.copy2(ROOT / "scaffold/bin/install-git-hooks.sh", repository / "scaffold/bin")
    shutil.copy2(ROOT / "scaffold/hooks/protected-commit.py", repository / "scaffold/hooks")
    shutil.copy2(ROOT / "scaffold/hooks/run-protected-commit.sh", repository / "scaffold/hooks")
    shutil.copy2(ROOT / ".githooks/pre-commit", repository / ".githooks")
    subprocess.run(["git", "init", "-q", repository], check=True)
    return repository


def test_installer_is_idempotent(tmp_path: Path) -> None:
    repository = _installer_fixture(tmp_path)
    installer = repository / "scaffold/bin/install-git-hooks.sh"

    first = subprocess.run([installer], text=True, capture_output=True, check=False)
    second = subprocess.run([installer], text=True, capture_output=True, check=False)

    assert first.returncode == 0
    assert second.returncode == 0
    configured = subprocess.run(
        ["git", "-C", repository, "config", "--local", "--get", "core.hooksPath"],
        text=True,
        capture_output=True,
        check=True,
    )
    assert configured.stdout.strip() == ".githooks"


def test_installer_refuses_existing_different_hook_path(tmp_path: Path) -> None:
    repository = _installer_fixture(tmp_path)
    subprocess.run(
        ["git", "-C", repository, "config", "--local", "core.hooksPath", "company-hooks"],
        check=True,
    )

    result = subprocess.run(
        [repository / "scaffold/bin/install-git-hooks.sh"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "refusing to overwrite" in result.stderr
    configured = subprocess.run(
        ["git", "-C", repository, "config", "--local", "--get", "core.hooksPath"],
        text=True,
        capture_output=True,
        check=True,
    )
    assert configured.stdout.strip() == "company-hooks"


def test_installed_git_hook_allows_topic_and_blocks_protected_without_tty(tmp_path: Path) -> None:
    repository = _installer_fixture(tmp_path)
    installer = repository / "scaffold/bin/install-git-hooks.sh"
    subprocess.run([installer], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", repository, "config", "user.name", "Test User"], check=True)
    subprocess.run(
        ["git", "-C", repository, "config", "user.email", "test@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", repository, "checkout", "-q", "-b", "topic"], check=True)

    unprotected = subprocess.run(
        ["git", "-C", repository, "commit", "--allow-empty", "-m", "topic commit"],
        text=True,
        capture_output=True,
        check=False,
        start_new_session=True,
    )
    subprocess.run(["git", "-C", repository, "checkout", "-q", "-b", "dev"], check=True)
    protected = subprocess.run(
        ["git", "-C", repository, "commit", "--allow-empty", "-m", "dev commit"],
        text=True,
        capture_output=True,
        check=False,
        start_new_session=True,
    )

    assert unprotected.returncode == 0, unprotected.stderr
    assert protected.returncode != 0
    assert "requires interactive confirmation" in protected.stderr


def test_native_hook_configuration_shapes() -> None:
    claude = json.loads((ROOT / ".claude/settings.json").read_text())

    claude_hook = claude["hooks"]["PreToolUse"]
    assert len(claude_hook) == 1
    assert claude_hook[0]["matcher"] == "Bash"
    assert "run-protected-commit.sh" in claude_hook[0]["hooks"][0]["command"]
    assert not (ROOT / ".codex/hooks.json").exists()
    assert os.access(ROOT / "scaffold/hooks/run-protected-commit.sh", os.X_OK)


def test_policy_assigns_codex_approval_to_native_parent_ux() -> None:
    agents = " ".join((ROOT / "AGENTS.md").read_text().split())
    readme = " ".join((ROOT / "scaffold/README.md").read_text().split())

    assert "Codex has no project hook" in agents
    assert "Codex has no project hook" in readme
    assert "native approval or user-question interface" in agents
    assert "native approval or user-question UI" in readme


def test_claude_hook_configuration_resolves_repository_entrypoint() -> None:
    claude = json.loads((ROOT / ".claude/settings.json").read_text())
    command = claude["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    payload = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "git status", "cwd": str(ROOT)},
        }
    )
    environment = os.environ.copy()
    environment["CLAUDE_PROJECT_DIR"] = str(ROOT)

    result = subprocess.run(
        ["sh", "-c", command],
        cwd=ROOT,
        env=environment,
        input=payload,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.returncode != 127
    assert "unavailable" not in result.stderr


@pytest.mark.parametrize(
    ("branch", "expects_ask"), [("dev", True), ("master", True), ("main", False)]
)
def test_configured_claude_hook_decision_for_actual_commit(
    repo: Path, branch: str, expects_ask: bool
) -> None:
    subprocess.run(["git", "-C", repo, "checkout", "-q", "-b", branch], check=True)
    claude = json.loads((ROOT / ".claude/settings.json").read_text())
    command = claude["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    payload = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m message", "cwd": str(repo)},
        }
    )
    environment = os.environ.copy()
    environment["CLAUDE_PROJECT_DIR"] = str(ROOT)

    result = subprocess.run(
        ["sh", "-c", command],
        cwd=ROOT,
        env=environment,
        input=payload,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    if expects_ask:
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "ask"
        assert branch in decision["permissionDecisionReason"]
    else:
        assert result.stdout == ""


def test_wrapper_normalizes_unexpected_classifier_exit_to_two(tmp_path: Path) -> None:
    hook_dir = tmp_path / "hooks"
    fake_bin = tmp_path / "bin"
    hook_dir.mkdir()
    fake_bin.mkdir()
    wrapper = hook_dir / "run-protected-commit.sh"
    shutil.copy2(ROOT / "scaffold/hooks/run-protected-commit.sh", wrapper)
    fake_python = fake_bin / "python3"
    fake_python.write_text("#!/bin/sh\nexit 42\n")
    fake_python.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

    result = subprocess.run(
        [wrapper, "native"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stderr.strip()
    assert "status 42" in result.stderr


def test_wrapper_normalizes_classifier_exit_one_to_two_with_reason(tmp_path: Path) -> None:
    hook_dir = tmp_path / "hooks"
    fake_bin = tmp_path / "bin"
    hook_dir.mkdir()
    fake_bin.mkdir()
    wrapper = hook_dir / "run-protected-commit.sh"
    shutil.copy2(ROOT / "scaffold/hooks/run-protected-commit.sh", wrapper)
    fake_python = fake_bin / "python3"
    fake_python.write_text("#!/bin/sh\necho 'classifier declined' >&2\nexit 1\n")
    fake_python.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

    result = subprocess.run(
        [wrapper, "native"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stderr.strip()
    assert "classifier declined" in result.stderr
