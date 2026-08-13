"""Confidence-threshold gate test for the metagen run pipeline.

After the adversarial debate accepts a payload, only candidates whose
``confidence_score >= METAGEN_CONFIDENCE_THRESHOLD`` may persist; below-threshold
candidates are dropped (metagen has no llm_pending state). This test drives
``MetagenService._run_inner`` with an accepted payload whose candidates straddle the
threshold and asserts that only the at/above-threshold candidates reach persistence
(``_apply_per_item_budget``) while the below-threshold one is dropped.

Spec:
  spec/feature/BACKEND_LLM.md §Metagen Adversarial Debate — "Below-threshold candidates
    are dropped ... Only candidates with outcome=accept AND
    confidence_score >= METAGEN_CONFIDENCE_THRESHOLD persist as status='llm_approved'."
  spec/feature/BACKEND_LLM.md §Metagen Adversarial Debate — Confidence threshold
    METAGEN_CONFIDENCE_THRESHOLD (default 0.7).
"""

import types
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.admin.config_service import RUNTIME_CONFIG_DEFAULTS, RuntimeConfigDTO
from src.backend.metagen.service import MetagenConfDTO, MetagenService
from tests.unit.conftest import route_db_execute

_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"


def _make_conf(conf_id: str) -> MetagenConfDTO:
    now = datetime.now(tz=UTC)
    return MetagenConfDTO(
        id=conf_id,
        name="catalog-docs",
        is_enabled=True,
        schedule_tier=None,
        dataset_filter="",
        result_limit=3,
        overwrite_pending=True,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_below_threshold_candidate_dropped_at_or_above_persists(monkeypatch) -> None:
    """Only candidates at/above METAGEN_CONFIDENCE_THRESHOLD reach persistence.

    Seeds THREE accepted candidates straddling the threshold (above, exactly at, below)
    and asserts persistence is attempted for exactly the above + at-threshold pair and
    never for the below-threshold one.

    Spec: BACKEND_LLM.md §Metagen Adversarial Debate — below-threshold candidates dropped;
    at/above-threshold persist.
    """
    rc = RuntimeConfigDTO(**RUNTIME_CONFIG_DEFAULTS)
    threshold = rc.metagen_confidence_threshold

    above = round(min(threshold + 0.2, 1.0), 3)
    at = threshold
    below = round(max(threshold - 0.2, 0.0), 3)
    # Guard the fixture itself: the three scores must actually straddle the threshold,
    # otherwise the gate assertions below would be vacuous.
    assert above >= threshold and at >= threshold and below < threshold

    conf_id = str(uuid.uuid4())
    conf = _make_conf(conf_id)

    svc = MetagenService(
        datahub=AsyncMock(),
        db=AsyncMock(spec=AsyncSession),
        cache=AsyncMock(),
        llm=AsyncMock(),
        vector=AsyncMock(),
    )

    # Runtime config (threshold + debate tunables) — no DB round-trip.
    monkeypatch.setattr(
        "src.backend.metagen.service.get_runtime_config", AsyncMock(return_value=rc)
    )
    # Neutralise the LLM/debate scaffolding so the test isolates the confidence gate.
    monkeypatch.setattr("src.backend.metagen.service.build_run_prompt", lambda **_k: "PROMPT")
    monkeypatch.setattr(
        "src.backend.metagen.service.build_metagen_validate_tool", lambda **_k: MagicMock()
    )
    monkeypatch.setattr(
        "src.backend.metagen.service.build_metagen_review_tool", lambda **_k: MagicMock()
    )
    debate_result = types.SimpleNamespace(
        outcome="accept",
        transcript={},
        payload={
            "candidates": [
                {"dataset_urn": _URN, "item_id": "above", "value": "v-above",
                 "confidence_score": above},
                {"dataset_urn": _URN, "item_id": "at", "value": "v-at",
                 "confidence_score": at},
                {"dataset_urn": _URN, "item_id": "below", "value": "v-below",
                 "confidence_score": below},
            ]
        },
    )
    monkeypatch.setattr(
        "src.backend.metagen.service.run_debate", AsyncMock(return_value=debate_result)
    )

    # Enumeration / evidence / rejected-clear are exercised elsewhere; stub them so this
    # test spans only the accept→gate→persist segment.
    svc._enumerate_in_scope_datasets = AsyncMock(return_value=([_URN], []))  # type: ignore[method-assign]
    svc._fetch_evidence = AsyncMock(return_value={})  # type: ignore[method-assign]
    svc._clear_rejected_candidates = AsyncMock(return_value=0)  # type: ignore[method-assign]
    svc._enumerate_target_items = MagicMock(  # type: ignore[method-assign]
        return_value=[{"item_id": "above", "kind": "column.description"}]
    )
    svc._record_metagen_event = AsyncMock()  # type: ignore[method-assign]
    apply_budget = AsyncMock(return_value=(True, False))
    svc._apply_per_item_budget = apply_budget  # type: ignore[method-assign]

    # DB: only the in-loop boundary lookup and approved-item lookup remain.
    boundary_row = types.SimpleNamespace(is_enabled=True, allowed=["column.description"])
    boundary_res = MagicMock()
    boundary_res.scalar_one_or_none.return_value = boundary_row
    approved_res = MagicMock()
    approved_res.fetchall.return_value = []
    route_db_execute(
        svc._db,  # type: ignore[arg-type]
        [("metagen_boundary", boundary_res), ("metagen_candidates", approved_res)],
    )

    result = await svc._run_inner(
        conf=conf, dataset_urns=None, dry_run=False, run_id=str(uuid.uuid4())
    )

    # Persistence attempted for exactly the two non-dropped candidates.
    assert apply_budget.await_count == 2
    persisted_item_ids = {c.kwargs["item_id"] for c in apply_budget.await_args_list}
    assert persisted_item_ids == {"above", "at"}
    # The below-threshold candidate was injected but must never be persisted.
    assert "below" not in persisted_item_ids
    # Run counts corroborate the two persisted candidates.
    assert result.counts["candidates_added"] == 2
