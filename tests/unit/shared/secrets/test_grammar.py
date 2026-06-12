"""Unit tests for src/shared/secrets/grammar.py.

Covers parse_name_key and SECRET_REF_RE. Assertions derive from:
  spec/feature/SECRET_RESOLUTION.md §Run-time resolve flow — 'split on last __'
  spec/feature/SECRET_RESOLUTION.md §Name prefix policy — '<name> cannot contain __ …
    the reference parser splits unambiguously on the last __'
  spec/feature/SECRET_RESOLUTION.md §Error taxonomy — SecretRefMalformed cases
"""

from __future__ import annotations

import pytest

from src.shared.secrets.grammar import SECRET_REF_RE, parse_name_key
from src.shared.secrets.interface import SecretRefMalformed

# ── parse_name_key ─────────────────────────────────────────────────────────────


class TestParseNameKey:
    """Spec: SECRET_RESOLUTION.md §Run-time resolve flow — split on last __."""

    def test_simple_valid_ref_splits_correctly(self) -> None:
        """'team-pg__password' splits into name='team-pg', key='password'.

        Spec: SECRET_RESOLUTION.md §Reference syntax — '${name__key}' resolves to
        Secret 'dataspoke-source-cred-<name>', data key '<key>'. name is a
        DNS-label-safe token (lowercase alphanumerics and hyphens).
        """
        name, key = parse_name_key("team-pg__password")
        assert name == "team-pg", f"Expected name='team-pg', got {name!r}"
        assert key == "password", f"Expected key='password', got {key!r}"

    def test_split_uses_last_double_underscore(self) -> None:
        """When the key segment itself contains __, the LAST __ is the name/key split point.

        Spec: SECRET_RESOLUTION.md §Name prefix policy — '<name> is [a-z0-9-]+, cannot
        contain __; the reference parser splits unambiguously on the last __'. A name
        containing __ cannot arise from a regex-matched token (SECRET_REF_RE enforces
        [a-z0-9-]+ for the name group). This test exercises parse_name_key in isolation
        to verify the last-__ split rule using a spec-legal input shape where the extra
        underscores appear in the key segment (key is [A-Za-z0-9_.-]+).

        We assert only that key == the segment AFTER the last __, not an exact name value,
        because the input "team-pg__some__key" is not a regex-matched token (its key
        segment contains __), and parse_name_key's contract is only to split on last __.
        """
        _, key = parse_name_key("team-pg__some__key")
        assert key == "key", (
            f"Expected key='key' (segment after last __), got {key!r}. "
            "parse_name_key must split on the LAST __, not the first."
        )

    def test_key_may_contain_dots_and_underscores(self) -> None:
        """Key segment may contain dots and underscores per the spec pattern [A-Za-z0-9_.-]+."""
        name, key = parse_name_key("team-pg__ssl.cert_v2")
        assert name == "team-pg"
        assert key == "ssl.cert_v2"

    def test_no_double_underscore_raises_malformed(self) -> None:
        """Ref without __ raises SecretRefMalformed.

        Spec: SECRET_RESOLUTION.md §Error taxonomy — 'SecretRefMalformed: no __
        or empty segment → 422 SECRET_REF_MALFORMED'.
        """
        with pytest.raises(SecretRefMalformed):
            parse_name_key("team-pg-password")

    def test_empty_name_segment_raises_malformed(self) -> None:
        """'__key' with empty name segment raises SecretRefMalformed.

        Spec: SECRET_RESOLUTION.md §Error taxonomy — empty segment.
        """
        with pytest.raises(SecretRefMalformed):
            parse_name_key("__password")

    def test_empty_key_segment_raises_malformed(self) -> None:
        """'name__' with empty key segment raises SecretRefMalformed.

        Spec: SECRET_RESOLUTION.md §Error taxonomy — empty segment.
        """
        with pytest.raises(SecretRefMalformed):
            parse_name_key("team-pg__")

    def test_malformed_message_names_the_ref(self) -> None:
        """SecretRefMalformed message includes the bad ref so callers can surface it.

        Spec: SECRET_RESOLUTION.md §Error taxonomy — detail includes which ref.
        """
        with pytest.raises(SecretRefMalformed, match="no-double-score"):
            parse_name_key("no-double-score")

    def test_empty_name_message_names_the_ref(self) -> None:
        """SecretRefMalformed message for empty-name case includes the bad ref."""
        with pytest.raises(SecretRefMalformed, match="__pw"):
            parse_name_key("__pw")

    def test_empty_key_message_names_the_ref(self) -> None:
        """SecretRefMalformed message for empty-key case includes the bad ref."""
        with pytest.raises(SecretRefMalformed, match="team__"):
            parse_name_key("team__")


