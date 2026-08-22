#!/usr/bin/env python3
"""Shared protected-branch commit classifier for agent and Git hooks."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

PROTECTED_BRANCHES = {"dev", "master"}
SHELL_SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "then", "do", "else"}
COMMIT_CAPABLE = re.compile(
    r"(?:^|[;&|()`\n])\s*(?:env(?:\s+[^;&|()`\n]+)*\s+)?(?:[\w./-]+/)?git\s+[^;&|()`\n]*\bcommit\b"
)


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _segments(command: str) -> list[tuple[list[str], str | None]]:
    segments: list[tuple[list[str], str | None]] = []
    for line_number, line in enumerate(command.splitlines() or [command]):
        current: list[str] = []
        for token in _tokens(line):
            if token in SHELL_SEPARATORS:
                if current:
                    segments.append((current, token))
                    current = []
            else:
                current.append(token)
        if current:
            separator = ";" if line_number < len(command.splitlines()) - 1 else None
            segments.append((current, separator))
    return segments


def _executable_index(segment: list[str]) -> int:
    index = 0
    while index < len(segment):
        token = segment[index]
        if Path(token).name == "env":
            index += 1
            while index < len(segment):
                if segment[index] in {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}:
                    index += 2
                elif segment[index].startswith("-") or "=" in segment[index]:
                    index += 1
                else:
                    break
            continue
        if token == "command":
            index += 1
            while index < len(segment) and segment[index].startswith("-"):
                index += 1
            continue
        if "=" in token and not token.startswith(("/", "./", "../")):
            index += 1
            continue
        break
    return index


def _git_commit_cwd(segment: list[str], cwd: Path) -> Path | None:
    """Return the repository directory for a git commit command segment."""
    index = _executable_index(segment)

    if index >= len(segment) or Path(segment[index]).name != "git":
        return None
    index += 1
    git_cwd = cwd
    while index < len(segment):
        token = segment[index]
        if token == "-C":
            if index + 1 >= len(segment):
                raise ValueError("git -C is missing its directory")
            candidate = Path(segment[index + 1])
            git_cwd = candidate if candidate.is_absolute() else git_cwd / candidate
            index += 2
            continue
        if token.startswith("-C") and token != "-C":
            candidate = Path(token[2:])
            git_cwd = candidate if candidate.is_absolute() else git_cwd / candidate
            index += 1
            continue
        if token == "--git-dir":
            if index + 1 >= len(segment):
                raise ValueError("git --git-dir is missing its directory")
            git_dir = Path(segment[index + 1])
            git_dir = git_dir if git_dir.is_absolute() else git_cwd / git_dir
            git_cwd = git_dir.parent if git_dir.name == ".git" else git_dir
            index += 2
            continue
        if token.startswith("--git-dir="):
            git_dir = Path(token.partition("=")[2])
            git_dir = git_dir if git_dir.is_absolute() else git_cwd / git_dir
            git_cwd = git_dir.parent if git_dir.name == ".git" else git_dir
            index += 1
            continue
        if token in {"-c", "--config-env", "--work-tree", "--namespace"}:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return git_cwd.resolve() if token == "commit" else None
    return None


def _environment_cwd(segment: list[str], cwd: Path) -> Path:
    index = 0
    while index < len(segment) and "=" in segment[index]:
        index += 1
    if index < len(segment) and Path(segment[index]).name == "command":
        index += 1
        while index < len(segment) and segment[index].startswith("-"):
            index += 1
    if index >= len(segment) or Path(segment[index]).name != "env":
        return cwd
    index += 1
    env_cwd = cwd
    while index < len(segment):
        token = segment[index]
        if token in {"-C", "--chdir"}:
            if index + 1 >= len(segment):
                raise ValueError(f"env {token} is missing its directory")
            target = Path(segment[index + 1])
            env_cwd = (target if target.is_absolute() else env_cwd / target).resolve()
            index += 2
            continue
        if token.startswith("--chdir="):
            target = Path(token.partition("=")[2])
            env_cwd = (target if target.is_absolute() else env_cwd / target).resolve()
            index += 1
            continue
        if token.startswith("-C") and token != "-C":
            target = Path(token[2:])
            env_cwd = (target if target.is_absolute() else env_cwd / target).resolve()
            index += 1
            continue
        if token in {"-u", "--unset", "-S", "--split-string"}:
            index += 2
            continue
        if token.startswith("-") or "=" in token:
            index += 1
            continue
        break
    return env_cwd


def _branch(repo_cwd: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo_cwd), "symbolic-ref", "--quiet", "--short", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    if result.returncode == 1:
        return None
    detail = result.stderr.strip() or f"git symbolic-ref exited {result.returncode}"
    raise ValueError(f"cannot resolve Git branch from {repo_cwd}: {detail}")


def _cd_target(segment: list[str]) -> Path | None:
    index = 1
    while index < len(segment) and segment[index] in {"-L", "-P"}:
        index += 1
    if index < len(segment) and segment[index] == "--":
        index += 1
    if index != len(segment) - 1:
        return None
    return Path(segment[index])


def _protected_commit(command: str, cwd: Path) -> str | None:
    try:
        segments = _segments(command)
    except ValueError:
        opaque = "`" in command or "$(" in command
        return "an unresolved shell branch" if opaque and COMMIT_CAPABLE.search(command) else None

    possible_cwds = {cwd.resolve()}
    deferred_cwds: set[Path] = set()
    deferred_operator: str | None = None
    saw_commit = False
    saw_opaque_substitution = False
    for segment, next_separator in segments:
        if any("`" in token for token in segment):
            saw_opaque_substitution = True
        if segment and segment[0] == "cd":
            target = _cd_target(segment)
            if target is None:
                if COMMIT_CAPABLE.search(command):
                    return "an unresolved shell branch"
                continue
            original_cwds = possible_cwds.copy()
            prior_deferred_cwds = deferred_cwds.copy()
            prior_deferred_operator = deferred_operator
            changed_cwds = {
                (target if target.is_absolute() else active_cwd / target).resolve()
                for active_cwd in original_cwds
            }
            if next_separator == "&&":
                possible_cwds = changed_cwds
                deferred_cwds = original_cwds
                if prior_deferred_operator == "||":
                    possible_cwds |= prior_deferred_cwds
                else:
                    deferred_cwds |= prior_deferred_cwds
                deferred_operator = "&&"
            elif next_separator == "||":
                deferred_cwds = prior_deferred_cwds | changed_cwds
                deferred_operator = "||"
            else:
                possible_cwds |= changed_cwds | prior_deferred_cwds
                deferred_cwds.clear()
                deferred_operator = None
            continue
        executable_index = _executable_index(segment)
        if executable_index < len(segment) and Path(segment[executable_index]).name in {
            "bash",
            "dash",
            "sh",
            "zsh",
        }:
            command_index = -1
            for option_index in range(executable_index + 1, len(segment)):
                option = segment[option_index]
                if option == "-c" or (
                    option.startswith("-") and "c" in option[1:] and not option.startswith("--")
                ):
                    command_index = option_index + 1
                    break
            if command_index > 0 and command_index < len(segment):
                for active_cwd in possible_cwds:
                    nested_branch = _protected_commit(segment[command_index], active_cwd)
                    if nested_branch is not None:
                        return nested_branch
        for active_cwd in possible_cwds:
            effective_cwd = _environment_cwd(segment, active_cwd)
            commit_cwd = _git_commit_cwd(segment, effective_cwd)
            if commit_cwd is not None:
                saw_commit = True
                branch = _branch(commit_cwd)
                if branch in PROTECTED_BRANCHES:
                    return branch
        if deferred_cwds and (deferred_operator == "||" or next_separator != deferred_operator):
            possible_cwds |= deferred_cwds
            deferred_cwds.clear()
            deferred_operator = None
    if not saw_commit and saw_opaque_substitution and COMMIT_CAPABLE.search(command):
        return "an unresolved shell branch"
    return None


def _read_payload() -> dict[str, Any]:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"invalid hook input: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("hook input must be a JSON object")
    return payload


def _native_hook() -> int:
    try:
        payload = _read_payload()
        tool_name = str(payload.get("tool_name", ""))
        if tool_name != "Bash":
            return 0
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            raise ValueError("matching shell tool_input must be a JSON object")
        if "command" in tool_input:
            command = tool_input["command"]
        elif "cmd" in tool_input:
            command = tool_input["cmd"]
        else:
            raise ValueError("matching shell tool_input requires command or cmd")
        if not isinstance(command, str):
            raise ValueError("tool command must be a string")
        cwd_value = tool_input.get("cwd", payload.get("cwd"))
        if not isinstance(cwd_value, str) or not cwd_value:
            raise ValueError("matching Bash payload requires a non-empty string cwd")
        cwd = Path(cwd_value)
        branch = _protected_commit(command, cwd)
    except (OSError, ValueError) as exc:
        print(f"Protected-commit hook failed closed: {exc}", file=sys.stderr)
        return 2
    if branch is None:
        return 0
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": (
                        f"Committing directly to {branch} requires explicit user approval."
                    ),
                }
            }
        )
    )
    return 0


def _git_hook() -> int:
    try:
        branch = _branch(Path.cwd())
    except (OSError, ValueError) as exc:
        print(f"Commit blocked: protected-commit classifier failed ({exc}).", file=sys.stderr)
        return 2
    if branch not in PROTECTED_BRANCHES:
        return 0
    try:
        with (
            open("/dev/tty", encoding="utf-8") as terminal_input,
            open("/dev/tty", "w", encoding="utf-8", buffering=1) as terminal_output,
        ):
            terminal_output.write(
                f"Accidental-commit guard: commit directly to '{branch}'? Type 'yes' to confirm: "
            )
            terminal_output.flush()
            response = terminal_input.readline().rstrip("\n")
    except OSError as exc:
        print(
            f"Commit blocked: {branch} requires interactive confirmation ({exc}).",
            file=sys.stderr,
        )
        return 1
    if response != "yes":
        print("Commit blocked: explicit confirmation was not provided.", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"native", "git-hook"}:
        print("usage: protected-commit.py {native|git-hook}", file=sys.stderr)
        return 2
    return _native_hook() if sys.argv[1] == "native" else _git_hook()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        print(f"Protected-commit hook failed closed: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
