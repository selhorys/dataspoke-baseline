---
name: datahub-gql-401-is-not-json
description: A missing/stale DataHub PAT yields an HTTP 401 with a non-JSON body, so resp.json() throws before any GraphQL-errors assertion — "stale token" diagnoses on those asserts are unreachable
metadata:
  type: project
---

DataHub GMS rejects an unauthenticated / invalid-PAT request to `/api/graphql` in
`metadata-service/auth-filter/.../AuthenticationEnforcementFilter.java` via
`response.sendError(SC_UNAUTHORIZED, "Unauthorized to perform this action.")` — a servlet error page,
**not** a GraphQL envelope. Corroborated by `helm-charts/bin/dev-peripherals/datahub.sh`, which
detects a stale PAT purely by `HTTP_CODE != 200`.

Consequence for the E2E helpers: `gqlMutate()` in `uc1-01-datahub-managed.spec.ts` does
`return resp.json()` unconditionally, so a missing/stale `DATASPOKE_TEST_DATAHUB_TOKEN` makes the
`await gqlMutate(...)` **throw a JSON SyntaxError** — the test never reaches the following
`expect(result.errors).toBeFalsy()`. A GraphQL `errors` array therefore means GMS *accepted* the
credential and refused the operation (insufficient privilege, or broken Managed Ingestion).

**Why:** worth remembering because a failure message attached to the `errors` assertion that blames a
"missing or stale token" reads plausible but can never print.

**How to apply:** flag any test whose GraphQL-errors assertion message diagnoses a credential
problem — that diagnosis belongs inside the fetch helper (check `resp.ok()` / content-type before
parsing), not on the errors assert. Related: the user-memory
`project_envfile_stale_test_creds_on_resume` (how creds actually go stale).
