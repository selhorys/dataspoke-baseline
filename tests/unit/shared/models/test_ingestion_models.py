"""Unit tests for src/shared/models/ingestion.py — pure recipe helpers.

Covers parse_recipe, extract_secret_refs, cron_to_tier, build_matcher,
build_matcher_checked, and has_selection_patterns.
All tests are pure (no DB, no network, no LLM) — these are stable spec invariants.

Spec: spec/feature/BACKEND.md §Ingestion Service
Spec: spec/feature/BACKEND_SCHEMA.md §ingestion_source
Spec: spec/feature/SECRET_RESOLUTION.md §Overview (reference syntax ${name__key})
Spec: spec/USE_CASE_en.md §UC1
"""

from __future__ import annotations

import logging
import sys

import pytest

from src.shared.models.ingestion import (
    CRON_TO_TIER,
    MAX_REASON_CHARS,
    Mode,
    build_matcher,
    build_matcher_checked,
    cron_to_tier,
    extract_secret_refs,
    has_selection_patterns,
    parse_recipe,
    truncate_reason,
)

# The four selection-pattern keys, in the cascade order the matcher reads them, each with
# a source.type that declares it, a well-formed value, and a name that value covers. The
# degradation-reason contract is per-key (the reason names the offending
# ``source.config.<key>``), so the reason tests run once per row rather than once.
#
# spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — cascade order
#     'schema_pattern → table_pattern → topic_patterns → dataset_pattern'.
_CASCADE_KEY_CASES: list[tuple[str, str, dict[str, list[str]], str]] = [
    (
        "postgres",
        "schema_pattern",
        {"allow": ["^catalog$"]},
        "example_db.catalog.title_master",
    ),
    (
        "postgres",
        "table_pattern",
        {"allow": [r"^example_db\.catalog\..*$"]},
        "example_db.catalog.title_master",
    ),
    (
        "kafka",
        "topic_patterns",
        {"allow": [r"^imazon\..*$"]},
        "example_kafka.imazon.orders.events",
    ),
    (
        "bigquery",
        "dataset_pattern",
        {"allow": [r"^analytics\..*$"]},
        "analytics.events",
    ),
]

# The logger ``build_matcher`` emits its degradation warning on.
_INGESTION_MODEL_LOGGER = "src.shared.models.ingestion"

# ── Mode enum ─────────────────────────────────────────────────────────────────


class TestModeEnum:
    def test_three_modes_defined(self) -> None:
        """Spec: BACKEND.md §Ingestion Service — three modes DATAHUB_MANAGED,
        ACTIVE_CUSTOM_MANAGED, PASSIVE.

        Spec: spec/feature/BACKEND_SCHEMA.md §ingestion_source — mode column.
        """
        assert Mode.DATAHUB_MANAGED.value == "DATAHUB_MANAGED"
        assert Mode.ACTIVE_CUSTOM_MANAGED.value == "ACTIVE_CUSTOM_MANAGED"
        assert Mode.PASSIVE.value == "PASSIVE"

    def test_mode_is_str_enum(self) -> None:
        """Mode values are strings (str-compatible enum), not raw enum objects.

        Mode is a StrEnum, so str(Mode.X), f"{Mode.X}" and Mode.X.value all agree.
        """
        assert Mode.ACTIVE_CUSTOM_MANAGED.value == "ACTIVE_CUSTOM_MANAGED"
        # Mode(str) — value-equality with literal strings
        assert Mode.ACTIVE_CUSTOM_MANAGED == "ACTIVE_CUSTOM_MANAGED"
        # StrEnum coercion carries no class-name prefix, so interpolated values
        # stay wire-safe wherever a Mode reaches a URN, log line, or payload.
        assert str(Mode.ACTIVE_CUSTOM_MANAGED) == "ACTIVE_CUSTOM_MANAGED"
        assert f"{Mode.ACTIVE_CUSTOM_MANAGED}" == "ACTIVE_CUSTOM_MANAGED"


# ── parse_recipe ──────────────────────────────────────────────────────────────


class TestParseRecipe:
    def test_valid_shape_returns_type_and_config(self) -> None:
        """parse_recipe accepts {source:{type, config}} and returns (type, config).

        Spec: BACKEND.md §Ingestion Service — 'The recipe is stored DataHub-compatible
        ({source:{type, config}})'.
        """
        recipe = {
            "source": {
                "type": "postgres",
                "config": {"host_port": "pg:5432"},
            }
        }
        source_type, config = parse_recipe(recipe)
        assert source_type == "postgres"
        assert config == {"host_port": "pg:5432"}

    def test_missing_config_defaults_to_empty_dict(self) -> None:
        """Absent config key is treated as {} — not an error.

        Spec: BACKEND.md — source.config is optional (passive sources may omit it).
        """
        recipe = {"source": {"type": "kafka"}}
        source_type, config = parse_recipe(recipe)
        assert source_type == "kafka"
        assert config == {}

    def test_missing_recipe_root_raises(self) -> None:
        """Non-dict recipe raises ValueError.

        Spec: BACKEND.md — recipe shape is validated on create/replace.
        """
        with pytest.raises(ValueError):
            parse_recipe("not a dict")  # type: ignore[arg-type]

    def test_missing_source_key_raises(self) -> None:
        """recipe without 'source' raises ValueError."""
        with pytest.raises(ValueError):
            parse_recipe({"sink": {}})

    def test_missing_type_raises(self) -> None:
        """recipe.source without 'type' raises ValueError."""
        with pytest.raises(ValueError):
            parse_recipe({"source": {"config": {}}})

    def test_empty_type_raises(self) -> None:
        """Empty string for recipe.source.type raises ValueError."""
        with pytest.raises(ValueError):
            parse_recipe({"source": {"type": "", "config": {}}})

    def test_non_dict_config_raises(self) -> None:
        """Non-dict config raises ValueError."""
        with pytest.raises(ValueError):
            parse_recipe({"source": {"type": "postgres", "config": ["list"]}})


# ── extract_secret_refs ───────────────────────────────────────────────────────


