---
name: conftest-promotion-set-is-one-name
description: tests/integration/conftest.py promotes exactly one env name into the app Settings namespace; DATASPOKE_DEV_* now spans dev peripheral credentials, so any prefix-driven promotion is a leak
metadata:
  type: project
---

`tests/integration/conftest.py` has exactly **two** writers into `os.environ`:

1. `_load_dotenv()` — copies every `helm-charts/.env.dev` key **under its own name**, skipping
   keys already present. No prefix rewrite. Safe only because `.env.dev.example` carries zero
   unprefixed Tier-1 `DATASPOKE_*` names (just `DATASPOKE_AWS_PROFILE` / `DATASPOKE_DOCKER_SUDO`),
   so it cannot shadow a Pydantic Settings field.
2. `_promote_test_runtime_overrides()` — the promoted set is **one name**:
   `DATASPOKE_DEV_JWT_SECRET_KEY` → `DATASPOKE_JWT_SECRET_KEY`. Load-bearing because tests import
   `src.backend.auth.tokens.issue_access_token` and must sign with the API pod's key.
   Written by `install.sh` `_sync_env_from_secret`.

**Why:** the source prefix does not bound the set. `DATASPOKE_DEV_*` is Tiers 3+4 combined
(HELM_CHART.md §Configuration — Five-Tier Env Vars, L543-544), so it covers DataHub MySQL
passwords, every Langfuse install internal, dummy-data credentials, the LLM seed key, and the
Google OAuth client secret alongside the six laptop-side access values.

**How to apply:** when reviewing any change to this file, enumerate the promoted set before and
after. A loop over `os.environ`, a `startswith("DATASPOKE_DEV")`, or a `removeprefix` in the
promotion helper silently pushes a dev peripheral credential into the app's Settings namespace —
treat it as high severity, not a refactor. Related: [[project_dsn_url_fields_anchor]].
