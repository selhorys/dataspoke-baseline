"""Spot tests for Metadata Generation — item list endpoints and candidate review.

Concerns covered (10 test functions across 2 groups):

Item endpoints (Group 4, raw-SQL seeded):
  test_metagen_items_list_global_paginated_envelope
  test_metagen_items_list_filters_dataset_kind_status
  test_metagen_item_detail_by_composite_id

Candidate review (Group 5, raw-SQL seeded):
  test_metagen_candidate_approve_flips_status_and_emits_event
  test_metagen_candidate_approve_demotes_prior_approved_sibling
  test_metagen_candidate_reject_emits_event
  test_metagen_candidate_reject_approved_clears_datahub_description
  test_metagen_item_status_pending_when_only_rejected_candidates
  test_metagen_candidate_approve_demotes_cross_conf_sibling
  test_metagen_candidate_review_without_enabled_boundary_returns_422_METAGEN_DATASET_NOT_IN_BOUNDARY

All tests use raw-SQL seeding via tests.integration.util.metagen helpers because
the concern under test is the review/query behavior, not the run pipeline that
would normally produce candidate rows.

Per-item budget rules (result_limit, overwrite_pending FIFO eviction) are
covered at the unit level in tests/unit/backend/metagen/test_service.py
(_apply_per_item_budget); integration coverage would be redundant.

spec: USE_CASE_en.md §UC4: Metadata Generation
spec: BACKEND.md §Approval flow — mutable approval, partial unique index on approved
spec: TESTING.md §Spot vs Api-Wired Integration Tests
"""

import urllib.parse
import uuid
from contextlib import suppress

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.util.metagen import (
    delete_metagen_conf,
    delete_metagen_state_for_urn,
    seed_metagen_candidate,
    seed_metagen_conf,
    seed_metagen_item,
)

# Declare DataHub fixture dependency so module_dummy_data ingests catalog.title_master
# into DataHub + PG before any tests run.
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

# Primary test dataset — catalog.title_master (Imazon UC4 table).
# spec: TESTING.md §Imazon Dummy-Data Reference
_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_TEST_URN2 = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.book_master,DEV)"
_ENCODED_URN = urllib.parse.quote(_TEST_URN, safe="")
_ENCODED_URN2 = urllib.parse.quote(_TEST_URN2, safe="")


# ── Group 4: Item endpoints (raw-SQL seeded) ──────────────────────────────────


@pytest.mark.asyncio
async def test_metagen_items_list_global_paginated_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """GET /metagen/item returns MetagenItemListResponse envelope with pagination keys.

    Seeds two items across two datasets; verifies envelope structure and
    pagination parameters are echoed correctly.

    spec: USE_CASE_en.md §UC4 — item list endpoint
    spec: API.md §Standard Envelope — items, offset, limit, total_count
    spec: API.md §Metadata Generation — MetagenItemListResponse envelope
    """
    item_list_url = "/api/v1/spoke/metagen/item"

    try:
        await seed_metagen_item(
            async_session,
            dataset_urn=_TEST_URN,
            item_id="dataset.description",
            kind="dataset.description",
        )
        await seed_metagen_item(
            async_session,
            dataset_urn=_TEST_URN2,
            item_id="dataset.description",
            kind="dataset.description",
        )

        # Paginated GET
        resp = await api_client.get(
            f"{item_list_url}?offset=0&limit=10",
            headers=admin_headers,
        )
        assert resp.status_code == 200, (
            f"GET /metagen/item failed: {resp.status_code} {resp.text}. "
            "spec: USE_CASE_en.md §UC4"
        )
        body = resp.json()

        # Envelope keys
        assert "items" in body and isinstance(body["items"], list), (
            "MetagenItemListResponse must have 'items' list. spec: API.md §Standard Envelope"
        )
        assert "offset" in body, (
            "MetagenItemListResponse must have 'offset'. spec: API.md §Standard Envelope"
        )
        assert "limit" in body, (
            "MetagenItemListResponse must have 'limit'. spec: API.md §Standard Envelope"
        )
        assert "total_count" in body and isinstance(body["total_count"], int), (
            "MetagenItemListResponse must have 'total_count' int. "
            "spec: API.md §Standard Envelope"
        )
        assert body["offset"] == 0, (
            f"offset echo mismatch: {body.get('offset')!r}. spec: API.md §Standard Envelope"
        )
        assert body["limit"] == 10, (
            f"limit echo mismatch: {body.get('limit')!r}. spec: API.md §Standard Envelope"
        )
        assert body["total_count"] >= 2, (
            f"total_count must be >= 2 after seeding two items; got {body.get('total_count')!r}"
        )

        # Each item must have the required summary fields
        for item in body["items"]:
            assert "dataset_urn" in item, "item missing dataset_urn"
            assert "item_id" in item, "item missing item_id"
            assert item["kind"] in (
                "dataset.description",
                "column.description",
            ), f"item kind invalid: {item.get('kind')!r}"
            assert item["status"] in (
                "pending",
                "llm_approved",
                "approved",
            ), f"item status invalid: {item.get('status')!r}"
            assert "candidate_count" in item, "item missing candidate_count"
            assert "composite_id" in item, (
                "item missing composite_id. spec: USE_CASE_en.md §UC4 — API Mapping"
            )
            assert item["composite_id"] == f"{item['dataset_urn']}::{item['item_id']}", (
                f"composite_id format mismatch: {item['composite_id']!r}. "
                "spec: USE_CASE_en.md §UC4 — API Mapping"
            )

    finally:
        await delete_metagen_state_for_urn(async_session, _TEST_URN)
        await delete_metagen_state_for_urn(async_session, _TEST_URN2)


