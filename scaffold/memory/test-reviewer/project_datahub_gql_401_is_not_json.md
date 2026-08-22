---
name: datahub-gql-401-status-not-content-type
description: Measured — a stale DataHub PAT returns HTTP 401 with a well-formed JSON body ({timestamp,status,error,path}), so resp.json() does NOT throw; only the status code discriminates a credential failure
metadata:
  type: project
---

Measured against the live dev GMS on 2026-07-31 (bogus and absent `Authorization` both):

```
status 401 | content-type: application/json
{"timestamp":1785463655173,"status":401,"error":"Unauthorized","path":"/api/graphql"}
```

So the earlier belief that `resp.json()` **throws** on a stale PAT is wrong for this deployment.
`sendError(SC_UNAUTHORIZED)` in `AuthenticationEnforcementFilter` is rendered by Spring Boot's
error handler as JSON for a JSON request. The body parses cleanly into a dict that has **no
`errors` key and no `data` key**.

Consequences when reviewing DataHub GQL helpers/tests:

- The **status check is load-bearing**; a content-type check alone catches nothing here. Flag any
  helper (or docstring justifying one) that treats content-type as the credential discriminator.
- A status-blind `resp.json()` does not blow up — it yields `{"timestamp":…,"status":401,…}`, so
  `"errors" not in body` is **True** and `body.get("data", {}).get(<field>)` is `None`. The failure
  then surfaces one step later as "returned no URN", i.e. a real but misattributed failure, not a
  masked pass.
- A failure message attached to an `errors` assertion that blames a missing/stale token still
  cannot print (the `errors` key is absent on a 401) — the credential diagnosis belongs in the
  fetch helper, keyed on status.
- `helm-charts/bin/dev-peripherals/datahub.sh` corroborates the stance: it detects a stale PAT
  purely by `HTTP_CODE != 200`.

**How to apply:** verify empirically before accepting either story — one `httpx.post` with a bogus
Bearer token settles it. Related: the user-memory `project_envfile_stale_test_creds_on_resume`
(how creds actually go stale), [[uc1-event-status-anchor]].
