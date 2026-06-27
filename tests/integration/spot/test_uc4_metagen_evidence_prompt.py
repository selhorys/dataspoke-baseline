"""Spot test: evidence reaches the metagen Producer prompt.

Concern: after the evidence-parity fix, the Producer prompt must contain
both (a) the document body seeded in DataHub and (b) the seeded ontology
node name, proving that related_documents and ontology_rag are wired through
the evidence pipeline all the way to the run() → build_run_prompt call.

This test drives MetagenService.run() (the full entry point) with:
- A real DataHub document seeded against _TEST_URN
- A real approved OntogenNode + node_embedding row in PostgreSQL
- MetagenConfig (is_enabled=True, dataset_urns=[_TEST_URN]) and
  MetagenBoundary (is_enabled=True, allowed=["dataset.description"])
  inserted directly via the test session
- run_debate patched to capture the producer_prompt argument and return
  a minimal DebateResult(outcome="turns_exhausted") so no candidates are
  persisted and the run exits cleanly without side effects
- The stub LLM embed() returning the same fixed vector as the seeded
  node_embedding row (guaranteeing cosine similarity = 1.0 → node appears
  in RAG top-k)

The test asserts on the captured producer_prompt string — proving the full
chain run() → _fetch_evidence → build_run_prompt → run_debate is wired.

Per feedback_spot_vs_api_wired_principle: this concern (prompt content)
requires intercepting the LLM call — api-wired cannot do that without
breaking the contract. Spot is the right home.

Per feedback_spot_is_stub_only: the LLM embed call is stubbed (fixed vector);
no real LLM inference runs.

spec: spec/feature/BACKEND.md §Metadata Generation Service (Generation Pipeline, step 3 — evidence sources incl. related documents + per-dataset ontology RAG)
spec: spec/TESTING.md §Spot vs Api-Wired Integration Tests
"""

import asyncio
import os
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.backend.metagen.debate_models import DebateResult
from src.backend.metagen.service import MetagenService
from src.shared.config import EMBEDDING_DIMENSION
from src.shared.vector.client import PgVectorManager
from tests.integration.util.datahub import (
    get_datahub_token,
    hard_delete_document,
    seed_native_document,
)

# ── Module-level dummy-data declarations ──────────────────────────────────────
# Catalog schema must exist in DataHub for the test URN to resolve.
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

# Test dataset URN: catalog.title_master (Imazon reference dataset).
# spec: TESTING.md §Imazon Dummy-Data Reference
_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"

# Fixed embedding vector for seeded rows and stub embed response.
# Using [1.0, 0.0, …] so cosine similarity between query and stored
# embedding is 1.0 (maximum) — guarantees the node appears in the RAG results.
_FIXED_VEC: list[float] = [1.0] + [0.0] * (EMBEDDING_DIMENSION - 1)

# Polling timeout for DataHub search index consistency.
_INDEX_TIMEOUT_SECONDS = 15


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _dsn() -> str:
    host = os.environ["DATASPOKE_TEST_POSTGRES_HOST"]
    port = os.environ["DATASPOKE_TEST_POSTGRES_PORT"]
    user = os.environ["DATASPOKE_TEST_POSTGRES_USER"]
    password = os.environ["DATASPOKE_TEST_POSTGRES_PASSWORD"]
    db = os.environ.get("DATASPOKE_TEST_POSTGRES_DB", "dataspoke")
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"


