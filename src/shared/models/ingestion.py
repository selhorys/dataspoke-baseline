"""Ingestion domain models and recipe helpers.

Shared between the API layer (src/api/) and the backend layer (src/backend/)
so neither imports from the other for ingestion primitives.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any

from src.shared.secrets.grammar import SECRET_REF_RE

logger = logging.getLogger(__name__)

# ── Platform enum ─────────────────────────────────────────────────────────────


class Platform(StrEnum):
    POSTGRESQL = "postgres"
    MYSQL = "mysql"
    ORACLE = "oracle"
    ATHENA = "athena"
    BIGQUERY = "bigquery"
    SNOWFLAKE = "snowflake"
    KAFKA = "kafka"


# ── Mode enum ─────────────────────────────────────────────────────────────────


class Mode(StrEnum):
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

# Source types whose dataset URN names are always database-prefixed
# (``database.schema.table`` for postgres, ``database.table`` for mysql), so the
# recipe's ``database`` always names the first dot-separated segment.
#
# ``athena`` is deliberately absent: an athena recipe declares ``catalog_name``
# (never ``database``), and its URN names are ``schema.table``. Keeping it out of
# this set means the database gate stays inactive for athena even if a recipe
# carries a stray ``database`` key.
_DB_PREFIXED_SOURCE_TYPES = frozenset({"postgres", "mysql"})

# The ``source.config`` keys ``build_matcher_checked`` derives coverage from. Its
# branch cascade and the ``has_selection_patterns`` predicate read the same
# constants, so a key rename cannot desync them; a *new* branch key must be added
# to ``_SELECTION_PATTERN_CASCADE`` as well, or the predicate will not see it.
_SCHEMA_PATTERN_KEY = "schema_pattern"
_TABLE_PATTERN_KEY = "table_pattern"
_TOPIC_PATTERNS_KEY = "topic_patterns"
_DATASET_PATTERN_KEY = "dataset_pattern"

# Ordered exactly as ``build_matcher_checked`` tests them: the first key present in
# ``source.config`` selects the branch and the later keys are never reached
# (``table_pattern`` only participates as ``schema_pattern``'s co-predicate).
# ``has_selection_patterns`` walks this same order so predicate and matcher agree
# on which key decides.
_SELECTION_PATTERN_CASCADE: tuple[str, ...] = (
    _SCHEMA_PATTERN_KEY,
    _TABLE_PATTERN_KEY,
    _TOPIC_PATTERNS_KEY,
    _DATASET_PATTERN_KEY,
)

# Upper bound on a degradation reason emitted to a log record. A reason embeds
# writer-supplied text (a pattern string, an exception message quoting one), and a
# recipe pattern can expand to hundreds of kilobytes, so every reason is truncated
# before it reaches a log line. See ``truncate_reason``.
MAX_REASON_CHARS = 500


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
    """Recursively collect ``name__key`` tokens from all string values.

    Uses the shared ``SECRET_REF_RE`` pattern, so the set extracted (and
    verified) at save time is by construction the set substituted at run time
    by the resolver.
    """
    if isinstance(obj, str):
        for match in SECRET_REF_RE.finditer(obj):
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


def _match_nothing(_name: str) -> bool:
    """Predicate for a source whose coverage cannot be inferred: matches nothing."""
    return False


def truncate_reason(reason: str, limit: int = MAX_REASON_CHARS) -> str:
    """Bound a degradation reason to ``limit`` characters for logging.

    A reason quotes writer-supplied recipe text, which carries no length bound of
    its own — a single expanded pattern string has produced a six-figure-character
    reason. Every log call site passes the reason through here and emits it with
    ``%r``, so the record stays one bounded, escaped line whatever the recipe holds.
    """
    if len(reason) <= limit:
        return reason
    return f"{reason[:limit]}… (truncated, {len(reason)} chars)"


def build_matcher(recipe: dict[str, Any]) -> Callable[[str], bool]:
    """Build a dataset-name predicate from the recipe's allow/deny patterns.

    Exists alongside ``build_matcher_checked`` as the convenience form for callers
    that only need to evaluate names and have no branch to take on *why* a pattern
    set is empty — tests, ad-hoc coverage previews, and any read path that neither
    deletes rows nor reports a defect. It logs and discards the degradation reason,
    so an empty match set here is indistinguishable from "the pattern set could not
    be built". A caller that acts on an empty match set — deleting stored mappings,
    or reporting a coverage defect — must call ``build_matcher_checked`` and branch
    on the reason instead.

    See ``build_matcher_checked`` for the full matching contract.
    """
    matcher, reason = build_matcher_checked(recipe)
    if reason is not None:
        # Logged here rather than in build_matcher_checked, so a caller that keeps
        # the reason reports it once, with its own subject identity attached.
        # %r + truncate_reason: the reason embeds writer-supplied recipe text, and a
        # raw newline in it would otherwise split this record for a line-based
        # collector.
        logger.warning("build_matcher: %r", truncate_reason(reason))
    return matcher


def build_matcher_checked(recipe: dict[str, Any]) -> tuple[Callable[[str], bool], str | None]:
    """Build a dataset-name predicate, plus why it could not be built over the recipe.

    Uses ``datahub.configuration.common.AllowDenyPattern`` from the acryl-datahub
    SDK to evaluate whether a given dataset name is covered by this source.

    The dataset name passed to the returned callable must be the
    platform-specific name as it appears in the DataHub dataset URN:
      - postgres: ``database.schema.table`` (three dot-separated parts)
      - oracle: ``schema.table``, or ``database.schema.table`` when the recipe sets
        ``add_database_name_to_urn``
      - athena: ``schema.table``
      - mysql: ``database.table``
      - kafka: ``topic`` (bare, no instance) or ``platform_instance.topic``
      - bigquery / snowflake: the full dataset identifier

    Pattern semantics per DataHub's SQL source config:
      - ``schema_pattern`` is matched against the **container segment only** — the
        second segment of a three-segment name (``^catalog$`` applied to the schema
        part of ``example_db.catalog.orders``) and the leading segment of a
        two-segment name (``^schema_a$`` applied to ``schema_a`` of
        ``schema_a.table_1``). See ``_schema_segment``.
      - ``table_pattern`` is matched against the **full** URN name string.
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

    Known divergence from the connector: every ``AllowDenyPattern`` here is built from
    ``allow`` / ``deny`` only, so a recipe setting ``"ignoreCase": false`` on a pattern
    is still evaluated case-insensitively (the SDK's default is ``True``). Such a source
    is reported as covering names its own DataHub connector would exclude on case, so
    the sweep over-reports its coverage. This is the present contract, not a bug being
    tracked: the matcher is a declared-coverage approximation throughout.

    Database scoping:
      When the recipe declares a ``database`` and the source type's URN names are
      database-prefixed, every returned predicate additionally requires the name to
      begin with ``<database>.`` (the database followed by a dot) before any pattern
      is evaluated. The prefix is exact and case-sensitive; the trailing dot makes it
      a whole-segment match, so ``example_db`` accepts ``example_db.public.orders`` but
      rejects ``example_db2.public.orders``. The gate activates per source type:
        - ``postgres`` / ``mysql``: URN names are always database-prefixed
          (``database.schema.table`` / ``database.table``), so a non-empty ``database``
          always gates.
        - ``oracle``: URN names default to ``schema.table`` with no database segment,
          so the gate activates only when the recipe also sets
          ``add_database_name_to_urn`` truthy (which prepends the database segment).
          Gating on ``database`` alone would reject every oracle name.
        - athena / bigquery / snowflake / kafka: the gate never activates; their names
          are not database-prefixed in this form (an athena recipe scopes with
          ``catalog_name``, not ``database``).

    Caller contract:
      The matcher evaluates dataset names only — it has no view of the URN platform,
      and it does not gate on ``source.type`` being a recognised connector. Platform
      scoping is the caller's responsibility: the sync sweep feeds the matcher only
      names whose URN platform equals the recipe's ``source.type``. The sweep's
      zero-coverage warning reports a derivable-but-matches-nothing source only when
      DataHub holds datasets for that declared platform; a ``source.type`` naming no
      platform present in the estate is offered no candidate name at all, and that
      case is reported nowhere.

    If a well-formed recipe carries none of the above keys in ``source.config``, the
    matcher returns ``False`` for every name (match-nothing). A source with no
    derivable selection patterns maps no datasets — coverage that cannot be
    inferred must not be assumed.

    Returns:
        ``(predicate, reason)``. The predicate is match-nothing whenever the pattern
        set is not usable; ``reason`` is what separates the two ways that happens, and
        the distinction is load-bearing for a caller that prunes stored mappings
        against the match set.

        ``reason is None`` — **evaluated**. The predicate was built over a pattern set
        that is well-shaped and compiles, or over a well-formed recipe that declares no
        selection pattern at all. In both cases an empty match set is real evidence
        that no dataset is covered, so pruning on it is sound. A pattern-less recipe is
        the documented outcome rather than a defect, and reporting it would mark every
        recipe-less source in a sweep, on every sweep.

        ``reason`` a string — **not evaluated**. Nothing was ever run over the estate,
        so an empty match set is absence of evidence and a caller must not prune on it.
        The string is short and human-readable, naming the offending ``source.config``
        key where one is known and what could not be read otherwise; it embeds
        writer-supplied recipe text, so log it via ``truncate_reason`` with ``%r``.
        Four conditions produce a reason, and a caller may treat them alike:
          - the recipe cannot be parsed at all (no ``source``, no ``source.type``, a
            non-dict ``source.config``) — including the empty ``{}`` a failed read of
            DataHub's stored recipe string collapses to,
          - a pattern value the matcher cannot read (a bare string or ``null`` where an
            allow/deny mapping belongs),
          - a pattern string that fails ``re.compile``,
          - any other failure the guarded block catches, including the acryl-datahub
            SDK being unimportable while the recipe declares selection patterns. That
            one degrades only a source that actually declares patterns, so a no-SDK
            context does not make every source look like a recipe defect.

    Note: This is an explicit approximation. DataHub exposes no native
    source→dataset reverse lookup; the matcher reconstructs coverage from
    the declared filter syntax only.
    """
    try:
        source_type, config = parse_recipe(recipe)
    except ValueError as exc:
        # The recipe could not be read, so no pattern set was ever evaluated — as
        # distinct from a readable recipe that declares no patterns. Match nothing and
        # say why: a caller that prunes on the match set must not prune here.
        return _match_nothing, (
            f"recipe could not be read ({exc}), so no selection pattern was evaluated"
        )

    # Database scoping gate: when active, every predicate requires the name to
    # start with ``<database>.`` before its pattern branch runs.
    db = config.get("database")
    st = source_type.lower()
    db_gated = (
        isinstance(db, str)
        and bool(db)
        and (
            st in _DB_PREFIXED_SOURCE_TYPES
            or (st == "oracle" and bool(config.get("add_database_name_to_urn")))
        )
    )
    db_prefix = f"{db}." if db_gated else ""

    def _gate(pred: Callable[[str], bool]) -> Callable[[str], bool]:
        """Wrap ``pred`` with the database-prefix guard when the gate is active."""
        if not db_gated:
            return pred
        def _gated(
            name: str, _p: Callable[[str], bool] = pred, _pfx: str = db_prefix
        ) -> bool:
            return name.startswith(_pfx) and _p(name)

        return _gated

    # The config key whose value is being read when the guarded block below raises.
    # Set as each branch is entered so the failure names the key an operator has to
    # go and fix, rather than only the exception text.
    key_in_progress: str | None = None

    try:
        from datahub.configuration.common import AllowDenyPattern

        def _precompile(*pattern_lists: Any) -> None:
            """Compile the raw pattern strings eagerly, inside this guarded block.

            ``AllowDenyPattern`` compiles lazily, so constructing one validates no
            regex — a malformed pattern would otherwise raise ``re.error`` on the
            first ``allowed()`` call, i.e. from inside the returned predicate, past
            every handler here. Running ``re.compile`` over the same public input
            strings first moves that failure into the ``except`` below, where it is
            logged and degrades this one source to match-nothing. The runtime match
            path stays ``AllowDenyPattern``'s own compilation; this is a validity
            check on the inputs, not a substitute for it.
            """
            for patterns in pattern_lists:
                for pattern in patterns:
                    re.compile(pattern)

        def _make_adp(allow: list[str], deny: list[str]) -> Callable[[str], bool]:
            _precompile(allow, deny)
            adp = AllowDenyPattern(allow=allow, deny=deny)
            return lambda name: adp.allowed(name)

        def _schema_segment(name: str) -> str:
            """Extract the container segment a ``schema_pattern`` is evaluated against.

            The container's position depends on how many segments the connector
            puts in the URN name:
              - three or more (``database.schema.table``, postgres / oracle with
                ``add_database_name_to_urn``): the second segment.
              - exactly two (``schema.table`` on athena and default oracle,
                ``database.table`` on mysql): the **leading** segment — in a
                two-segment name the trailing segment is always the table, so the
                leading one is the container.
              - one: the whole name, the only shape for which returning the entire
                string can be correct. Returning it for a two-segment name would
                make an anchored pattern such as ``^schema_a$`` unmatchable.
            """
            parts = name.split(".")
            if len(parts) >= 3:
                return parts[1]
            if len(parts) == 2:
                return parts[0]
            return name

        # Schema-level pattern (postgres / mysql / oracle / athena family).
        # schema_pattern is applied to the container segment only.
        if _SCHEMA_PATTERN_KEY in config:
            key_in_progress = _SCHEMA_PATTERN_KEY
            sp = config[_SCHEMA_PATTERN_KEY]
            schema_adp = _make_adp(sp.get("allow", [".*"]), sp.get("deny", []))

            def schema_pred(name: str, _a: Callable[[str], bool] = schema_adp) -> bool:
                return _a(_schema_segment(name))

            # table_pattern (when present) applies to the full URN name string.
            if _TABLE_PATTERN_KEY in config:
                key_in_progress = _TABLE_PATTERN_KEY
                tp = config[_TABLE_PATTERN_KEY]
                table_adp = _make_adp(tp.get("allow", [".*"]), tp.get("deny", []))

                def schema_table_pred(
                    name: str,
                    _s: Callable[[str], bool] = schema_pred,
                    _t: Callable[[str], bool] = table_adp,
                ) -> bool:
                    return _s(name) and _t(name)

                return _gate(schema_table_pred), None
            return _gate(schema_pred), None

        # Table-only pattern — matched against the full URN name string.
        if _TABLE_PATTERN_KEY in config:
            key_in_progress = _TABLE_PATTERN_KEY
            tp = config[_TABLE_PATTERN_KEY]
            return _gate(_make_adp(tp.get("allow", [".*"]), tp.get("deny", []))), None

        # Kafka topic patterns — allow and deny evaluated against the topic name.
        # A kafka dataset URN name may be bare (<topic>) or instance-prefixed
        # (<platform_instance>.<topic>).  Candidate forms are the full name and,
        # when a dot is present, the substring after the first dot (bare topic).
        # covered(name) = ALLOW_matches_any_form AND NOT DENY_matches_any_form.
        # deny is checked independently so it cannot be bypassed by the allow
        # branch matching a different form of the same name.
        if _TOPIC_PATTERNS_KEY in config:
            key_in_progress = _TOPIC_PATTERNS_KEY
            tp = config[_TOPIC_PATTERNS_KEY]
            allow_pats: list[str] = tp.get("allow", [".*"])
            deny_pats: list[str] = tp.get("deny", [])
            _precompile(allow_pats, deny_pats)
            allow_adp = AllowDenyPattern(allow=allow_pats, deny=[])
            deny_adp: Any = AllowDenyPattern(allow=deny_pats, deny=[]) if deny_pats else None

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

            return _gate(_kafka_pred), None

        # BigQuery / Snowflake dataset pattern — matched against the full name.
        if _DATASET_PATTERN_KEY in config:
            key_in_progress = _DATASET_PATTERN_KEY
            dp = config[_DATASET_PATTERN_KEY]
            return _gate(_make_adp(dp.get("allow", [".*"]), dp.get("deny", []))), None

    except ImportError as exc:
        # acryl-datahub not available in this context — no SDK means no evaluation is
        # possible, so match nothing. It degrades only a recipe that declares patterns:
        # the import is the first statement in this block, so every source reaches it,
        # and flagging the pattern-less ones would report the SDK's absence as though
        # each of them carried a recipe defect.
        if any(key in config for key in _SELECTION_PATTERN_CASCADE):
            return _match_nothing, (
                f"acryl-datahub SDK is not importable ({exc}), so the declared "
                f"selection patterns cannot be evaluated"
            )
    except Exception as exc:
        # A wrongly-typed pattern value, a pattern that fails to compile, or any other
        # construction failure: match nothing (no coverage can be inferred) and hand
        # the caller the reason. ``_precompile`` runs the pattern strings through
        # ``re.compile`` first, so this handler, not the caller's matcher call, sees a
        # malformed regex — the caller can therefore trust that a returned matcher
        # with no reason will not blow up mid-sweep on a bad pattern.
        where = f"source.config.{key_in_progress}" if key_in_progress else "source.config"
        return _match_nothing, f"{where} is not a usable allow/deny pattern set: {exc}"

    # No derivable selection patterns → no inferable coverage → matches nothing, and
    # that is the documented outcome rather than a degradation.
    return _match_nothing, None


def has_selection_patterns(recipe: dict[str, Any]) -> bool:
    """Report whether the recipe declares a usable selection pattern ``build_matcher`` reads.

    Decided by the **first** of ``_SELECTION_PATTERN_CASCADE`` (``schema_pattern`` →
    ``table_pattern`` → ``topic_patterns`` → ``dataset_pattern``) present in
    ``source.config``, and by that key alone: True when its value has the
    AllowDenyPattern shape (a mapping, from which ``build_matcher`` reads ``allow`` /
    ``deny``), False otherwise. This is the rule ``build_matcher`` itself follows —
    the first present key selects the branch and the later keys are never reached —
    so a config whose leading key is wrongly typed reports False even when a later
    key is well-formed, matching the match-nothing result the matcher produces for it.

    The shape check is the trust boundary: ``recipe`` is writer-supplied JSONB passed
    through unchanged, so a key can hold ``null`` or a bare string. Such a value makes
    ``build_matcher`` fail and log, and this predicate reports ``False`` — a
    wrongly-typed pattern value is a recipe defect with its own log line, not a
    matcher/config coverage defect. A recipe that cannot be parsed at all returns
    ``False`` too, but that case is decided before this predicate is consulted:
    ``build_matcher_checked`` returns a reason for it (**not evaluated**), and callers
    gate on that reason first, so an unreadable recipe is never reported as coverage.

    Callers use this to separate the reasons an **evaluated** source maps nothing: no
    derivable patterns at all (expected — coverage is never assumed), versus a
    derivable, well-formed pattern set that matched no dataset (a defect signal worth
    reporting).
    """
    try:
        _, config = parse_recipe(recipe)
    except ValueError:
        return False
    for key in _SELECTION_PATTERN_CASCADE:
        if key in config:
            return isinstance(config[key], Mapping)
    return False
