# Prauto — Autonomous PR Worker

Prauto is a cron-driven bash worker that monitors GitHub issues labeled `prauto:ready`, invokes Claude Code CLI to analyze and implement changes, and submits pull requests.

See `spec/AI_PRAUTO.md` for the full specification (heartbeat cycle, label lifecycle, phase state machine, security model, prompt templates).

## Prerequisites

- `claude` CLI installed and authenticated
- `gh` CLI installed and authenticated (PAT with Issues, PRs, Contents permissions)
- `git` configured for the repository
- `jq` for JSON processing

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

4. Add a cron entry (adjust the path and schedule):

   ```bash
   # Run heartbeat every 30 minutes, Mon-Fri 9:00-18:00 KST
   */30 9-18 * * 1-5 cd /path/to/dataspoke-baseline && .prauto/heartbeat.sh >> .prauto/state/heartbeat.log 2>&1
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

## Directory Structure

```
.prauto/
├── config.env                  # [COMMITTED] Shared settings
├── config.local.env            # [GITIGNORED] Instance-specific settings
├── heartbeat.sh                # Main cron entry point
├── lib/
│   ├── helpers.sh              # Shared bash helpers
│   ├── quota.sh                # Token quota check
│   ├── issues.sh               # Issue scanning and claiming
│   ├── claude.sh               # Claude Code CLI wrapper
│   ├── git-ops.sh              # Branch creation, worktree, push
│   ├── pr.sh                   # PR creation, feedback, squash-finalize
│   ├── phases.sh               # Phase-specific handlers
│   └── state.sh                # Job state management
├── prompts/
│   ├── system-append.md        # Worker identity prompt
│   ├── issue-analysis.md       # Phase 1: analysis prompt
│   ├── implementation.md       # Phase 2: implementation prompt
│   ├── pr-review.md            # PR reviewer feedback prompt
│   ├── feedback-response.md    # Plan counter-proposal response prompt
│   └── squash-commit.md        # Squash commit message generation
├── state/                      # [GITIGNORED] Runtime state
│   ├── heartbeat.lock          # PID-based lock file
│   ├── heartbeat.log           # Cron output log
│   └── sessions/               # Per-issue session dirs (Claude outputs, job records)
├── worktrees/                  # [GITIGNORED] Git worktrees for active jobs
└── README.md
```

## Manual Run

```bash
cd /path/to/dataspoke-baseline
.prauto/heartbeat.sh
```

## Troubleshooting

- **Lock issues**: Check `.prauto/state/heartbeat.lock` — if the PID is stale, delete the file.
- **Job stuck**: Check heartbeat log and GitHub labels (`prauto:wip`, `prauto:review`) to determine the current phase.
- **Logs**: Check `.prauto/state/heartbeat.log` for cron output.
- **Session history**: Check `.prauto/state/sessions/` for Claude session outputs.
