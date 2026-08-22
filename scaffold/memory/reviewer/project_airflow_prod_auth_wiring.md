---
name: project-airflow-prod-auth-wiring
description: Verified Airflow 3.1.8 SimpleAuthManager behaviour plus the prod-install asymmetries that hide a broken Airflow api-server
metadata:
  type: project
---

Facts established by reading the real `apache_airflow_core==3.1.8` wheel and by
offline `helm template` runs during the #138 review (prod Airflow credentialed
auth). Load-bearing for any change to Airflow auth, the credentials Secret, or
the prod install ordering.

**Airflow 3.1.8 `SimpleAuthManager` (verified, not inferred):**
- `init()` **returns early** when `core.simple_auth_manager_all_admins` is true —
  so a pre-seeded passwords file is genuinely inert under all-admins, and the
  `a+` open (which catches only `BlockingIOError`, hence the writable emptyDir)
  never happens.
- `get_users()` does `u.split(":")` then unpacks `username, role` → a username
  containing `:` or `,` raises `ValueError`; role is `.upper()`d, so `ADMIN` is
  fine. Config default is `admin:admin` — dropping the env var silently falls
  back to that.
- `create_token` rejects an empty username *or* password with **400**, before
  any lookup; `_get_passwords` filters the file down to configured usernames and
  preserves a matching pre-seeded entry (only an *absent* username gets a
  generated random password).
- `init_auth_manager` has exactly one call site (`api_fastapi/app.py`), so only
  the api-server needs the file; the scheduler/dag-processor/triggerer get the
  env var harmlessly.
- `getboolean` accepts only `t|true|1` / `f|false|0` after `.lower().strip()` —
  `yes`/`on` raise. See [[value-crosses-parser-boundaries]].

**Install/chart asymmetries that make Airflow failures silent in prod:**
- The prod branch waits on `dataspoke-api`, `-event-consumer`, `-frontend` only.
  The `dataspoke-airflow-api-server` rollout wait exists **in the dev branch
  only**, and `helm upgrade` runs without `--wait`. A broken Airflow api-server
  therefore still prints "Installation complete".
- `airflow.extraEnv` is passed through Helm's `tpl` (upstream
  `custom_airflow_environment`), and reaches *every* Airflow component.
- Any `airflow.apiServer.extraInitContainers/extraVolumes/extraVolumeMounts` an
  operator overlay sets **replaces** the list wholesale (Helm list semantics), so
  a partial override strands a volumeMount with no volume.
- `--components` / `--from-component` are hard-rejected for `--profile prod`, so
  the #137-class "fast path drops the wiring" hazard does not reach prod.

**How to apply:** treat "the install succeeded" as no evidence that Airflow auth
works; ask for an explicit api-server rollout wait plus a login probe whenever a
change puts a DataSpoke-authored container or file in the api-server's startup
path. Also check `_restart_airflow_key_consumers`' trigger set — it fires only on
signing-key/metadata-DSN rotation, so any new rotation-sensitive Airflow input
needs adding there.