class TestExtractSecretRefs:
    def test_finds_double_underscore_ref(self) -> None:
        """${name__key} tokens with DNS-label-safe name and __ separator are returned.

        Spec: SECRET_RESOLUTION.md — '${name__key}' is the secret reference syntax;
        name is a DNS-label-safe token (lowercase alphanumerics and hyphens).
        """
        recipe = {
            "source": {
                "type": "postgres",
                "config": {"password": "${dummy-data-pg__password}"},
            }
        }
        refs = extract_secret_refs(recipe)
        assert refs == {"dummy-data-pg__password"}

    def test_ignores_plain_var_without_double_underscore(self) -> None:
        """${plainvar} without __ does not match the secret ref pattern and is ignored.

        Spec: SECRET_RESOLUTION.md §Overview — the pattern requires an explicit
        __ separator; tokens without __ are not matched and are silently skipped.
        """
        recipe = {
            "source": {
                "type": "postgres",
                "config": {"tag": "${ENVIRONMENT}"},  # no __ → not a secret ref
            }
        }
        refs = extract_secret_refs(recipe)
        assert refs == set()

    def test_recurses_nested_dicts(self) -> None:
        """Nested config dicts are recursed fully.

        Spec: BACKEND.md — recipe.source.config may contain nested AllowDenyPattern dicts.
        """
        recipe = {
            "source": {
                "type": "postgres",
                "config": {
                    "ssl": {"cert": "${team-pg__ssl_cert}"},
                    "password": "${team-pg__password}",
                },
            }
        }
        refs = extract_secret_refs(recipe)
        assert refs == {"team-pg__ssl_cert", "team-pg__password"}

    def test_recurses_lists(self) -> None:
        """Lists of strings containing ${ref} are also scanned."""
        recipe = {
            "source": {
                "type": "kafka",
                "config": {
                    "extra_secrets": ["${kafka-cred__api_key}", "${kafka-cred__api_secret}"],
                },
            }
        }
        refs = extract_secret_refs(recipe)
        assert refs == {"kafka-cred__api_key", "kafka-cred__api_secret"}

    def test_deduplicates_same_ref_used_twice(self) -> None:
        """Same ${name__key} used in two places appears once in the output set."""
        recipe = {
            "source": {
                "type": "postgres",
                "config": {
                    "password": "${team-pg__password}",
                    "backup_password": "${team-pg__password}",
                },
            }
        }
        refs = extract_secret_refs(recipe)
        assert refs == {"team-pg__password"}

    def test_empty_config_returns_empty_set(self) -> None:
        """No placeholders in config → empty set."""
        recipe = {
            "source": {
                "type": "postgres",
                "config": {"host_port": "localhost:5432"},
            }
        }
        refs = extract_secret_refs(recipe)
        assert refs == set()

    def test_malformed_recipe_returns_empty_set(self) -> None:
        """Malformed recipe (missing source) → empty set (no ValueError raised)."""
        refs = extract_secret_refs({"not": "a recipe"})
        assert refs == set()

    def test_ref_with_double_underscore_in_key_segment(self) -> None:
        """${name__key} where the key segment itself contains __ — full token is returned.

        The regex matches name=[a-z0-9-]+ then __ then key=[A-Za-z0-9_.-]+, so
        ${team__pg__password} captures name=team, key=pg__password; the reconstructed
        token team__pg__password is what extract_secret_refs returns. The resolver
        then splits on the last __ (team__pg → name, password → key).

        Spec: SECRET_RESOLUTION.md §Run-time resolve flow — 'split on last __'.
        """
        recipe = {
            "source": {
                "type": "postgres",
                "config": {"password": "${team__pg__password}"},
            }
        }
        refs = extract_secret_refs(recipe)
        # The whole inner token is returned — parsing happens in the resolver
        assert refs == {"team__pg__password"}


# ── cron_to_tier ─────────────────────────────────────────────────────────────


class TestCronToTier:
    """Spec: BACKEND_SCHEMA.md §ingestion_source — schedule_tier derived from schedule.

    Spec: BACKEND.md §Ingestion Service — 'For ACTIVE_CUSTOM_MANAGED, on upsert the
    service validates it maps to one of the three tiers (hourly/daily/weekly).'
    """

    def test_canonical_hourly_cron(self) -> None:
        """'0 * * * *' maps to 'hourly'."""
        assert cron_to_tier("0 * * * *") == "hourly"

    def test_alias_hourly(self) -> None:
        """'@hourly' maps to 'hourly'."""
        assert cron_to_tier("@hourly") == "hourly"

    def test_canonical_daily_cron(self) -> None:
        """'0 0 * * *' maps to 'daily'."""
        assert cron_to_tier("0 0 * * *") == "daily"

    def test_alias_daily(self) -> None:
        """'@daily' maps to 'daily'."""
        assert cron_to_tier("@daily") == "daily"

    def test_alias_midnight(self) -> None:
        """'@midnight' maps to 'daily' (midnight ≡ daily)."""
        assert cron_to_tier("@midnight") == "daily"

    def test_canonical_weekly_cron(self) -> None:
        """'0 0 * * 0' maps to 'weekly'."""
        assert cron_to_tier("0 0 * * 0") == "weekly"

    def test_alias_weekly(self) -> None:
        """'@weekly' maps to 'weekly'."""
        assert cron_to_tier("@weekly") == "weekly"

    def test_none_returns_none(self) -> None:
        """None schedule means manual-only — returns None.

        Spec: BACKEND_SCHEMA.md — 'schedule: NULL means manual-only'.
        """
        assert cron_to_tier(None) is None

    def test_unknown_cron_raises_value_error(self) -> None:
        """Unknown cron expression raises ValueError (not silently ignored).

        Spec: BACKEND.md — 'schedule is a non-None string not recognised … raises ValueError'.
        """
        with pytest.raises(ValueError, match="hourly.*daily.*weekly|three allowed tiers"):
            cron_to_tier("*/5 * * * *")

    def test_leading_whitespace_stripped(self) -> None:
        """Whitespace is stripped before lookup — '  @daily  ' is still valid."""
        assert cron_to_tier("  @daily  ") == "daily"

    def test_all_cron_to_tier_keys_present(self) -> None:
        """CRON_TO_TIER public constant exposes all recognised cron aliases.

        Spec: BACKEND.md — 'Expose the canonical crons as a public constant'.
        """
        assert "0 * * * *" in CRON_TO_TIER
        assert "0 0 * * *" in CRON_TO_TIER
        assert "0 0 * * 0" in CRON_TO_TIER
        assert "@hourly" in CRON_TO_TIER
        assert "@daily" in CRON_TO_TIER
        assert "@weekly" in CRON_TO_TIER


# ── build_matcher ─────────────────────────────────────────────────────────────


