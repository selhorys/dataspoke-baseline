"""Display-link safety rule — Python side of the cross-language drift guard.

Spec anchor: ``spec/API.md`` §Data Resource → **Display-link safety**. That block
defines the rule as five classes (Scheme / Authority / Characters / Shape /
Length) and states it "is enforced at **both** boundaries. On write,
``PATCH /admin/peripherals/{datahub,langfuse}`` rejects a violating value with
``422``. On read, ``GET /spoke/common/peripheral-links`` coerces one to ``""``".

Those two boundaries are two *different regex engines* compiled from the same
pattern string, and a third copy lives in the frontend:

1. ``SAFE_DISPLAY_URL_PATTERN`` / ``SAFE_PROJECT_ID_PATTERN`` in
   ``src/api/schemas/common.py``, compiled by Python ``re`` and applied on read by
   ``sanitize_display_url`` / ``sanitize_project_id``.
2. The same constants handed to pydantic ``Field(pattern=...)`` on write and on
   ``PeripheralLinksResponse`` — compiled by pydantic's Rust regex engine.
3. ``src/frontend/lib/safe-url.ts``, compiled by the JavaScript engine, which
   ``spec/feature/FRONTEND_BASIC.md`` §Shell requires: "Both peripheral values are
   re-checked in the client against the display-link safety rule … The client
   check is not redundant with the API's: it also covers the env-sourced values."

This module asserts copies 1 and 2 against the shared corpus at
``tests/fixtures/safe-url-cases.json``; ``src/frontend/lib/safe-url.test.ts``
asserts copy 3 against the same file. Every corpus case carries a ``rule`` key
naming the spec rule-class row it derives from, and this module fails a case that
cites a row the spec does not define.

Spec traceability:
- spec/API.md §Data Resource → Display-link safety — the five rule classes and
  the two enforcement boundaries.
- spec/feature/FRONTEND_BASIC.md §Shell — the client re-check and its rationale.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.api.schemas.admin import (
    DatahubPeripheralPatchRequest,
    LangfusePeripheralPatchRequest,
)
from src.api.schemas.common import (
    SAFE_DISPLAY_URL_MAX_LENGTH,
    SAFE_PROJECT_ID_MAX_LENGTH,
    sanitize_display_url,
    sanitize_project_id,
)
from src.api.schemas.peripheral_links import PeripheralLinksResponse

# ── Corpus loading ────────────────────────────────────────────────────────────

CORPUS_PATH = Path(__file__).resolve().parents[3] / "fixtures" / "safe-url-cases.json"

_CORPUS: dict[str, Any] = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))

# The rule-class names are the rows of the spec's Display-link safety table.
RULE_CLASSES: dict[str, str] = _CORPUS["_rule_classes"]


def _cases(pattern: str, bucket: str) -> list[tuple[str, str, str]]:
    """Return ``(rule, label, value)`` triples for one corpus bucket."""
    return [(c["rule"], c["label"], c["value"]) for c in _CORPUS[pattern][bucket]]


def _ids(cases: list[tuple[str, str, str]]) -> list[str]:
    return [f"{rule}: {label}" for rule, label, _ in cases]


DISPLAY_ACCEPT = _cases("display_url", "accept")
DISPLAY_REJECT = _cases("display_url", "reject")
PROJECT_ACCEPT = _cases("project_id", "accept")
PROJECT_REJECT = _cases("project_id", "reject")


# ── Corpus integrity ──────────────────────────────────────────────────────────


def test_corpus_carries_both_sides_for_every_pattern() -> None:
    """The corpus seeds accepted and rejected values for both patterns.

    Without this the parametrized tests below could all pass against an empty or
    one-sided corpus — the "seed both sides" failure mode.

    spec: spec/TESTING.md §Assertion Discipline — "A test of a filter, query
        predicate, or matching rule must seed **both** rows that match and rows
        that do not".
    """
    assert len(DISPLAY_ACCEPT) >= 10, (
        f"display_url corpus must carry accepted values; got {len(DISPLAY_ACCEPT)}"
    )
    assert len(DISPLAY_REJECT) >= 40, (
        f"display_url corpus must carry rejected values; got {len(DISPLAY_REJECT)}"
    )
    assert len(PROJECT_ACCEPT) >= 5, (
        f"project_id corpus must carry accepted values; got {len(PROJECT_ACCEPT)}"
    )
    assert len(PROJECT_REJECT) >= 10, (
        f"project_id corpus must carry rejected values; got {len(PROJECT_REJECT)}"
    )


def test_every_case_cites_a_spec_rule_class() -> None:
    """Each corpus case names one of the spec's five rule-class rows.

    This is the anti-drift check on the corpus itself: a case that cannot cite a
    row of the Display-link safety table is asserting a property the spec does not
    grant, which is how a test ends up pinning the regex instead of the contract.

    spec: spec/API.md §Data Resource → Display-link safety — the rule-class table
        (Scheme / Authority / Characters / Shape / Length).
    """
    expected = {"Scheme", "Authority", "Characters", "Shape", "Length", "Slug"}
    assert set(RULE_CLASSES) == expected, (
        f"The corpus rule classes must match the spec table rows (plus the Slug "
        f"sub-rule of Length); got {sorted(RULE_CLASSES)}"
    )

    all_cases = DISPLAY_ACCEPT + DISPLAY_REJECT + PROJECT_ACCEPT + PROJECT_REJECT
    for rule, label, _value in all_cases:
        assert rule in RULE_CLASSES, (
            f"{label!r} cites rule class {rule!r}, which is not a row of the "
            f"Display-link safety table. Known rows: {sorted(RULE_CLASSES)}"
        )

    # Every rule class must actually be exercised — an unexercised row means the
    # spec grants a property nothing tests. `Length` is deliberately absent from
    # the corpus: a 512-character value is not reviewable in a fixture, so the
    # length bounds are exercised by the local boundary tests below (mirroring
    # src/frontend/lib/safe-url.test.ts §"length bounds"). Splitting `Slug` out of
    # `Length` is what stops the slug-grammar cases from reporting the length
    # bound as covered when nothing asserted it.
    corpus_exercised = {rule for rule, _, _ in all_cases}
    assert corpus_exercised == expected - {"Length"}, (
        f"Rule classes with no corpus case: {sorted(expected - {'Length'} - corpus_exercised)}"
    )


def test_shape_rule_is_documented_as_grammar_not_anti_spoofing() -> None:
    """Every Shape rejection has an accepted slash-introduced twin in the corpus.

    The spec is explicit that Shape "is a grammar constraint, not an anti-spoofing
    rule". A corpus that rejected ``https://evil.com?x=1`` without also accepting
    ``https://evil.com/?x=1`` would read as though the guard defends against host
    spoofing via query/fragment, which it does not — the effective host is
    ``evil.com`` in both. Pairing them keeps the corpus honest about what the rule
    is for.

    spec: spec/API.md §Data Resource → Display-link safety, Shape row — "A path,
        query, or fragment must be introduced by ``/``. This is a grammar
        constraint, not an anti-spoofing rule".
    """
    shape_rejects = [v for rule, _, v in DISPLAY_REJECT if rule == "Shape"]
    shape_accepts = [v for rule, _, v in DISPLAY_ACCEPT if rule == "Shape"]
    assert shape_rejects, "The corpus must exercise the Shape rejection side"

    for rejected in shape_rejects:
        introducers = [i for i in (rejected.find("?"), rejected.find("#")) if i != -1]
        assert introducers, f"Shape rejection {rejected!r} has no query/fragment introducer"
        idx = min(introducers)
        twin = rejected[:idx] + "/" + rejected[idx:]
        assert twin in shape_accepts, (
            f"Shape rejection {rejected!r} has no accepted slash-introduced twin "
            f"({twin!r}) in the corpus. Without it the case reads as an "
            f"anti-spoofing defence the guard does not have."
        )
        assert sanitize_display_url(twin) == twin, (
            f"{twin!r} must be accepted — the Shape rule is grammar, not anti-spoofing"
        )


# ── Copy 1: Python `re` on read ───────────────────────────────────────────────


@pytest.mark.parametrize(("rule", "label", "value"), DISPLAY_ACCEPT, ids=_ids(DISPLAY_ACCEPT))
def test_sanitize_display_url_accepts(rule: str, label: str, value: str) -> None:
    """A corpus-accepted display URL survives the read boundary verbatim.

    spec: spec/API.md §Data Resource → Display-link safety — the rule class named
        by this case's id; only a *violating* value is coerced on read.
    """
    assert sanitize_display_url(value) == value, (
        f"[{rule}] {label!r}: a conforming display URL must pass through unchanged"
    )


@pytest.mark.parametrize(("rule", "label", "value"), DISPLAY_REJECT, ids=_ids(DISPLAY_REJECT))
def test_sanitize_display_url_rejects(rule: str, label: str, value: str) -> None:
    """A corpus-rejected display URL is coerced to "" on read.

    spec: spec/API.md §Data Resource → Display-link safety — "On read,
        ``GET /spoke/common/peripheral-links`` coerces one to ``""`` …
        Degrading to ``""`` reuses the documented 'render no link' state".
    """
    assert sanitize_display_url(value) == "", (
        f"[{rule}] {label!r}: a violating display URL must be coerced to ''"
    )


@pytest.mark.parametrize(("rule", "label", "value"), PROJECT_ACCEPT, ids=_ids(PROJECT_ACCEPT))
def test_sanitize_project_id_accepts(rule: str, label: str, value: str) -> None:
    """A corpus-accepted project id survives the read boundary verbatim.

    spec: spec/API.md §Data Resource → Display-link safety, Length row —
        ``project_id`` "is further restricted to an alphanumeric slug" (the Slug
        sub-rule; the numeric bound is asserted in the length-bounds block below).
    """
    assert sanitize_project_id(value) == value, (
        f"[{rule}] {label!r}: a conforming project id must pass through unchanged"
    )


@pytest.mark.parametrize(("rule", "label", "value"), PROJECT_REJECT, ids=_ids(PROJECT_REJECT))
def test_sanitize_project_id_rejects(rule: str, label: str, value: str) -> None:
    """A corpus-rejected project id is coerced to "" on read.

    ``project_id`` "lands in a path segment" per the Display-link safety preamble,
    so a value that is not an alphanumeric slug must not reach the deep-link.

    spec: spec/API.md §Data Resource → Display-link safety, Length row.
    """
    assert sanitize_project_id(value) == "", (
        f"[{rule}] {label!r}: a violating project id must be coerced to ''"
    )


# ── Copy 2: pydantic's Rust regex engine on write ─────────────────────────────
#
# The same pattern string, a different engine. Not redundant with the block above:
# `re` and the Rust regex crate differ on `$` anchoring and on Unicode class
# membership, which is exactly the drift this file guards.


@pytest.mark.parametrize(("rule", "label", "value"), DISPLAY_ACCEPT, ids=_ids(DISPLAY_ACCEPT))
def test_patch_request_accepts_display_url(rule: str, label: str, value: str) -> None:
    """A conforming display URL is admitted by the admin PATCH schema.

    Covers both display-URL-typed request fields — the DataHub browser URL and the
    Langfuse host, which the spec names together as the values "that clients
    interpolate into a browser ``href``".

    spec: spec/API.md §Data Resource → Display-link safety — "DataHub
        ``frontend_url``, Langfuse ``host``".
    """
    assert DatahubPeripheralPatchRequest(frontend_url=value).frontend_url == value, (
        f"[{rule}] {label!r}: PATCH must admit a conforming frontend_url"
    )
    assert LangfusePeripheralPatchRequest(host=value).host == value, (
        f"[{rule}] {label!r}: PATCH must admit a conforming Langfuse host"
    )


@pytest.mark.parametrize(("rule", "label", "value"), DISPLAY_REJECT, ids=_ids(DISPLAY_REJECT))
def test_patch_request_rejects_display_url(rule: str, label: str, value: str) -> None:
    """A violating display URL is refused at the write boundary.

    spec: spec/API.md §Data Resource → Display-link safety — "On write,
        ``PATCH /admin/peripherals/{datahub,langfuse}`` rejects a violating value
        with ``422``". (The 422 status itself is asserted over HTTP in
        ``tests/integration/spot/test_peripheral_links.py``; here it is the schema
        that must refuse.)
    """
    with pytest.raises(ValidationError):
        DatahubPeripheralPatchRequest(frontend_url=value)
    with pytest.raises(ValidationError):
        LangfusePeripheralPatchRequest(host=value)


@pytest.mark.parametrize(("rule", "label", "value"), PROJECT_ACCEPT, ids=_ids(PROJECT_ACCEPT))
def test_patch_request_accepts_project_id(rule: str, label: str, value: str) -> None:
    """A conforming project id is admitted by the Langfuse PATCH schema.

    spec: spec/API.md §Data Resource → Display-link safety, Length row.
    """
    assert LangfusePeripheralPatchRequest(project_id=value).project_id == value, (
        f"[{rule}] {label!r}: PATCH must admit a conforming project_id"
    )


@pytest.mark.parametrize(("rule", "label", "value"), PROJECT_REJECT, ids=_ids(PROJECT_REJECT))
def test_patch_request_rejects_project_id(rule: str, label: str, value: str) -> None:
    """A violating project id is refused at the write boundary.

    spec: spec/API.md §Data Resource → Display-link safety — write boundary;
        Length row.
    """
    with pytest.raises(ValidationError):
        LangfusePeripheralPatchRequest(project_id=value)


# ── The response model is the backstop on the read path ───────────────────────


@pytest.mark.parametrize(("rule", "label", "value"), DISPLAY_REJECT, ids=_ids(DISPLAY_REJECT))
def test_peripheral_links_response_rejects_unsafe_display_url(
    rule: str, label: str, value: str
) -> None:
    """``PeripheralLinksResponse`` refuses to serialize a violating display URL.

    The router already coerces violating values before constructing the model;
    this asserts the model would still refuse if that coercion were removed, so
    the read-boundary guarantee does not rest on a single call site.

    spec: spec/API.md §Data Resource → Display-link safety — the read boundary is
        a property of ``GET /spoke/common/peripheral-links``, not of one function.
    """
    with pytest.raises(ValidationError):
        PeripheralLinksResponse(datahub_url=value)
    with pytest.raises(ValidationError):
        PeripheralLinksResponse(langfuse_url=value)


@pytest.mark.parametrize(("rule", "label", "value"), PROJECT_REJECT, ids=_ids(PROJECT_REJECT))
def test_peripheral_links_response_rejects_unsafe_project_id(
    rule: str, label: str, value: str
) -> None:
    """``PeripheralLinksResponse`` refuses to serialize a violating project id.

    spec: spec/API.md §Data Resource → Display-link safety — read boundary.
    """
    with pytest.raises(ValidationError):
        PeripheralLinksResponse(langfuse_project_id=value)


# ── Engine agreement ──────────────────────────────────────────────────────────


def test_no_engine_divergence_remains() -> None:
    """The corpus records no disagreement between the three regex copies.

    This asserts the state of the *fixture*, not of production code — a populated
    ``engine_divergence`` bucket would mean someone recorded a known disagreement
    rather than fixing it. The production-behaviour guarantee comes from the
    parametrized blocks above (Python) and from ``src/frontend/lib/safe-url.test.ts``
    (JavaScript), which assert the same corpus cases; a one-sided regex edit fails
    one of those, not this.

    The bucket is kept in the schema as a documented home for a future
    disagreement — see the corpus ``_readme`` for the shape an entry must take.
    """
    assert _CORPUS["display_url"]["engine_divergence"] == [], (
        "display_url.engine_divergence must be empty — the three regex copies are "
        f"expected to agree on every corpus case. Recorded: "
        f"{_CORPUS['display_url']['engine_divergence']}"
    )
    assert _CORPUS["project_id"]["engine_divergence"] == [], (
        "project_id.engine_divergence must be empty. Recorded: "
        f"{_CORPUS['project_id']['engine_divergence']}"
    )


@pytest.mark.parametrize(
    "codepoint",
    [
        0x00, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x1F,  # C0 controls
        0x20, 0xA0, 0x1680, 0x2028, 0x2029, 0x3000,  # whitespace
        0x85, 0xFEFF,  # the two engine-definition edge cases
        0x200E, 0x200F, 0x202A, 0x202E, 0x2066, 0x2069,  # bidi overrides
    ],
)
def test_read_and_write_engines_agree_on_every_barred_codepoint(codepoint: int) -> None:
    """Python ``re`` and pydantic's Rust engine answer identically for each barred char.

    Property form of the corpus: rather than listing values, this sweeps the
    alphabet the Characters rule names (C0 controls, whitespace, bidi overrides)
    and asserts both Python engines bar each one *and* agree with each other. The
    U+FEFF / U+0085 divergence that the corpus now carries as ordinary reject
    cases would have been caught here automatically.

    spec: spec/API.md §Data Resource → Display-link safety, Characters row — "No
        whitespace, no C0 control characters, and no unicode bidi-override
        characters anywhere in the value".
    """
    value = "https://evil" + chr(codepoint) + ".example.com"

    read_accepts = sanitize_display_url(value) == value
    try:
        DatahubPeripheralPatchRequest(frontend_url=value)
        write_accepts = True
    except ValidationError:
        write_accepts = False

    assert read_accepts == write_accepts, (
        f"U+{codepoint:04X}: the read boundary (Python re) and the write boundary "
        f"(Rust regex) disagree — read_accepts={read_accepts}, "
        f"write_accepts={write_accepts}. Same pattern string, different engines."
    )
    assert not read_accepts, (
        f"U+{codepoint:04X} must be barred anywhere in a display URL. "
        f"spec: spec/API.md §Data Resource → Display-link safety, Characters row."
    )


@pytest.mark.parametrize(
    ("_rule", "_label", "value"),
    DISPLAY_ACCEPT + DISPLAY_REJECT,
    ids=_ids(DISPLAY_ACCEPT + DISPLAY_REJECT),
)
def test_sanitize_display_url_is_total_and_idempotent(
    _rule: str, _label: str, value: str
) -> None:
    """Sanitizing yields either the input or "", and sanitizing twice changes nothing.

    The spec offers exactly two read-boundary outcomes — the value, or ``""``
    ("coerces one to ``""``"). A sanitizer that returned a *modified* URL (say, a
    stripped one) would satisfy every accept/reject case above while silently
    rewriting an operator's configured host.

    spec: spec/API.md §Data Resource → Display-link safety — the read boundary
        coerces to ``""``; it does not repair.
    """
    once = sanitize_display_url(value)
    assert once in {"", value}, (
        f"sanitize_display_url must return the input or '', never a rewritten "
        f"value; got {once!r} for {value!r}"
    )
    assert sanitize_display_url(once) == once, "sanitize_display_url must be idempotent"


# ── Length rule ───────────────────────────────────────────────────────────────
#
# Local rather than corpus-driven, mirroring the same decision in
# src/frontend/lib/safe-url.test.ts §"length bounds": the corpus stores values
# verbatim and a 512-character string is not reviewable in a fixture. Both
# boundaries of both bounds are asserted, at both enforcement points.


@pytest.mark.parametrize("boundary", ["at_limit", "over_limit"])
def test_display_url_length_bound(boundary: str) -> None:
    """A display URL is accepted at 512 characters and coerced one character over.

    Both sides of the bound are asserted: an at-limit case alone would pass
    against a sanitizer with no length check at all.

    spec: spec/API.md §Data Resource → Display-link safety, Length row —
        "Bounded — 512 characters for a URL".
    """
    prefix = "https://e.example.com/"
    filler = SAFE_DISPLAY_URL_MAX_LENGTH - len(prefix) + (1 if boundary == "over_limit" else 0)
    value = prefix + "a" * filler

    if boundary == "at_limit":
        assert len(value) == SAFE_DISPLAY_URL_MAX_LENGTH
        assert sanitize_display_url(value) == value, (
            "A display URL exactly at the 512-character bound must be accepted"
        )
        assert DatahubPeripheralPatchRequest(frontend_url=value).frontend_url == value, (
            "The write boundary must admit a display URL exactly at the bound"
        )
    else:
        assert len(value) == SAFE_DISPLAY_URL_MAX_LENGTH + 1
        assert sanitize_display_url(value) == "", (
            "A display URL one character over the 512-character bound must be coerced to ''"
        )
        with pytest.raises(ValidationError):
            DatahubPeripheralPatchRequest(frontend_url=value)


@pytest.mark.parametrize("boundary", ["at_limit", "over_limit"])
def test_project_id_length_bound(boundary: str) -> None:
    """A project id is accepted at 256 characters and coerced one character over.

    spec: spec/API.md §Data Resource → Display-link safety, Length row — "256 for
        ``project_id``".
    """
    length = SAFE_PROJECT_ID_MAX_LENGTH + (1 if boundary == "over_limit" else 0)
    value = "a" * length

    if boundary == "at_limit":
        assert sanitize_project_id(value) == value, (
            "A project id exactly at the 256-character bound must be accepted"
        )
        assert LangfusePeripheralPatchRequest(project_id=value).project_id == value, (
            "The write boundary must admit a project id exactly at the bound"
        )
    else:
        assert sanitize_project_id(value) == "", (
            "A project id one character over the 256-character bound must be coerced to ''"
        )
        with pytest.raises(ValidationError):
            LangfusePeripheralPatchRequest(project_id=value)
