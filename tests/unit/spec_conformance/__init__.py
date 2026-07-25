"""Spec-conformance unit tests.

These tests compare the running implementation against the priority-1 API contract
(``spec/API.md``) so that spec↔implementation drift fails a test run instead of being
rediscovered by hand.

They are unit-tier: they read markdown files from the repo and import the FastAPI app
object. No dev environment, network, or database is involved
(spec/TESTING.md §Unit Testing → Scope).
"""