class TestBuildMatcher:
    """Spec: BACKEND.md §Sync + mapping sweep §Matching.

    build_matcher returns a predicate from the recipe's allow/deny patterns.
    Dataset name passed in must be 'database.schema.table' for postgres.
    schema_pattern is matched against the schema segment only.
    deny wins over allow.
    """

    def test_schema_pattern_allow_catalog_matches_catalog_table(self) -> None:
        """schema_pattern allow=['^catalog$'] matches 'example_db.catalog.orders'.

        Spec: BACKEND.md — 'schema_pattern is matched against the schema segment only'.
        """
        recipe = {
            "source": {
                "type": "postgres",
                "config": {
                    "database": "example_db",
                    "schema_pattern": {"allow": ["^catalog$"]},
                },
            }
        }
        matcher = build_matcher(recipe)
        assert matcher("example_db.catalog.orders") is True

    def test_schema_pattern_allow_catalog_rejects_public_table(self) -> None:
        """schema_pattern allow=['^catalog$'] rejects 'example_db.public.orders'.

        Spec: BACKEND.md — schema_pattern allow-list is exclusive.
        """
        recipe = {
            "source": {
                "type": "postgres",
                "config": {
                    "database": "example_db",
                    "schema_pattern": {"allow": ["^catalog$"]},
                },
            }
        }
        matcher = build_matcher(recipe)
        assert matcher("example_db.public.orders") is False

    def test_deny_wins_over_allow(self) -> None:
        """deny pattern takes precedence over matching allow pattern.

        Spec: BACKEND.md — 'deny wins' per AllowDenyPattern semantics.
        """
        recipe = {
            "source": {
                "type": "postgres",
                "config": {
                    "schema_pattern": {
                        "allow": ["^catalog$"],
                        "deny": ["^catalog$"],
                    },
                },
            }
        }
        matcher = build_matcher(recipe)
        # deny overrides allow — catalog should be rejected
        assert matcher("example_db.catalog.orders") is False

    def test_table_pattern_matches_full_database_schema_table_name(self) -> None:
        """table_pattern is matched against the full 'database.schema.table' string.

        Spec: BACKEND.md — 'table_pattern matched against the full database.schema.table string'.
        """
        recipe = {
            "source": {
                "type": "postgres",
                "config": {
                    "table_pattern": {"allow": ["^example_db\\.catalog\\.title_master$"]},
                },
            }
        }
        matcher = build_matcher(recipe)
        assert matcher("example_db.catalog.title_master") is True
        assert matcher("example_db.catalog.editions") is False

    def test_no_patterns_is_match_nothing(self) -> None:
        """No selection patterns declared → match-nothing (no inferable coverage).

        Spec: BACKEND.md — 'a source with no derivable selection patterns maps no datasets'.
        A source whose recipe carries no schema_pattern/table_pattern/topic_patterns/
        dataset_pattern cannot have its coverage inferred, so the matcher returns False
        for every name rather than claiming to cover everything.
        """
        recipe = {
            "source": {
                "type": "postgres",
                "config": {"host_port": "pg:5432"},
            }
        }
        matcher = build_matcher(recipe)
        assert matcher("any.schema.table") is False
        assert matcher("example_db.catalog.orders") is False

    def test_datahub_gc_no_patterns_maps_nothing(self) -> None:
        """Regression: DataHub built-in sources (datahub-gc, datahub-documents) carry no
        selection patterns. Each must map zero datasets, not the full estate.

        Without this fix, every dataset was attributed to datahub-gc because the old
        allow-all default matched everything.

        Spec: BACKEND.md — 'a source with no derivable selection patterns maps no datasets'.
        """
        gc_recipe = {"source": {"type": "datahub-gc", "config": {}}}
        gc_matcher = build_matcher(gc_recipe)
        # Postgres-style dataset name
        assert gc_matcher("example_db.catalog.title_master") is False
        # Kafka-style dataset name (instance-prefixed)
        assert gc_matcher("example_kafka.imazon.orders.events") is False
        # Generic names
        assert gc_matcher("some.random.dataset") is False
        assert gc_matcher("anything") is False

        docs_recipe = {"source": {"type": "datahub-documents", "config": {}}}
        docs_matcher = build_matcher(docs_recipe)
        assert docs_matcher("example_db.catalog.title_master") is False
        assert docs_matcher("example_kafka.imazon.orders.events") is False

    def test_schema_and_table_pattern_both_must_pass(self) -> None:
        """When both schema_pattern and table_pattern present, dataset must pass both.

        Spec: BACKEND.md — 'a dataset must pass both predicates to be included'.
        """
        recipe = {
            "source": {
                "type": "postgres",
                "config": {
                    "schema_pattern": {"allow": ["^catalog$"]},
                    "table_pattern": {"allow": ["^example_db\\.catalog\\.title_master$"]},
                },
            }
        }
        matcher = build_matcher(recipe)
        # Passes schema AND table pattern
        assert matcher("example_db.catalog.title_master") is True
        # Passes schema but not table
        assert matcher("example_db.catalog.editions") is False
        # Passes neither
        assert matcher("example_db.public.orders") is False

    def test_passive_allow_deny_scope_pattern(self) -> None:
        """PASSIVE recipes may use schema_pattern as declared AllowDenyPattern scope.

        schema_pattern is applied to the SCHEMA SEGMENT ONLY (not the full table name).
        deny pattern ".*internal.*" applied to schema "orders" — "orders" does not match
        ".*internal.*" so the dataset is NOT denied.

        Spec: BACKEND.md §Ingestion Service — 'schema_pattern is matched against the
        schema segment only'. BACKEND_SCHEMA.md §ingestion_source — 'PASSIVE: config
        carries only the declared scope as an AllowDenyPattern-shaped filter'.
        """
        recipe = {
            "source": {
                "type": "postgres",
                "config": {
                    "schema_pattern": {"allow": ["^orders$"], "deny": ["^internal$"]},
                },
            }
        }
        matcher = build_matcher(recipe)
        # Schema "orders" matches allow=["^orders$"] and NOT deny=["^internal$"] → True
        assert matcher("db.orders.daily_fulfillment_summary") is True
        # schema_pattern deny applies to schema segment only; "orders" != "internal"
        assert matcher("db.orders.internal_audit") is True  # schema "orders" allowed
        # Schema "catalog" does not match allow=["^orders$"] → False
        assert matcher("db.catalog.title_master") is False

    def test_malformed_recipe_returns_match_nothing_predicate(self) -> None:
        """Malformed recipe (missing source) → predicate that returns False for all inputs."""
        matcher = build_matcher({"broken": "recipe"})
        assert matcher("any.table") is False

    # ── kafka topic_patterns ───────────────────────────────────────────────────

    def test_kafka_instance_prefixed_name_matches_topic_pattern(self) -> None:
        """Instance-prefixed kafka name 'example_kafka.imazon.orders.events' is matched
        by topic_patterns allow=['^imazon\\..*$'] — the pattern is evaluated against the
        bare topic ('imazon.orders.events') after stripping the leading platform-instance
        segment.

        Spec: BACKEND.md §Sync sweep — UC1 Case 3 passive kafka; DataHub evaluates
        topic_patterns against the topic name before instance prefix is applied.
        """
        recipe = {
            "source": {
                "type": "kafka",
                "config": {
                    "topic_patterns": {"allow": [r"^imazon\..*$"]},
                },
            }
        }
        matcher = build_matcher(recipe)
        assert matcher("example_kafka.imazon.orders.events") is True
        assert matcher("example_kafka.imazon.shipping.updates") is True

    def test_kafka_bare_topic_matches_topic_pattern(self) -> None:
        """Bare topic name 'imazon.orders.events' (no platform instance) is matched
        directly by topic_patterns allow=['^imazon\\..*$'] without any segment stripping.
        """
        recipe = {
            "source": {
                "type": "kafka",
                "config": {
                    "topic_patterns": {"allow": [r"^imazon\..*$"]},
                },
            }
        }
        matcher = build_matcher(recipe)
        assert matcher("imazon.orders.events") is True

    def test_kafka_non_matching_topic_is_excluded(self) -> None:
        """An instance-prefixed name whose topic segment does not match the allow pattern
        is rejected.  'example_kafka.other.topic' strips to 'other.topic', which does not
        match '^imazon\\..*$'.
        """
        recipe = {
            "source": {
                "type": "kafka",
                "config": {
                    "topic_patterns": {"allow": [r"^imazon\..*$"]},
                },
            }
        }
        matcher = build_matcher(recipe)
        assert matcher("example_kafka.other.topic") is False

    def test_kafka_deny_pattern_topic_anchored_allow_and_deny(self) -> None:
        """Topic-anchored allow + topic-anchored deny: deny wins even for instance-prefixed names.

        allow=['^imazon\\..*$'], deny=['^imazon\\.shipping\\..*$']:
          - 'example_kafka.imazon.orders.events'    → topic 'imazon.orders.events'    → allowed
          - 'example_kafka.imazon.shipping.updates' → topic 'imazon.shipping.updates' → denied

        Spec: BACKEND.md — 'deny wins'; deny is evaluated against every candidate form
        (full name and bare topic) so the instance prefix cannot shield a denied topic.
        """
        recipe = {
            "source": {
                "type": "kafka",
                "config": {
                    "topic_patterns": {
                        "allow": [r"^imazon\..*$"],
                        "deny": [r"^imazon\.shipping\..*$"],
                    },
                },
            }
        }
        matcher = build_matcher(recipe)
        assert matcher("example_kafka.imazon.orders.events") is True
        assert matcher("example_kafka.imazon.shipping.updates") is False

    def test_kafka_broad_allow_with_topic_anchored_deny_excludes_denied_topics(self) -> None:
        """Broad allow=['.*'] does not let a topic-anchored deny be bypassed.

        With allow=['.*'] and deny=['^imazon\\.secret\\..*$']:
          - 'example_kafka.imazon.secret.x' → full name matches broad allow, but bare topic
            'imazon.secret.x' matches deny → excluded (deny wins over allow).
          - 'imazon.secret.x' (bare)        → matches broad allow AND matches deny → excluded.
          - 'example_kafka.imazon.orders.events' → matches broad allow, bare topic
            'imazon.orders.events' does not match deny → included.

        Spec: BACKEND.md — deny and allow are evaluated against all candidate forms
        independently; deny winning on any form excludes the dataset.
        """
        recipe = {
            "source": {
                "type": "kafka",
                "config": {
                    "topic_patterns": {
                        "allow": [".*"],
                        "deny": [r"^imazon\.secret\..*$"],
                    },
                },
            }
        }
        matcher = build_matcher(recipe)
        # instance-prefixed denied topic — must be excluded
        assert matcher("example_kafka.imazon.secret.x") is False
        # bare denied topic — must be excluded
        assert matcher("imazon.secret.x") is False
        # non-denied sibling under same broad allow — must be included
        assert matcher("example_kafka.imazon.orders.events") is True


