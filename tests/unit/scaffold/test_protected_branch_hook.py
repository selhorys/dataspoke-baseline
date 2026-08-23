"""Tests for the layered protected-branch commit approval hooks.

Requirements: AGENTS.md §Git Commit Convention.
"""

from __future__ import annotations

import importlib.util
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
        "git log --grep=commit",
        "false && git log --oneline --grep=commit",
        "git log -1 --format=%H commit-ish-ref",
    ],
)
def test_native_hook_avoids_non_commit_false_positives(repo: Path, command: str) -> None:
    subprocess.run(["git", "-C", repo, "checkout", "-q", "-b", "master"], check=True)

    result = _native(repo, command)

    assert result.returncode == 0
    assert result.stdout == ""


@pytest.mark.parametrize(
    "command",
    [
        'echo "it\'s a test"',
        "echo building it's fine",
        "git status -m \"$(cat <<'EOF'\nfix: don't break things\nEOF\n)\"",
    ],
)
def test_native_hook_does_not_crash_on_unbalanced_quote_text(repo: Path, command: str) -> None:
    subprocess.run(["git", "-C", repo, "checkout", "-q", "-b", "dev"], check=True)

    result = _native(repo, command)

    assert result.returncode == 0
    assert result.stdout == ""


def test_native_hook_fails_open_for_unparseable_non_git_command(repo: Path) -> None:
    subprocess.run(["git", "-C", repo, "checkout", "-q", "-b", "dev"], check=True)

    result = _native(repo, "echo it's just prose with no git in it")

    assert result.returncode == 0
    assert result.stdout == ""


def test_native_hook_still_fails_closed_for_unparseable_opaque_git_commit(repo: Path) -> None:
    subprocess.run(["git", "-C", repo, "checkout", "-q", "-b", "dev"], check=True)

    result = _native(repo, "echo `git commit -m it's-nested`")

    assert result.returncode == 0
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "ask"


@pytest.mark.parametrize(
    "command",
    [
        'echo "$(git commit -m x)"',
        'printf \'%s\' "$(git commit -m x)"',
        'VAR="$(git commit -m x)"',
    ],
)
def test_native_hook_flags_quoted_dollar_paren_substitution(repo: Path, command: str) -> None:
    subprocess.run(["git", "-C", repo, "checkout", "-q", "-b", "dev"], check=True)

    result = _native(repo, command)

    assert result.returncode == 0
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "ask"


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
