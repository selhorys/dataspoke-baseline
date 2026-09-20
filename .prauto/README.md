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
- **Scheduler** — an agent-supervised Hermes cron job. Each tick wakes a supervisor agent that
  reports the trigger to Slack, detaches the executor when idle, and spawns the background
  monitor (`.prauto/scheduler/monitor.sh`) that posts brief Slack notes until the coding agent
  finishes. The supervisor performs no issue work and pre-sets no `PRAUTO_AGENT`; local
  configuration and the executor control agent selection.

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
| `scheduler/launch.sh` | Hermes-binding launcher — detach+verify the executor and monitor in one step |
| `scheduler/daemonize.py` | Detach primitive — setsid double-fork so children survive process-group teardown |
| `scheduler/monitor.sh` | Hermes-binding monitor — detached Slack reporter for one executor run |

The executor is self-contained: `bash .prauto/heartbeat.sh` runs a full tick (it selects the
agent itself when `PRAUTO_AGENT` is unset or `auto`).

## Agent session adapters

Claude and Codex use different session contracts. A fresh Claude invocation receives a harness
generated `--session-id` and later resumes with that value. A fresh Codex invocation uses
`codex exec --json --sandbox workspace-write`. By default it omits an explicit model and inherits
the authenticated account's Codex default; configured compatible model/effort overrides are passed
only as a pair for the fresh invocation. An effort without a model is invalid. The executor records
its native `thread_id` only from the JSONL
`thread.started` event. A later continuation is exactly `codex exec resume --json <thread-id>
<prompt>`: the native thread retains its model and reasoning settings, so the executor does not
reinject them. No Claude-only session, sandbox, tool, turn, or budget flags are applied.

Codex model availability varies by client and authenticated account. `PRAUTO_CODEX_MODEL` is
optional; Codex role bindings also omit a hard-coded model so they inherit that account default.
Before a configured model or effort override is used, the executor preflights compatibility. A
rejected or invalid override fails closed before Codex is invoked, leaves the work item non-ready,
and produces diagnostic evidence. It is a configuration/account-compatibility outcome, never a
quota pause, and PRauto never silently selects a replacement model.

When the harness regains control after a worker invocation, it best-effort pushes any committed
checkpoint on the issue branch, links that branch to the issue, and posts one idempotent issue
comment per commit with a GitHub commit link. Uncommitted work is still discarded with the
worktree; a strict push remains part of final PR creation.

If Codex exits before `thread.started`, PRauto has no trustworthy resume target. It preserves the
raw JSONL artifact and its stderr sidecar for diagnosis but treats the run as an ordinary failed
attempt, so the next heartbeat follows the normal retry/restart path instead of posting a
resumable quota marker. After `thread.started`, PRauto stores a local, persistent anchor for the
issue's current `prauto:ready` lifecycle. It resumes only when the GitHub marker was authored by
the authenticated worker, has a strict UUID, and exactly matches that local anchor; GitHub still
remains the SSOT for phase state.

## Prerequisites

- A coding-agent CLI with a logged-in account: Claude Code (`claude`) and/or Codex (`codex`) —
  both use login-based OAuth, not API tokens.
- `gh` CLI authenticated (PAT with Issues, PRs, Contents permissions).
- `git`, `jq`.

The integration and E2E stages additionally need a reachable dev cluster; see
`spec/AI_PRAUTO.md §Dev Cluster and Deploys`. The executor provisions its own cluster when
needed. A required cluster, lock, or provisioning failure blocks PR readiness rather than
counting as a passing skipped regression.

## Configuration knobs (contract)

| Knob | Default | Meaning |
|------|---------|---------|
| `PRAUTO_GITHUB_REPO` | `selhorys/dataspoke-baseline` | Repo the worker operates on |
| `PRAUTO_GITHUB_LABEL_*` | `prauto:{ready,wip,review,plan-review,done,failed}` | Labels driving the state machine |
| `PRAUTO_BASE_BRANCH` | `dev` | Base branch for branches and PRs |
| `PRAUTO_BRANCH_PREFIX` | `prauto/` | Branch prefix (`prauto/I-<n>`) |
| `PRAUTO_WORKER_ID` | (set per instance) | Instance identity — unique per worker on a shared repo |
| `PRAUTO_AGENT` | `auto` | `claude` \| `codex` \| `auto` (Claude then Codex) |
| `PRAUTO_CODEX_MODEL` | (unset) | Optional fresh-session override; when unset, Codex inherits the authenticated account's default model |
| `PRAUTO_CODEX_EFFORT` | (unset) | Required with `PRAUTO_CODEX_MODEL` as its fresh-session override pair; invalid alone; resumed threads retain their existing setting |
| `PRAUTO_OPEN_ISSUE_LIMIT` | `1` | Max open issues this worker holds concurrently |
| `PRAUTO_MAX_RETRIES_PER_JOB` | `4` | Heartbeat-marked attempts before abandonment; counted separately for each `prauto:ready` label lifecycle |
| `PRAUTO_DEV_ENV_FILE` | `helm-charts/.env.dev` | This worker's dedicated dev-cluster env file; resolves under the repo checkout, never a worktree |
| `PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY` | `true` | Restrict `prauto:ready` pickup to org members |
| `PRAUTO_GITHUB_EXPECTED_ACTOR` | (optional) | The GitHub login the executor must authenticate as; the executor aborts if `gh api user` resolves to anything else — the guard against a comment/label/assignee being attributed to the keyring account instead of the worker |
| `PRAUTO_QUOTA_TIMEOUT` | `45` | Seconds a dry-run may run before it is treated as a timeout (proceed) rather than a rate-limit |

