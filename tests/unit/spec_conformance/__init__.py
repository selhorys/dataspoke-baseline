"""Spec-conformance unit tests.

These tests compare the shipped implementation against the spec that owns it — the
priority-1 API contract (``spec/API.md``) for the route and error catalogues,
``spec/AI_PLUGIN.md`` for the plugin under ``plugin/`` — so that spec↔implementation drift
fails a test run instead of being rediscovered by hand.

They are unit-tier: they read markdown files from the repo and import the FastAPI app
object. No dev environment, network, or database is involved
(spec/TESTING.md §Unit Testing → Scope).
"""
