"""Fixtures for the prauto shell-library unit tests.

``prauto_state_isolation`` is autouse: it hands every test a scratch state dir
(exported as ``PRAUTO_TEST_STATE_DIR`` for harnesses that append
``ISOLATE_REAL_STATE_SHELL``) and fails any test that created, modified or
removed a marker, provision/teardown log or lock-token file in the REAL
``.prauto/state/``. That directory belongs to the live heartbeat; see
``prauto_isolation`` for why a test must never write there.

Caveat: the check is a before/after comparison, so a genuine heartbeat running
on the same machine during a test can trip it. Re-run when the reported name is
not one the test could have produced.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.unit.scaffold.prauto_isolation import (
    REAL_STATE_DIR,
    SCRATCH_STATE_ENV,
    diff_state_snapshots,
    snapshot_real_state,
)


@pytest.fixture(autouse=True)
def prauto_state_isolation(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    scratch = tmp_path_factory.mktemp("prauto-scratch-state")
    monkeypatch.setenv(SCRATCH_STATE_ENV, str(scratch))
    before = snapshot_real_state()
    yield scratch
    problems = diff_state_snapshots(before, snapshot_real_state())
    if problems:
        pytest.fail(
            f"test touched the real {REAL_STATE_DIR} (it belongs to the live heartbeat; "
            "point PRAUTO_DIR/STATE_DIR/DEV_ENV_STATE_FILE/DEV_LOCK_TOKEN_FILE at a scratch dir): "
            + "; ".join(problems),
            pytrace=False,
        )
