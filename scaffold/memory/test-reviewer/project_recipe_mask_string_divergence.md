---
name: recipe-mask-string-divergence
description: Ingestion recipe secret-mask string differs between spec (<hidden>) and impl (********) — flag tests pinning either exact value
metadata:
  type: project
---

The ingestion-source recipe secret mask string has no single authoritative value across spec and impl:
- `spec/USE_CASE_en.md` §UC1 Case 1 example YAML renders the masked password as `<hidden>`.
- `src/backend/ingestion/service.py` `_MASKED_VALUE = "********"` is what the sync sweep actually writes.
- The behavioral spec (`spec/feature/BACKEND.md` §Sync sweep step 1) only says "Mask plaintext secret values" — it mandates the *behavior*, not the exact string.

**Why:** A test asserting `password == "********"` pins the impl sentinel, not a spec invariant; a spec-compliant change to `<hidden>` (matching the spec's own example) would break it. Classic impl-pinning (T2 failure). Surfaced in the UC1 DATAHUB_MANAGED augmentation review (2026-06-13).

**How to apply:** When auditing any ingestion-recipe masking assertion, treat `== "********"` (or `== "<hidden>"`) as a REVISE-level T2 finding. The spec invariant is "plaintext value is gone AND replaced by a mask" — recommend `password != <plaintext>` plus a loose mask check. Distinguish from the secret-*reference* path: asserting `password == "${name__key}"` IS sound because BACKEND.md §Sync sweep step 1 mandates verbatim ref preservation and the test controls that input. Related: [[no-destructive-git-during-review]].
