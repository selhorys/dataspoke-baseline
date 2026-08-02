---
name: airflow-extraenv-tpl-injection-surface
description: airflow.extraEnv is rendered through Helm `tpl`, so any Secret-derived value install.sh composes into it is a template-injection sink that can re-enable anonymous-admin Airflow; plus the measured getboolean accept-set and the which-pod-rolls-on-which-rotation asymmetry
metadata:
  type: project
---

`helm-charts/bin/install.sh::_build_airflow_extra_env_file` writes a YAML env
list and passes it as `--set-file airflow.extraEnv=<file>`. The vendored
apache-airflow chart renders it as **`{{- tpl . $Global | nindent 2 }}`**
(`charts/airflow-1.20.0.tgz` → `templates/_helpers.yaml:168-170`, helper
`custom_airflow_environment`, included by *every* Airflow component). So every
value install.sh composes into that file is evaluated as a Go template with
full chart context before YAML parsing.

**Measured (helm v3.18.4, offline `helm template`), issue #138 review:**

- `{{ printf "%c" 58 }}` → `:` and `{{ printf "%c" 44 }}` → `,`. A username
  carrying neither literal character passes
  `_check_airflow_credentials_prod`'s `*","*|*":"*` guard and still injects
  extra `username:role` entries.
- Full structural YAML injection works: a charset-clean username rendered
  `- name: AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_ALL_ADMINS` / `value: "True"` as
  a sibling env var. `AIRFLOW__SECTION__KEY` env beats airflow.cfg, so the
  anonymous-admin bypass #138 removes is reachable from the credentials Secret.
- `lookup` is available inside `tpl`. Empty under `helm template` (no cluster),
  **live under `helm upgrade`** — a username can render any Secret the
  installer can read into the Airflow manifests and the release history.
- YAML double-quoted `\x2c` / `\x3a` escapes also defeat the charset guard
  (`"a\x2cb\x3aADMIN"` → `a,b:ADMIN`), independent of `tpl`.
- The equivalent sink for chart config: `templates/configmaps/configmap.yaml:57`
  renders `{{ $key }} = {{ tpl ($val | toString) $Global }}`.

**Airflow 3.1.8 facts (read from the PyPI wheel, NOT the 3.2.0 in the uv cache
— the two differ; `SimpleAllAdminMiddleware` and the `get_users()` teams/
3-tuple form are 3.2.0-only):**

- `configuration.py:1200` `getboolean` lowercases+strips, strips a trailing
  `# comment`, accepts **`t` / `true` / `1`** as True and `f`/`false`/`0` as
  False, and raises `AirflowConfigException` on anything else. Any pre-flight
  comparing the overlay value with `== "True"` misses `"true"`, `"TRUE"`,
  `"t"`, `"1"` and silently withholds its exposure warning.
- `services/login.py:76` `create_token_all_admins` raises **403** when
  all_admins is false, so `GET /auth/token` and `GET /auth/token/login` are
  genuinely closed. `POST /auth/token` then does a **plaintext, non-constant-
  time** compare against the passwords JSON.
- `simple_auth_manager.py:98` `get_users()` is `u.split(":")` unpacked into
  exactly 2 — 1 or 3 parts is an uncaught `ValueError`.
- `_get_passwords` (:357) **prunes** file entries not in
  `simple_auth_manager_users`; `init()` (:109) then generates a replacement
  with **`random.choices`** (:378, Mersenne Twister — not `secrets`) and
  `_print_output`s it to **stdout**, i.e. the api-server pod log. Any
  user-list-vs-file divergence therefore publishes the live Airflow admin
  password to the log plane.
- `init_auth_manager` is called from exactly one place
  (`api_fastapi/app.py:108`), so only api-server ever opens the file.

**Rotation asymmetry to re-check on any Airflow credential diff.** The API
Deployment rolls on *any* credentials-Secret change
(`dataspoke/templates/api-deployment.yaml:22-29`, `checksum/secret` from a live
`lookup`). The Airflow api-server does not: `_restart_airflow_key_consumers`
fires only on `AIRFLOW_KEYS_ROTATED` / `AIRFLOW_METADATA_DSN_ROTATED`. A
password materialised at pod start (init container + emptyDir) therefore
survives a rotation the API already adopted → every workflow trigger 401s
silently. The *username* is different: it is baked into the manifest, so
changing it does roll the pod.

**Helm list-vs-map merge.** `-f` deep-merges maps but **replaces** lists. An
operator overlay touching `airflow.apiServer.extraInitContainers` /
`extraVolumes` / `extraVolumeMounts` for any unrelated reason removes the
passwords init container wholesale. Ordering the installer's fragment before
the overlay does not "let the overlay win" gracefully — it deletes the auth
mechanism.

**How to apply:** treat any Secret-derived string that reaches
`airflow.extraEnv` (or `airflow.config.*`) as needing an allowlist regex, not a
denylist of delimiters — the `tpl` pass manufactures whatever character the
denylist names. Related: [[env-to-sed-helm-interpolation-boundary]],
[[install-sh-preflight-gate-mechanics]],
[[prod-bootstrap-recipe-measurements]].