# ── build_matcher: database scoping gate ──────────────────────────────────────


class TestBuildMatcherDatabaseScope:
    """Database-prefix gate added to build_matcher per spec/feature/BACKEND.md §Sync +
    mapping sweep: the matcher is derived from the recipe's platform+database+
    schema_pattern/table_pattern. When the recipe declares a ``database`` and the source
    type's URN names are database-prefixed, every predicate additionally requires
    ``name.startswith("<database>.")`` before pattern evaluation (exact, case-sensitive,
    trailing dot = whole-segment match).

    Platform scoping (caller's responsibility) is NOT tested here — see the sync-sweep
    unit tests and test_uc1_01_datahub_managed.py for that layer.

    Spec: spec/feature/BACKEND.md §Sync + mapping sweep
    Spec: build_matcher docstring §Database scoping and §Caller contract
    """

    def test_uc1_shaped_recipe_rejects_kafka_name_accepts_example_db(self) -> None:
        """UC1 production-bug regression: a postgres source (database=example_db,
        schema_pattern deny=[^information_schema$, ^pg_.*$, ^catalog$]) must NOT match
        the kafka name 'example_kafka.imazon.orders.events', because its first segment
        'example_kafka' != 'example_db'.

        The same matcher must accept 'example_db.imazon.orders' — a postgres name whose
        schema segment 'imazon' passes the deny list (not information_schema, pg_*, or
        catalog).

        Spec: BACKEND.md §Sync + mapping sweep — matcher derived from
        platform+database+schema_pattern; the database prefix gates the full predicate so
        cross-platform names are never matched by a platform-specific source.
        Spec: build_matcher docstring §Database scoping.
        """
        recipe = {
            "source": {
                "type": "postgres",
                "config": {
                    "database": "example_db",
                    "schema_pattern": {
                        "deny": [
                            "^information_schema$",
                            "^pg_.*$",
                            "^catalog$",
                        ],
                    },
                },
            }
        }
        matcher = build_matcher(recipe)
        # kafka name — first segment is example_kafka, not example_db → rejected by db gate
        assert matcher("example_kafka.imazon.orders.events") is False
        # postgres name — database segment matches, schema 'imazon' passes the deny list
        assert matcher("example_db.imazon.orders") is True

    def test_database_gate_rejects_other_db_accepts_declared_db(self) -> None:
        """With database=example_db and allow-all schema_pattern, names under a different
        database are rejected; names under example_db are accepted.

        Spec: BACKEND.md §Sync + mapping sweep — database scoping is exact prefix match.
        Spec: build_matcher docstring §Database scoping.
        """
        recipe = {
            "source": {
                "type": "postgres",
                "config": {
                    "database": "example_db",
                    "schema_pattern": {"allow": [".*"]},
                },
            }
        }
        matcher = build_matcher(recipe)
        assert matcher("other_db.public.t") is False
        assert matcher("example_db.public.t") is True

    def test_no_database_key_leaves_gate_inactive(self) -> None:
        """When the recipe has no 'database' key, the database gate is inactive and names
        from any database-prefix are evaluated purely by pattern.

        Spec: build_matcher docstring §Database scoping — gate requires a non-empty string
        database key to activate; absent key → unchanged behaviour.
        """
        recipe = {
            "source": {
                "type": "postgres",
                "config": {
                    "schema_pattern": {"allow": [".*"]},
                },
            }
        }
        matcher = build_matcher(recipe)
        # Without the database gate, names from any prefix pass the allow-all pattern
        assert matcher("other_db.public.t") is True

    def test_empty_string_database_leaves_gate_inactive(self) -> None:
        """database='' (empty string) does not activate the gate — empty prefix would
        trivially match every name; the spec requires a non-empty database value.

        Spec: build_matcher docstring §Database scoping — gate activates only for a
        non-empty str database value.
        """
        recipe = {
            "source": {
                "type": "postgres",
                "config": {
                    "database": "",
                    "schema_pattern": {"allow": [".*"]},
                },
            }
        }
        matcher = build_matcher(recipe)
        # Gate is inactive — allow-all pattern matches regardless of db prefix
        assert matcher("other_db.public.t") is True

    def test_non_string_database_leaves_gate_inactive(self) -> None:
        """database=<non-str> (e.g. a dict) does not activate the gate; the value is not
        a string so the isinstance(db, str) guard skips it.

        Spec: build_matcher docstring §Database scoping — gate requires isinstance(db, str).
        """
        recipe = {
            "source": {
                "type": "postgres",
                "config": {
                    "database": {"host": "pg"},  # malformed — not a string
                    "schema_pattern": {"allow": [".*"]},
                },
            }
        }
        matcher = build_matcher(recipe)
        # Gate is inactive — allow-all pattern matches regardless of db prefix
        assert matcher("other_db.public.t") is True

    def test_mysql_two_segment_names_gated_by_database(self) -> None:
        """mysql URN names are database.table (two segments); the database gate applies
        the same startswith('<database>.') prefix check, so 'shop.orders' passes when
        database=shop and 'crm.orders' is rejected.

        Spec: build_matcher docstring §Database scoping — mysql URN names are always
        database-prefixed (database.table); a non-empty database always gates.
        """
        recipe = {
            "source": {
                "type": "mysql",
                "config": {
                    "database": "shop",
                    "table_pattern": {"allow": [".*"]},
                },
            }
        }
        matcher = build_matcher(recipe)
        assert matcher("shop.orders") is True
        assert matcher("crm.orders") is False

    def test_oracle_without_add_database_name_to_urn_gate_inactive(self) -> None:
        """Oracle URN names default to schema.table (no database prefix), so gating on
        database alone would reject every name. The gate must remain inactive when
        add_database_name_to_urn is absent or falsy.

        Spec: build_matcher docstring §Database scoping — for oracle the gate activates
        ONLY when add_database_name_to_urn is truthy; absent flag → gate inactive.
        """
        recipe = {
            "source": {
                "type": "oracle",
                "config": {
                    "database": "orcl",
                    "schema_pattern": {"allow": [".*"]},
                    # add_database_name_to_urn not set → oracle names are schema.table
                },
            }
        }
        matcher = build_matcher(recipe)
        # Gate is inactive — oracle names like hr.employees are accepted without db prefix
        assert matcher("hr.employees") is True

    def test_oracle_with_add_database_name_to_urn_gate_active(self) -> None:
        """When oracle recipe sets add_database_name_to_urn=True, URN names gain the
        database prefix (orcl.hr.employees). The gate then requires the name to start
        with 'orcl.' and rejects bare schema.table names.

        Spec: build_matcher docstring §Database scoping — oracle gate activates only
        when add_database_name_to_urn is truthy, matching the connector's behaviour of
        prepending the database segment to the URN name.
        """
        recipe = {
            "source": {
                "type": "oracle",
                "config": {
                    "database": "orcl",
                    "add_database_name_to_urn": True,
                    "schema_pattern": {"allow": [".*"]},
                },
            }
        }
        matcher = build_matcher(recipe)
        assert matcher("orcl.hr.employees") is True
        # Bare schema.table — missing database prefix → rejected
        assert matcher("hr.employees") is False

    def test_database_prefix_is_whole_segment_not_substring(self) -> None:
        """The database prefix check uses '<database>.' (with trailing dot), so
        'example_db' does NOT match names starting with 'example_db2.' — the trailing
        dot enforces a whole-segment boundary.

        Spec: build_matcher docstring §Database scoping — 'the trailing dot makes it a
        whole-segment match, so example_db accepts example_db.public.orders but rejects
        example_db2.public.orders'.
        """
        recipe = {
            "source": {
                "type": "postgres",
                "config": {
                    "database": "example_db",
                    "schema_pattern": {"allow": [".*"]},
                },
            }
        }
        matcher = build_matcher(recipe)
        # example_db2 shares the prefix string but is a different segment → rejected
        assert matcher("example_db2.s.t") is False
        # The declared database is accepted
        assert matcher("example_db.s.t") is True

    def test_kafka_with_stray_database_key_gate_never_activates(self) -> None:
        """A kafka recipe that incidentally carries a 'database' key in config must NOT
        activate the database scoping gate — kafka URN names are topic-based (bare or
        instance-prefixed) and are never database-prefixed in the dataspoke URN scheme.

        The guard ``source_type in _DB_PREFIXED_SOURCE_TYPES`` ensures only postgres and
        mysql trigger the gate; bigquery, snowflake, and kafka always bypass it regardless
        of whether a 'database' key happens to appear in the config.

        Without this guard the test would fail: the gate would require the name to start
        with 'anything.' and the Imazon kafka name 'example_kafka.imazon.orders.events'
        would be tested against the stray prefix — producing an incorrect exclusion.

        Spec: build_matcher docstring §Database scoping — 'bigquery / snowflake / kafka:
        the gate never activates'.
        Spec: BACKEND.md §Sync + mapping sweep.
        """
        recipe = {
            "source": {
                "type": "kafka",
                "config": {
                    "database": "anything",  # stray key — must not activate gate
                    "topic_patterns": {
                        "allow": [r"^imazon\..*$"],
                    },
                },
            }
        }
        matcher = build_matcher(recipe)
        # Gate must NOT activate for kafka — instance-prefixed topic passes allow pattern
        assert matcher("example_kafka.imazon.orders.events") is True

    def test_athena_with_stray_database_key_gate_never_activates(self) -> None:
        """An athena recipe carrying a stray ``database`` key must NOT activate the gate.

        athena URN names are ``schema.table`` with no database segment (an athena recipe
        scopes with ``catalog_name``, not ``database``), so gating on a stray ``database``
        would require every name to start with ``anything.`` and reject the whole estate —
        the same silent-total-failure shape as the kafka case above.

        Seeds both sides of the gate decision: the anchored ``^schema_c$`` covers
        ``schema_c.table_1`` (leading segment is the container on a two-segment name) and
        must keep covering it with the stray key present.

        Spec: build_matcher docstring §Database scoping — 'athena / bigquery / snowflake /
        kafka: the gate never activates; their names are not database-prefixed in this form
        (an athena recipe scopes with catalog_name, not database)'.
        Spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Name-shape contract —
        'schema.table / database.table | athena, mysql, oracle (default) | leading segment'.
        """
        recipe = {
            "source": {
                "type": "athena",
                "config": {
                    "database": "anything",  # stray key — must not activate gate
                    "schema_pattern": {"allow": ["^schema_c$"]},
                },
            }
        }
        matcher = build_matcher(recipe)
        # Gate must NOT activate for athena — the two-segment name has no database prefix.
        assert matcher("schema_c.table_1") is True
        # The pattern still discriminates: a different container is not covered.
        assert matcher("schema_d.table_1") is False


