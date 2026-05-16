"""Spot tests for Metadata Generation — item list endpoints and candidate review.

Concerns covered (8 test functions across 2 groups):

Item endpoints (Group 4, raw-SQL seeded):
  test_metagen_items_list_global_paginated_envelope
  test_metagen_items_list_filters_dataset_kind_status
  test_metagen_item_detail_by_composite_id

Candidate review (Group 5, raw-SQL seeded):
  test_metagen_candidate_approve_flips_status_and_emits_event
  test_metagen_candidate_approve_demotes_prior_approved_sibling
  test_metagen_candidate_reject_emits_event
  test_metagen_candidate_reject_approved_returns_409_METAGEN_CANNOT_REJECT_APPROVED
  test_metagen_candidate_review_without_enabled_boundary_returns_422_METAGEN_DATASET_NOT_IN_BOUNDARY

All tests use raw-SQL seeding via tests.integration.util.metagen helpers because
the concern under test is the review/query behavior, not the run pipeline that
would normally produce candidate rows.

Per-item budget rules (result_limit, overwrite_pending FIFO eviction) are
covered at the unit level in tests/unit/backend/metagen/test_service.py
(_apply_per_item_budget); integration coverage would be redundant.

spec: USE_CASE_en.md §UC4 (L552-776)
spec: BACKEND.md §UC4 Metadata Generation — mutable approval, partial unique index on approved
spec: TESTING.md §Spot vs Api-Wired Integration Tests
"""

