"""``datahub_gms_url`` is described identically across the plugin spec and the access skill.

``datahub_gms_url`` (env ``DATAHUB_GMS_URL``) is the DataHub GMS **origin** — scheme +
host[:port], no path.  The ``plugin/bin/datahub-graphql`` helper owns the endpoint suffix, so
neither the spec nor the skill may bake a path into the example value or describe the helper as
posting to ``<datahub_gms_url>/graphql``.  Before this check the spec's JSON example, the skill's
JSON example, and the helper each implied a different shape, and nothing under ``tests/`` would
have noticed a recurrence.

Spec: spec/AI_PLUGIN.md §Credential Model (resolved-config JSON block) and §Optional DataHub
access ("``datahub_gms_url`` is the GMS **origin** with no path component").
Spec: spec/DATAHUB_INTEGRATION.md §Test / dev tooling (GMS has its own ingress host, no path
prefix) — the origin-only convention this test keeps the plugin docs aligned with.
Spec: spec/TESTING.md §Unit Testing → Scope — pure file reads under the repo root; no network,
no dev cluster, no database.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).parents[3]
SPEC = ROOT / "spec" / "AI_PLUGIN.md"
SKILL = ROOT / "plugin" / "skills" / "dataspoke-access" / "SKILL.md"
PLUGIN_DIR = ROOT / "plugin"

_JSON_BLOCK = re.compile(r"```json\n(.*?)```", re.DOTALL)
_GMS_LINE = re.compile(r'"datahub_gms_url"\s*:\s*"([^"]*)"')
_FORBIDDEN = "<datahub_gms_url>/graphql"
# SKILL.md step 3: "collect the DataHub GMS origin (no path;\n   e.g. `https://...`)".
_STEP3_EXAMPLE = re.compile(r"GMS origin[^`]*?e\.g\.\s*`([^`]+)`", re.DOTALL)
# A DataHub-ish example URL carrying a /gms or /api/gms path segment.
_GMS_PATH_URL = re.compile(r"https?://datahub[\w.-]*/(?:api/)?gms\b")


def _documented_gms_url(path: Path) -> str:
    """Return the ``datahub_gms_url`` value from the first JSON block that carries the key.

    Only that key is parsed so the check does not couple to other keys in the same block.
    """
    text = path.read_text(encoding="utf-8")
    values = [
        match.group(1)
        for block in _JSON_BLOCK.findall(text)
        for match in [_GMS_LINE.search(block)]
        if match
    ]
    assert values, f"no JSON block with a datahub_gms_url key found in {path}"
    return values[0]


def test_spec_and_skill_document_the_same_gms_url() -> None:
    """The spec's config example and the skill's config example agree on the value."""
    assert _documented_gms_url(SPEC) == _documented_gms_url(SKILL)


def test_documented_gms_url_is_origin_only() -> None:
    """The example value has no path: the helper, not the config, owns ``/api/graphql``."""
    for path in (SPEC, SKILL):
        value = _documented_gms_url(path)
        parsed = urlparse(value)
        assert parsed.scheme in ("http", "https"), f"{path}: {value!r} lacks an http(s) scheme"
        assert parsed.netloc, f"{path}: {value!r} lacks a host"
        assert parsed.path in ("", "/"), f"{path}: {value!r} carries a path component"
        assert not parsed.query and not parsed.fragment, f"{path}: {value!r} is not an origin"


def test_no_doc_describes_helper_posting_to_gms_url_graphql() -> None:
    """No plugin file or the plugin spec describes the endpoint as ``<datahub_gms_url>/graphql``."""
    candidates = [SPEC, *sorted(p for p in PLUGIN_DIR.rglob("*") if p.is_file())]
    scanned_text_files = 0
    offenders = []
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        scanned_text_files += 1
        if _FORBIDDEN in text:
            offenders.append(str(path.relative_to(ROOT)))
    # Backstop: the scan actually covered the spec, the skill, and the helper script.
    assert scanned_text_files > 2
    assert SKILL in candidates and (PLUGIN_DIR / "bin" / "datahub-graphql") in candidates
    assert not offenders, f"stale {_FORBIDDEN!r} wording in: {offenders}"


def test_skill_step3_prompt_example_matches_documented_gms_url() -> None:
    """The prose ``e.g. <url>`` example in SKILL.md step 3 equals the JSON-block value."""
    text = SKILL.read_text(encoding="utf-8")
    expected = _documented_gms_url(SKILL)
    # The step 3 prompt example is the backticked URL following ``e.g.`` after the GMS origin ask.
    examples = _STEP3_EXAMPLE.findall(text)
    # Backstop: the step 3 example was actually found, so the equality below is not vacuous.
    assert examples, f"no step 3 GMS origin example found in {SKILL}"
    for example in examples:
        assert example == expected, f"step 3 example {example!r} != config value {expected!r}"


def test_no_plugin_doc_uses_a_gms_path_suffixed_example() -> None:
    """No plugin file or the plugin spec shows a ``.../gms`` or ``.../api/gms`` style URL."""
    candidates = [SPEC, *sorted(p for p in PLUGIN_DIR.rglob("*") if p.is_file())]
    offenders = []
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if _GMS_PATH_URL.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert SKILL in candidates, "scan did not cover the access skill"
    assert not offenders, f"path-suffixed GMS example URL in: {offenders}"


def test_documented_json_blocks_are_valid_json() -> None:
    """Backstop for the regex extraction: the blocks it reads parse as JSON objects."""
    for path in (SPEC, SKILL):
        text = path.read_text(encoding="utf-8")
        blocks = [b for b in _JSON_BLOCK.findall(text) if "datahub_gms_url" in b]
        assert blocks, f"{path}: expected a JSON block carrying datahub_gms_url"
        for block in blocks:
            assert isinstance(json.loads(block), dict)
