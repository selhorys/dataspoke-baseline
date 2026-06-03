"""Ingestion domain models and recipe helpers.

Shared between the API layer (src/api/) and the backend layer (src/backend/)
so neither imports from the other for ingestion primitives.
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# ── Platform enum ─────────────────────────────────────────────────────────────


class Platform(str, Enum):
    POSTGRESQL = "postgres"
    MYSQL = "mysql"
    ORACLE = "oracle"
    BIGQUERY = "bigquery"
    SNOWFLAKE = "snowflake"
    KAFKA = "kafka"


# ── Mode enum ─────────────────────────────────────────────────────────────────


class Mode(str, Enum):
    DATAHUB_MANAGED = "DATAHUB_MANAGED"
    ACTIVE_CUSTOM_MANAGED = "ACTIVE_CUSTOM_MANAGED"
    PASSIVE = "PASSIVE"


# ── Canonical schedule crons ──────────────────────────────────────────────────
#
# The three tiers used by ACTIVE_CUSTOM_MANAGED sources. The mapping is
# intentionally permissive: multiple equivalent cron forms that express the
# same cadence are accepted for "hourly" and "daily" (e.g. '0 * * * *' and
# '@hourly' are the same logical tier). Any schedule not listed here is an
# error for ACTIVE_CUSTOM_MANAGED; None means manual-only.

_CRON_TO_TIER: dict[str, str] = {
    # hourly
    "0 * * * *": "hourly",
    "@hourly": "hourly",
    # daily (midnight)
    "0 0 * * *": "daily",
    "@daily": "daily",
    "@midnight": "daily",
    # weekly (Sunday midnight)
    "0 0 * * 0": "weekly",
    "@weekly": "weekly",
}

# Expose the canonical crons as a public constant for tests and utilities.
CRON_TO_TIER: dict[str, str] = dict(_CRON_TO_TIER)


# ── Recipe helpers ────────────────────────────────────────────────────────────


def parse_recipe(recipe: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Validate and parse a DataHub-format recipe dict.

    Expects ``{source: {type: <str>, config: <dict>}}``.

    Returns:
        (source_type, config_dict) on success.

    Raises:
        ValueError: if the shape is wrong or source.type is missing/empty.
    """
    if not isinstance(recipe, dict):
        raise ValueError("recipe must be a dict")
    source = recipe.get("source")
    if not isinstance(source, dict):
        raise ValueError("recipe.source must be a dict")
    source_type = source.get("type")
    if not source_type or not isinstance(source_type, str):
        raise ValueError("recipe.source.type must be a non-empty string")
    config = source.get("config")
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("recipe.source.config must be a dict when present")
    return source_type, config


# Secret reference pattern: ${name__key} where
#   name  — DNS-label-safe token: lowercase alphanumerics and hyphens (e.g. dummy-data-pg)
#   key   — Secret data-key: alphanumerics, hyphens, underscores, dots (e.g. password)
# Example: ${dummy-data-pg__password} → name=dummy-data-pg, key=password
_SECRET_REF_RE = re.compile(r"\$\{([a-z0-9-]+)__([A-Za-z0-9_.-]+)\}")


def extract_secret_refs(recipe: dict[str, Any]) -> set[str]:
    """Return the set of ``name__key`` tokens found inside ``${...}`` placeholders.

    Scans all string values in ``recipe.source.config`` recursively (including
    nested dicts and lists). Returns the reconstructed ``name__key`` token for
    every matched reference.

    Returns an empty set when the recipe has no source.config or no
    ``${...}`` placeholders.
    """
    try:
        _, config = parse_recipe(recipe)
    except ValueError:
        return set()

    refs: set[str] = set()
    _collect_refs(config, refs)
    return refs


def _collect_refs(obj: Any, refs: set[str]) -> None:
    """Recursively collect ``name__key`` tokens from all string values."""
    if isinstance(obj, str):
        for match in _SECRET_REF_RE.finditer(obj):
            refs.add(f"{match.group(1)}__{match.group(2)}")
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_refs(v, refs)
    elif isinstance(obj, list):
        for item in obj:
            _collect_refs(item, refs)


def cron_to_tier(schedule: str | None) -> str | None:
    """Map a cron expression to one of ``'hourly'``, ``'daily'``, ``'weekly'``, or ``None``.

    ``None`` input means manual-only — returns ``None``.

    Raises:
        ValueError: if the schedule is a non-None string not recognised as one
            of the three canonical tiers. Callers must validate before persisting
            an ACTIVE_CUSTOM_MANAGED source.
    """
    if schedule is None:
        return None
    tier = _CRON_TO_TIER.get(schedule.strip())
    if tier is None:
        raise ValueError(
            f"schedule {schedule!r} does not map to one of the three allowed tiers "
            f"(hourly / daily / weekly). Use one of: {sorted(_CRON_TO_TIER)}"
        )
    return tier


