---
name: spec-conformance-paths-only
description: tests/unit/spec_conformance only compares route PATHS against API.md — query-param name drift between frontend and backend passes every suite
metadata:
  type: feedback
---

`tests/unit/spec_conformance/` matches impl routes against `spec/API.md`
**segment-wise on paths only** (`_api_md.py` collapses path params to `{}`).
Nothing in the repo asserts that a route declares the query params API.md's
§Query Parameters table says it supports.

**Why:** this is exactly how issue #90 survived — `/spoke/metagen/event`
declared `after` instead of `from`/`to` for the life of the feature with a fully
green unit suite (2694 passed), green spec-conformance (76 passed), and a green
frontend typecheck. A passing conformance run is not evidence that a param
contract holds.

**How to apply:** never accept "spec_conformance passes" as proof that a
request-shape or query-param change is spec-aligned — verify by dumping
`app.openapi()` parameters for the route, or by firing a real ASGI request and
compiling the resulting SQL. When reviewing a stage that fixes param drift, ask
whether the paired test stage adds a param-level assertion; otherwise the same
drift recurs silently. Related: [[metagen-event-from-to-ignored]].
