---
name: project-auth-email-storage-case-divergence
description: Email case is normalised twice — create_user lowercases on write, corpuser_urn on derivation; AUTH.md §URN conventions specifies both, so neither is impl-pinning
metadata:
  type: project
---

`spec/feature/AUTH.md §URN conventions` specifies both halves of the email-case
contract: user creation normalises the address to lowercase **on write**, and URN
derivation lowercases **again** as a second line of defence, so a row stored in any
case still derives `urn:li:corpuser:bob@example.com`.

The impl matches in two places: `src/backend/auth/users.py::create_user` stores
`email.lower()`, and `src/backend/datahub/users.py::corpuser_urn` lowercases on
derivation.

**Why it matters:** `users.email` is `CITEXT` — case-insensitive on compare but
case-preserving on storage — while the corpuser URN is case-sensitive, and that URN is
minted by DataHub's OIDC JIT rather than by DataSpoke. Without normalisation a row
registered as `Bob@example.com` is probed at a URN JIT never created, so the user
reports `skipped_unprovisioned` forever with no operator signal.

**How to apply:** tests may assert either half — write-time storage or derivation.
Both are spec-backed; do not flag a storage-lowercasing assertion as impl-pinning.

Worth remembering: the write-time half was undocumented when this memory was first
written, so a test asserting it cited a section that then said only "case-preserving on
storage". The spec was corrected rather than the test weakened. On a similar
spec-vs-test conflict, check which side is actually wrong before assuming the test is.

Related: [[dead-assert-tuple-ruff-blind]]
