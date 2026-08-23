# Prauto — Autonomous PR Worker (contract surface)

Prauto is the DataSpoke autonomous PR worker: it monitors GitHub issues labeled `prauto:ready`,
produces implementation PRs via a headless coding-agent CLI (Claude Code or Codex), and manages the
issue-to-PR lifecycle.

The system is a **loop master + contract** split:

- **Contract** (this directory, plus `spec/AI_PRAUTO.md`) — the durable, substrate-independent
  rules: GitHub labels, the phase state machine, the evidence-based plan gate, generator ≠
  reviewer, deploy ordering, and the security model.
- **Loop master** — the executor. It wakes on a schedule (a Hermes `cronjob`), gates on
  concurrency, probes agent availability (Claude → Codex fallback), derives phase from GitHub, and
  dispatches worker/reviewer processes. See the `prauto-loop-master` skill.

See `spec/AI_PRAUTO.md` for the full specification.

## What lives here

| Path | Purpose |
|------|---------|
| `config.env` | Repo-level conventions (committed): labels, branch prefix, max retries, model, org-member filter, reviewer |
| `config.local.env` | Instance identity + secrets (gitignored): `PRAUTO_WORKER_ID`, `PRAUTO_AGENT`, dev-cluster binding, `GH_TOKEN`, `ANTHROPIC_API_KEY` |
| `config.local.env.example` | Committed template for `config.local.env` |
| `prompts/*.md` | Worker phase prompt templates (analysis, implementation, integration-fix, e2e-fix, pr-review, squash-commit, feedback-response, system-append) — consumed by the loop master's worker dispatch |
| `state/` | Runtime state (gitignored) — session artifacts and logs |
| `worktrees/` | Per-issue git worktrees (gitignored) |

The v0.7 bash harness (`heartbeat.sh` + `lib/*.sh`) and its two operational skills
(`prauto-run-heartbeat`, `prauto-check-status`) have been removed. The loop master is the sole
executor; what remains here is the contract surface both the former harness and the loop master
consume.

## Prerequisites

- A coding-agent CLI with a logged-in account: Claude Code (`claude`) and/or Codex (`codex`) —
  both use login-based OAuth, not API tokens.
- `gh` CLI authenticated (PAT with Issues, PRs, Contents permissions).
- `git`, `jq`.

The integration and E2E stages additionally need a reachable dev cluster; see
`spec/AI_PRAUTO.md §Dev Cluster and Deploys`. Each stage skips itself when its dependencies are
absent.

## Configuration knobs (contract)

| Knob | Default | Meaning |
|------|---------|---------|
| `PRAUTO_GITHUB_REPO` | `selhorys/dataspoke-baseline` | Repo the worker operates on |
| `PRAUTO_GITHUB_LABEL_*` | `prauto:{ready,wip,review,plan-review,done,failed}` | Labels driving the state machine |
| `PRAUTO_BASE_BRANCH` | `dev` | Base branch for branches and PRs |
| `PRAUTO_BRANCH_PREFIX` | `prauto/` | Branch prefix (`prauto/I-<n>`) |
| `PRAUTO_WORKER_ID` | (set per instance) | Instance identity — unique per worker on a shared repo |
| `PRAUTO_AGENT` | `claude` | `claude` \| `codex` \| `auto` (Claude then Codex) |
| `PRAUTO_OPEN_ISSUE_LIMIT` | `1` | Max open issues this worker holds concurrently |
| `PRAUTO_MAX_RETRIES_PER_JOB` | `3` | Heartbeat-marked attempts before abandonment |
| `PRAUTO_DEV_ENV_FILE` | `helm-charts/.env.dev` | This worker's dedicated dev-cluster env file; resolves under the repo checkout, never a worktree |
| `PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY` | `true` | Restrict `prauto:ready` pickup to org members |

### Loop-master binding (Hermes cron)

The loop master is a scheduled Hermes cron job. Its settings are preserved as env vars so the job
is reproducible from the repo. Repo-level fields (what the job *is*) live in `config.env`; the
instance-identity fields (where/who runs it) live in `config.local.env` (gitignored).

| Var | File | Meaning |
|-----|------|---------|
| `PRAUTO_LOOP_MASTER_HERMES_SCHEDULE` | config.env | Wake cadence (`every 4h`) |
| `PRAUTO_LOOP_MASTER_HERMES_NAME` | config.env | Cron job name |
| `PRAUTO_LOOP_MASTER_HERMES_SKILLS` | config.env | Skills loaded per tick (`prauto-loop-master claude-code codex`) |
| `PRAUTO_LOOP_MASTER_HERMES_TOOLSETS` | config.env | Toolsets scoped to the tick (`terminal file`) |
| `PRAUTO_LOOP_MASTER_HERMES_PROFILE` | config.local.env | Hermes profile that hosts the job |
| `PRAUTO_LOOP_MASTER_HERMES_WORKDIR` | config.local.env | Local checkout path (the job's `workdir`) |
| `PRAUTO_LOOP_MASTER_HERMES_DELIVER` | config.local.env | Where tick summaries go (`local`, `telegram`, …) |

See `spec/AI_PRAUTO.md §Installing the loop-master cron job` for the create call that consumes these.

## Labels

The `prauto:*` label set must exist on the repo. Sync once:

```bash
npx github-label-sync --access-token "$(gh auth token)" --labels .github/labels.yml <owner>/<repo>
```

## Optional: Dedicated GitHub Bot Account

Running prauto under a separate GitHub account (e.g., `youraccount-prauto`) keeps bot activity
visually distinct from human commits and PR comments. This is optional — prauto works fine with
the repo owner's credentials.

### 1. Invite the bot account as a collaborator

From the repo owner account, go to:

```
https://github.com/youraccount/yourrepo/settings/access
```

Search for the bot account and send the invitation. For personal repositories, collaborators
receive Write access by default (no role selector is shown). For organization repositories, select
the **Write** role.

### 2. Accept the invitation

Log in as the bot account and accept the collaborator invitation at
`https://github.com/notifications`.

### 3. Create a classic PAT from the bot account

Go to `https://github.com/settings/tokens/new` (logged in as the bot account) and check the
**`repo`** scope.

> **Why classic PAT, not fine-grained?**
> Fine-grained PATs require the Resource owner to be the token creator or one of their
> organizations. Another personal account (the repo owner) cannot appear as a Resource owner, so
> fine-grained PATs cannot be scoped to a repo owned by a different personal account.

### 4. Set the token in `config.local.env`

```bash
GH_TOKEN="«redacted:ghp_…»"
```

All GitHub API operations (issue labels, comments, PR creation) will then run as the bot account.
Git commit identity remains whatever is set in `PRAUTO_GIT_AUTHOR_NAME` / `PRAUTO_GIT_AUTHOR_EMAIL`.
