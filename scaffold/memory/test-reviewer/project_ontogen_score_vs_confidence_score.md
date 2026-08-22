---
name: ontogen-score-vs-confidence-score
description: Ontogen result rows carry `confidence_score`, NOT `score` — USE_CASE_en.md L391/395/399 uses `score` as narrative shorthand and contradicts API.md L386/390/394 + impl + frontend; flag any test asserting a bare `score` field
metadata:
  type: project
---

Ontogen `GET /spoke/ontogen/result/{node,edge,triple}` rows expose **`confidence_score`**. There is
no `score` field and no Pydantic alias/serializer creating one.

| Source | Priority | Says |
|---|---|---|
| `spec/API.md` L386/390/394 | 1 (API contract) | "Each row carries `confidence_score`, `status`, `created_at`, and `run_id`" |
| `spec/USE_CASE_en.md` L391/395/399 | 1 (scenario set) | "each row carries `score`, `status`, and `run_id`" ← **narrative shorthand, wrong** |
| `spec/feature/BACKEND_SCHEMA.md` L268 | 5 | column `confidence_score REAL` |
| `src/api/schemas/ontogen.py` L121/151/184 | impl SSOT | `confidence_score: float` |
| `src/frontend/types/ontogen.ts` L78/99/121 | impl | `confidence_score: number` |

**Why:** two priority-1 docs disagree. For a response *field name* API.md governs — it is the API
contract doc, and CLAUDE.md pins `src/api/` as the API-contract SSOT. USE_CASE_en.md is the
scenario set; its prose says "score" informally. Everything except that one prose line agrees on
`confidence_score`. Do not "fix" USE_CASE_en.md silently — priority-1 docs change only on explicit
request; surface the contradiction instead.

**How to apply:** flag `assert "score" in row` (or `row["score"]`) on any ontogen result row as a
T2 failure, even when it cites USE_CASE_en.md §UC3 §API Mapping — the citation *exists* but the
cited line contradicts the API contract. This was introduced in issue #58 Part B2 cycle 1 as an
"advisory" field-presence addition and is invisible under `[stub]` (zero rows persist → loop body
never runs → vacuous), so collection and stub runs stay green while `[real]` is unconditionally
red. A citation existing at the cited location is necessary but NOT sufficient — cross-check
field names against API.md. See [[run-id-filter-then-assert-tautology]] for the sibling UC3
anchors.
