"""Regression test: every allow-listed `Skill(...)` permission names a real skill.

`.claude/settings.json`'s `permissions.allow` previously contained `Skill(prauto-check-status)`,
naming a skill that does not exist under `.agents/skills/`. This asserts the direction that is
actually an invariant — every allowed skill must resolve to a real `.agents/skills/<name>/SKILL.md`
— not its converse. Four skills (`ref-setup`, `spec-to-bulk-issue`, `test-manual-api-wired`,
`test-manual-ui`) are deliberately absent from the allow-list so they prompt for confirmation
before running; asserting "every existing skill must be allowed" would contradict that by design.

Requirements: issue #176 (test stage).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[3]
SETTINGS = ROOT / ".claude/settings.json"


def _allow_listed_skill_names(settings: Path | None = None) -> list[str]:
    # Read the SETTINGS module global by name (rather than binding it as a default
    # parameter value) so it stays monkeypatchable at call time.
    allow = json.loads((settings or SETTINGS).read_text())["permissions"]["allow"]
    names = []
    for entry in allow:
        match = re.fullmatch(r"Skill\(([^)]+)\)", entry)
        if match:
            names.append(match.group(1))
    return names


def test_settings_allow_list_contains_at_least_one_skill_entry() -> None:
    # Backstop: proves the parametrized assertion below actually exercised real entries
    # instead of vacuously passing over an empty list.
    assert len(_allow_listed_skill_names()) >= 1


def test_every_allow_listed_skill_has_a_backing_skill_definition() -> None:
    missing = [
        name
        for name in _allow_listed_skill_names()
        if not (ROOT / ".agents/skills" / name / "SKILL.md").is_file()
    ]
    assert not missing, (
        "permissions.allow names Skill(...) entries with no .agents/skills/<name>/SKILL.md: "
        f"{missing}"
    )