@pytest.mark.asyncio
async def test_metagen_items_list_filters_dataset_kind_status(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """GET /metagen/item with dataset_urn / kind / status filters returns matching subset.

    Seeds:
      _TEST_URN: item_id="dataset.description" (kind=dataset.description,
          one llm_approved candidate -> status=llm_approved)
      _TEST_URN: item_id="column.isbn.description" (kind=column.description,
          zero candidates -> status=pending)
      _TEST_URN2: item_id="dataset.description" (kind=dataset.description)

    Verifies:
      ?dataset_urn=_TEST_URN returns only _TEST_URN items
      ?kind=column.description returns only column-kind items
      ?status=llm_approved includes the dataset.description item with llm_approved cand

    spec: USE_CASE_en.md §UC4 — API Mapping — item list filterable by dataset_urn, kind, status
    spec: API.md §Metadata Generation — item list filter params
    """
    item_list_url = "/api/v1/spoke/metagen/item"

    try:
        # Seed dataset.description item with one llm_approved candidate
        await seed_metagen_item(
            async_session,
            dataset_urn=_TEST_URN,
            item_id="dataset.description",
            kind="dataset.description",
        )
        await seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id="dataset.description",
            value="Imazon title master catalog.",
            status="llm_approved",
        )

        # Seed column.description item with no candidates (status=pending)
        await seed_metagen_item(
            async_session,
            dataset_urn=_TEST_URN,
            item_id="column.isbn.description",
            kind="column.description",
            field_path="isbn",
        )

        # Seed an item for URN2 (to confirm dataset_urn filter excludes it)
        await seed_metagen_item(
            async_session,
            dataset_urn=_TEST_URN2,
            item_id="dataset.description",
            kind="dataset.description",
        )

        # Filter by dataset_urn — must return only _TEST_URN items
        encoded_urn = urllib.parse.quote(_TEST_URN, safe="")
        resp_by_urn = await api_client.get(
            f"{item_list_url}?dataset_urn={encoded_urn}&limit=50",
            headers=admin_headers,
        )
        assert resp_by_urn.status_code == 200, (
            f"GET items?dataset_urn failed: {resp_by_urn.status_code}"
        )
        by_urn_items = resp_by_urn.json()["items"]
        urn_set = {i["dataset_urn"] for i in by_urn_items}
        assert urn_set <= {_TEST_URN}, (
            f"dataset_urn filter returned items for other URNs: {urn_set!r}. "
            "spec: API.md §Metadata Generation — dataset_urn filter"
        )

        # Filter by kind=column.description
        resp_by_kind = await api_client.get(
            f"{item_list_url}?dataset_urn={encoded_urn}&kind=column.description&limit=50",
            headers=admin_headers,
        )
        assert resp_by_kind.status_code == 200
        by_kind_items = resp_by_kind.json()["items"]
        assert all(i["kind"] == "column.description" for i in by_kind_items), (
            f"kind filter returned non-column items: {[i['kind'] for i in by_kind_items]!r}. "
            "spec: API.md §Metadata Generation — kind filter"
        )

        # Filter by status=llm_approved — must include the seeded dataset.description item
        resp_by_status = await api_client.get(
            f"{item_list_url}?dataset_urn={encoded_urn}&status=llm_approved&limit=50",
            headers=admin_headers,
        )
        assert resp_by_status.status_code == 200
        by_status_items = resp_by_status.json()["items"]
        assert all(
            i["status"] in ("llm_approved", "approved") for i in by_status_items
        ), (
            f"status=llm_approved filter returned non-matching items: "
            f"{[i['status'] for i in by_status_items]!r}. "
            "spec: API.md §Metadata Generation — status filter"
        )
        item_ids = {i["item_id"] for i in by_status_items}
        assert "dataset.description" in item_ids, (
            f"status=llm_approved filter must include the seeded dataset.description item; "
            f"got {item_ids!r}"
        )

    finally:
        await delete_metagen_state_for_urn(async_session, _TEST_URN)
        await delete_metagen_state_for_urn(async_session, _TEST_URN2)


