"""Unit tests for metrics bootstrap — seed_factory_defaults idempotency.

Spec sources:
  spec/USE_CASE_en.md §UC5 §Factory defaults:
    - On first start, seeds one metric of each built-in type.
    - Defaults: mode="active", is_enabled=false, schedule_tier="daily",
      dataset_filter="", type-appropriate metric_conf.
    - Seeds ship disabled so the governance lead opts in explicitly.
    - Bootstrap never overwrites an existing row.
  spec/feature/BACKEND.md §Metrics Service §Factory defaults:
    - "type-appropriate `metric_conf` (`{"time_window_sec": 172800}` for the first two,
      `{}` for `doc-health`)" — i.e. the seeded measurement window for
      ingestion-freshness and validation-score is 172800 seconds.

Bootstrap tests use a mock AsyncSession instead of a live DB. The mock is wired
to simulate SELECT (no existing row) and to capture db.add() calls so assertions
derive from the spec contract rather than internal implementation details.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.metrics.bootstrap import _FACTORY_DEFAULTS, seed_factory_defaults

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_empty_db():
    """Return a mock AsyncSession where SELECT always returns 'row not found'."""
    db = AsyncMock()
    db.add = MagicMock()  # AsyncSession.add is synchronous
    empty_result = MagicMock()
    empty_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=empty_result)
    return db


def _make_db_with_existing_row(existing_id: str):
    """Return a mock AsyncSession that finds an existing row keyed on metric_id param.

    The mock inspects the WHERE clause to determine which metric_id is being queried
    and returns the existing row only for the matching ID, so the guard fires
    regardless of the iteration order over _FACTORY_DEFAULTS.
    """

    db = AsyncMock()
    db.add = MagicMock()  # AsyncSession.add is synchronous

    existing_row = MagicMock()
    existing_row.id = existing_id

    found_result = MagicMock()
    found_result.scalar_one_or_none.return_value = existing_row

    empty_result = MagicMock()
    empty_result.scalar_one_or_none.return_value = None

    def _side_effect(stmt):
        # Compile the statement to extract bound parameters and find the queried metric_id.
        try:
            compiled = stmt.compile(compile_kwargs={"literal_binds": False})
            queried_id = compiled.params.get("id_1") or compiled.params.get("id_2")
        except Exception:
            queried_id = None
        if queried_id == existing_id:
            return found_result
        return empty_result

    db.execute = AsyncMock(side_effect=_side_effect)
    return db, existing_row


# ── seed_factory_defaults on empty table ──────────────────────────────────────


@pytest.mark.asyncio
async def test_seed_factory_defaults_adds_three_rows_on_empty_db() -> None:
    """seed_factory_defaults calls db.add three times on empty metric_definitions.

    Spec: spec/USE_CASE_en.md §UC5 §Factory defaults — seeds one metric of each
          built-in type: ingestion-freshness, validation-score, doc-health.
    """
    db = _make_empty_db()

    await seed_factory_defaults(db)

    assert db.add.call_count == 3, (
        f"Expected db.add called 3 times (one per built-in type), "
        f"got {db.add.call_count}. "
        "Spec: spec/USE_CASE_en.md §UC5 §Factory defaults."
    )


@pytest.mark.asyncio
async def test_seed_factory_defaults_adds_correct_ids() -> None:
    """The three seeded rows have ids ingestion-freshness, validation-score, doc-health.

    Spec: spec/USE_CASE_en.md §UC5 §Factory defaults.
    """
    db = _make_empty_db()

    await seed_factory_defaults(db)

    added_ids = {call.args[0].id for call in db.add.call_args_list}
    assert added_ids == {"ingestion-freshness", "validation-score", "doc-health"}, (
        f"Expected exactly three built-in IDs; got {added_ids}. "
        "Spec: spec/USE_CASE_en.md §UC5 §Factory defaults."
    )


@pytest.mark.asyncio
async def test_seed_rows_are_disabled_by_default() -> None:
    """All seeded rows have is_enabled=False.

    Spec: spec/USE_CASE_en.md §UC5 §Factory defaults — seeds ship disabled.
    """
    db = _make_empty_db()
    await seed_factory_defaults(db)

    for add_call in db.add.call_args_list:
        row = add_call.args[0]
        assert row.is_enabled is False, (
            f"Seeded metric '{row.id}' must be is_enabled=False. "
            "Spec: spec/USE_CASE_en.md §UC5 §Factory defaults."
        )


@pytest.mark.asyncio
async def test_seed_rows_have_daily_schedule_tier() -> None:
    """All seeded rows have schedule_tier='daily'.

    Spec: spec/USE_CASE_en.md §UC5 §Factory defaults — schedule_tier='daily'.
    """
    db = _make_empty_db()
    await seed_factory_defaults(db)

    for add_call in db.add.call_args_list:
        row = add_call.args[0]
        assert row.schedule_tier == "daily", (
            f"Seeded metric '{row.id}' must have schedule_tier='daily'. "
            "Spec: spec/USE_CASE_en.md §UC5 §Factory defaults."
        )


@pytest.mark.asyncio
async def test_seed_rows_mode_is_active() -> None:
    """All seeded rows have mode='active'.

    Spec: spec/USE_CASE_en.md §UC5 §Factory defaults — mode='active'.
    """
    db = _make_empty_db()
    await seed_factory_defaults(db)

    for add_call in db.add.call_args_list:
        row = add_call.args[0]
        assert row.mode == "active", (
            f"Seeded metric '{row.id}' must have mode='active'. "
            "Spec: spec/USE_CASE_en.md §UC5 §Factory defaults."
        )


@pytest.mark.asyncio
async def test_seed_rows_metric_conf_matches_spec() -> None:
    """Seed row metric_conf matches spec defaults for each type.

    Spec: spec/feature/BACKEND.md §Metrics Service §Factory defaults — "type-appropriate
          ``metric_conf`` (``{"time_window_sec": 172800}`` for the first two, ``{}`` for
          ``doc-health``)".
    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — "``time_window_sec``
          for ``ingestion-freshness`` and ``validation-score`` — **the** measurement
          window (positive int seconds … factory default ``172800``)".
    """
    db = _make_empty_db()
    await seed_factory_defaults(db)

    added = {call.args[0].id: call.args[0] for call in db.add.call_args_list}

    assert added["ingestion-freshness"].metric_conf == {"time_window_sec": 172800}, (
        "ingestion-freshness factory default time_window_sec must be 172800 (2 days). "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )
    assert added["validation-score"].metric_conf == {"time_window_sec": 172800}, (
        "validation-score factory default time_window_sec must be 172800 (2 days). "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )
    assert added["doc-health"].metric_conf == {}


@pytest.mark.asyncio
async def test_seed_rows_dataset_filter_is_the_empty_clause() -> None:
    """All seeded rows ship with `dataset_filter=""` — every registered dataset.

    Spec: spec/feature/BACKEND.md §Metrics Service §Factory defaults — 'Defaults are
          […] `dataset_filter=""`';
    Spec: spec/API.md §`dataset_filter` grammar — "the empty string matches every
          registered dataset".
    """
    db = _make_empty_db()
    await seed_factory_defaults(db)

    assert db.add.call_args_list, "backstop: the bootstrap must have seeded rows"
    for add_call in db.add.call_args_list:
        row = add_call.args[0]
        assert row.dataset_filter == "", (
            f"Seeded metric '{row.id}' must have dataset_filter=''. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Factory defaults."
        )


@pytest.mark.asyncio
async def test_seed_rows_carry_one_series_descriptor_per_emitted_key() -> None:
    """Each seed names every emitted key of its type as a `{name, color, idx}` series.

    Spec: spec/feature/BACKEND.md §Metrics Service §Factory defaults — "a `metrics`
          descriptor per emitted key (each with a distinct color and an `idx` in
          emission order)";
    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — the emitted keys.
    """
    db = _make_empty_db()
    await seed_factory_defaults(db)

    added = {call.args[0].id: call.args[0] for call in db.add.call_args_list}

    expected_names = {
        "ingestion-freshness": ["total", "ingested_in_time"],
        "validation-score": ["total", "validation_score_sum"],
        "doc-health": ["total", "doc_health"],
    }
    for metric_id, names in expected_names.items():
        series = added[metric_id].metrics
        assert [s["name"] for s in series] == names, (
            f"{metric_id}: one descriptor per emitted key, in emission order; got {series!r}"
        )
        assert [s["idx"] for s in series] == list(range(1, len(names) + 1)), (
            f"{metric_id}: idx follows emission order. Spec: BACKEND.md §Factory defaults."
        )
        assert len({s["color"] for s in series}) == len(series), (
            f"{metric_id}: each series takes a distinct color. "
            "Spec: BACKEND.md §Factory defaults."
        )


@pytest.mark.asyncio
async def test_seed_series_colors_are_hex_triplets() -> None:
    """Every seeded color is a `#RRGGBB` string — the shape the schema accepts.

    A seed the schema layer would reject on the next PUT is a latent 422 on a row the
    operator never authored.

    Spec: spec/API.md §Metric — Definition body — "`color` is a `#RRGGBB` hex string".
    """
    import re

    db = _make_empty_db()
    await seed_factory_defaults(db)

    hex_re = re.compile(r"^#[0-9A-Fa-f]{6}$")
    seen = 0
    for add_call in db.add.call_args_list:
        for series in add_call.args[0].metrics:
            assert hex_re.match(series["color"]), (
                f"{series!r} carries a color the schema layer would reject. "
                "Spec: spec/API.md §Metric — Definition body."
            )
            seen += 1
    assert seen == 6, f"backstop: three seeds × two series each; inspected {seen}"


# ── seed_factory_defaults is idempotent ───────────────────────────────────────


@pytest.mark.asyncio
async def test_second_call_is_no_op() -> None:
    """Second call to seed_factory_defaults does not call db.add for existing rows.

    Spec: spec/USE_CASE_en.md §UC5 §Factory defaults — idempotent; only inserted
          when the metric_definitions row is absent.
    """
    db = AsyncMock()
    db.add = MagicMock()  # AsyncSession.add is synchronous

    # All three rows already exist
    existing = MagicMock()
    existing.id = "ingestion-freshness"
    found_result = MagicMock()
    found_result.scalar_one_or_none.return_value = existing
    db.execute = AsyncMock(return_value=found_result)

    await seed_factory_defaults(db)

    assert db.add.call_count == 0, (
        "Second call (all rows exist) must not add any rows. "
        "Spec: spec/USE_CASE_en.md §UC5 §Factory defaults."
    )
    # commit is still called once even if nothing was inserted
    assert db.commit.await_count >= 1


# ── Pre-existing row is not overwritten ───────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("pre_existing_id", [
    "ingestion-freshness",
    "validation-score",
    "doc-health",
])
async def test_pre_existing_row_not_overwritten(pre_existing_id: str) -> None:
    """Pre-seeded row with custom values is not overwritten, regardless of which ID is pre-existing.

    Iterates all three factory IDs to confirm order-independence of the bootstrap check.

    Spec: spec/USE_CASE_en.md §UC5 §Factory defaults — bootstrap never overwrites
          an existing row.
    """
    db, existing_row = _make_db_with_existing_row(pre_existing_id)
    existing_row.is_enabled = True  # customized

    await seed_factory_defaults(db)

    # The other two default metrics are still seeded
    assert db.add.call_count == 2, (
        f"Expected db.add called twice (for the two non-existing metrics when "
        f"'{pre_existing_id}' is pre-existing). "
        "Spec: spec/USE_CASE_en.md §UC5 §Factory defaults."
    )

    # The existing row was found but NOT passed to db.add again
    for add_call in db.add.call_args_list:
        row = add_call.args[0]
        assert row.id != pre_existing_id, (
            f"Pre-existing '{pre_existing_id}' row must not be re-added. "
            "Spec: spec/USE_CASE_en.md §UC5 §Factory defaults."
        )


# ── _FACTORY_DEFAULTS constant shape ─────────────────────────────────────────


def test_factory_defaults_constant_has_three_entries() -> None:
    """_FACTORY_DEFAULTS contains exactly three entries.

    Spec: spec/USE_CASE_en.md §UC5 §Factory defaults — one per built-in type.
    """
    assert len(_FACTORY_DEFAULTS) == 3


def test_factory_defaults_ids_are_correct() -> None:
    """_FACTORY_DEFAULTS contains the three built-in metric IDs.

    Spec: spec/USE_CASE_en.md §UC5 §Factory defaults.
    """
    ids = {d["id"] for d in _FACTORY_DEFAULTS}
    assert ids == {"ingestion-freshness", "validation-score", "doc-health"}
