"""Metadata Generation backend service (UC4).

Implements LLM-powered per-field proposal generation and field-level approval:
  - Per-dataset config CRUD
  - run() pipeline: evidence gathering + LLM proposal generation
  - review_result() with field-level verdict (approve/reject subsets)
  - cross_data.md action handling (create/modify/split/retitle dataProducts)

Spec: spec/feature/BACKEND.md §Metadata Generation Service
"""
