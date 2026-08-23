---
name: project-runtime-envfile-rename-blast-radius
description: Renaming the gitignored runtime helm-charts/.env has a wide hardcoded-path blast radius and a gitignore secret-leak gotcha
metadata:
  type: project
---

When a change renames the gitignored runtime env file `helm-charts/.env` (e.g. to
`.env.dev`/`.env.prod` for a `--profile`-aware installer), review beyond the bin scripts.

**Why:** Many files hardcode the literal path `helm-charts/.env`, independent of the
install scripts. After the rename they silently stop finding it.

**How to apply:** grep `helm-charts/.env` repo-wide (exclude `.env.dev`/`.env.prod`/`*.example`).
Known hardcoders to verify: `tests/integration/conftest.py` `_load_dotenv`,
`tests/integration/util/{postgres,datahub,airflow,kafka,k8s}.py`, `migrations/env.py`,
`tests/e2e/fixtures/env.ts`, `tests/e2e/playwright.config.ts`,
`.claude/skills/test-manual-api-wired/helpers/setup_env.sh` +
`.claude/skills/test-manual-ui/helpers/preflight.sh` (these HARD-source with an exit-1
guard → skill aborts), `.claude/skills/k8s-deploy/SKILL.md`, `.claude/agents/k8s-helm.md`,
`spec/feature/SECRET_RESOLUTION.md`, `spec/USE_CASE_en/kr.md`. conftest's no-overwrite
auto-load masks the break in the canonical `source .env.dev` flow but the bare
`uv run pytest` path then KeyErrors at import. Docs that claim "conftest loads .env.dev"
become false if the loader isn't also updated — flag the doc/code contradiction.

**.gitignore leak gotcha:** dropping the old `helm-charts/.env` ignore line means a
straggler's un-renamed secrets file (PG pw, DataHub PAT, LLM key) is no longer ignored and
`git add .` will stage it. Keep the old path ignored (or use `helm-charts/.env*` +
`!helm-charts/*.example`). Not a "migration shim" — it's leak prevention. See
[[bash-errexit-grep-output]] for the grep-no-match pitfall when scanning.

**Fix-pass spot-check:** A generator "updated all refs in <file>" claim can still
miss inline lines in that very file — e.g. `helm-charts/README.md` prose got
`.env.dev` but the copy-paste `grep DATASPOKE_LOCK_URL helm-charts/.env` command two
lines down did not; `datahub-api/SKILL.md` heading was fixed but an inline `# set in
helm-charts/.env` comment was not. grep each claimed-updated file line-by-line; do not
trust per-file completion claims. Also non-loader doc strings that escaped the first
sweep: `tests/e2e/global-setup.ts` JSDoc, `dummy-data/manifests/postgres.yaml` comment,
`ref/setup.sh` + `ref/README.md` parentheticals. Golden `spec/USE_CASE_en/kr.md`
`.env.example` refs need explicit user sign-off before editing (priority-1).
