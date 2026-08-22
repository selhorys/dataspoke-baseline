---
name: shared-response-model-unpopulated-field
description: When two routes share one Pydantic response model, check every handler's constructor populates every field — the unset one silently serializes as its default; the repo now has a tripwire test that flags the sharing itself
metadata:
  type: feedback
---

When reviewing (or auditing) a response model used by more than one route, grep every
`return <Model>(...)` call site and diff the kwarg set against `<Model>.model_fields`.
A field with a default that one handler omits is serialized as that default, not an error.

**Why:** issue #86 phase 4 — `MetagenItemListResponse` served both
`GET /spoke/common/data/{urn}/attr/metagen/item` (populated `candidate_count` via
`count_dataset_candidates`) and `GET /spoke/metagen/item` (never passed it). Because the
field was declared `candidate_count: int = 0`, the cross-dataset route returned a
permanently-zero aggregate for as long as the model was shared. No test caught it at the
time: the cross-dataset route test asserted only `total_count` and `len(items)`.

**Tripwire since:** `tests/unit/spec_conformance/test_response_envelope.py::TestSharedModelBinding::test_models_bound_to_several_routes_are_declared`
fails for ANY paginated response model bound as the `response_model` of several routes
until the name is declared in the `SHARED_PAGINATED_MODELS` allowlist (same file, ~line 121)
with a justifying comment. Consequences when reviewing:
- A backend stage that introduces a shared paginated model leaves `tests/unit/` red by
  design, with that single failure. Confirm it is the ONLY failure, then treat the
  allowlist entry as the test agent's item — not a backend regression.
- The guard proves nothing about *population*. It only forces a declaration. Still diff
  the constructor kwargs yourself (issue #145's `AdminApiTokenListResponse` shares two
  admin routes and both pass an identical kwarg set through one `_admin_token_to_item`
  mapper — that is what makes the sharing safe, not the allowlist entry).

**How to apply:** the smell is a `= 0` / `= None` / `default_factory` on a *semantic*
field (an aggregate, a status, a count) rather than on envelope plumbing. Pydantic's
default `extra="ignore"` also means an accidental kwarg on the *other* sibling is
dropped silently, so constructor-side greps are the only signal. Verify the fix from
`app.openapi()`, not by reading source — see [[feedback-verify-generator-dead-code-claims]]
for the same "prove it, don't read it" stance.