@pytest.mark.asyncio
async def test_metagen_item_detail_by_composite_id(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """GET /metagen/item/{urn}::{item_id} returns full item detail with candidate list.

    Seeds one item with two candidates; verifies both appear in the candidates
    list of the detail response.

    spec: USE_CASE_en.md §UC4 — API Mapping — composite_id = '{dataset_urn}::{item_id}'
    spec: API.md §Metadata Generation — composite_id path parsing
    spec: API.md §Metadata Generation — item detail candidates list
    """
    item_id = "dataset.description"
    composite_id = f"{_TEST_URN}::{item_id}"
    encoded_composite = urllib.parse.quote(composite_id, safe="")
    item_detail_url = f"/api/v1/spoke/metagen/item/{encoded_composite}"

    conf_name = f"detail-conf-{uuid.uuid4().hex[:8]}"
    conf_id = None
    try:
        conf_id = await seed_metagen_conf(async_session, name=conf_name, is_enabled=True)
        cid1 = await seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id=item_id,
            value="First candidate value.",
            status="llm_approved",
            confidence=0.91,
            conf_id=conf_id,
        )
        cid2 = await seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id=item_id,
            value="Second candidate value.",
            status="llm_approved",
            confidence=0.80,
            conf_id=conf_id,
        )

        resp = await api_client.get(item_detail_url, headers=admin_headers)
        assert resp.status_code == 200, (
            f"GET item detail by composite_id failed: {resp.status_code} {resp.text}. "
            "spec: API.md §Metadata Generation — GET item detail by composite_id"
        )
        body = resp.json()

        # Item fields
        assert body["dataset_urn"] == _TEST_URN, (
            f"detail dataset_urn mismatch: {body.get('dataset_urn')!r}"
        )
        assert body["item_id"] == item_id, (
            f"detail item_id mismatch: {body.get('item_id')!r}"
        )
        assert body["composite_id"] == composite_id, (
            f"detail composite_id mismatch: {body.get('composite_id')!r}. "
            "spec: USE_CASE_en.md §UC4 — API Mapping"
        )

        # Both seeded candidates must be present
        assert "candidates" in body and isinstance(body["candidates"], list), (
            "MetagenItemDetailResponse must have 'candidates' list. "
            "spec: API.md §Metadata Generation — item detail candidates list"
        )
        returned_ids = {c["candidate_id"] for c in body["candidates"]}
        assert cid1 in returned_ids, (
            f"Seeded candidate {cid1!r} not in detail response. "
            "spec: USE_CASE_en.md §UC4 — Review"
        )
        assert cid2 in returned_ids, (
            f"Seeded candidate {cid2!r} not in detail response. "
            "spec: USE_CASE_en.md §UC4 — Review"
        )

        # Each candidate has required fields, including conf_id/conf_name.
        for cand in body["candidates"]:
            assert "candidate_id" in cand, "candidate missing candidate_id"
            assert "value" in cand, "candidate missing value"
            assert "confidence_score" in cand, "candidate missing confidence_score"
            assert cand["status"] in (
                "llm_approved",
                "approved",
                "rejected",
            ), f"candidate status invalid: {cand.get('status')!r}"
            assert "evidence" in cand, "candidate missing evidence"
            assert "created_at" in cand, "candidate missing created_at"
            assert cand["conf_id"] == conf_id, (
                f"candidate must carry the producing conf_id; got {cand.get('conf_id')!r}. "
                "spec: API.md §Metadata Generation — candidate exposes conf_id/conf_name"
            )
            assert cand["conf_name"] == conf_name, (
                f"candidate must carry the producing conf_name; got {cand.get('conf_name')!r}. "
                "spec: API.md §Metadata Generation — candidate exposes conf_id/conf_name"
            )

    finally:
        await delete_metagen_state_for_urn(async_session, _TEST_URN)
        if conf_id is not None:
            await delete_metagen_conf(async_session, conf_id)


# ── Group 5: Candidate review (raw-SQL seeded) ────────────────────────────────


