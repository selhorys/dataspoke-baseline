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
        """Spec: BACKEND.md §Ingestion Service — three modes DATAHUB_MANAGED,
        ACTIVE_CUSTOM_MANAGED, PASSIVE.

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

        Spec: SECRET_RESOLUTION.md §Reference syntax — the pattern requires an explicit
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
    unit tests and test_uc1_datahub_managed.py for that layer.

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
