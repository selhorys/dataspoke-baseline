---
name: operator-runbook-is-credential-surface
description: helm-charts/README.md prod runbook and values-prod.example.yaml are a credential-path surface not in the sensitive-path globs; verify every doc claim about install.sh/uninstall.sh against the code
metadata:
  type: project
---

`helm-charts/README.md` (§Prod profile runbook), `helm-charts/values-prod.example.yaml`,
and `spec/feature/HELM_CHART.md` §prod are a **credential-handling surface** even though
they contain no code. They are the sole instruction set for creating the 12-key
`dataspoke-secrets`, rotating the seeded default admin, and tearing down without
stranding or destroying key material.

They are **not** covered by the sensitive-path globs in `.claude/agents/security-reviewer.md`
(which list `helm-charts/**/templates/secrets.yaml` and `values*.yaml` but not `README.md`
or the prod example overlay by name). Worth proposing as an addition.

**Why:** a doc-only diff on this path caused a high-severity finding — the runbook asserted
prod does no automatic admin seeding, while the prod branch of `install.sh` calls
`post-install/seed-admin-user.sh` unconditionally unless `--skip-seed`. The
`_has_component seed` gate that makes seeding opt-in is **dev-branch only**; prod has its
own ungated call. `spec/API.md` ("invokes it during both dev and prod installs") agreed with
the code; README and HELM_CHART.md agreed with each other and were both wrong. Result: the
published default `dataspoke@dataspoke.local / dataspoke` is live on the internet-facing
`api.` ingress the moment install returns, while the runbook tells the operator it is not.

**How to apply:** when a diff touches these files, never review the prose against itself.
Open `install.sh` / `uninstall.sh` and confirm each behavioral claim (what auto-runs, what
preflight rejects, what teardown retains) and each URL against the router prefixes in
`src/api/main.py`. Doc-vs-doc consistency is worthless here; three artifacts can agree and
all be wrong. Cross-check `spec/API.md` — as the priority-1 contract it has been the
accurate one. See [[project-auth-fail-closed-spans-layers]] for the analogous
"one layer's correctness is undone by another's" pattern.
