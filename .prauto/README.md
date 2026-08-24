# Prauto — Autonomous PR Worker (contract surface)

Prauto is the DataSpoke autonomous PR worker: it monitors GitHub issues labeled `prauto:ready`,
produces implementation PRs via a headless coding-agent CLI (Claude Code or Codex), and manages the
issue-to-PR lifecycle.

The system is an **executor + scheduler + contract** split:

- **Contract** (this directory, plus `spec/AI_PRAUTO.md`) — the durable, substrate-independent
  rules: GitHub labels, the phase state machine, the evidence-based plan gate, generator ≠
  reviewer, deploy ordering, quota-pause/resume, and the security model.
- **Executor** — `.prauto/heartbeat.sh` + `.prauto/lib/*.sh`. It implements the tick
  deterministically: PID lock, config, agent selection, claim, phase derivation, dispatch,
  finalize, and the quota-pause resume protocol. Run `bash .prauto/heartbeat.sh` for a manual
  tick.
- **Scheduler** — a thin Hermes cron job (`prauto-loop-master` skill) that probes the agents,
  sets `PRAUTO_AGENT` to the winner, and invokes the executor.

See `spec/AI_PRAUTO.md` for the full specification.

## What lives here

| Path | Purpose |
|------|---------|
| `heartbeat.sh` | The executor entrypoint (one wake of the tick) |
| `lib/*.sh` | Executor modules: `helpers` (logging), `state` (lock/reset), `quota` (agent probe + pause/resume), `agent` (dispatch), `issues` (SSOT readers), `git-ops` (worktrees), `pr` (PR lifecycle), `phases` (phase handlers) |
| `config.env` | Repo-level conventions (committed): labels, branch prefix, max retries, model, org-member filter, reviewer |
| `config.local.env` | Instance identity + secrets (gitignored): `PRAUTO_WORKER_ID`, `PRAUTO_AGENT`, dev-cluster binding, `GH_TOKEN`, `ANTHROPIC_API_KEY` |
| `config.local.env.example` | Committed template for `config.local.env` |
| `prompts/*.md` | Worker phase prompt templates (analysis, implementation, integration-fix, e2e-fix, pr-review, squash-commit, feedback-response, system-append) — consumed by the executor's dispatch |
| `state/` | Runtime state (gitignored) — session artifacts and logs |
| `worktrees/` | Per-issue git worktrees (gitignored) |

The executor is self-contained: `bash .prauto/heartbeat.sh` runs a full tick (it selects the
agent itself when `PRAUTO_AGENT` is unset or `auto`).

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
| `PRAUTO_GITHUB_EXPECTED_ACTOR` | (optional) | The GitHub login the executor must authenticate as; the executor aborts if `gh api user` resolves to anything else — the guard against a comment/label/assignee being attributed to the keyring account instead of the worker |
| `PRAUTO_QUOTA_TIMEOUT` | `45` | Seconds a dry-run may run before it is treated as a timeout (proceed) rather than a rate-limit |

### Scheduler binding (Hermes cron)

The scheduler is a thin Hermes cron job: it probes the agents, sets `PRAUTO_AGENT` to the winner,
and invokes `bash .prauto/heartbeat.sh`. Its settings are preserved as env vars so the job is
reproducible from the repo. Repo-level fields (what the job *is*) live in `config.env`; the
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
the repo owner's credentials. This method uses `gh`'s login-based multi-account support end to
end — no manually-created PAT.

### 1. Invite the bot account as a collaborator

From the repo owner account, go to:

```
https://github.com/youraccount/yourrepo/settings/access
```

Search for the bot account and send the invitation (Write role for an org repo; personal repos
grant Write by default). Log in as the bot account and accept it at
`https://github.com/notifications`.

### 2. Log the bot account into `gh` as a second account

```bash
gh auth login --hostname github.com
```

`gh` detects the already-logged-in owner account and offers to add another rather than replacing
it — authorize the device code while signed into github.com **as the bot account** in the
browser. `gh auth status` then lists both, one flagged `Active account: true`; `gh auth switch
--user <bot-account>` changes which one is active. `gh auth token --user <bot-account>` prints
that account's token without switching — put it straight into `GH_TOKEN` in `config.local.env`.
It's an OAuth token (`gho_…`, revocable via `gh auth logout`), not a manually-created PAT.

### 3. Generate and register a dedicated SSH key

`GH_TOKEN` covers `gh`-driven GitHub API calls (labels, comments, PR creation) — it does **not**
cover `git push`, which authenticates via SSH. Reusing your own key won't work (GitHub allows a
given public key on only one account), so generate one dedicated to the bot:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_<bot-account> -C "<bot-account>" -N ""
gh auth refresh -h github.com -s admin:public_key   # targets whichever account is active
gh ssh-key add ~/.ssh/id_ed25519_<bot-account>.pub --title "prauto-worker ($(hostname -s))"
gh auth refresh -h github.com -s admin:public_key -r admin:public_key   # drop it again — one-time use only
```

`admin:public_key` is needed only for `gh ssh-key add` itself; nothing at runtime uses it (`git
push` authenticates via the key file, not the token's scopes), so remove it right after. Each
`gh auth refresh` reissues the token — re-run `gh auth token --user <bot-account>` and update
`GH_TOKEN` in `config.local.env` after this step, since the string from step 2 no longer matches.

### 4. Scope the SSH key and git identity to prauto's worktrees only

Prauto runs each issue in a linked `git worktree` under `worktrees/`. A linked worktree's
`gitdir` (what `includeIf.gitdir` matches against) is `<repo>/.git/worktrees/<name>` — **not**
the worktree's own checkout path — so scope the pattern there. This keeps the bot identity fully
isolated from the repo owner's own commits/pushes in the primary checkout.

```gitconfig
# ~/.gitconfig
[includeIf "gitdir:/abs/path/to/<repo>/.git/worktrees/"]
	path = ~/.gitconfig-<bot-account>
```

```gitconfig
# ~/.gitconfig-<bot-account>
[core]
	# -F /dev/null skips ~/.ssh/config, which otherwise stacks any Host github.com
	# IdentityFile onto this one and lets ssh silently pick the wrong key.
	sshCommand = ssh -F /dev/null -i ~/.ssh/id_ed25519_<bot-account> -o IdentitiesOnly=yes -o UserKnownHostsFile=~/.ssh/known_hosts
[user]
	name = <bot display name>
	email = <bot-account-id>+<bot-account>@users.noreply.github.com
```

Verify with `git -C <repo>/.git/worktrees/<any-existing-worktree> ls-remote origin` (or `ssh -F
/dev/null -i ~/.ssh/id_ed25519_<bot-account> -o IdentitiesOnly=yes -T git@github.com`) — it
should greet the bot account, not the owner.

### 5. Match `PRAUTO_GIT_AUTHOR_NAME` / `PRAUTO_GIT_AUTHOR_EMAIL` to the same identity

The worker's system prompt runs `git commit --author="{PRAUTO_GIT_AUTHOR_NAME}
<{PRAUTO_GIT_AUTHOR_EMAIL}>"`, which sets commit *authorship* independently of the `user.email`
from step 4 (that config supplies the *committer* identity `git commit` requires, plus the SSH
key). Set `PRAUTO_GIT_AUTHOR_EMAIL` to the same `<id>+<login>@users.noreply.github.com` address —
an unverified or mismatched email leaves commits shown with no linked GitHub account, or linked
to the wrong one.

With all five steps done, GitHub API calls, git push, and commit authorship all consistently
resolve to the bot account.