# ── build_matcher: schema_pattern name-shape contract ─────────────────────────


class TestSchemaPatternNameShapes:
    """The name-shape table for ``schema_pattern`` — which segment it is evaluated against.

    Spec: feature/BACKEND.md §Sync + mapping sweep, step 2 §Name-shape contract:
    'schema_pattern is matched against the name's **container segment**, whose position
    depends on how many segments the connector puts in the name — in a two-segment name
    the trailing segment is always the table, so the leading one is the container', with
    the table:
      | database.schema.table | postgres, oracle with add_database_name_to_urn | second segment |
      | schema.table / database.table | athena, mysql, oracle (default) | leading segment |
      | single segment | unqualified names with no container in the URN | the whole name |

    Each case seeds both sides — a name the container pattern covers and one it does not —
    and additionally pins *which* segment was read, by asserting that a pattern anchored on
    a neighbouring segment does NOT match.
    """

    def test_postgres_three_segment_uses_second_segment(self) -> None:
        """postgres ``database.schema.table`` → schema_pattern reads the SECOND segment.

        spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Name-shape contract —
        'database.schema.table | postgres … | second segment'.
        """
        covered = {
            "source": {
                "type": "postgres",
                "config": {"schema_pattern": {"allow": ["^catalog$"]}},
            }
        }
        matcher = build_matcher(covered)
        # Container segment of example_db.catalog.title_master is 'catalog' → covered.
        assert matcher("example_db.catalog.title_master") is True
        # A sibling under a different schema is not covered.
        assert matcher("example_db.public.title_master") is False

        # Pin the segment read: anchoring on the LEADING segment must not match a
        # three-segment name, because the leading segment is the database, not the
        # container.
        leading_anchored = {
            "source": {
                "type": "postgres",
                "config": {"schema_pattern": {"allow": ["^example_db$"]}},
            }
        }
        assert build_matcher(leading_anchored)("example_db.catalog.title_master") is False

    def test_athena_two_segment_uses_leading_segment(self) -> None:
        """athena ``schema.table`` → schema_pattern reads the LEADING segment.

        This is the #100 shape: under a three-segments-or-nothing reading the whole name
        is returned and an anchored ``^schema_c$`` can never match.

        spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Name-shape contract —
        'schema.table / database.table | athena, mysql, oracle (default) | leading segment'.
        """
        recipe = {
            "source": {
                "type": "athena",
                "config": {"schema_pattern": {"allow": ["^schema_c$"]}},
            }
        }
        matcher = build_matcher(recipe)
        assert matcher("schema_c.table_1") is True
        # Different container → not covered.
        assert matcher("schema_d.table_1") is False

        # Pin the segment read: anchoring on the TRAILING segment (the table) must not
        # match, and neither must a pattern anchored on the full name.
        trailing_anchored = {
            "source": {
                "type": "athena",
                "config": {"schema_pattern": {"allow": ["^table_1$"]}},
            }
        }
        assert build_matcher(trailing_anchored)("schema_c.table_1") is False
        full_name_anchored = {
            "source": {
                "type": "athena",
                "config": {"schema_pattern": {"allow": [r"^schema_c\.table_1$"]}},
            }
        }
        assert build_matcher(full_name_anchored)("schema_c.table_1") is False

    def test_oracle_default_two_segment_uses_leading_segment(self) -> None:
        """oracle default ``schema.table`` (HR.EMPLOYEES) → LEADING segment is the container.

        spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Name-shape contract —
        'schema.table … | oracle (default) | leading segment'.
        """
        recipe = {
            "source": {
                "type": "oracle",
                "config": {"schema_pattern": {"allow": ["^HR$"]}},
            }
        }
        matcher = build_matcher(recipe)
        assert matcher("HR.EMPLOYEES") is True
        assert matcher("SALES.EMPLOYEES") is False

        # Pin the segment read: the trailing segment is the table, never the container.
        trailing_anchored = {
            "source": {
                "type": "oracle",
                "config": {"schema_pattern": {"allow": ["^EMPLOYEES$"]}},
            }
        }
        assert build_matcher(trailing_anchored)("HR.EMPLOYEES") is False

    def test_mysql_two_segment_under_active_database_gate(self) -> None:
        """mysql ``database.table`` under an active database gate → LEADING segment.

        On mysql the leading segment is the database rather than a schema, so an anchored
        schema_pattern filters the database name — and the database gate is active at the
        same time, so both predicates run over the same segment.

        spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Name-shape contract —
        'On a two-tier platform whose leading segment is the database rather than a schema
        (mysql), an anchored schema_pattern is therefore filtering the database name.'
        """
        recipe = {
            "source": {
                "type": "mysql",
                "config": {
                    "database": "mydb",
                    "schema_pattern": {"allow": ["^mydb$"]},
                },
            }
        }
        matcher = build_matcher(recipe)
        assert matcher("mydb.orders") is True
        # Gate rejects another database, and its leading segment fails the pattern too.
        assert matcher("otherdb.orders") is False

        # Pin the segment read: the trailing segment is the table.
        trailing_anchored = {
            "source": {
                "type": "mysql",
                "config": {
                    "database": "mydb",
                    "schema_pattern": {"allow": ["^orders$"]},
                },
            }
        }
        assert build_matcher(trailing_anchored)("mydb.orders") is False

    def test_single_segment_name_uses_whole_name(self) -> None:
        """A one-segment (unqualified) name has no container segment → the whole name.

        spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Name-shape contract —
        'single segment | unqualified names with no container in the URN | the whole name'.
        """
        recipe = {
            "source": {
                "type": "postgres",
                "config": {"schema_pattern": {"allow": ["^orders$"]}},
            }
        }
        matcher = build_matcher(recipe)
        assert matcher("orders") is True
        assert matcher("shipments") is False

    def test_two_segment_schema_and_table_pattern_both_must_pass(self) -> None:
        """schema_pattern + table_pattern on a two-segment name: both predicates must pass.

        The compound #100 failure: when the container segment is read wrongly the anchored
        schema_pattern fails, and because both predicates must pass, a correct table_pattern
        cannot rescue the dataset. Seeds all three combinations — both pass, schema passes
        only, table passes only.

        spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Name-shape contract — 'When
        both schema_pattern and table_pattern are present a dataset must pass both, so an
        anchored schema_pattern evaluated against the wrong segment cannot be rescued by a
        correct table_pattern.'
        spec: feature/BACKEND.md §Sync + mapping sweep step 2 — 'table_pattern /
        dataset_pattern are matched against the full URN name.'
        """
        recipe = {
            "source": {
                "type": "athena",
                "config": {
                    "schema_pattern": {"allow": ["^schema_c$"]},
                    "table_pattern": {"allow": [r"^schema_c\.table_1$"]},
                },
            }
        }
        matcher = build_matcher(recipe)
        # Both predicates pass — this is the assertion #100 breaks.
        assert matcher("schema_c.table_1") is True
        # schema passes, table_pattern (full name) does not.
        assert matcher("schema_c.table_2") is False
        # table_pattern cannot pass for another container, and schema fails as well.
        assert matcher("schema_d.table_1") is False


