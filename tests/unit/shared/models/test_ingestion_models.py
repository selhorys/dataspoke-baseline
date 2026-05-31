"""Unit tests for src/shared/models/ingestion.py — pure recipe helpers.

Covers parse_recipe, extract_secret_refs, cron_to_tier, and build_matcher.
All tests are pure (no DB, no network, no LLM) — these are stable spec invariants.

Spec: spec/feature/BACKEND.md §Ingestion Service
Spec: spec/feature/BACKEND_SCHEMA.md §ingestion_source
Spec: spec/feature/SECRET_RESOLUTION.md §Reference syntax
Spec: spec/USE_CASE_en.md §UC1
"""

from __future__ import annotations

import pytest

from src.shared.models.ingestion import (
    CRON_TO_TIER,
    Mode,
    build_matcher,
    cron_to_tier,
    extract_secret_refs,
    parse_recipe,
)


# ── Mode enum ─────────────────────────────────────────────────────────────────


class TestModeEnum:
    def test_three_modes_defined(self) -> None:
        """Spec: BACKEND.md §Ingestion Service — three modes DATAHUB_MANAGED, ACTIVE_CUSTOM_MANAGED, PASSIVE.

        Spec: spec/feature/BACKEND_SCHEMA.md §ingestion_source — mode column.
        """
        assert Mode.DATAHUB_MANAGED.value == "DATAHUB_MANAGED"
        assert Mode.ACTIVE_CUSTOM_MANAGED.value == "ACTIVE_CUSTOM_MANAGED"
        assert Mode.PASSIVE.value == "PASSIVE"

    def test_mode_is_str_enum(self) -> None:
        """Mode values are strings (str-compatible enum), not raw enum objects.

        Use .value for comparison — str(Mode.X) includes the class name in Python 3.11+.
        """
        assert Mode.ACTIVE_CUSTOM_MANAGED.value == "ACTIVE_CUSTOM_MANAGED"
        # Mode(str) — value-equality with literal strings
        assert Mode.ACTIVE_CUSTOM_MANAGED == "ACTIVE_CUSTOM_MANAGED"


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
        """${name__key} tokens with __ are returned.

        Spec: SECRET_RESOLUTION.md — '${name__key}' is the secret reference syntax.
        """
        recipe = {
            "source": {
                "type": "postgres",
                "config": {"password": "${team_pg__password}"},
            }
        }
        refs = extract_secret_refs(recipe)
        assert refs == {"team_pg__password"}

    def test_ignores_plain_var_without_double_underscore(self) -> None:
        """${plainvar} without __ is not a secret ref and must be ignored.

        Spec: SECRET_RESOLUTION.md §Reference syntax — 'Only tokens whose inner text
        contains __ are returned; tokens without __ are silently skipped.'
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
                    "ssl": {"cert": "${team_pg__ssl_cert}"},
                    "password": "${team_pg__password}",
                },
            }
        }
        refs = extract_secret_refs(recipe)
        assert refs == {"team_pg__ssl_cert", "team_pg__password"}

    def test_recurses_lists(self) -> None:
        """Lists of strings containing ${ref} are also scanned."""
        recipe = {
            "source": {
                "type": "kafka",
                "config": {
                    "extra_secrets": ["${kafka_cred__api_key}", "${kafka_cred__api_secret}"],
                },
            }
        }
        refs = extract_secret_refs(recipe)
        assert refs == {"kafka_cred__api_key", "kafka_cred__api_secret"}

    def test_deduplicates_same_ref_used_twice(self) -> None:
        """Same ${name__key} used in two places appears once in the output set."""
        recipe = {
            "source": {
                "type": "postgres",
                "config": {
                    "password": "${team_pg__password}",
                    "backup_password": "${team_pg__password}",
                },
            }
        }
        refs = extract_secret_refs(recipe)
        assert refs == {"team_pg__password"}

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

    def test_ref_with_multiple_double_underscores_in_name(self) -> None:
        """${a__b__c} uses last __ as split point — returns 'a__b__c' as the full ref token.

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

    def test_no_patterns_is_allow_all(self) -> None:
        """No patterns declared → allow all (default).

        Spec: BACKEND.md — 'If none of the above keys are found … defaults to allow_all'.
        """
        recipe = {
            "source": {
                "type": "postgres",
                "config": {"host_port": "pg:5432"},
            }
        }
        matcher = build_matcher(recipe)
        assert matcher("any.schema.table") is True
        assert matcher("example_db.catalog.orders") is True

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
        # Schema "orders" matches allow=["^orders$"] — the table name is irrelevant for schema_pattern
        # Note: schema_pattern deny applies to schema segment only; "orders" != "internal"
        assert matcher("db.orders.internal_audit") is True  # schema "orders" is allowed; deny applies to schema not table
        # Schema "catalog" does not match allow=["^orders$"] → False
        assert matcher("db.catalog.title_master") is False

    def test_malformed_recipe_returns_match_nothing_predicate(self) -> None:
        """Malformed recipe (missing source) → predicate that returns False for all inputs."""
        matcher = build_matcher({"broken": "recipe"})
        assert matcher("any.table") is False