import urllib.parse
import uuid
from contextlib import suppress

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.util.metagen import (
    delete_metagen_state_for_urn,
    seed_metagen_candidate,
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
    spec: src/api/schemas/metagen.py L103-104 — MetagenItemListResponse
    """
    item_list_url = "/api/v1/spoke/common/metagen/item"

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
                "item missing composite_id. spec: USE_CASE_en.md §UC4 API Mapping L684"
            )
            assert item["composite_id"] == f"{item['dataset_urn']}::{item['item_id']}", (
                f"composite_id format mismatch: {item['composite_id']!r}. "
                "spec: USE_CASE_en.md §UC4 L684"
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

    spec: USE_CASE_en.md §UC4 L683 — item list filterable by dataset_urn, kind, status
    spec: src/api/routers/spoke/common/metagen.py L184-207 — filter params
    """
    item_list_url = "/api/v1/spoke/common/metagen/item"

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
            "spec: src/api/routers/spoke/common/metagen.py L195"
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
            "spec: src/api/routers/spoke/common/metagen.py L196"
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
            "spec: src/api/routers/spoke/common/metagen.py L197"
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

    spec: USE_CASE_en.md §UC4 L684 — composite_id = '{dataset_urn}::{item_id}'
    spec: src/api/routers/spoke/common/metagen.py L213-229 — composite_id parsing
    spec: src/api/schemas/metagen.py L120 — MetagenItemDetailResponse.candidates
    """
    item_id = "dataset.description"
    composite_id = f"{_TEST_URN}::{item_id}"
    encoded_composite = urllib.parse.quote(composite_id, safe="")
    item_detail_url = f"/api/v1/spoke/common/metagen/item/{encoded_composite}"

    try:
        cid1 = await seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id=item_id,
            value="First candidate value.",
            status="llm_approved",
            confidence=0.91,
        )
        cid2 = await seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id=item_id,
            value="Second candidate value.",
            status="llm_approved",
            confidence=0.80,
        )

        resp = await api_client.get(item_detail_url, headers=admin_headers)
        assert resp.status_code == 200, (
            f"GET item detail by composite_id failed: {resp.status_code} {resp.text}. "
            "spec: src/api/routers/spoke/common/metagen.py L213"
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
            "spec: USE_CASE_en.md §UC4 L684"
        )

        # Both seeded candidates must be present
        assert "candidates" in body and isinstance(body["candidates"], list), (
            "MetagenItemDetailResponse must have 'candidates' list. "
            "spec: src/api/schemas/metagen.py L120"
        )
        returned_ids = {c["candidate_id"] for c in body["candidates"]}
        assert cid1 in returned_ids, (
            f"Seeded candidate {cid1!r} not in detail response. "
            "spec: USE_CASE_en.md §UC4 L617-631"
        )
        assert cid2 in returned_ids, (
            f"Seeded candidate {cid2!r} not in detail response. "
            "spec: USE_CASE_en.md §UC4 L617-631"
        )

        # Each candidate has required fields
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

    finally:
        await delete_metagen_state_for_urn(async_session, _TEST_URN)


# ── Group 5: Candidate review (raw-SQL seeded) ────────────────────────────────


@pytest.mark.asyncio
async def test_metagen_candidate_approve_flips_status_and_emits_event(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """Approving an llm_approved candidate flips its status to 'approved'.

    Also emits METAGEN.CANDIDATE_APPROVE event on per-dataset event endpoint.

    spec: USE_CASE_en.md §UC4 L649-657 — approve verdict -> status=approved
    spec: BACKEND.md §766-767 — METAGEN.CANDIDATE_APPROVE detail keys:
      item_id, candidate_id, reason
    """
    item_id = "dataset.description"
    unique_reason = f"spot test approve {uuid.uuid4()}"
    boundary_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"
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
            "spec: USE_CASE_en.md §UC4 L649"
        )
        review_body = review_resp.json()
        assert review_body.get("status") == "approved", (
            f"candidate status after approve must be 'approved'; "
            f"got {review_body.get('status')!r}. spec: USE_CASE_en.md §UC4 L649"
        )
        assert review_body.get("candidate_id") == cid, (
            "candidate_id mismatch in review response. spec: BACKEND.md §766"
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
            "spec: BACKEND.md §766 event catalogue"
        )
        ev_detail = approve_event["detail"]
        assert ev_detail["candidate_id"] == cid, (
            f"CANDIDATE_APPROVE detail candidate_id must be {cid!r}; "
            f"got {ev_detail.get('candidate_id')!r}. spec: BACKEND.md §766"
        )
        assert ev_detail["item_id"] == item_id, (
            f"CANDIDATE_APPROVE detail item_id must be {item_id!r}; "
            f"got {ev_detail.get('item_id')!r}. spec: BACKEND.md §766"
        )
        assert ev_detail["reason"] == unique_reason, (
            f"CANDIDATE_APPROVE detail reason must be {unique_reason!r}; "
            f"got {ev_detail.get('reason')!r}. spec: BACKEND.md §766"
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

    spec: USE_CASE_en.md §UC4 L649-657 — "approving a new candidate atomically
      demotes the previously approved sibling"
    spec: BACKEND.md §UC4 — partial unique index enforced; sibling demotion via
      flush + commit pattern in service
    spec: src/backend/metagen/service.py L742-764 — flush demotion before commit
    """
    item_id = "dataset.description"
    boundary_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"
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
            "spec: BACKEND.md §UC4 — mutable approval must not raise unique constraint error"
        )
        assert resp_b.json().get("status") == "approved", (
            f"candidate B must be approved; got {resp_b.json().get('status')!r}. "
            "spec: USE_CASE_en.md §UC4 L649"
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
            "spec: USE_CASE_en.md §UC4 L649"
        )
        assert candidates.get(cid_a) == "llm_approved", (
            f"candidate A must be demoted to llm_approved; got {candidates.get(cid_a)!r}. "
            "spec: BACKEND.md §UC4 — partial unique index UNIQUE (dataset_urn, item_id)"
            " WHERE status='approved' — at most one approved candidate per item"
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

    spec: USE_CASE_en.md §UC4 L649-657 — reject verdict -> status=rejected
    spec: BACKEND.md §766-767 — METAGEN.CANDIDATE_REJECT detail keys:
      item_id, candidate_id, reason
    """
    item_id = "dataset.description"
    unique_reason = f"spot test reject {uuid.uuid4()}"
    boundary_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"
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
            "spec: USE_CASE_en.md §UC4 L649"
        )
        review_body = review_resp.json()
        assert review_body.get("status") == "rejected", (
            f"candidate status after reject must be 'rejected'; "
            f"got {review_body.get('status')!r}. spec: USE_CASE_en.md §UC4 L649"
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
            "spec: BACKEND.md §767 event catalogue"
        )
        ev_detail = reject_event["detail"]
        assert ev_detail["candidate_id"] == cid, (
            f"CANDIDATE_REJECT detail candidate_id must be {cid!r}; "
            f"got {ev_detail.get('candidate_id')!r}. spec: BACKEND.md §767"
        )
        assert ev_detail["item_id"] == item_id, (
            f"CANDIDATE_REJECT detail item_id must be {item_id!r}; "
            f"got {ev_detail.get('item_id')!r}. spec: BACKEND.md §767"
        )
        assert ev_detail["reason"] == unique_reason, (
            f"CANDIDATE_REJECT detail reason must be {unique_reason!r}; "
            f"got {ev_detail.get('reason')!r}. spec: BACKEND.md §767"
        )

    finally:
        await delete_metagen_state_for_urn(async_session, _TEST_URN)
        with suppress(Exception):
            await api_client.delete(boundary_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_candidate_reject_approved_returns_409_METAGEN_CANNOT_REJECT_APPROVED(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """Rejecting an already-approved candidate returns 409 METAGEN_CANNOT_REJECT_APPROVED.

    spec: USE_CASE_en.md §UC4 L655-656 — rejecting an approved candidate is refused
      with 409 METAGEN_CANNOT_REJECT_APPROVED
    spec: BACKEND.md L531-532 — reject is only valid for llm_approved candidates;
      approved returns 409 METAGEN_CANNOT_REJECT_APPROVED
    spec: BACKEND.md L949 — ConflictError error-code table: METAGEN_CANNOT_REJECT_APPROVED
    spec: src/backend/metagen/service.py L788-792 — ConflictError raised when
      cand.status == 'approved' and verdict == 'reject'
    """
    item_id = "dataset.description"
    boundary_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"

    try:
        await api_client.put(
            boundary_url,
            headers=admin_headers,
            json={"is_enabled": True, "allowed": ["dataset.description"]},
        )

        # Seed a candidate directly with status='approved' (bypasses approve flow)
        cid = await seed_metagen_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id=item_id,
            value="Already approved candidate.",
            status="approved",
        )

        review_url = (
            f"/api/v1/spoke/common/data/{_ENCODED_URN}"
            f"/attr/metagen/item/{item_id}/candidate/{cid}/method/review"
        )
        reject_resp = await api_client.post(
            review_url,
            headers=admin_headers,
            json={"verdict": "reject", "reason": "attempt to reject approved"},
        )
        assert reject_resp.status_code == 409, (
            f"Reject on approved candidate must return 409; "
            f"got {reject_resp.status_code} {reject_resp.text}. "
            "spec: src/backend/metagen/service.py L788-792"
        )
        assert "METAGEN_CANNOT_REJECT_APPROVED" in str(reject_resp.json()), (
            f"409 response must carry METAGEN_CANNOT_REJECT_APPROVED code; "
            f"got {reject_resp.json()!r}. "
            "spec: BACKEND.md L949 — ConflictError table; "
            "spec: BACKEND.md L531-532 — reject of approved returns 409"
        )

    finally:
        await delete_metagen_state_for_urn(async_session, _TEST_URN)
        with suppress(Exception):
            await api_client.delete(boundary_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_candidate_review_without_enabled_boundary_returns_422_METAGEN_DATASET_NOT_IN_BOUNDARY(  # noqa: E501
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """Review attempt with no active boundary returns 422 METAGEN_DATASET_NOT_IN_BOUNDARY.

    spec: BACKEND.md L547-549 — boundary guard: candidate review against a dataset whose
      metagen_boundary is absent or is_enabled=false returns 422 METAGEN_DATASET_NOT_IN_BOUNDARY
    spec: BACKEND.md L953 — PreconditionFailedError maps to HTTP 422
    spec: src/backend/metagen/service.py L712-720 — PreconditionFailedError raised
      when boundary is None or boundary.is_enabled=false
    """
    item_id = "dataset.description"
    boundary_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"

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
            "spec: src/backend/metagen/service.py L712-720"
        )
        assert "METAGEN_DATASET_NOT_IN_BOUNDARY" in str(review_resp.json()), (
            f"422 response must carry METAGEN_DATASET_NOT_IN_BOUNDARY code; "
            f"got {review_resp.json()!r}. "
            "spec: BACKEND.md L547-549 — boundary guard; "
            "spec: BACKEND.md L953 — PreconditionFailedError → 422"
        )

    finally:
        await delete_metagen_state_for_urn(async_session, _TEST_URN)
