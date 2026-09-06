"""Regression test for `scaffold/bin/check-bindings.sh`'s DELETED_MECHANISM_TOKENS invariant.

`check-bindings.sh` maintains a hand-written tuple naming mechanisms declared deleted. Its
own loop forces any `scaffold/memory/*/*.md` note that mentions one of those tokens to carry a
historical/deleted/removed/superseded marker. If a named mechanism is later revived on disk (as
`.prauto/heartbeat.sh` and `.prauto/lib/` were by commit bf46c508) without removing its token
from the tuple, that check inverts: it then forces every memory note truthfully describing the
now-live mechanism to carry a false marker. This test closes that recurrence by asserting the
converse invariant directly against the tuple as it exists in the script today.

Requirements: issue #176 (test stage).
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[3]
CHECK_BINDINGS = ROOT / "scaffold/bin/check-bindings.sh"

# `git grep` for a deleted-mechanism token also matches this test module's own source (it names
# every token to describe its resolution shapes) and its sibling below (which names one token in
# its docstring). Both must be exempted by their exact repo-relative path — not a broad `tests/`
# prefix, which would blind the liveness check to any *other* test file that happens to
# reference a deleted-mechanism token.
STRAY_EXEMPT_PATHS = frozenset(
    {
        "scaffold/bin/check-bindings.sh",
        "tests/unit/scaffold/test_deleted_mechanism_tokens_are_dead.py",
        "tests/unit/scaffold/test_claude_skill_permissions_resolve.py",
    }
)

# check-bindings.sh's own memory-note gate requires only that one of these words appear
# *anywhere in the file*, case-insensitively — with no requirement that it sit near the token's
# own mention. A note can therefore truthfully describe a mechanism as still *live* right next
# to its token and still satisfy check-bindings.sh, as long as an unrelated marker word appears
# elsewhere in the same file. This test does not rely on that weaker file-level rule: it requires
# a marker within a small line window of the token's own mention before tolerating a memory hit.
SUPERSESSION_MARKERS = ("historical", "deleted", "removed", "superseded")
MEMORY_MARKER_WINDOW = 2  # lines of context on each side of the token's own line


def _deleted_mechanism_tokens(script: Path | None = None) -> tuple[str, ...]:
    """Parse the DELETED_MECHANISM_TOKENS tuple out of the script's own source.

    Parsing rather than transcribing a copy keeps this test bound to whatever tokens
    check-bindings.sh actually declares, instead of pinning today's tuple contents. Reading
    the `CHECK_BINDINGS` module global by name (rather than binding it as a default
    parameter value) keeps this monkeypatchable at call time.

    The tuple body is parsed with `ast.literal_eval` rather than a quoted-string regex so that
    every element is captured regardless of quote style, and a truncated or non-literal match
    raises loudly instead of silently narrowing the tuple this test asserts against.
    """
    text = (script or CHECK_BINDINGS).read_text()
    match = re.search(r"DELETED_MECHANISM_TOKENS = \((.*?)\n\)\n", text, re.DOTALL)
    assert match, "DELETED_MECHANISM_TOKENS tuple not found in check-bindings.sh"
    return ast.literal_eval("(" + match.group(1) + "\n)")


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return result.stdout.splitlines()


def _has_nearby_supersession_marker(path: Path, line_no: int) -> bool:
    lines = path.read_text().splitlines()
    start = max(0, line_no - 1 - MEMORY_MARKER_WINDOW)
    end = min(len(lines), line_no + MEMORY_MARKER_WINDOW)
    window = " ".join(lines[start:end]).lower()
    return any(marker in window for marker in SUPERSESSION_MARKERS)


def test_token_parser_finds_a_non_empty_tuple() -> None:
    # Backstop: if the script's formatting ever drifts out from under the regex above, fail
    # loudly here instead of the assertions below silently iterating zero tokens.
    assert len(_deleted_mechanism_tokens()) >= 1


def test_deleted_mechanism_tokens_do_not_resolve_to_a_live_path() -> None:
    """Every token is checked against the union of every resolution shape it could take,
    regardless of the shape it happens to have today. A single shape-routed check per token
    (dispatching on whether it contains a slash or a dot) lets a token revived under a
    *different* shape than its current one pass silently — e.g. a bare skill-style token
    revived as a tracked script, or a dot-bearing token revived as a directory. The union
    below closes that gap: it is deliberately more checks than any single token's declared
    shape strictly requires.
    """
    tracked = _tracked_files()
    tracked_basenames = {Path(path).name for path in tracked}
    tracked_components: set[str] = set()
    for path in tracked:
        tracked_components.update(Path(path).parts)

    for token in _deleted_mechanism_tokens():
        stem = token.rstrip("/")
        candidate = ROOT / stem
        skill_dir = ROOT / ".agents/skills" / stem
        resolutions = {
            f"{candidate} exists on disk (file or directory)": candidate.exists(),
            "a tracked file has this basename": stem in tracked_basenames,
            "a tracked path has this as a path component": stem in tracked_components,
            f"{skill_dir} is an installed skill directory": skill_dir.is_dir(),
        }
        live = [description for description, hit in resolutions.items() if hit]
        assert not live, (
            f"{token!r} is declared a deleted mechanism but resolves to a live path: {live}"
        )


def test_deleted_mechanism_tokens_have_no_live_reference_outside_their_declaration() -> None:
    """A token may legitimately appear in `check-bindings.sh` (declaring it dead), in this pair
    of test modules (asserting the declaration), or in a `scaffold/memory/*/*.md` note whose own
    nearby text (within `MEMORY_MARKER_WINDOW` lines) carries a supersession marker. Any other
    tracked reference — a permission allow-list, an agent binding, a doc, or a memory note that
    names the token without nearby supersession language — means some live file still treats the
    mechanism as real, which is exactly the class of bug this issue fixed (`.claude/settings.json`'s
    orphaned `Skill(prauto-check-status)` entry).
    """
    for token in _deleted_mechanism_tokens():
        result = subprocess.run(
            ["git", "grep", "-n", "-F", token],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode in (0, 1), f"git grep failed for {token!r}: {result.stderr}"
        stray = []
        for line in result.stdout.splitlines():
            if not line:
                continue
            file_path, _, rest = line.partition(":")
            if file_path in STRAY_EXEMPT_PATHS:
                continue
            if file_path.startswith("scaffold/memory/"):
                line_no_str, _, _content = rest.partition(":")
                if _has_nearby_supersession_marker(ROOT / file_path, int(line_no_str)):
                    continue
            stray.append(line)
        assert not stray, (
            f"{token!r} is declared a deleted mechanism but is still referenced live "
            f"outside its own declaration: {stray}"
        )