# ── SECRET_REF_RE ──────────────────────────────────────────────────────────────


class TestSecretRefRe:
    """Spec: SECRET_RESOLUTION.md §Reference syntax — the ${name__key} grammar."""

    def test_matches_valid_ref_with_lowercase_name(self) -> None:
        """Pattern matches ${team-pg__password} — DNS-label-safe name."""
        m = SECRET_REF_RE.search("${team-pg__password}")
        assert m is not None
        assert m.group(1) == "team-pg"
        assert m.group(2) == "password"

    def test_does_not_match_ref_without_double_underscore(self) -> None:
        """${ENVIRONMENT} has no __ — not a secret ref, not matched.

        Spec: SECRET_RESOLUTION.md — tokens without __ are silently skipped.
        """
        m = SECRET_REF_RE.search("${ENVIRONMENT}")
        assert m is None

    def test_does_not_match_uppercase_name_segment(self) -> None:
        """${UPPER__key} is not matched — name segment must be [a-z0-9-]+."""
        m = SECRET_REF_RE.search("${UPPER__key}")
        assert m is None

    def test_matches_numeric_name_segment(self) -> None:
        """Name segment may contain digits: ${db01__pw}."""
        m = SECRET_REF_RE.search("${db01__pw}")
        assert m is not None
        assert m.group(1) == "db01"
        assert m.group(2) == "pw"

    def test_matches_key_with_dot_and_underscore(self) -> None:
        """Key segment may contain dots and underscores: ${pg__ssl.cert_v2}."""
        m = SECRET_REF_RE.search("${pg__ssl.cert_v2}")
        assert m is not None
        assert m.group(2) == "ssl.cert_v2"

    def test_finds_multiple_refs_in_string(self) -> None:
        """findall finds all ${name__key} tokens in one string."""
        text = "start ${team-pg__password} middle ${kafka__api_key} end"
        matches = SECRET_REF_RE.findall(text)
        assert len(matches) == 2
        names = [m[0] for m in matches]
        keys = [m[1] for m in matches]
        assert "team-pg" in names
        assert "kafka" in names
        assert "password" in keys
        assert "api_key" in keys


# ── Cross-module identity guarantee ───────────────────────────────────────────


class TestPatternIdentityGuarantee:
    """The save-time pattern (extract_secret_refs) and run-time pattern (resolver
    substitution) are the same compiled object — not just equal, but identical.

    Spec: SECRET_RESOLUTION.md §Design — 'One grammar definition: … both the resolver
    substitution and extract_secret_refs import it.'
    """

    def test_ingestion_model_uses_same_compiled_pattern_object_as_grammar(self) -> None:
        """src.shared.models.ingestion._collect_refs uses SECRET_REF_RE from grammar.py.

        The models module imports SECRET_REF_RE from src.shared.secrets.grammar; this
        test verifies the import is the same object (by identity) as the one exported
        from grammar.py directly — ensuring the extract set == the substitute set
        by construction.
        """
        import src.shared.models.ingestion as _ingestion_module

        # The models module exposes the pattern it uses via _collect_refs internals.
        # We verify identity by checking both sides point to the same id().
        from src.shared.secrets.grammar import SECRET_REF_RE as grammar_re

        # Retrieve the pattern the models module actually imported.
        models_re = _ingestion_module.SECRET_REF_RE  # type: ignore[attr-defined]
        assert models_re is grammar_re, (
            "src.shared.models.ingestion.SECRET_REF_RE is NOT the same compiled object "
            "as src.shared.secrets.grammar.SECRET_REF_RE. "
            "The save-time extraction set and run-time substitution set may diverge."
        )
