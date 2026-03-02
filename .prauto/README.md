# Prauto — Autonomous PR Worker

Prauto is a cron-driven bash worker that monitors GitHub issues labeled `prauto:ready`, invokes Claude Code CLI to analyze and implement changes, and submits pull requests.

See `spec/AI_PRAUTO.md` for the full specification.

## Prerequisites

- `claude` CLI installed and authenticated
- `gh` CLI installed and authenticated (fine-grained PAT with Issues, PRs, Contents permissions)
- `git` configured for the repository
- `jq` for JSON processing

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

---

## Setup

1. Copy the instance config template:

   ```bash
   cp .prauto/config.local.env.example .prauto/config.local.env
   ```

2. Edit `.prauto/config.local.env` with your worker identity, Claude model, and secrets.

3. Add a cron entry (adjust the path and schedule):

   ```bash
   # Run heartbeat every 30 minutes, Mon-Fri 9:00-18:00 KST
   */30 9-18 * * 1-5 cd /path/to/dataspoke-baseline && .prauto/heartbeat.sh >> .prauto/state/heartbeat.log 2>&1
   ```

## Directory Structure

```
.prauto/
├── config.env                  # [COMMITTED] Shared settings
├── config.local.env            # [GITIGNORED] Instance-specific settings
├── heartbeat.sh                # [COMMITTED] Main cron entry point
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
│   └── squash-commit.md        # Phase 4: squash commit message generation
├── state/                      # [GITIGNORED] Runtime state
│   ├── current-job.json        # Active job metadata
│   ├── heartbeat.lock          # PID-based lock file
│   ├── heartbeat.log           # Cron output log
│   ├── history/                # Completed job summaries
│   └── sessions/               # Claude session outputs
└── README.md
```

## Security: Organization-Member Filter

By default, prauto only picks up `prauto:ready` issues authored by members of the repository's GitHub organization. This prevents external actors from injecting work into the pipeline by opening issues with the `prauto:ready` label.

Controlled by `PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY` in `config.env` (set to `"true"` by default). Set to `""` (empty) to disable the filter. This feature requires the repository to be owned by a GitHub organization; it does not work with personal-account repos.

## How It Works

Each heartbeat performs at most one job:

1. Acquires a PID-based lock (prevents concurrent runs)
2. Loads config and checks Claude token quota
3. Resumes any interrupted job from a prior heartbeat
4. Squash-finalizes approved PRs — rebuilds the commit message via Claude, producing a single conventional commit with `(issue #N, PR #N)` reference and `Co-Authored-By` trailers for org-member approvers. Does NOT merge the PR — leaves that for the human
5. Checks open PRs for reviewer comments to address (skips PRs with a "feedback addressed" marker)
6. Finds an eligible issue (oldest with `prauto:ready` label)
7. Claims the issue (optimistic lock via label swap)
8. Runs Phase 1: Analysis (read-only Claude session)
9. Posts plan and waits for approval (non-minor changes pause here; minor changes proceed immediately)
10. Runs Phase 2: Implementation (read+write Claude session)
11. Pushes branch and creates/updates PR

## GitHub Label Setup

Labels are defined in `.github/labels.yml`. Sync them to the repository once:

```bash
npx github-label-sync --access-token "$(gh auth token)" --labels .github/labels.yml <owner>/<repo>
```

## Label Lifecycle

```
[human adds prauto:ready]
    ├── prauto claims → removes prauto:ready, adds prauto:wip
    │       ├── success → removes prauto:wip, adds prauto:review
    │       │       └── approved + squash-finalized → removes prauto:review, adds prauto:done
    │       └── failure → removes prauto:wip, adds prauto:failed
    └── (no prauto pickup yet → stays prauto:ready)
```

## Manual Run

```bash
cd /path/to/dataspoke-baseline
.prauto/heartbeat.sh
```

## Troubleshooting

- **Lock issues**: Check `.prauto/state/heartbeat.lock` — if the PID is stale, delete the file.
- **Job stuck**: Check `.prauto/state/current-job.json` for the current phase and retry count.
- **Logs**: Check `.prauto/state/heartbeat.log` for cron output.
- **Session history**: Check `.prauto/state/sessions/` for Claude session outputs.
