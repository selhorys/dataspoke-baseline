---
name: verify-install-branch-claims
description: Docs claiming "prod does X / does not do Y" must be checked against the prod branch of install.sh, not the dev branch's component gating
metadata:
  type: feedback
---

When reviewing deployment docs, verify every "the prod profile does/doesn't do X"
claim by reading the `elif [[ "$PROFILE" == "prod" ]]` branch of
`helm-charts/bin/install.sh` end to end — not just the phase banners
(`step N 3`), and not by reasoning from the dev branch's `_has_component` gating.

**Why:** the dev branch gates admin seeding behind `_has_component seed`, which led
two separate docs to state that prod "runs no automatic post-install seeding — the
`seed` component is dev-only". The prod branch does not use `_has_component` at all;
it seeds unconditionally unless `--skip-seed`. That understates the urgency of the
credential-rotation step, since a well-known default admin is live the moment the
install finishes.

**How to apply:** the phase banners stop before the tail of the branch. Read past the
final `step N N` marker to the completion banner. Also confirm whether env vars the
child scripts depend on are `export`ed (they are — `ENV_FILE` at install.sh:101), since
that changes whether a documented `ENV_FILE=` prefix is describing an install-time
behavior or only a manual re-run. Related: [[verify-branch-reachability-rationales]].
