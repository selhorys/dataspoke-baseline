# Prauto — Autonomous PR Worker

Prauto is a cron-driven bash worker that monitors GitHub issues labeled `prauto:ready`, invokes Claude Code CLI to analyze and implement changes, and submits pull requests.

See `spec/AI_PRAUTO.md` for the full specification (heartbeat cycle, label lifecycle, phase state machine, security model, prompt templates).

## Prerequisites

- `claude` CLI installed and authenticated
- `gh` CLI installed and authenticated (PAT with Issues, PRs, Contents permissions)
- `git` configured for the repository
- `jq` for JSON processing

The integration and E2E stages additionally need a reachable dev cluster:

- A dev-profile env file selected by `PRAUTO_DEV_ENV_FILE` (default `helm-charts/.env.dev`) — the
  stages resolve it under the repo checkout, source it, and pass it to `health-check.sh` /
  `install.sh` via `--env-file`. Point it at **this worker's own dedicated cluster** (see
  [Per-worker dev cluster](#per-worker-dev-cluster)); the default is the shared human cluster. A
  relative value resolves under the repo checkout, never the branch worktree — that anchoring is a
  security property, so a branch cannot redirect prauto's deploys by writing its own env file.
- `kubectl` and `helm` on `PATH`, with the dev cluster context available
- `docker` for the API and frontend image builds
- `pnpm` plus the Playwright browsers (`pnpm -C tests/e2e exec playwright install`) for the E2E stage

Each stage skips itself when its dependencies are absent — a machine without a dev env still runs
analysis, implementation (the generator → reviewer workflow), the static gates, and unit tests.

### Per-worker dev cluster

Each worker binds to its own dev-profile cluster via `PRAUTO_DEV_ENV_FILE` in `config.local.env`.
This is what makes provisioning and deploying safe to automate: prauto never contends with a human
engineer's cluster for the dev-env lock, and the blast radius of a bad chart or a destructive reset
stops at a cluster only prauto uses. The committed default (`helm-charts/.env.dev`) is the shared
human cluster and is safe only for single-worker or testing use.

When the health check is red or the cluster is absent and `PRAUTO_CLUSTER_PROVISION_ENABLED` is
`true` (default), prauto runs a full `install.sh --profile dev` against `PRAUTO_DEV_ENV_FILE` to
provision, then re-checks. Provisioning failure skips the cluster stages (it never fails the issue)
and does not count against `PRAUTO_MAX_RETRIES_PER_JOB`.

> **GKE Autopilot abort mode**: on Autopilot, a GMS scale-up timeout can abort `install.sh` before
> the DataHub ingress+PAT step. Automatic resume is not implemented — the stage simply skips. To
> resume by hand, run `./helm-charts/bin/install.sh --profile dev --from-component datahub`
> (not `dataspoke-infra`); a fragmented `--from-component` resume skips the env-sync step, so rebuild
> stale `DATASPOKE_DEV_*` credentials in the env file from the cluster secrets afterward.

## Setup

1. Copy the instance config template:

   ```bash
   cp .prauto/config.local.env.example .prauto/config.local.env
   ```

2. Edit `.prauto/config.local.env` with your worker identity, Claude model, and secrets.

3. Sync GitHub labels (once):

   ```bash
   npx github-label-sync --access-token "$(gh auth token)" --labels .github/labels.yml <owner>/<repo>
   ```

4. Schedule the heartbeat. Choose one of the options below:

   **Option A — `cron` (Linux / CI)**

   ```bash
   # Run heartbeat every 30 minutes, Mon-Fri 9:00-18:00 KST
   */30 9-18 * * 1-5 cd /path/to/dataspoke-baseline && .prauto/heartbeat.sh >> .prauto/state/heartbeat_cron.log 2>&1
   ```

   The crontab (or launchd) entry owns the heartbeat interval and the log destination — the
   heartbeat writes no log of its own, and a script cannot reschedule its own trigger. Change the
   schedule here, not in `config.local.env`.

   **Option B — `launchd` (macOS)**

   On macOS, cron jobs cannot access the Keychain that can be useful to use claude code without `ANTHROPIC_API_KEY` set. in this case, launchd can be used instead of cron. create launchd setting file (e.g. `~/Library/LaunchAgents/com.dataspoke.prauto.heartbeat.plist`) and load it. Note that when using launchd, PATH must include directories for **all** required CLIs (`claude`, `gh`, `git`, `jq`).

   ```bash
   vi ~/Library/LaunchAgents/com.dataspoke.prauto.heartbeat.plist                                # edit
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dataspoke.prauto.heartbeat.plist  # load
   launchctl bootout gui/$(id -u)/com.dataspoke.prauto.heartbeat                                 # unload
   launchctl kickstart gui/$(id -u)/com.dataspoke.prauto.heartbeat                               # run now
   ```

## Optional: Dedicated GitHub Bot Account

Running prauto under a separate GitHub account (e.g., `youraccount-prauto`) keeps bot activity visually distinct from human commits and PR comments. This is optional — prauto works fine with the repo owner's credentials.

### 1. Invite the bot account as a collaborator

From the repo owner account, go to:

```
https://github.com/youraccount/yourrepo/settings/access
```

Search for the bot account and send the invitation. For personal repositories, collaborators receive Write access by default (no role selector is shown). For organization repositories, select the **Write** role.

### 2. Accept the invitation

Log in as the bot account and accept the collaborator invitation at `https://github.com/notifications`.

### 3. Create a classic PAT from the bot account

Go to `https://github.com/settings/tokens/new` (logged in as the bot account) and check the **`repo`** scope.

> **Why classic PAT, not fine-grained?**
> Fine-grained PATs require the Resource owner to be the token creator or one of their organizations. Another personal account (the repo owner) cannot appear as a Resource owner, so fine-grained PATs cannot be scoped to a repo owned by a different personal account.

### 4. Set the token in `config.local.env`

```bash
GH_TOKEN="ghp_xxxxxxxxxxxx"
```

All GitHub API operations (issue labels, comments, PR creation) will then run as the bot account. Git commit identity remains whatever is set in `PRAUTO_GIT_AUTHOR_NAME` / `PRAUTO_GIT_AUTHOR_EMAIL`.

## What an Implementation Run Does

After analysis and plan approval, `implement_and_finalize` runs these stages in order. Each stage
skips itself when the diff does not reach its layer or its dependencies are unavailable.

1. **Implementation** — the implementation session drives `.claude/workflows/wf-minimal.js` via the
   `Workflow` tool, passing the approved plan plus the `stages` / `security` the analysis phase
   emitted. wf-minimal runs each generator stage (`spec`, `backend`, `airflow-dag`, `test`,
   `frontend`) paired with an adversarial reviewer (generator ≠ reviewer), with one fix pass on a
   `REVISE`. `security-reviewer` runs in parallel on stages flagged in `security`. On success the
   session commits the workflow's unstaged changes; it never pushes. If the workflow **escalates** (a
   reviewer's findings persist after the fix pass), the job is abandoned to `prauto:failed` with the
   escalating stage and findings — no PR is opened. `k8s-helm` is never a stage: deploys are
   orchestrator-owned.
2. **Integration fix loop** — under the dev-env lock. When the diff touches `src/api/`,
   `src/backend/`, or `src/shared/`, the branch's API is deployed first (`install.sh --components
   api`, which pins the rebuilt image by digest so helm rolls it by construction, then waits on
   `kubectl rollout status`) so the tests reach the branch's code. Then spot
   and api-wired run as two separate groups — mixing them puts competing Airflow load on the cluster
   and flakes on timing. An attempt fails if either group fails; failures feed a Claude fix session,
   bounded by `PRAUTO_INTEGRATION_FIX_MAX_RETRIES`.
3. **E2E stage** — runs only when the diff touches `src/frontend/`, `tests/e2e/`, or `src/api/` (the
   contract surface the UI consumes). Deploys the branch's frontend (`install.sh --components
   frontend` plus a forced rollout restart), then runs the Playwright suite. Bounded by
   `PRAUTO_E2E_FIX_MAX_RETRIES`.

   > **This stage leaves the cluster frontend enabled.** Dev defaults to
   > `frontend.enabled: false` (host `pnpm dev` is the standard workflow), and the stage flips it on
   > without turning it back off. Until someone reinstalls, `app.<domain>` serves the UI from
   > whichever branch prauto last tested. Restore the dev default with
   > `./helm-charts/bin/install.sh --profile dev`.
4. **Finalize** — pushes, opens or updates the PR, posts unit and integration results as PR
   comments, and swaps labels to `prauto:review`.

**Deploy ordering is a correctness constraint.** The integration stage deploys the API and the E2E
stage deploys the frontend; the API deploy must come first. `--components api` is a full-release
upgrade that reverts `frontend.enabled → false`, deleting the cluster frontend, so an API deploy
after the frontend deploy would destroy the UI the E2E stage needs. The frontend deploy also rolls
the API pod, so the E2E stage must run strictly after the integration groups and never concurrently
with them.

### Dev-env lock protocol

The integration and E2E stages each acquire the dev-env lock at `$DATASPOKE_DEV_LOCK_URL` (base
URL; the endpoint is `$DATASPOKE_DEV_LOCK_URL/lock`) for the duration of their run, and release it
afterward. They pass `DATASPOKE_DEV_LOCK_PREACQUIRED=1` so the pytest and Playwright suites
reuse the orchestrator's hold instead of acquiring their own. When the lock endpoint is
unreachable, or another owner holds the lock, the stage skips.

Both stages gate on `./helm-charts/bin/health-check.sh --keep-lock` first. A red check triggers
provisioning ([Per-worker dev cluster](#per-worker-dev-cluster)) and a re-check; the stage is
**skipped** rather than failed only if provisioning fails — an unhealthy or unprovisionable cluster
is evidence about the infrastructure, not the branch, and must not burn the job's retries.
`--keep-lock` is required: without it the health check offers to release a lock held by another
owner, which prauto must never do.

The orchestrator resolves the env file (`PRAUTO_DEV_ENV_FILE`), `health-check.sh`, and `install.sh`
from the repo checkout rather than the branch worktree, and exports the env file only into the
subshell of the command that needs it — its credentials stay out of the heartbeat and out of every
Claude session.

## Manual Run

```bash
cd /path/to/dataspoke-baseline
.prauto/heartbeat.sh
```

## Troubleshooting

- **Lock issues**: Check `.prauto/state/heartbeat.lock` — if the PID is stale, delete the file.
- **Dev-env lock stuck**: A crashed run can leave the dev-env lock held. Release it with
  `curl -X POST "$DATASPOKE_DEV_LOCK_URL/lock/release" -H 'Content-Type: application/json' -d '{"owner": "prauto-<worker-id>"}'`.
- **Job stuck**: Check heartbeat log and GitHub labels (`prauto:wip`, `prauto:review`) to determine the current phase.
- **Logs**: Check `.prauto/state/heartbeat_cron.log` — the destination the cron/launchd entry redirects to.
- **Integration or E2E silently skipped**: The log names the reason — a missing `PRAUTO_DEV_ENV_FILE`,
  a red health check with provisioning disabled or failed, an unreachable lock endpoint, a failed
  branch deploy, or a diff that touches none of the stage's layers.
- **Job abandoned with `prauto:failed` right after implementation**: the implementation workflow
  escalated — a per-stage reviewer's findings persisted after a fix pass. The abandonment comment
  names the escalating stage and its findings.
- **Session history**: Check `.prauto/state/sessions/` for Claude session outputs.