@pytest.mark.asyncio
async def test_metagen_candidate_approve_flips_status_and_emits_event(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """Approving an llm_approved candidate flips its status to 'approved'.

    Also emits METAGEN.CANDIDATE_APPROVE event on per-dataset event endpoint.

    spec: USE_CASE_en.md §UC4 — Review — approve verdict -> status=approved
    spec: BACKEND.md §Event Catalogue — METAGEN (dataset) CANDIDATE_APPROVE/REJECT —
      detail keys: item_id, candidate_id, reason
    """
    item_id = "dataset.description"
    unique_reason = f"spot test approve {uuid.uuid4()}"
    boundary_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/boundary"
    review_prefix = (
        f"/api/v1/spoke/common/data/{_ENCODED_URN}"
        f"/attr/metagen/item/{item_id}/candidate"
    )
    dataset_event_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/event/metagen"

    try:
        # Boundary required for review
        await api_client.put(
            boundary_url,
            headers=admin_headers,
            json={"is_enabled": True, "allowed": ["dataset.description"]},
        )

        cid = await seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id=item_id,
            value="Approved candidate value.",
            status="llm_approved",
        )

        review_url = f"{review_prefix}/{cid}/method/review"
        review_resp = await api_client.post(
            review_url,
            headers=admin_headers,
            json={"verdict": "approve", "reason": unique_reason},
        )
        assert review_resp.status_code == 200, (
            f"POST review (approve) failed: {review_resp.status_code} {review_resp.text}. "
            "spec: USE_CASE_en.md §UC4 — Review"
        )
        review_body = review_resp.json()
        assert review_body.get("status") == "approved", (
            f"candidate status after approve must be 'approved'; "
            f"got {review_body.get('status')!r}. spec: USE_CASE_en.md §UC4 — Review"
        )
        assert review_body.get("candidate_id") == cid, (
            "candidate_id mismatch in review response. "
            "spec: BACKEND.md §Event Catalogue — METAGEN (dataset) CANDIDATE_APPROVE/REJECT"
        )

        # METAGEN.CANDIDATE_APPROVE event emitted on per-dataset endpoint;
        # bind by candidate_id and unique reason to avoid stale event matches.
        ev_resp = await api_client.get(
            f"{dataset_event_url}?limit=20",
            headers=admin_headers,
        )
        assert ev_resp.status_code == 200
        events = ev_resp.json().get("events", [])
        approve_event = next(
            (
                e for e in events
                if e["event_type"] == "METAGEN.CANDIDATE_APPROVE"
                and e["detail"].get("candidate_id") == cid
            ),
            None,
        )
        assert approve_event is not None, (
            f"METAGEN.CANDIDATE_APPROVE event for candidate_id={cid!r} must be emitted. "
            "spec: BACKEND.md §Event Catalogue — METAGEN (dataset) CANDIDATE_APPROVE/REJECT"
        )
        ev_detail = approve_event["detail"]
        assert ev_detail["candidate_id"] == cid, (
            f"CANDIDATE_APPROVE detail candidate_id must be {cid!r}; "
            f"got {ev_detail.get('candidate_id')!r}. "
            "spec: BACKEND.md §Event Catalogue — METAGEN (dataset) CANDIDATE_APPROVE/REJECT"
        )
        assert ev_detail["item_id"] == item_id, (
            f"CANDIDATE_APPROVE detail item_id must be {item_id!r}; "
            f"got {ev_detail.get('item_id')!r}. "
            "spec: BACKEND.md §Event Catalogue — METAGEN (dataset) CANDIDATE_APPROVE/REJECT"
        )
        assert ev_detail["reason"] == unique_reason, (
            f"CANDIDATE_APPROVE detail reason must be {unique_reason!r}; "
            f"got {ev_detail.get('reason')!r}. "
            "spec: BACKEND.md §Event Catalogue — METAGEN (dataset) CANDIDATE_APPROVE/REJECT"
        )

    finally:
        await delete_metagen_state_for_urn(async_session, _TEST_URN)
        with suppress(Exception):
            await api_client.delete(boundary_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_candidate_approve_demotes_prior_approved_sibling(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """Approving candidate B when candidate A is approved atomically demotes A to llm_approved.

    Covers the mutable approval contract and partial unique index
    UNIQUE (dataset_urn, item_id) WHERE status='approved'.

    spec: USE_CASE_en.md §UC4 — Review — "approving a new candidate atomically
      demotes the previously approved sibling"
    spec: BACKEND.md §Approval flow — partial unique index enforced; sibling demotion via
      flush + commit pattern in service
    spec: BACKEND.md §Approval flow — sibling demotion in a single transaction
    """
    item_id = "dataset.description"
    boundary_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/boundary"
    review_prefix = (
        f"/api/v1/spoke/common/data/{_ENCODED_URN}"
        f"/attr/metagen/item/{item_id}/candidate"
    )
    item_detail_url = (
        f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/item/{item_id}"
    )

    try:
        await api_client.put(
            boundary_url,
            headers=admin_headers,
            json={"is_enabled": True, "allowed": ["dataset.description"]},
        )

        # Seed candidate A (llm_approved)
        cid_a = await seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id=item_id,
            value="Candidate A value.",
            status="llm_approved",
        )
        # Seed candidate B (llm_approved)
        cid_b = await seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id=item_id,
            value="Candidate B value.",
            status="llm_approved",
        )

        # Approve A first
        resp_a = await api_client.post(
            f"{review_prefix}/{cid_a}/method/review",
            headers=admin_headers,
            json={"verdict": "approve", "reason": "approve A first"},
        )
        assert resp_a.status_code == 200, (
            f"Approve A failed: {resp_a.status_code} {resp_a.text}"
        )
        assert resp_a.json().get("status") == "approved", (
            f"candidate A must be approved; got {resp_a.json().get('status')!r}"
        )

        # Approve B — must atomically demote A back to llm_approved
        resp_b = await api_client.post(
            f"{review_prefix}/{cid_b}/method/review",
            headers=admin_headers,
            json={"verdict": "approve", "reason": "approve B to demote A"},
        )
        assert resp_b.status_code == 200, (
            f"Approve B failed: {resp_b.status_code} {resp_b.text}. "
            "spec: BACKEND.md §Approval flow — mutable approval must not raise unique"
            " constraint error"
        )
        assert resp_b.json().get("status") == "approved", (
            f"candidate B must be approved; got {resp_b.json().get('status')!r}. "
            "spec: USE_CASE_en.md §UC4 — Review"
        )

        # GET item detail — A must now be llm_approved again
        detail_resp = await api_client.get(item_detail_url, headers=admin_headers)
        assert detail_resp.status_code == 200, (
            f"GET item detail failed: {detail_resp.status_code}"
        )
        candidates = {
            c["candidate_id"]: c["status"]
            for c in detail_resp.json().get("candidates", [])
        }
        assert candidates.get(cid_b) == "approved", (
            f"candidate B must be approved after demotion; got {candidates.get(cid_b)!r}. "
            "spec: USE_CASE_en.md §UC4 — Review"
        )
        assert candidates.get(cid_a) == "llm_approved", (
            f"candidate A must be demoted to llm_approved; got {candidates.get(cid_a)!r}. "
            "spec: BACKEND.md §Approval flow — partial unique index UNIQUE (dataset_urn,"
            " item_id) WHERE status='approved' — at most one approved candidate per item"
        )

    finally:
        await delete_metagen_state_for_urn(async_session, _TEST_URN)
        with suppress(Exception):
            await api_client.delete(boundary_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_candidate_reject_emits_event(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """Rejecting an llm_approved candidate flips status to 'rejected' and emits event.

    spec: USE_CASE_en.md §UC4 — Review — reject verdict -> status=rejected
    spec: BACKEND.md §Event Catalogue — METAGEN (dataset) CANDIDATE_APPROVE/REJECT —
      detail keys: item_id, candidate_id, reason
    """
    item_id = "dataset.description"
    unique_reason = f"spot test reject {uuid.uuid4()}"
    boundary_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/boundary"
    dataset_event_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/event/metagen"

    try:
        await api_client.put(
            boundary_url,
            headers=admin_headers,
            json={"is_enabled": True, "allowed": ["dataset.description"]},
        )

        cid = await seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id=item_id,
            value="Rejected candidate value.",
            status="llm_approved",
        )

        review_url = (
            f"/api/v1/spoke/common/data/{_ENCODED_URN}"
            f"/attr/metagen/item/{item_id}/candidate/{cid}/method/review"
        )
        review_resp = await api_client.post(
            review_url,
            headers=admin_headers,
            json={"verdict": "reject", "reason": unique_reason},
        )
        assert review_resp.status_code == 200, (
            f"POST review (reject) failed: {review_resp.status_code} {review_resp.text}. "
            "spec: USE_CASE_en.md §UC4 — Review"
        )
        review_body = review_resp.json()
        assert review_body.get("status") == "rejected", (
            f"candidate status after reject must be 'rejected'; "
            f"got {review_body.get('status')!r}. spec: USE_CASE_en.md §UC4 — Review"
        )

        # METAGEN.CANDIDATE_REJECT event emitted;
        # bind by candidate_id and unique reason to avoid stale event matches.
        ev_resp = await api_client.get(
            f"{dataset_event_url}?limit=20",
            headers=admin_headers,
        )
        assert ev_resp.status_code == 200
        events = ev_resp.json().get("events", [])
        reject_event = next(
            (
                e for e in events
                if e["event_type"] == "METAGEN.CANDIDATE_REJECT"
                and e["detail"].get("candidate_id") == cid
            ),
            None,
        )
        assert reject_event is not None, (
            f"METAGEN.CANDIDATE_REJECT event for candidate_id={cid!r} must be emitted. "
            "spec: BACKEND.md §Event Catalogue — METAGEN (dataset) CANDIDATE_APPROVE/REJECT"
        )
        ev_detail = reject_event["detail"]
        assert ev_detail["candidate_id"] == cid, (
            f"CANDIDATE_REJECT detail candidate_id must be {cid!r}; "
            f"got {ev_detail.get('candidate_id')!r}. "
            "spec: BACKEND.md §Event Catalogue — METAGEN (dataset) CANDIDATE_APPROVE/REJECT"
        )
        assert ev_detail["item_id"] == item_id, (
            f"CANDIDATE_REJECT detail item_id must be {item_id!r}; "
            f"got {ev_detail.get('item_id')!r}. "
            "spec: BACKEND.md §Event Catalogue — METAGEN (dataset) CANDIDATE_APPROVE/REJECT"
        )
        assert ev_detail["reason"] == unique_reason, (
            f"CANDIDATE_REJECT detail reason must be {unique_reason!r}; "
            f"got {ev_detail.get('reason')!r}. "
            "spec: BACKEND.md §Event Catalogue — METAGEN (dataset) CANDIDATE_APPROVE/REJECT"
        )

    finally:
        await delete_metagen_state_for_urn(async_session, _TEST_URN)
        with suppress(Exception):
            await api_client.delete(boundary_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_candidate_reject_approved_clears_datahub_description(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """Rejecting an APPROVED candidate flips it to rejected, emits CANDIDATE_REJECT,
    and removes the editable DataHub description it had written.

    Reject is valid on an approved candidate (no 409). The approve step writes
    EditableDatasetProperties.description to DataHub; rejecting that approved
    candidate runs the aspect-clear path so the editable description falls back to
    empty/None. We seed via raw-SQL (spot-appropriate), approve over REST to produce
    the editable aspect, then reject over REST and read the truth back: status via
    item-detail, the event via the metagen event feed, and the cleared editable
    description via a GMS aspect read-back (the same read-back the api-wired UC4 test
    uses). `make_datahub()` always returns the real client, so the editable aspect is
    written/cleared in DataHub even under stub mode.

    spec: API.md §Metadata Generation — reject valid on approved candidate; removes editable aspect
    spec: feature/BACKEND.md §Approval flow — rejecting an approved candidate flips it to
      rejected and removes the editable DataHub aspect (dataset.description →
      EditableDatasetProperties.description=""); emits METAGEN.CANDIDATE_REJECT
    """
    from datahub.ingestion.graph.client import DatahubClientConfig, DataHubGraph
    from datahub.metadata.schema_classes import EditableDatasetPropertiesClass

    from tests.integration.util.datahub import _gms_url, get_datahub_token

    item_id = "dataset.description"
    approved_value = "Imazon title_master dataset (spot reject-approved test)."
    unique_reason = f"spot reject approved {uuid.uuid4()}"
    boundary_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/boundary"
    dataset_event_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/event/metagen"
    review_url = (
        f"/api/v1/spoke/common/data/{_ENCODED_URN}"
        f"/attr/metagen/item/{item_id}/candidate"
    )
    graph: DataHubGraph | None = None

    try:
        dh_token = get_datahub_token()
        graph = DataHubGraph(DatahubClientConfig(server=_gms_url, token=dh_token))

        await api_client.put(
            boundary_url,
            headers=admin_headers,
            json={"is_enabled": True, "allowed": ["dataset.description"]},
        )

        # Seed an llm_approved candidate, then APPROVE it over REST so DataSpoke
        # writes EditableDatasetProperties.description to DataHub.
        cid = await seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id=item_id,
            value=approved_value,
            status="llm_approved",
        )
        approve_resp = await api_client.post(
            f"{review_url}/{cid}/method/review",
            headers=admin_headers,
            json={"verdict": "approve", "reason": "approve before reject"},
        )
        assert approve_resp.status_code == 200, (
            f"Approve before reject failed: {approve_resp.status_code} {approve_resp.text}"
        )
        assert approve_resp.json().get("status") == "approved", (
            f"candidate must be approved before the reject step; "
            f"got {approve_resp.json().get('status')!r}. spec: USE_CASE_en.md §UC4 — Review"
        )

        # GMS read-back: the editable description now holds the approved value.
        editable_after_approve = graph.get_aspect(
            entity_urn=_TEST_URN, aspect_type=EditableDatasetPropertiesClass
        )
        assert (
            editable_after_approve is not None
            and editable_after_approve.description == approved_value
        ), (
            "editableDatasetProperties.description must equal the approved value after "
            f"approve; got {getattr(editable_after_approve, 'description', None)!r}. "
            "spec: API.md §Metadata Generation — approve emits value to editable aspect"
        )

        # Now REJECT the approved candidate. Must succeed (no 409) and flip to rejected.
        reject_resp = await api_client.post(
            f"{review_url}/{cid}/method/review",
            headers=admin_headers,
            json={"verdict": "reject", "reason": unique_reason},
        )
        assert reject_resp.status_code == 200, (
            f"Reject on approved candidate must succeed (200); "
            f"got {reject_resp.status_code} {reject_resp.text}. "
            "spec: API.md §Metadata Generation — reject valid on approved candidate"
        )
        assert reject_resp.json().get("status") == "rejected", (
            f"candidate status after rejecting an approved candidate must be 'rejected'; "
            f"got {reject_resp.json().get('status')!r}. "
            "spec: feature/BACKEND.md §Approval flow — reject flips to rejected"
        )

        # REST read-back: item detail shows the candidate as rejected.
        detail_resp = await api_client.get(
            f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/item/{item_id}",
            headers=admin_headers,
        )
        assert detail_resp.status_code == 200, (
            f"GET item detail failed: {detail_resp.status_code} {detail_resp.text}"
        )
        target = next(
            (
                c
                for c in detail_resp.json().get("candidates", [])
                if c["candidate_id"] == cid
            ),
            None,
        )
        assert target is not None and target["status"] == "rejected", (
            f"item-detail read-back must show the candidate as 'rejected'; got "
            f"{target['status'] if target else 'missing'!r}. "
            "spec: feature/BACKEND.md §Approval flow"
        )

        # METAGEN.CANDIDATE_REJECT event recorded, bound by candidate_id + unique reason.
        ev_resp = await api_client.get(
            f"{dataset_event_url}?limit=20", headers=admin_headers
        )
        assert ev_resp.status_code == 200
        reject_event = next(
            (
                e
                for e in ev_resp.json().get("events", [])
                if e["event_type"] == "METAGEN.CANDIDATE_REJECT"
                and e["detail"].get("candidate_id") == cid
            ),
            None,
        )
        assert reject_event is not None, (
            f"METAGEN.CANDIDATE_REJECT event for candidate_id={cid!r} must be emitted. "
            "spec: feature/BACKEND.md §Event Catalogue — METAGEN (dataset) CANDIDATE_REJECT"
        )
        assert reject_event["detail"].get("reason") == unique_reason, (
            f"CANDIDATE_REJECT detail reason must be {unique_reason!r}; "
            f"got {reject_event['detail'].get('reason')!r}. "
            "spec: feature/BACKEND.md §Event Catalogue — METAGEN (dataset) CANDIDATE_REJECT"
        )

        # GMS read-back: the aspect-clear path ran — editable description is removed.
        editable_after_reject = graph.get_aspect(
            entity_urn=_TEST_URN, aspect_type=EditableDatasetPropertiesClass
        )
        cleared_desc = (
            editable_after_reject.description if editable_after_reject is not None else None
        )
        assert cleared_desc in (None, ""), (
            f"Rejecting an approved dataset.description candidate must remove the editable "
            f"DataHub description (expected ''/None); got {cleared_desc!r}. "
            "spec: feature/BACKEND.md §Approval flow — reject of approved removes editable aspect"
        )

    finally:
        await delete_metagen_state_for_urn(async_session, _TEST_URN)
        with suppress(Exception):
            await api_client.delete(boundary_url, headers=admin_headers)
        # Remove the editable override written during approve so the dataset falls
        # back to its seeded non-editable description.
        if graph is not None:
            with suppress(Exception):
                from datahub.emitter.mcp import MetadataChangeProposalWrapper

                graph.emit_mcp(
                    MetadataChangeProposalWrapper(
                        entityUrn=_TEST_URN,
                        aspect=EditableDatasetPropertiesClass(description=None),
                    )
                )


@pytest.mark.asyncio
async def test_metagen_item_status_pending_when_only_rejected_candidates(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """An item whose only candidates are rejected is reported with status='pending'.

    Item status is derived over NON-rejected candidates, so an item with rejected
    candidates only (no llm_approved/approved) is 'pending', not 'llm_approved'.

    spec: feature/BACKEND.md §Item status — derived over non-rejected candidates;
      'pending' when no non-rejected candidate exists.
    """
    item_id = "dataset.description"
    composite_id = f"{_TEST_URN}::{item_id}"
    encoded_composite = urllib.parse.quote(composite_id, safe="")
    item_detail_url = f"/api/v1/spoke/metagen/item/{encoded_composite}"

    try:
        await seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id=item_id,
            value="Rejected candidate 1.",
            status="rejected",
        )
        await seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id=item_id,
            value="Rejected candidate 2.",
            status="rejected",
        )

        resp = await api_client.get(item_detail_url, headers=admin_headers)
        assert resp.status_code == 200, f"GET item detail failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert body["status"] == "pending", (
            f"Item with only rejected candidates must be 'pending'; got {body['status']!r}. "
            "spec: feature/BACKEND.md §Item status — derived over non-rejected candidates"
        )
    finally:
        await delete_metagen_state_for_urn(async_session, _TEST_URN)


