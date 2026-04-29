"""Ontology Generation backend service (UC3).

Implements the subject/predicate/object triple ontology:
  - Singleton conf CRUD
  - Seed CRUD (raw Markdown)
  - LLM inference pipeline with node/edge/triple reuse via pgvector
  - Per-result review (approve/reject) with triple dependency gate
  - AGE graph materialisation (best-effort read replica)

Spec: spec/feature/BACKEND.md §Ontology Generation Service
"""
