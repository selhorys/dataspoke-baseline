---
name: renamed-guard-comparison-target
description: When a change renames the resource a safety guard compares against, the guard is inert for exactly the pre-change population it was written to protect — always ask "what does this read on a cluster that predates the change?"
metadata:
  type: feedback
---

When reviewing a guard that aborts on "live value disagrees with source value", check
what the guard reads on a deployment installed **before** the change. If the same commit
renames the resource holding the live value, the guard finds nothing, takes its
create-fresh branch, and silently does the destructive thing.

**Why:** in the #111 Fernet work, `_ensure_airflow_fernet_secret`
(`helm-charts/bin/install.sh`) aborts when `DATASPOKE_AIRFLOW_FERNET_KEY` disagrees with
the live `dataspoke-airflow-metadata-encryption-key`. The same change introduced that
name; every pre-change cluster holds the key in the Airflow subchart's
`<fullname>-fernet-key`. Two *other* functions in the same file (the credentials-Secret
create path and the dev self-heal) both fall back to the old name — so the file itself
proved the old name was live, while the guard never consulted it. `spec/feature/HELM_CHART.md`
§Rotation tolerance meanwhile promised the abort applies "in both profiles".

**How to apply:** for any rename + guard landing together, grep the *sibling* helpers in
the same file for the legacy name — if adoption/read paths know about it but the
comparison path does not, that asymmetry is the bug. Same shape applies to renamed DB
columns, cache-key prefixes, and config keys, not just K8s Secrets. Severity still gets
capped by real blast radius — see [[fernet-blast-radius-env-connections]].

**Status of the originating instance (2026-07-30):** fixed — `_ensure_airflow_fernet_secret`
now `elif`s onto `dataspoke-airflow-fernet-key` for the comparison. Do not re-report it; the
rule above is the durable part.