@pytest.mark.asyncio
async def test_metagen_candidate_approve_demotes_cross_conf_sibling(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """Approving a candidate from conf B demotes an approved sibling produced by conf A
    on the same item — the one-approved-per-item invariant holds across confs.

    spec: feature/BACKEND.md §Approval flow — approving a candidate atomically demotes
      the approved sibling from any other conf; UNIQUE (dataset_urn, item_id) WHERE
      status='approved' holds globally across confs.
    """
    item_id = "dataset.description"
    boundary_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/boundary"
    review_prefix = (
        f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/item/{item_id}/candidate"
    )
    item_detail_url = (
        f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/item/{item_id}"
    )

    conf_a = None
    conf_b = None
    try:
        await api_client.put(
            boundary_url,
            headers=admin_headers,
            json={"is_enabled": True, "allowed": ["dataset.description"]},
        )

        conf_a = await seed_metagen_conf(
            async_session, name=f"conf-a-{uuid.uuid4().hex[:8]}", is_enabled=True
        )
        conf_b = await seed_metagen_conf(
            async_session, name=f"conf-b-{uuid.uuid4().hex[:8]}", is_enabled=True
        )

        # Candidate A from conf A, already approved (seeded directly).
        cid_a = await seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id=item_id,
            value="Conf A approved value.",
            status="approved",
            conf_id=conf_a,
        )
        # Candidate B from conf B, awaiting review.
        cid_b = await seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id=item_id,
            value="Conf B value.",
            status="llm_approved",
            conf_id=conf_b,
        )

        # Approve B — must atomically demote A (a different conf's approved sibling).
        resp_b = await api_client.post(
            f"{review_prefix}/{cid_b}/method/review",
            headers=admin_headers,
            json={"verdict": "approve", "reason": "promote conf B over conf A"},
        )
        assert resp_b.status_code == 200, (
            f"Cross-conf approve must succeed; got {resp_b.status_code} {resp_b.text}. "
            "spec: feature/BACKEND.md §Approval flow — cross-conf demotion"
        )
        assert resp_b.json()["status"] == "approved"

        detail = await api_client.get(item_detail_url, headers=admin_headers)
        statuses = {c["candidate_id"]: c["status"] for c in detail.json()["candidates"]}
        assert statuses[cid_b] == "approved", "Conf B's candidate must be approved"
        assert statuses[cid_a] == "llm_approved", (
            "Conf A's previously-approved candidate must be demoted to llm_approved. "
            "spec: feature/BACKEND.md §Approval flow — one-approved-per-item across confs"
        )
    finally:
        await delete_metagen_state_for_urn(async_session, _TEST_URN)
        for cid in (conf_a, conf_b):
            if cid is not None:
                await delete_metagen_conf(async_session, cid)
        with suppress(Exception):
            await api_client.delete(boundary_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_candidate_review_without_enabled_boundary_returns_422_METAGEN_DATASET_NOT_IN_BOUNDARY(  # noqa: E501
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """Review attempt with no active boundary returns 422 METAGEN_DATASET_NOT_IN_BOUNDARY.

    spec: BACKEND.md §Approval flow (Boundary guard) — candidate review against a
      dataset whose metagen_boundary is absent or is_enabled=false returns 422
    spec: BACKEND.md §Approval flow — METAGEN_DATASET_NOT_IN_BOUNDARY maps to HTTP 422
    spec: BACKEND.md §Approval flow (Boundary guard) — raised when the boundary is
      absent or disabled
    """
    item_id = "dataset.description"
    boundary_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/boundary"

    try:
        # Ensure no boundary exists for _TEST_URN
        with suppress(Exception):
            await api_client.delete(boundary_url, headers=admin_headers)

        cid = await seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id=item_id,
            value="Candidate without boundary.",
            status="llm_approved",
        )

        review_url = (
            f"/api/v1/spoke/common/data/{_ENCODED_URN}"
            f"/attr/metagen/item/{item_id}/candidate/{cid}/method/review"
        )
        review_resp = await api_client.post(
            review_url,
            headers=admin_headers,
            json={"verdict": "approve", "reason": "should fail"},
        )
        assert review_resp.status_code == 422, (
            f"Review without boundary must return 422; "
            f"got {review_resp.status_code} {review_resp.text}. "
            "spec: BACKEND.md §Approval flow (Boundary guard) — review needs an enabled boundary"
        )
        assert "METAGEN_DATASET_NOT_IN_BOUNDARY" in str(review_resp.json()), (
            f"422 response must carry METAGEN_DATASET_NOT_IN_BOUNDARY code; "
            f"got {review_resp.json()!r}. "
            "spec: BACKEND.md §Approval flow (Boundary guard) — 422 METAGEN_DATASET_NOT_IN_BOUNDARY"
        )

    finally:
        await delete_metagen_state_for_urn(async_session, _TEST_URN)
