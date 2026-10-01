"""Isolation helpers for the prauto shell-library unit tests.

The prauto libraries compute their durable-state paths from ``PRAUTO_DIR`` at
source time (``phases.sh`` -> ``DEV_ENV_STATE_FILE``; ``state.sh`` ->
``STATE_DIR``; ``dev-env.sh`` -> ``DEV_LOCK_TOKEN_FILE``). The test harnesses
source the libraries from the REAL ``.prauto/lib`` (fine: read-only) but must
never let those paths resolve under the real ``.prauto/state/``, which a real
heartbeat on the same machine reads: a test-written marker naming a pytest tmp
env file is discarded by it, and a test run can overwrite or prune a genuine
marker or its provisioning logs.

Two pieces live here:

* ``ISOLATE_REAL_STATE_SHELL`` — a shell snippet a harness appends AFTER its
  ``source .../phases.sh`` line, redirecting all three paths into the scratch
  directory the autouse fixture in ``conftest.py`` provides.
* ``snapshot_real_state`` / ``diff_state_snapshots`` — the tripwire the same
  fixture uses to fail any test that nevertheless touches the guarded names.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

ROOT = Path(__file__).parents[3]
REAL_STATE_DIR = ROOT / ".prauto" / "state"

# Env var carrying the per-test scratch state dir into the harness subprocesses.
SCRATCH_STATE_ENV = "PRAUTO_TEST_STATE_DIR"

# The durable names a heartbeat owns: the provisioning/reap marker, the private
# provision/teardown transcripts, and the dev-lock token claim.
GUARDED_PATTERNS = (
    "dev-env-provisioned.json",
    "provision-*",
    "teardown-*",
    "dev-lock-token.json",
)

# `:?` makes a harness run outside the autouse fixture fail loudly rather than
# silently fall back to the real state directory.
ISOLATE_REAL_STATE_SHELL = f"""
DEV_ENV_STATE_FILE="${{{SCRATCH_STATE_ENV}:?}}/dev-env-provisioned.json"
STATE_DIR="${{{SCRATCH_STATE_ENV}:?}}"
DEV_LOCK_TOKEN_FILE="${{{SCRATCH_STATE_ENV}:?}}/dev-lock-token.json"
"""


def snapshot_real_state(state_dir: Path = REAL_STATE_DIR) -> dict[str, tuple[int, int]]:
    """Map each guarded name present in ``state_dir`` to ``(mtime_ns, size)``."""
    if not state_dir.is_dir():
        return {}
    snap: dict[str, tuple[int, int]] = {}
    for entry in state_dir.iterdir():
        if any(fnmatch.fnmatch(entry.name, pat) for pat in GUARDED_PATTERNS):
            try:
                st = entry.stat()
            except FileNotFoundError:  # removed between listdir and stat
                continue
            snap[entry.name] = (st.st_mtime_ns, st.st_size)
    return snap


def diff_state_snapshots(
    before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]
) -> list[str]:
    """Human-readable created/modified/removed lines; empty when identical."""
    problems = [f"created: {name}" for name in sorted(after.keys() - before.keys())]
    problems += [f"removed: {name}" for name in sorted(before.keys() - after.keys())]
    problems += [
        f"modified: {name}"
        for name in sorted(before.keys() & after.keys())
        if before[name] != after[name]
    ]
    return problems
