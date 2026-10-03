"""Tests for the isolation tripwire itself (tests/unit/scaffold/conftest.py).

Why this exists: the prauto shell libraries derive their durable-state paths from
``PRAUTO_DIR`` at source time. A harness that points ``PRAUTO_DIR`` at the real
``.prauto`` and then provisions/tears down writes a marker plus provision/teardown logs
into the real ``.prauto/state/`` — where a live heartbeat finds a marker naming a pytest
tmp env file, and where log pruning deletes genuine provisioning transcripts.

spec: spec/AI_PRAUTO.md §Provisioning ("Teardown verifies deletion before clearing its
marker" — the marker is the live heartbeat's only evidence of a cluster it must tear
down, so a test must never be able to forge, overwrite or clear it).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

from tests.unit.scaffold.prauto_isolation import (
    ISOLATE_REAL_STATE_SHELL,
    REAL_STATE_DIR,
    SCRATCH_STATE_ENV,
    diff_state_snapshots,
    snapshot_real_state,
)

ROOT = Path(__file__).parents[3]
PRAUTO = ROOT / ".prauto"


def test_snapshot_covers_exactly_the_guarded_names(tmp_path: Path) -> None:
    for name in (
        "dev-env-provisioned.json",
        "provision-AbC123",
        "teardown-XyZ789",
        "dev-lock-token.json",
        "conflict-attempt-5.json",
        "retry-count-1.json",  # not guarded: owned by other tests/heartbeats
        "heartbeat.log",
    ):
        (tmp_path / name).write_text("x")
    assert sorted(snapshot_real_state(tmp_path)) == [
        "conflict-attempt-5.json",
        "dev-env-provisioned.json",
        "dev-lock-token.json",
        "provision-AbC123",
        "teardown-XyZ789",
    ]
    assert snapshot_real_state(tmp_path / "does-not-exist") == {}


def test_diff_reports_created_modified_and_removed(tmp_path: Path) -> None:
    (tmp_path / "provision-old").write_text("a")
    (tmp_path / "dev-lock-token.json").write_text("a")
    before = snapshot_real_state(tmp_path)
    assert diff_state_snapshots(before, snapshot_real_state(tmp_path)) == []

    (tmp_path / "dev-env-provisioned.json").write_text("marker")  # created
    (tmp_path / "provision-old").unlink()  # removed (what log pruning does)
    token = tmp_path / "dev-lock-token.json"
    token.write_text("a longer body")  # modified (size differs even on coarse mtimes)
    os.utime(token, ns=(1, 1))
    (tmp_path / "unrelated.txt").write_text("ignored")

    assert diff_state_snapshots(before, snapshot_real_state(tmp_path)) == [
        "created: dev-env-provisioned.json",
        "removed: provision-old",
        "modified: dev-lock-token.json",
    ]


def test_isolation_snippet_moves_every_durable_path_out_of_the_real_state_dir(
    prauto_state_isolation: Path,
) -> None:
    """Sourcing the libraries with the REAL PRAUTO_DIR (what most harnesses do) and then
    applying the snippet leaves no state path under the real .prauto/state/."""
    script = "\n".join(
        [
            f"PRAUTO_DIR={shlex.quote(str(PRAUTO))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/helpers.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/phases.sh'))}",
            'printf "before:%s|%s|%s\\n" "$DEV_ENV_STATE_FILE" "${STATE_DIR:-}" '
            '"$DEV_LOCK_TOKEN_FILE"',
            ISOLATE_REAL_STATE_SHELL,
            'printf "after:%s|%s|%s\\n" "$DEV_ENV_STATE_FILE" "$STATE_DIR" "$DEV_LOCK_TOKEN_FILE"',
        ]
    )
    result = subprocess.run(  # noqa: S603
        ["bash", "-c", script], capture_output=True, check=False, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    lines = dict(line.split(":", 1) for line in result.stdout.splitlines() if ":" in line)

    # Control: without the snippet the library really does resolve into the real dir
    # (otherwise the assertions below would pass vacuously).
    assert str(REAL_STATE_DIR) in lines["before"]

    marker, state_dir, token = lines["after"].split("|")
    scratch = str(prauto_state_isolation)
    assert os.environ[SCRATCH_STATE_ENV] == scratch
    assert marker == f"{scratch}/dev-env-provisioned.json"
    assert state_dir == scratch
    assert token == f"{scratch}/dev-lock-token.json"
    assert str(REAL_STATE_DIR) not in lines["after"]


def test_the_snippet_refuses_to_run_outside_the_fixture() -> None:
    """`:?` makes a harness without the scratch dir fail loudly instead of falling back."""
    env = {k: v for k, v in os.environ.items() if k != SCRATCH_STATE_ENV}
    result = subprocess.run(  # noqa: S603
        ["bash", "-c", ISOLATE_REAL_STATE_SHELL],
        capture_output=True,
        check=False,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode != 0
    assert SCRATCH_STATE_ENV in result.stderr


# A harness points PRAUTO_DIR at the real checkout when the interpolated expression names the
# PRAUTO path constant or spells out a `.prauto` path, however it is quoted or joined.
_REAL_PRAUTO_DIR = re.compile(r"PRAUTO_DIR=[^\n]*?(?:\bPRAUTO\b|['\"/]\.prauto\b)")
# Libraries whose top level derives durable-state paths (marker, STATE_DIR, token) from
# PRAUTO_DIR: phases.sh (-> dev-env.sh) and state.sh. Matched by library name or by a
# module constant that names one.
_STATE_BEARING_LIB = re.compile(
    r"\b(?:phases|state|dev-env)\.sh\b|\b(?:PHASES|STATE_SH|STATE_LIB(?:RARY)?)\b"
)

# Exempt files, with the reason. Everything else that matches must isolate. (Harnesses that
# point PRAUTO_DIR at a scratch/copied dir simply do not match the real-dir pattern.)
_ALLOW_LIST = {
    "test_prauto_state_isolation.py": "this file: it embeds the patterns as data",
}


def harness_needs_isolation(text: str) -> bool:
    return bool(_REAL_PRAUTO_DIR.search(text) and _STATE_BEARING_LIB.search(text))


def test_the_scan_recognizes_each_way_a_harness_can_name_the_real_dir() -> None:
    sources_state = "source x/lib/state.sh\n"
    for spelling in (
        "PRAUTO_DIR={shlex.quote(str(PRAUTO))}",
        "PRAUTO_DIR={shlex.quote(str(ROOT / '.prauto'))}",
        'PRAUTO_DIR={shlex.quote(str(ROOT / ".prauto"))}',
        "PRAUTO_DIR={quoted(str(REPO / '.prauto'))}",
        "PRAUTO_DIR={ROOT}/.prauto",
    ):
        assert harness_needs_isolation(spelling + "\n" + sources_state), spelling
    # Scratch dirs and libraries without state-derived paths are not flagged.
    assert not harness_needs_isolation(
        "PRAUTO_DIR={shlex.quote(str(tmp_path / 'prauto'))}\n" + sources_state
    )
    assert not harness_needs_isolation(
        "PRAUTO_DIR={shlex.quote(str(PRAUTO))}\nsource x/lib/git-ops.sh\n"
    )
    assert harness_needs_isolation("PRAUTO_DIR={q(str(PRAUTO))}\nsource x/lib/dev-env.sh\n")
    assert harness_needs_isolation("PRAUTO_DIR={q(str(PRAUTO))}\nsource {PHASES}\n")


def test_every_harness_that_can_reach_state_paths_against_the_real_prauto_dir_isolates() -> None:
    """Static completeness check: a harness that sets PRAUTO_DIR to the real .prauto and
    sources phases.sh, dev-env.sh or state.sh must append the isolation snippet, or it can
    write a marker, provisioning logs or a lock token into the live heartbeat's state dir
    (state.sh fixes STATE_DIR; dev-env.sh fixes the token file; phases.sh the marker)."""
    offenders, checked = [], 0
    for path in sorted(Path(__file__).parent.glob("test_prauto_*.py")):
        if path.name in _ALLOW_LIST:
            continue
        text = path.read_text()
        if not harness_needs_isolation(text):
            continue
        checked += 1
        if "ISOLATE_REAL_STATE_SHELL" not in text:
            offenders.append(path.name)
    assert checked >= 5, "the scan matched too few files to be trusted"
    assert offenders == []
