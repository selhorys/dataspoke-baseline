"""Unit tests for the ontogen prompt builder (prompts.py).

Tests the spec-mandated structural elements of build_run_prompt:
- Seeds section is included when seeds_md is non-empty; omitted when blank.
- One-shot section is included when provided; omitted when None.
- Evidence section is included when evidence is non-empty; absent when empty.
- Each dataset's URN appears in the evidence section when evidence is provided.

spec: feature/BACKEND.md §Ontology Generation Service §Inference Pipeline
      — seeds, one-shot, and per-dataset evidence (with URN) are spec-mandated inputs;
        LLM output must contain nodes/edges/triples (output contract, not prompt template).
"""

from src.backend.ontogen.prompts import build_run_prompt


_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.catalog.title_master,DEV)"
_NONCE = "abc12345"


def _minimal_evidence() -> dict:
    return {
        _DATASET_URN: {
            "dataset_name": "title_master",
            "description": "Master title catalog",
            "platform": "postgres",
            "schema_fields": [
                {"fieldPath": "isbn", "nativeDataType": "VARCHAR", "description": "ISBN code"},
            ],
            "tags": [],
            "glossary_terms": [],
            "upstream_urns": [],
            "queries": [],
        }
    }


# ── Seeds section ──────────────────────────────────────────────────────────────


def test_prompt_includes_seeds_when_provided() -> None:
    """Seeds content appears in the prompt when seeds_md is non-empty.

    spec: feature/BACKEND.md §Ontology Generation Service §Inference Pipeline
          — domain context (seeds) must appear in the prompt when provided.
    """
    seed_text = "Title is a published work."
    prompt = build_run_prompt(
        seeds_md=f"## Domain: Books\n\nKey concept: {seed_text}",
        evidence_per_dataset={},
        one_shot=None,
        nonce=_NONCE,
    )
    assert seed_text in prompt, "Seed content must appear in the prompt when provided."


def test_prompt_omits_seeds_content_when_empty() -> None:
    """Seed content is absent from the prompt when seeds_md is blank.

    Differential check: the same characterizing seed content that appears in the
    prompt when seeds_md is non-empty must be absent when seeds_md is blank.

    spec: feature/BACKEND.md §Ontology Generation Service §Inference Pipeline
          — seeds are conditionally included; when no seeds are active, no seed
          content leaks into the prompt.
    """
    seed_text = "Title is a published work."
    with_seeds = build_run_prompt(
        seeds_md=f"## Domain: Books\n\nKey concept: {seed_text}",
        evidence_per_dataset={},
        one_shot=None,
        nonce=_NONCE,
    )
    without_seeds = build_run_prompt(
        seeds_md="",
        evidence_per_dataset={},
        one_shot=None,
        nonce=_NONCE,
    )
    assert seed_text in with_seeds
    assert seed_text not in without_seeds, (
        "Seed content must not appear in the prompt when seeds_md is blank."
    )


# ── One-shot section ──────────────────────────────────────────────────────────


def test_prompt_includes_one_shot_when_provided() -> None:
    """One-shot section is included when one_shot is non-empty.

    spec: feature/BACKEND.md §Ontology Generation Service §Inference Pipeline
          — additional instructions appended when one_shot is set.
    """
    one_shot = "Focus only on customer-facing concepts."
    prompt = build_run_prompt(
        seeds_md="",
        evidence_per_dataset={},
        one_shot=one_shot,
        nonce=_NONCE,
    )
    assert one_shot in prompt, (
        "One-shot instruction must appear in prompt when provided."
    )


def test_prompt_omits_one_shot_content_when_none() -> None:
    """One-shot content is absent from the prompt when one_shot is None.

    Differential check: the same characterizing one-shot text that appears when
    provided must be absent when one_shot is None.

    spec: feature/BACKEND.md §Ontology Generation Service §Inference Pipeline
          — one_shot is conditionally included.
    """
    one_shot_text = "Focus only on customer-facing concepts."
    with_oneshot = build_run_prompt(
        seeds_md="",
        evidence_per_dataset={},
        one_shot=one_shot_text,
        nonce=_NONCE,
    )
    without_oneshot = build_run_prompt(
        seeds_md="",
        evidence_per_dataset={},
        one_shot=None,
        nonce=_NONCE,
    )
    assert one_shot_text in with_oneshot
    assert one_shot_text not in without_oneshot, (
        "One-shot content must not appear in the prompt when one_shot is None."
    )


# ── Evidence section ──────────────────────────────────────────────────────────


def test_prompt_includes_dataset_urn_in_evidence_section() -> None:
    """Evidence section must include the dataset URN for each dataset.

    spec: feature/BACKEND.md §Ontology Generation Service §Inference Pipeline
          — evidence is per-dataset; each block identifies its source dataset.
    """
    prompt = build_run_prompt(
        seeds_md="",
        evidence_per_dataset=_minimal_evidence(),
        one_shot=None,
        nonce=_NONCE,
    )
    assert _DATASET_URN in prompt, (
        "Evidence section must include the dataset URN."
    )


def test_prompt_omits_evidence_content_when_no_datasets() -> None:
    """Evidence content (dataset URN) is absent from the prompt when no evidence is provided.

    Differential check: the dataset URN that appears in the prompt when evidence
    is provided must be absent when evidence_per_dataset is empty.

    spec: feature/BACKEND.md §Ontology Generation Service §Inference Pipeline
          — evidence is per-dataset; when no datasets are provided, no dataset
          identifiers appear.
    """
    with_evidence = build_run_prompt(
        seeds_md="",
        evidence_per_dataset=_minimal_evidence(),
        one_shot=None,
        nonce=_NONCE,
    )
    without_evidence = build_run_prompt(
        seeds_md="",
        evidence_per_dataset={},
        one_shot=None,
        nonce=_NONCE,
    )
    assert _DATASET_URN in with_evidence
    assert _DATASET_URN not in without_evidence, (
        "Dataset URN must not appear in the prompt when evidence_per_dataset is empty."
    )
