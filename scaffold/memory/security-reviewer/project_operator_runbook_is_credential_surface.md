---
name: operator-runbook-is-credential-surface
description: helm-charts/README.md prod runbook and values-prod.example.yaml are a credential-path surface (README now in the sensitive-path globs); verify every doc claim about install.sh/uninstall.sh (auto-seed, pre-flight, teardown remanence) against the code
metadata:
  type: project
---

`helm-charts/README.md` (§Prod profile runbook), `helm-charts/values-prod.example.yaml`,
and `spec/feature/HELM_CHART.md` §prod are a **credential-handling surface** even though
they contain no code. They are the sole instruction set for creating the 13-key
`dataspoke-secrets`, rotating the seeded default admin, and tearing down without
stranding or destroying key material.

`helm-charts/bin/install.sh`, `bin/uninstall.sh`, and **`helm-charts/README.md`** are all
now in `scaffold/roles/security-reviewer.md`'s sensitive-path globs — the last of these
was the standing gap this note used to track; it's closed.

**Why:** a doc-only diff on this path caused a high-severity finding — the runbook asserted
prod does no automatic admin seeding, while the prod branch of `install.sh` calls
`post-install/seed-admin-user.sh` unconditionally unless `--skip-seed`. The
`_has_component seed` gate that makes seeding opt-in is **dev-branch only**; prod has its
own ungated call. `spec/API.md` ("invokes it during both dev and prod installs") agreed with
the code; README and HELM_CHART.md agreed with each other and were both wrong. Result: the
published default `dataspoke@dataspoke.local / dataspoke` is live on the internet-facing
`api.` ingress the moment install returns, while the runbook tells the operator it is not.

**Three claim classes keep being wrong; check each against code, not prose.**

1. *What auto-runs* — the seed case above.
2. *What the pre-flight guarantees.* All three artifacts say it "fails before any
   resources are created". `ensure_namespace` (`install.sh:1706`) runs ahead of every
   gate, and the `--image-tag` refusal runs ahead of *it*, so the literal claim is false.
   The invariant that actually holds — and the one worth defending in review — is
   **no Secret is read-modified-written before validation** (`_ensure_airflow_fernet_secret`
   is deliberately ordered before `_derive_airflow_metadata_secret`).
3. *What teardown leaves behind.* README's "namespace deletion is prod's only full wipe"
   is false whenever the StorageClass sets `reclaimPolicy: Retain` — which
   `helm-charts/prod-prereq/README.md` now recommends as the deliberate default, because
   these PVs hold the credential store (password hashes, `api_tokens`,
   `password_reset_tokens`, Fernet-encrypted ingestion secrets) and Redis's AOF
   (refresh-token revocation keys). Retain + "namespace delete wipes it" reads as a
   complete decommission and is not one. Any diff that recommends a reclaim policy must
   be read together with every teardown claim in the same runbook.

**How to apply:** when a diff touches these files, never review the prose against itself.
Open `install.sh` / `uninstall.sh` and confirm each behavioral claim and each URL against
the router prefixes in `src/api/main.py`. Doc-vs-doc consistency is worthless here; three
artifacts can agree and all be wrong. Cross-check `spec/API.md` — as the priority-1
contract it has been the accurate one. See [[auth-fail-closed-spans-layers]] for
the analogous "one layer's correctness is undone by another's" pattern, and
[[install-sh-preflight-gate-mechanics]] for the gate-shape details, and
[[prod-bootstrap-recipe-measurements]] for §2's measured shell/kubectl facts and the
Airflow `all_admins` bypass that makes two documented "credentials" non-enforcing.