Claude-specific `PRAUTO_CLAUDE_MAX_TURNS_*` and `PRAUTO_CLAUDE_MAX_BUDGET_*` settings are not
passed to Codex. Codex quota probes and worker sessions use JSONL so the executor can classify its
structured rate-limit/error events and capture the native thread id.

### Scheduler binding (Hermes cron as reference)

The scheduler triggers the executor on a cadence. It probes no agent and pre-sets no
`PRAUTO_AGENT`; local configuration and executor selection
(`select_agent`) choose the agent. The reference binding shipped here is an **agent-supervised
Hermes cron job**: each tick wakes a supervisor agent that reports the trigger to Slack, detaches
the executor when idle, and spawns the background monitor (`.prauto/scheduler/monitor.sh`) that
keeps posting brief Slack notes until the coding agent finishes. The supervisor performs no issue
work and holds no state between ticks — the detached executor and monitor are the long-running
parts, and the executor's PID lock makes an "already running" tick a no-op. Other bindings must
follow the scope in `spec/AI_PRAUTO.md §Other scheduler bindings`.

The Hermes binding's settings are preserved as env vars so the job is reproducible from the repo.
Repo-level fields (what the job *is*) live in `config.env`; the instance-identity fields
(where/who runs it) live in `config.local.env` (gitignored).

| Var | File | Meaning |
|-----|------|---------|
| `PRAUTO_SCHEDULER_HERMES_SCHEDULE` | config.env | Wake cadence (`15 * * * *` — hourly at :15 past) |
| `PRAUTO_SCHEDULER_HERMES_NAME` | config.env | Cron job name |
| `PRAUTO_SCHEDULER_HERMES_SKILL` | config.env | Supervisor skill loaded by the cron job (`prauto-executor`) |
| `PRAUTO_SCHEDULER_HERMES_PROMPT_FILE` | config.env | Canonical supervisor prompt (`.prauto/scheduler/supervisor-prompt.md`) — the job's `prompt` field |
| `PRAUTO_SCHEDULER_HERMES_SKILL_FILE` | config.env | Supervisor skill source (`.prauto/scheduler/prauto-executor/SKILL.md`) — install into the Hermes profile |
| `PRAUTO_SLACK_TARGET` | config.env | Slack channel the supervisor and monitor report to (`slack:hermes-dev`) |
| `PRAUTO_MONITOR_CHECK_SECS` | config.env | Monitor liveness poll cadence (default 60) |
| `PRAUTO_MONITOR_INTERVAL_SECS` | config.env | Monitor Slack-report cadence while running (default 600) |
| `PRAUTO_MONITOR_SEND_RETRY_SECS` | config.env | Delay before the monitor's single Slack-send retry (default 5) |
| `PRAUTO_SCHEDULER_HERMES_WORKDIR` | config.local.env | Local checkout path (the job's `workdir`) |
| `PRAUTO_SCHEDULER_HERMES_DELIVER` | config.local.env | Where the supervisor's final response is archived (`local`); Slack reporting is explicit via `hermes send` |
| `PRAUTO_SCHEDULER_HERMES_HOME` | config.local.env | Optional: the Hermes profile home that resolves `PRAUTO_SLACK_TARGET` (defaults to the inherited `HERMES_HOME`, then the default home) |

The monitor's canonical source is `.prauto/scheduler/monitor.sh`; the supervisor detaches it via
`.prauto/scheduler/launch.sh` (which uses `.prauto/scheduler/daemonize.py` — a setsid double-fork
— so the executor and monitor run in their own sessions and survive the supervisor turn's
process-group teardown). `launch.sh` also validates the monitor detach: a non-numeric or
immediately-dead monitor PID is a `MONITOR_FAILED`/`MONITOR_EXITED_IMMEDIATELY` result (exit 1),
never a silent success. The monitor posts its own Slack notes via `hermes send` (no LLM, no
running gateway required). Because Slack is the only human surface for a detached run, the monitor
preflights its channel: the target must resolve under the profile home above (`hermes send --list`),
or the monitor exits nonzero with the reason in `.prauto/state/monitor-slack-unresolved` — reported
as `MONITOR_EXITED_IMMEDIATELY … reason=…`, never silence. A send that fails twice is appended to
`.prauto/state/monitor-undelivered.log`. See `spec/AI_PRAUTO.md §Executor and Scheduler` for the
create call that consumes these vars.

Before creating the job, install the supervisor skill into the Hermes profile that runs it, or the
job rejects the unknown `prauto-executor` skill:

```bash
mkdir -p ~/.hermes/skills/prauto-executor
cp .prauto/scheduler/prauto-executor/SKILL.md ~/.hermes/skills/prauto-executor/SKILL.md
```

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
