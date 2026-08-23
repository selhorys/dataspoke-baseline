---
name: project-ingestion-secret-ref-model
description: Ingestion recipes carry ${name__key} secret REFERENCES, not plaintext. Model documented in spec/API.md/BACKEND.md; calibration promoted to scaffold/roles/security-reviewer.md §3 Secrets.
metadata:
  type: project
---

The design itself is documented in `spec/API.md` §ingestion routes and
`spec/feature/BACKEND.md:319-342` — recipes carry `${name__key}` references, never plaintext, and
`GET /spoke/ingestion/secrets` returns metadata only. The reviewer-calibration consequence now
lives in `scaffold/roles/security-reviewer.md` §3 Secrets: do not flag a `${name__key}` token as a
hardcoded-secret finding; only flag an actual resolved secret value.