# ── has_selection_patterns ────────────────────────────────────────────────────


class TestHasSelectionPatterns:
    """Truth table for the derivable-coverage predicate.

    Spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes and prune
    invariant — 'Whether a source declares derivable coverage at all is decided by the
    **first selection-pattern key the matcher reads** in its cascade order — schema_pattern
    → table_pattern → topic_patterns → dataset_pattern: the source declares coverage when
    that deciding key carries an AllowDenyPattern-shaped value. Recipe JSONB is
    writer-supplied, so the key may instead hold null or a bare string; that is a recipe
    defect, and it places the source in the not-evaluated outcome … rather than in either
    pruning outcome.'
    """

    def test_schema_pattern_key_declares_coverage(self) -> None:
        """schema_pattern carrying an allow/deny mapping declares derivable coverage."""
        assert (
            has_selection_patterns(
                {
                    "source": {
                        "type": "postgres",
                        "config": {"schema_pattern": {"allow": ["^catalog$"]}},
                    }
                }
            )
            is True
        )

    def test_table_pattern_key_declares_coverage(self) -> None:
        """table_pattern carrying an allow/deny mapping declares derivable coverage."""
        assert (
            has_selection_patterns(
                {
                    "source": {
                        "type": "postgres",
                        "config": {"table_pattern": {"allow": [".*"]}},
                    }
                }
            )
            is True
        )

    def test_topic_patterns_key_declares_coverage(self) -> None:
        """topic_patterns (kafka) carrying an allow/deny mapping declares coverage."""
        assert (
            has_selection_patterns(
                {
                    "source": {
                        "type": "kafka",
                        "config": {"topic_patterns": {"allow": [r"^imazon\..*$"]}},
                    }
                }
            )
            is True
        )

    def test_dataset_pattern_key_declares_coverage(self) -> None:
        """dataset_pattern (bigquery / snowflake) carrying a mapping declares coverage."""
        assert (
            has_selection_patterns(
                {
                    "source": {
                        "type": "bigquery",
                        "config": {"dataset_pattern": {"allow": ["^analytics$"]}},
                    }
                }
            )
            is True
        )

    def test_config_with_no_selection_key_declares_nothing(self) -> None:
        """A well-formed recipe carrying none of the four keys declares no coverage.

        spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — the
        'Evaluated, no derivable patterns' row is 'the recipe is well-formed and carries
        none of the four selection-pattern keys'.
        """
        assert (
            has_selection_patterns(
                {"source": {"type": "postgres", "config": {"host_port": "pg:5432"}}}
            )
            is False
        )

    def test_empty_config_declares_nothing(self) -> None:
        """An empty source.config carries no selection-pattern key → no coverage."""
        assert has_selection_patterns({"source": {"type": "postgres", "config": {}}}) is False

    def test_malformed_recipe_declares_nothing(self) -> None:
        """A recipe that cannot be parsed declares no coverage (never reported as coverage).

        spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — an
        unparseable recipe is the 'Not evaluated' outcome, so it is never counted as
        derivable coverage.
        """
        assert has_selection_patterns({"broken": "recipe"}) is False

    def test_deciding_key_holding_a_bare_string_declares_nothing(self) -> None:
        """A bare string on the deciding key is a recipe defect → not derivable coverage.

        spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes —
        'the key may instead hold null or a bare string; that is a recipe defect'.
        """
        assert (
            has_selection_patterns(
                {"source": {"type": "postgres", "config": {"schema_pattern": "oops"}}}
            )
            is False
        )

    def test_deciding_key_holding_null_declares_nothing(self) -> None:
        """``null`` on the deciding key is the same recipe defect as a bare string."""
        assert (
            has_selection_patterns(
                {"source": {"type": "postgres", "config": {"schema_pattern": None}}}
            )
            is False
        )

    def test_wrongly_shaped_deciding_key_is_not_rescued_by_a_later_key(self) -> None:
        """The FIRST cascade key decides: a well-formed later key cannot rescue it.

        schema_pattern (first in cascade) holds a bare string while table_pattern is
        well-formed. The matcher never reaches table_pattern, so the source is not
        derivable coverage.

        spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes —
        'decided by the first selection-pattern key the matcher reads in its cascade order
        — schema_pattern → table_pattern → topic_patterns → dataset_pattern'.
        """
        assert (
            has_selection_patterns(
                {
                    "source": {
                        "type": "postgres",
                        "config": {
                            "schema_pattern": "oops",
                            "table_pattern": {"allow": [".*"]},
                        },
                    }
                }
            )
            is False
        )

    def test_wrongly_shaped_later_key_does_not_unset_coverage(self) -> None:
        """A wrongly-shaped value on a LATER key leaves the source derivable.

        topic_patterns (third in cascade) is well-formed and decides; dataset_pattern
        (fourth) holds a bare string the matcher never reads.

        spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — 'the
        source declares coverage when that deciding key carries an AllowDenyPattern-shaped
        value'; only the deciding key is consulted.
        """
        assert (
            has_selection_patterns(
                {
                    "source": {
                        "type": "kafka",
                        "config": {
                            "topic_patterns": {"allow": [r"^imazon\..*$"]},
                            "dataset_pattern": "oops",
                        },
                    }
                }
            )
            is True
        )