@pytest_asyncio.fixture
async def test_vector() -> PgVectorManager:
    """PgVectorManager bound to the dev-env PostgreSQL for this test."""
    engine = create_async_engine(_dsn(), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield PgVectorManager(session_factory=factory)
    await engine.dispose()


# ── Raw SQL seeders ────────────────────────────────────────────────────────────


async def _seed_ontogen_node(
    session: AsyncSession,
    *,
    node_id: str,
    name: str,
    description: str,
) -> None:
    """Insert an approved ontogen_nodes row."""
    await session.execute(
        text(
            "INSERT INTO dataspoke.ontogen_nodes"
            " (id, name, description, confidence_score, status)"
            " VALUES (:id, :name, :desc, 0.9, 'approved')"
            " ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, status=EXCLUDED.status"
        ),
        {
            "id": node_id,
            "name": name,
            "desc": description,
        },
    )
    await session.commit()


async def _seed_node_embedding(
    vector: PgVectorManager,
    *,
    node_id: str,
    name: str,
    embedding: list[float],
) -> None:
    """Insert an approved node_embeddings row for the given node."""
    vec_literal = "[" + ",".join(str(v) for v in embedding) + "]"
    async with vector._session_factory() as s:
        async with s.begin():
            await s.execute(
                text(
                    "INSERT INTO dataspoke.node_embeddings"
                    " (node_id, embedding, name, status, updated_at)"
                    " VALUES (:node_id, CAST(:emb AS vector), :name, 'approved',"
                    " CAST(:updated_at AS timestamptz))"
                    " ON CONFLICT (node_id) DO UPDATE SET"
                    " embedding=EXCLUDED.embedding, name=EXCLUDED.name,"
                    " status=EXCLUDED.status, updated_at=EXCLUDED.updated_at"
                ),
                {
                    "node_id": node_id,
                    "emb": vec_literal,
                    "name": name,
                    "updated_at": datetime.now(tz=UTC),
                },
            )


async def _seed_metagen_conf(session: AsyncSession, *, dataset_urn: str) -> str:
    """Insert a metagen_config (collection) row scoped to dataset_urn; return its UUID."""
    from tests.integration.util.metagen import seed_metagen_conf

    return await seed_metagen_conf(
        session,
        name=f"spot-evidence-{uuid.uuid4().hex[:8]}",
        is_enabled=True,
        dataset_filter={"dataset_urns": [dataset_urn]},
    )


async def _seed_metagen_boundary(session: AsyncSession, *, dataset_urn: str) -> None:
    """Upsert a MetagenBoundary row with is_enabled=True for dataset_urn."""
    await session.execute(
        text(
            "INSERT INTO dataspoke.metagen_boundary"
            " (dataset_urn, is_enabled, allowed, updated_at)"
            " VALUES (:urn, TRUE, ARRAY['dataset.description', 'column.description'], :now)"
            " ON CONFLICT (dataset_urn) DO UPDATE SET"
            " is_enabled=TRUE, allowed=EXCLUDED.allowed, updated_at=EXCLUDED.updated_at"
        ),
        {
            "urn": dataset_urn,
            "now": datetime.now(tz=UTC),
        },
    )
    await session.commit()


async def _cleanup_node(session: AsyncSession, vector: PgVectorManager, *, node_id: str) -> None:
    with suppress(Exception):
        async with vector._session_factory() as s:
            async with s.begin():
                await s.execute(
                    text("DELETE FROM dataspoke.node_embeddings WHERE node_id = :id"),
                    {"id": node_id},
                )
    with suppress(Exception):
        await session.execute(
            text("DELETE FROM dataspoke.ontogen_nodes WHERE id = :id"), {"id": node_id}
        )
        await session.commit()


async def _cleanup_metagen_config_and_boundary(
    session: AsyncSession, *, dataset_urn: str
) -> None:
    with suppress(Exception):
        await session.execute(
            text("DELETE FROM dataspoke.metagen_boundary WHERE dataset_urn = :urn"),
            {"urn": dataset_urn},
        )
        # The conf collection is UUID-keyed; this spot test's confs are named
        # 'spot-evidence-*'. Drop them by name prefix to leave no orphan rows.
        await session.execute(
            text("DELETE FROM dataspoke.metagen_config WHERE name LIKE 'spot-evidence-%'")
        )
        await session.commit()


async def _poll_until_document_indexed(
    dataset_urn: str,
    datahub_client,
    *,
    timeout_seconds: float,
) -> None:
    """Poll fetch_related_documents until non-empty or timeout.

    DataHub's GraphQL search index is eventually consistent. This replaces
    a fixed sleep per feedback_no_increase_timeout: fail fast with a clear
    message rather than masking the problem with a blind wait.

    spec: spec/feature/BACKEND.md §Metadata Generation Service (Generation Pipeline, step 3 — evidence sources incl. related documents + per-dataset ontology RAG)
    """
    from src.shared.datahub.documents import fetch_related_documents

    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while True:
        docs = await fetch_related_documents(dataset_urn, datahub_client)
        if docs:
            return
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            pytest.fail(
                f"Document never indexed after {timeout_seconds}s — "
                "DataHub search consistency issue"
            )
        await asyncio.sleep(1)


# ── Test ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_uc4_evidence_reaches_producer_prompt(
    async_session: AsyncSession,
    datahub_client,
    test_vector: PgVectorManager,
) -> None:
    """Evidence parity: document title/body and ontology RAG node name appear in the prompt.

    Drives MetagenService.run() end-to-end with a patched run_debate that
    captures the producer_prompt argument. Asserts the full wiring chain:
    run() → _enumerate_in_scope_datasets → _fetch_evidence → build_run_prompt
    → run_debate (captured).

    Setup:
    1. Seed a DataHub Document with a known title and distinctive body,
       linked to _TEST_URN.
    2. Seed an approved OntogenNode row + its node_embedding row (using the
       fixed vector so cosine similarity is 1.0 against the stub embed).
    3. Seed MetagenConfig (is_enabled=True, dataset_urns=[_TEST_URN]) and
       MetagenBoundary (is_enabled=True, allowed=["dataset.description"]).
    4. Build a stub LLM client whose embed() returns the same fixed vector.
    5. Patch run_debate to capture producer_prompt; return DebateResult with
       outcome="turns_exhausted" so no candidates are persisted.
    6. Call service.run(dataset_urns=[_TEST_URN]).

    Assertion: the captured producer_prompt contains:
    - The document title.
    - A distinctive substring from the document body.
    - The seeded ontology node name (proving ontology_rag reached the prompt).

    spec: feature/BACKEND.md §Metadata Generation Service (Generation Pipeline, step 3 — evidence reaches the Producer prompt)
    """
    suffix = uuid.uuid4().hex[:12]
    doc_id = f"spot-uc4-evidence-{suffix}"
    node_id = f"spot-uc4-node-{suffix}"
    doc_title = f"Fulfillment SOP {suffix}"
    doc_body = f"Pick-and-pack guide distinctive_{suffix}. Stage 1 routing."
    node_name = f"SpotOrderNode_{suffix}"
    node_description = "An order in the Imazon fulfillment domain"

    document_urn: str | None = None
    captured_prompt: list[str] = []
    result = None

    try:
        # ── Step 1: Seed DataHub document linked to _TEST_URN ─────────────────
        dh_token = get_datahub_token()
        document_urn = seed_native_document(
            document_id=doc_id,
            title=doc_title,
            body_markdown=doc_body,
            related_dataset_urns=[_TEST_URN],
            token=dh_token,
        )
        # Poll until the document is indexed (eventually consistent search).
        # Fails fast with a clear message rather than silently waiting.
        await _poll_until_document_indexed(
            _TEST_URN,
            datahub_client,
            timeout_seconds=_INDEX_TIMEOUT_SECONDS,
        )

        # ── Step 2: Seed approved OntogenNode row + embedding ─────────────────
        await _seed_ontogen_node(
            async_session,
            node_id=node_id,
            name=node_name,
            description=node_description,
        )
        await _seed_node_embedding(
            test_vector,
            node_id=node_id,
            name=node_name,
            embedding=_FIXED_VEC,
        )

        # ── Step 3: Seed metagen conf (collection) + MetagenBoundary ──────────
        conf_id = await _seed_metagen_conf(async_session, dataset_urn=_TEST_URN)
        await _seed_metagen_boundary(async_session, dataset_urn=_TEST_URN)

        # ── Step 4: Build stub LLM with fixed embed vector ────────────────────
        stub_llm = AsyncMock()
        stub_llm.embed = AsyncMock(return_value=_FIXED_VEC)

        # ── Step 5: Build service and patch run_debate to capture prompt ──────
        stub_cache = AsyncMock()
        stub_cache.set_nx = AsyncMock(return_value=True)   # lock acquired
        stub_cache.delete_if_value = AsyncMock()
        stub_cache.get = AsyncMock(return_value=None)

        svc = MetagenService(
            datahub=datahub_client,
            db=async_session,
            cache=stub_cache,
            llm=stub_llm,
            vector=test_vector,
        )

        def _fake_run_debate(**kwargs):
            captured_prompt.append(kwargs["producer_prompt"])
            return DebateResult(
                payload={},
                transcript={"producer_iterations": 1},
                outcome="turns_exhausted",
            )

        with patch(
            "src.backend.metagen.service.run_debate",
            new=AsyncMock(side_effect=_fake_run_debate),
        ):
            result = await svc.run(conf_id, dataset_urns=[_TEST_URN])

        # ── Assertions ────────────────────────────────────────────────────────
        # Guard: if _TEST_URN is unresolved, the per-URN loop is skipped and
        # captured_prompt stays empty — pin the real failure cause up front.
        assert _TEST_URN not in result.unresolved_urns, (
            "_TEST_URN was unresolved by DataHub — check that the Imazon dummy data "
            "(example_db.catalog.title_master) is loaded. Run "
            "`./helm-charts/bin/install.sh --profile dev --components api` or reset-seed via "
            "tests.integration.util --reset-seed before this test."
        )
        assert captured_prompt, (
            "run_debate must have been called at least once — run() did not reach the debate step. "
            "Check that MetagenConfig/MetagenBoundary rows were picked up."
        )
        prompt = captured_prompt[0]

        # (a) Document title appears in the prompt.
        assert doc_title in prompt, (
            f"Document title {doc_title!r} must appear in the Producer prompt. "
            "spec: feature/BACKEND.md §Metadata Generation Service (Generation Pipeline step 3) — related_documents wired to prompt"
        )

        # (b) Distinctive document body substring appears.
        assert f"distinctive_{suffix}" in prompt, (
            "Distinctive document body fragment must appear in the Producer prompt. "
            "spec: feature/BACKEND.md §Metadata Generation Service (Generation Pipeline step 3) — related_documents body forwarded to LLM"
        )

        # (c) Seeded ontology node name appears (RAG round-trip proof).
        assert node_name in prompt, (
            f"Ontology RAG node name {node_name!r} must appear in the Producer prompt. "
            "spec: feature/BACKEND.md §Metadata Generation Service (Generation Pipeline step 3) — per-dataset ontology RAG wired to prompt"
        )

    finally:
        # ── Cleanup ───────────────────────────────────────────────────────────
        with suppress(Exception):
            if document_urn:
                hard_delete_document(document_urn=document_urn, token=get_datahub_token())
        await _cleanup_node(async_session, test_vector, node_id=node_id)
        await _cleanup_metagen_config_and_boundary(async_session, dataset_urn=_TEST_URN)
        # Remove the METAGEN.RUN_COMPLETE / RUN_FAILED Event row written by svc.run().
        with suppress(Exception):
            run_id_str = result.run_id if result is not None else None  # type: ignore[possibly-undefined]
            if run_id_str:
                await async_session.execute(
                    text(
                        "DELETE FROM dataspoke.events "
                        "WHERE event_type IN ('METAGEN.RUN_COMPLETE', 'METAGEN.RUN_FAILED') "
                        "AND detail->>'run_id' = :run_id"
                    ),
                    {"run_id": run_id_str},
                )
                await async_session.commit()
