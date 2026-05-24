"""Ontogen Langfuse run_id surfacing — api-wired integration test.

Spec: spec/feature/BACKEND_LLM.md §Observability
      spec/USE_CASE_en.md §UC3 Ontology Generation

Verifies that a real end-to-end ontogen run (under test-mode stubs — no real
LLM, no real Langfuse) generates a UUID4 run_id and surfaces it in the
ONTOGEN_RUN_COMPLETE (or ONTOGEN_RUN_FAILED) event accessible via
GET /api/v1/spoke/common/ontogen/event.

Also checks that the existing producer_iterations / producer_errors_dropped
fields are still present in the event detail — regression guard to confirm
run_id addition did not displace prior telemetry fields.

Operating mode: DATASPOKE_TEST_MODE=true (StubLLMClient, no real Langfuse).
Langfuse env vars intentionally absent so _langfuse_handler is None and the
observability layer is exercised only at the run_id-threading level.

Prerequisite for running:
    ./helm-charts/bin/install.sh --profile dev --components api --skip-build
    uv run python -m tests.integration.util --reset-seed
    DATASPOKE_TEST_MODE=true uv run pytest \
        tests/integration/api_wired/test_ontogen_langfuse_run_id.py -v
"""

# spec: USE_CASE_en.md §UC3

import asyncio
import re
import time

import httpx
import pytest

# UUID4 regex: version nibble = 4, variant nibble = 8-b
_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

# ----- test data constants -----------------------------------------------
# No schemas/topics to seed: this test observes events emitted by a run that
# uses the empty-dataset path (no PG/Kafka fixture data needed).
# If the ontogen conf is missing we enable it inline in the test.


@pytest.mark.asyncio
async def test_ontogen_run_complete_event_surfaces_run_id(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Run ontogen under stub mode and assert run_id appears in ONTOGEN_RUN_COMPLETE event detail.

    Steps (mirror spec/USE_CASE_en.md §UC3 dry-run narrative):

    1. Ensure ontogen is configured — PUT conf with is_enabled=true.
    2. POST /spoke/common/ontogen/method/run?dry_run=true to trigger inference.
       (dry_run avoids DB-write side effects; stub mode returns immediately.)
    3. Poll GET /spoke/common/ontogen/event until ONTOGEN_RUN_COMPLETE or
       ONTOGEN_RUN_FAILED appears in the first page.
    4. Assert event detail contains 'run_id' matching the UUID4 pattern.
    5. Assert event detail still contains 'producer_iterations' and
       'producer_errors_dropped' (regression — these fields must not be
       displaced by run_id addition).

    Spec: BACKEND_LLM.md §Observability — run_id generated in service.run(),
    threaded through debate, recorded in ONTOGEN_RUN_COMPLETE detail.
    Spec: API.md §Ontology Generation — POST /ontogen/method/run,
    GET /ontogen/event.
    """
    # ── Step 1: Enable ontogen (idempotent PUT) ───────────────────────────
    # spec: USE_CASE_en.md §UC3 — operator enables ontogen before running
    put_resp = await api_client.put(
        "/api/v1/spoke/common/ontogen/attr/conf",
        headers={**admin_headers, "Content-Type": "application/json"},
        json={
            "is_enabled": True,
            "dataset_filter": {},
        },
    )
    assert put_resp.status_code == 200, (
        f"PUT /ontogen/attr/conf must return 200; got {put_resp.status_code}: {put_resp.text}. "
        "spec: API.md §Ontology Generation §conf PUT"
    )

    # ── Step 2: POST dry-run to trigger inference ─────────────────────────
    # spec: USE_CASE_en.md §UC3 — fire a dry-run inference request
    # Inline the full request payload for readability (feedback_test_readability.md)
    run_resp = await api_client.post(
        "/api/v1/spoke/common/ontogen/method/run",
        headers={**admin_headers},
        params={"dry_run": "true"},
    )
    assert run_resp.status_code == 200, (
        f"POST /ontogen/method/run?dry_run=true must return 200; "
        f"got {run_resp.status_code}: {run_resp.text}. "
        "spec: API.md §Ontology Generation §method/run"
    )

    run_body = run_resp.json()
    # spec: API.md §OntogenRunResponse — must carry status and dry_run fields
    assert run_body.get("dry_run") is True, (
        f"OntogenRunResponse.dry_run must be True; got {run_body!r}. "
        "spec: API.md §Ontology Generation §OntogenRunResponse"
    )

    # ── Step 3: Poll for the RUN_COMPLETE (or RUN_FAILED) event ──────────
    # spec: USE_CASE_en.md §UC3 — observe the run outcome via GET /event
    # Poll up to 30 s; stub mode should complete within 2-3 s
    terminal_event: dict | None = None
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        event_resp = await api_client.get(
            "/api/v1/spoke/common/ontogen/event",
            headers=admin_headers,
            params={"limit": 5},
        )
        assert event_resp.status_code == 200, (
            f"GET /ontogen/event must return 200; got {event_resp.status_code}: {event_resp.text}."
        )

        events = event_resp.json().get("events", [])
        for ev in events:
            etype = ev.get("event_type", "")
            if etype in ("ONTOGEN.RUN_COMPLETE", "ONTOGEN.RUN_FAILED"):
                terminal_event = ev
                break
        if terminal_event:
            break
        await asyncio.sleep(1.0)

    assert terminal_event is not None, (
        "Timed out (30 s) waiting for ONTOGEN.RUN_COMPLETE or ONTOGEN.RUN_FAILED event. "
        "spec: USE_CASE_en.md §UC3 — every run must emit a terminal event."
    )

    # ── Step 4: Assert run_id is a valid UUID4 ────────────────────────────
    # spec: BACKEND_LLM.md §Observability — 'run_id (uuid4) recorded in event detail'
    detail = terminal_event.get("detail") or {}
    assert "run_id" in detail, (
        f"Event detail must contain 'run_id'; got keys {list(detail.keys())!r}. "
        "spec: BACKEND_LLM.md §Observability — run_id must be present in terminal event detail"
    )

    run_id = detail["run_id"]
    assert isinstance(run_id, str) and _UUID4_RE.match(run_id), (
        f"detail['run_id'] must match UUID4 pattern; got {run_id!r}. "
        "spec: BACKEND_LLM.md §Observability — run_id is uuid4 from service.run()"
    )

    # ── Step 5: Regression — existing telemetry fields must still be present ─
    # spec: BACKEND_LLM.md §Inference Loop — 'producer_iterations and
    # producer_errors_dropped in RUN_COMPLETE detail; run_id must not displace them.'
    if terminal_event.get("event_type") == "ONTOGEN.RUN_COMPLETE":
        assert "producer_iterations" in detail, (
            f"ONTOGEN.RUN_COMPLETE detail must still contain 'producer_iterations' after "
            f"run_id was added; got keys {list(detail.keys())!r}. "
            "Regression: BACKEND_LLM.md §Inference Loop — producer_iterations must be present"
        )
        assert "producer_errors_dropped" in detail, (
            f"ONTOGEN.RUN_COMPLETE detail must still contain 'producer_errors_dropped'; "
            f"got keys {list(detail.keys())!r}. "
            "Regression: BACKEND_LLM.md §Inference Loop — producer_errors_dropped must be present"
        )