# ── build_matcher_checked: degradation reason contract ────────────────────────


class TestBuildMatcherCheckedReason:
    """``(predicate, reason)`` — the reason is what separates the three step-2 outcomes.

    Spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes and prune
    invariant. The 'Not evaluated' row is reached when 'the recipe cannot be parsed at all;
    the deciding selection-pattern key is wrongly shaped; a declared pattern does not
    compile; or the acryl-datahub library supplying AllowDenyPattern semantics cannot be
    imported while the source declares patterns' — signalled by 'warning naming the source
    and what could not be read'. The 'Evaluated, no derivable patterns' row is not a
    degradation and therefore carries 'Signal: none'.

    The reason contract asserted here is: not None for a degradation, naming the offending
    ``source.config.<key>`` where one exists — the key name only, never the full message.

    The fourth Not-evaluated condition — 'the acryl-datahub library supplying
    AllowDenyPattern semantics cannot be imported while the source declares patterns' — is
    covered by ``test_absent_sdk_degrades_only_a_pattern_carrying_recipe``. The behaviour
    worth pinning there is not that the import fails but that its consequence is
    *conditional*: a no-SDK context degrades only a recipe declaring one of the four
    cascade keys, so it does not report every pattern-less source as a recipe defect.
    """

    @pytest.mark.parametrize(
        ("source_type", "key", "well_formed_value", "covered_name"),
        _CASCADE_KEY_CASES,
        ids=[case[1] for case in _CASCADE_KEY_CASES],
    )
    def test_wrongly_shaped_pattern_value_reports_the_offending_key(
        self,
        source_type: str,
        key: str,
        well_formed_value: dict[str, list[str]],
        covered_name: str,
    ) -> None:
        """A bare string where an allow/deny mapping belongs → reason naming that key.

        Run for every key of the cascade, because the contract is per-key: the reason must
        name the ``source.config`` key an operator has to go and fix, and a single-key test
        cannot tell a per-key reason apart from one hard-coded to ``schema_pattern``.

        spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — 'the
        deciding selection-pattern key is wrongly shaped' is the Not-evaluated outcome,
        warned with 'the source and the offending key named'.
        spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — the
        cascade order is 'schema_pattern → table_pattern → topic_patterns →
        dataset_pattern'.
        """
        matcher, reason = build_matcher_checked(
            {"source": {"type": source_type, "config": {key: "oops"}}}
        )
        assert reason is not None, (
            f"A wrongly-shaped {key} value is the Not-evaluated outcome and must carry a "
            "reason; without one the sweep would prune this source's stored rows. "
            "spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes."
        )
        assert key in reason, (
            f"The reason must name the offending source.config key {key!r}; got {reason!r}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — "
            "'warned with the source and the offending key named'."
        )
        # Degraded → match-nothing. The same name is covered by a well-formed value on the
        # same key below, so this False is the degradation and not an unmatchable name.
        assert matcher(covered_name) is False

        healthy, healthy_reason = build_matcher_checked(
            {"source": {"type": source_type, "config": {key: well_formed_value}}}
        )
        assert healthy_reason is None, (
            f"A well-formed {key} is the Evaluated outcome and carries no reason; got "
            f"{healthy_reason!r}."
        )
        assert healthy(covered_name) is True

    @pytest.mark.parametrize(
        ("source_type", "key", "well_formed_value", "covered_name"),
        _CASCADE_KEY_CASES,
        ids=[case[1] for case in _CASCADE_KEY_CASES],
    )
    def test_uncompilable_pattern_reports_the_offending_key(
        self,
        source_type: str,
        key: str,
        well_formed_value: dict[str, list[str]],
        covered_name: str,
    ) -> None:
        """A pattern string that fails re.compile → reason naming the declaring key.

        Run for every key of the cascade for the same reason as the wrongly-shaped case:
        each key has its own branch, and each branch has to report its own key.

        spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — 'a
        declared pattern does not compile' is the Not-evaluated outcome.
        spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Trust boundary on
        writer-supplied patterns — 'A malformed, wrongly-typed or uncompilable pattern is
        caught when the matcher is built, degrading that one source to the not-evaluated
        outcome with a log line rather than aborting the sweep.'
        """
        matcher, reason = build_matcher_checked(
            {
                "source": {
                    "type": source_type,
                    # '[' is an unterminated character class — not a compilable regex.
                    "config": {key: {"allow": ["["]}},
                }
            }
        )
        assert reason is not None, (
            f"An uncompilable {key} pattern must degrade the source to the Not-evaluated "
            "outcome with a reason, rather than raising from inside the returned predicate. "
            "spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Trust boundary."
        )
        assert key in reason, (
            f"The reason must name the offending source.config key {key!r}; got {reason!r}."
        )
        # Match-nothing, and it does not raise when called — the failure was moved to
        # build time. A compilable pattern on the same key covers the same name.
        assert matcher(covered_name) is False

        healthy, healthy_reason = build_matcher_checked(
            {"source": {"type": source_type, "config": {key: well_formed_value}}}
        )
        assert healthy_reason is None, (
            f"A compilable {key} pattern carries no reason; got {healthy_reason!r}."
        )
        assert healthy(covered_name) is True

    def test_unparseable_recipe_reports_a_reason(self) -> None:
        """A recipe that cannot be parsed at all → reason, so the caller must not prune.

        No ``source.config`` key can be named here — the recipe never got that far — so the
        contract is only that a reason is present and non-empty.

        spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — 'the
        recipe cannot be parsed at all' is the Not-evaluated outcome, whose stored matched
        rows are 'left in place'.
        """
        matcher, reason = build_matcher_checked({"broken": "recipe"})
        assert reason is not None and reason.strip() != "", (
            "An unreadable recipe is a failed read of an upstream fact, not evidence that "
            "the source covers nothing — it must carry a reason. "
            "spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes."
        )
        assert matcher("example_db.catalog.title_master") is False

    def test_empty_recipe_reports_a_reason(self) -> None:
        """``{}`` — what a failed read of DataHub's recipe string collapses to — degrades.

        spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — 'A
        DATAHUB_MANAGED recipe is mirrored from DataHub, so an unreadable recipe is a failed
        read of an upstream fact; pruning on it would delete mappings that are still true'.
        """
        matcher, reason = build_matcher_checked({})
        assert reason is not None and reason.strip() != ""
        assert matcher("example_db.catalog.title_master") is False

    def test_well_formed_recipe_with_no_patterns_reports_no_reason(self) -> None:
        """No selection-pattern key on a well-formed recipe → match-nothing, reason None.

        This is the 'Evaluated, no derivable patterns' outcome, whose stored rows are
        **pruned** and whose signal is 'none' — so it must be distinguishable from every
        degradation above by ``reason is None``.

        spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — 'the
        recipe is well-formed and carries none of the four selection-pattern keys | pruned |
        none'.
        """
        matcher, reason = build_matcher_checked(
            {"source": {"type": "postgres", "config": {"host_port": "pg:5432"}}}
        )
        assert reason is None, (
            f"A pattern-less well-formed recipe is the documented Evaluated outcome, not a "
            f"degradation; got reason {reason!r}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes."
        )
        assert matcher("example_db.catalog.title_master") is False
        assert matcher("anything") is False

    def test_absent_sdk_degrades_only_a_pattern_carrying_recipe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No importable SDK → Not-evaluated, but only where patterns are declared.

        The conditionality is the contract, not the import failure: the outcome table
        scopes this row to 'while the source declares patterns', so a context without the
        SDK must not reclassify every pattern-less source as a recipe defect. Both legs are
        asserted because either alone would pass an implementation that degraded
        unconditionally, or one that never degraded at all.

        spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — the
        Not-evaluated row lists 'the acryl-datahub library supplying AllowDenyPattern
        semantics cannot be imported while the source declares patterns'.
        """
        monkeypatch.setitem(sys.modules, "datahub.configuration.common", None)

        _declares, declares_reason = build_matcher_checked(
            {
                "source": {
                    "type": "postgres",
                    "config": {"schema_pattern": {"allow": ["^catalog$"]}},
                }
            }
        )
        assert declares_reason is not None, (
            "A source declaring patterns with no SDK to evaluate them is the Not-evaluated "
            "outcome — its stored mappings must not be pruned on absence of evidence. "
            "spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes."
        )

        _silent, silent_reason = build_matcher_checked(
            {"source": {"type": "postgres", "config": {"host_port": "pg:5432"}}}
        )
        assert silent_reason is None, (
            f"A pattern-less recipe declares no coverage to evaluate, so an absent SDK "
            f"leaves it in the Evaluated outcome; got reason {silent_reason!r}. Degrading "
            "it would freeze the prune for every such source in a no-SDK context."
        )


# ── Degradation reason: bounded and escaped before it reaches a log record ─────


class TestReasonIsBoundedAndEscapedBeforeLogging:
    """The degradation reason is writer-controlled text, so it is bounded and escaped.

    Scope: ``truncate_reason`` itself, plus ``build_matcher``'s convenience log — the
    record emitted for callers that *discard* the reason. The log line the spec sentence
    below describes is the **sweep's** ``ingestion_sync_pattern_not_derivable`` record,
    which needs a DB session and is pinned in ``tests/integration/spot/
    test_internal_activities.py::test_sync_degradation_log_is_bounded_and_escaped_and_
    the_counter_persists``. Both call sites carry the same obligation and neither
    substitutes for the other.

    spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Trust boundary on
    writer-supplied patterns — 'The reason that log line reports is derived from recipe
    content and is therefore itself untrusted, so it is bounded in length and escaped
    before it reaches a log record: a writer cannot forge log structure or grow a record
    without limit.'

    What is pinned is the invariant, not the arithmetic: the tests assert that a reason
    below the bound is passed through untouched, that the output length stops growing with
    the input, and that an emitted record carries neither a raw newline nor unbounded
    length. The exact limit value and the exact truncation-marker text are free to change.
    """

    def test_reason_below_the_bound_is_returned_unchanged(self) -> None:
        """Bounding must not corrupt the ordinary case: a short reason is passed through.

        spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Trust boundary — the reason
        is 'bounded in length'; bounding is a cap, not a rewrite.
        """
        short = "source.config.schema_pattern is not a usable allow/deny pattern set: oops"
        assert len(short) < MAX_REASON_CHARS, (
            "This fixture must sit below the bound for the assertion to mean anything; "
            f"len={len(short)} vs MAX_REASON_CHARS={MAX_REASON_CHARS}."
        )
        assert truncate_reason(short) == short

    def test_output_length_stops_growing_with_the_input(self) -> None:
        """A reason 1000x the bound produces the same bounded output as one 10x the bound.

        The writer controls the input length (a recipe pattern is arbitrary JSONB text), so
        the contract is that output length is decoupled from it — proven by feeding two
        inputs two orders of magnitude apart and asserting both outputs sit under one
        constant. ``_SLACK`` covers the truncation marker only; it does not scale with the
        input, which is the whole point.

        spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Trust boundary — 'a writer
        cannot … grow a record without limit'.
        """
        slack = 256  # room for the truncation marker; independent of input length
        sizes = (MAX_REASON_CHARS * 10, MAX_REASON_CHARS * 1000)
        outputs = [truncate_reason("x" * size) for size in sizes]
        for size, out in zip(sizes, outputs, strict=True):
            assert len(out) <= MAX_REASON_CHARS + slack, (
                f"A {size}-char reason must be bounded before logging; got {len(out)} chars. "
                "spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Trust boundary."
            )
        # Decoupling, stated directly: a 100x larger input does not make a larger record.
        assert abs(len(outputs[1]) - len(outputs[0])) <= slack

    def test_emitted_log_record_carries_no_raw_newline(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A reason containing a raw newline reaches the log record escaped, on one line.

        Injection: ``{"allow": "not-a-list"}`` makes ``AllowDenyPattern`` construction raise
        a multi-line pydantic validation error, so the reason genuinely contains ``\\n`` —
        asserted first, so the absence assertion on the record is not trivially true. A
        line-based log collector would otherwise read the tail of a writer-supplied recipe
        string as a forged record of its own.

        spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Trust boundary — the reason
        is 'escaped before it reaches a log record: a writer cannot forge log structure'.
        """
        recipe = {
            "source": {
                "type": "postgres",
                "config": {"schema_pattern": {"allow": "not-a-list"}},
            }
        }
        _, reason = build_matcher_checked(recipe)
        assert reason is not None and "\n" in reason, (
            "Backstop: this fixture must actually put a raw newline in the reason, or the "
            f"absence assertion below proves nothing. Got reason={reason!r}."
        )

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=_INGESTION_MODEL_LOGGER):
            build_matcher(recipe)

        records = [r for r in caplog.records if r.name == _INGESTION_MODEL_LOGGER]
        assert records, (
            "build_matcher must log the degradation it discards. "
            "spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Trust boundary — "
            "'degrading that one source … with a log line'."
        )
        for record in records:
            assert "\n" not in record.getMessage(), (
                f"A writer-supplied newline must be escaped, not carried into the record; "
                f"got {record.getMessage()!r}."
            )

    def test_emitted_log_record_is_length_bounded(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A recipe pattern hundreds of kB long produces a short log record.

        Injection: an invalid group name built from 200 000 characters, which ``re``
        quotes back verbatim in its error — asserted to exceed the bound before the record
        is inspected, so the boundedness assertion has a subject.

        spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Trust boundary — 'a writer
        cannot forge log structure or grow a record without limit'.
        """
        oversized_pattern = "(?P<a-" + "a" * 200_000 + ">x)"
        recipe = {
            "source": {
                "type": "postgres",
                "config": {"table_pattern": {"allow": [oversized_pattern]}},
            }
        }
        _, reason = build_matcher_checked(recipe)
        assert reason is not None and len(reason) > MAX_REASON_CHARS * 10, (
            "Backstop: this fixture must produce a reason far past the bound, or the "
            f"boundedness assertion below is vacuous. Got len={len(reason or '')}."
        )

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=_INGESTION_MODEL_LOGGER):
            build_matcher(recipe)

        records = [r for r in caplog.records if r.name == _INGESTION_MODEL_LOGGER]
        assert records, "build_matcher must log the degradation it discards."
        # Slack covers the record's own prefix, the truncation marker and repr quoting —
        # all fixed-size; none of it scales with the 200 000-char recipe input.
        slack = 256
        for record in records:
            assert len(record.getMessage()) <= MAX_REASON_CHARS + slack, (
                f"A 200 000-char recipe pattern must not produce a 200 000-char log "
                f"record; got {len(record.getMessage())} chars. "
                "spec: feature/BACKEND.md §Sync + mapping sweep step 2 §Trust boundary."
            )