def build_matcher(recipe: dict[str, Any]) -> "Callable[[str], bool]":  # noqa: F821
    """Build a dataset-name predicate from the recipe's allow/deny patterns.

    Uses ``datahub.configuration.common.AllowDenyPattern`` from the acryl-datahub
    SDK to evaluate whether a given dataset name is covered by this source.

    The dataset name passed to the returned callable must be the
    platform-specific name as it appears in the DataHub dataset URN:
      - postgres / mysql / oracle: ``database.schema.table`` (three dot-separated parts)
      - kafka: ``topic`` (bare, no instance) or ``platform_instance.topic``
      - bigquery / snowflake: the full dataset identifier

    Pattern semantics per DataHub's SQL source config:
      - ``schema_pattern`` is matched against the **schema segment only**
        (e.g. ``^catalog$`` applied to the schema part of ``example_db.catalog.orders``).
      - ``table_pattern`` is matched against the **full** ``database.schema.table`` string.
      - ``topic_patterns`` (kafka) allow and deny are each evaluated against the topic
        name. Because a kafka dataset URN name may be ``<topic>`` (no platform instance)
        or ``<platform_instance>.<topic>`` (instance set), both candidate forms — the full
        name and, when a dot is present, the substring after the first dot — are tested.
        A name is covered when the allow patterns match at least one form AND the deny
        patterns do not match any form. Allow and deny are checked independently so deny
        cannot be bypassed when allow matches a different candidate form.
      - ``dataset_pattern`` (BigQuery / Snowflake) is matched against the full name.

    When both ``schema_pattern`` and ``table_pattern`` are present, a dataset
    must pass both predicates to be included.

    If none of the above keys are found in ``source.config``, the matcher
    returns ``False`` for every name (match-nothing). A source with no
    derivable selection patterns maps no datasets — coverage that cannot be
    inferred must not be assumed.

    Note: This is an explicit approximation. DataHub exposes no native
    source→dataset reverse lookup; the matcher reconstructs coverage from
    the declared filter syntax only.
    """
    from collections.abc import Callable  # local import to avoid circular dep

    try:
        _, config = parse_recipe(recipe)
    except ValueError:
        # Malformed recipe — match nothing.
        return lambda name: False

    try:
        from datahub.configuration.common import AllowDenyPattern

        def _make_adp(allow: list[str], deny: list[str]) -> "Callable[[str], bool]":
            adp = AllowDenyPattern(allow=allow, deny=deny)
            return lambda name: adp.allowed(name)

        def _schema_segment(name: str) -> str:
            """Extract the schema part from a ``database.schema.table`` name.

            Splits on '.' and returns the second segment (index 1) when at least
            two segments are present, otherwise returns the whole name.
            """
            parts = name.split(".")
            return parts[1] if len(parts) >= 3 else name

        # Schema-level pattern (postgres / mysql / oracle family).
        # schema_pattern is applied to the schema segment only.
        if "schema_pattern" in config:
            sp = config["schema_pattern"]
            schema_adp = _make_adp(sp.get("allow", [".*"]), sp.get("deny", []))
            schema_pred: "Callable[[str], bool]" = (
                lambda name, _a=schema_adp: _a(_schema_segment(name))
            )
            # table_pattern (when present) applies to the full database.schema.table string.
            if "table_pattern" in config:
                tp = config["table_pattern"]
                table_adp = _make_adp(tp.get("allow", [".*"]), tp.get("deny", []))
                return lambda name, _s=schema_pred, _t=table_adp: _s(name) and _t(name)
            return schema_pred

        # Table-only pattern — matched against the full database.schema.table string.
        if "table_pattern" in config:
            tp = config["table_pattern"]
            return _make_adp(tp.get("allow", [".*"]), tp.get("deny", []))

        # Kafka topic patterns — allow and deny evaluated against the topic name.
        # A kafka dataset URN name may be bare (<topic>) or instance-prefixed
        # (<platform_instance>.<topic>).  Candidate forms are the full name and,
        # when a dot is present, the substring after the first dot (bare topic).
        # covered(name) = ALLOW_matches_any_form AND NOT DENY_matches_any_form.
        # deny is checked independently so it cannot be bypassed by the allow
        # branch matching a different form of the same name.
        if "topic_patterns" in config:
            tp = config["topic_patterns"]
            allow_pats: list[str] = tp.get("allow", [".*"])
            deny_pats: list[str] = tp.get("deny", [])
            allow_adp = AllowDenyPattern(allow=allow_pats, deny=[])
            deny_adp: Any = (
                AllowDenyPattern(allow=deny_pats, deny=[]) if deny_pats else None
            )

            def _kafka_pred(
                name: str,
                _a: Any = allow_adp,
                _d: Any = deny_adp,
            ) -> bool:
                _head, sep, rest = name.partition(".")
                forms = [name] + ([rest] if sep else [])
                if not any(_a.allowed(f) for f in forms):
                    return False
                if _d is not None and any(_d.allowed(f) for f in forms):
                    return False
                return True

            return _kafka_pred

        # BigQuery / Snowflake dataset pattern — matched against the full name.
        if "dataset_pattern" in config:
            dp = config["dataset_pattern"]
            return _make_adp(dp.get("allow", [".*"]), dp.get("deny", []))

    except ImportError:
        # acryl-datahub not available in this context — no SDK means no
        # evaluation is possible; fall through to match-nothing.
        pass
    except Exception as exc:
        # Pattern construction failed — log so a malformed pattern is observable,
        # then fall through to match-nothing (no coverage can be inferred).
        logger.warning("build_matcher: pattern construction failed: %s", exc)

    # No derivable selection patterns → no inferable coverage → matches nothing.
    return lambda name: False
