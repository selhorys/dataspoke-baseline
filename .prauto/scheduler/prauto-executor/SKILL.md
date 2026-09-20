---
name: prauto-executor
description: "Use when running or debugging the DataSpoke PRauto worker."
version: 4.3.0
author: DataSpoke (dataspoke-baseline)
license: MIT
platforms: [macos, linux]
metadata:
  hermes:
    tags: [prauto, autonomous, orchestration, claude-code, codex, github, dataspoke]
    related_skills: [claude-code, codex, hermes-agent]
---

# PRauto Executor + Supervisor

The executor + scheduler for the DataSpoke autonomous PR worker (`prauto`).

The whole tick is owned by the executor — `.prauto/heartbeat.sh` plus `.prauto/lib/*.sh` in the
repo: PID lock, config, agent selection, claim, phase derivation, dispatch, finalize, and the
quota-pause resume protocol. The scheduler is an **agent-supervised Hermes cron job**: each tick
wakes THIS skill's supervisor procedure, which reports to Slack, detaches the executor when idle,
and spawns the background monitor (`.prauto/scheduler/monitor.sh`) that keeps reporting until the
coding agent finishes. The durable contract (labels, phase state machine, plan gate, security
model, deploy ordering, quota-pause/resume) lives in `spec/AI_PRAUTO.md`.

## Repo & config

- Repo: this checkout (the cron job's `workdir`).
- Executor: `.prauto/heartbeat.sh` (entrypoint) + `.prauto/lib/*.sh`.
- Launcher: `.prauto/scheduler/launch.sh` (detach+verify the executor and the monitor).
- Monitor: `.prauto/scheduler/monitor.sh` (detached Slack reporter; no LLM).
- Config: `.prauto/config.env` (committed) + `.prauto/config.local.env` (gitignored).
- `PRAUTO_AGENT`: `claude` | `codex` | `auto`. Pinned in `config.local.env`; the executor's
  `select_agent` honors it. The supervisor does NOT pre-set it.
- `PRAUTO_SLACK_TARGET`: Slack channel for reporting (default `slack:hermes-dev`).

## The supervisor (agent cron) — per-tick procedure

The cron job loads this skill and fires an agent with terminal access. Each tick the supervisor
must:

1. **Load the config first** (Hermes cron does not normally inherit these vars). The job's
   `workdir` is already the repo checkout, so source the committed config and the gitignored
   local overrides from the current directory, so `PRAUTO_SLACK_TARGET` and any
   `config.local.env` override resolve before the first send:
   ```bash
   source .prauto/config.env
   [[ -f .prauto/config.local.env ]] && source .prauto/config.local.env
   ```

2. **Report the trigger** to Slack (a terminal call, not an LLM answer; default
   `slack:hermes-dev`):
   `hermes send --to "${PRAUTO_SLACK_TARGET:-slack:hermes-dev}" "🔔 prauto heartbeat cron triggered …"`

3. **Launch + verify via the launcher** — do NOT use `nohup`/`&` (the terminal tool
   blocks shell-level background wrappers AND tears down their process group on turn end;
   the launcher uses `daemonize.py`'s setsid double-fork so the executor/monitor survive):
   ```bash
   bash .prauto/scheduler/launch.sh
   ```
   It prints exactly one status line plus (on early exit) the log tail:
   - `ALREADY_RUNNING pid=N` → the executor is mid-run; report and do not launch again.
   - `STARTED pid=N monitor_pid=M` → report 🚀 started + monitor attached.
   - `EXITED_IMMEDIATELY pid=N` + log tail → report ⚠️ exited immediately + the reason line
     (e.g. `No coding agent available`, `Claude auth check failed`), then STOP.
   - `LAUNCH_FAILED …` → report the failure verbatim.
   - `MONITOR_FAILED …` / `MONITOR_EXITED_IMMEDIATELY …` → the executor launched but the
     monitor (Slack reporting) did not survive; report ⚠️ reporting degraded + the status line,
     including its `reason=…` field when present (a bare status line hides the cause).

   The job's `prompt` field is the canonical `.prauto/scheduler/supervisor-prompt.md` placed
   verbatim, so it must be re-synced whenever that file changes (or when you find it stale) —
   otherwise the running supervisor follows an older procedure than the repo it came from:
   `hermes -p <profile> cron edit <job_id> --prompt "$(cat .prauto/scheduler/supervisor-prompt.md)"`.
   The skill loads from the profile's installed copy, so sync that too (repo file is canonical).

   `launch.sh` detaches the executor, waits ~5s to confirm it survived, then detaches the
   background monitor (`.prauto/scheduler/monitor.sh`) and verifies it too. The monitor posts a
   brief Slack note every `PRAUTO_MONITOR_INTERVAL_SECS` (default 600) while the executor runs,
   then a final result (done / no-agent / waiting-approval / quota-paused / crashed) and exits.

4. **Report** to Slack what `launch.sh` returned (see the status→message mapping above),
   then end the turn. Do NOT wait on the executor — it is detached and long-running.

## Reading executor state (log markers)

`.prauto/state/heartbeat_cron.log` — every wake appends a run header and `[INFO]`/`[WARN]` lines
(ANSI-coloured). Strip colour with `sed -E 's/\x1b\[[0-9;]*m//g'`.

| Marker | Meaning |
|---|---|
| `Lock acquired (PID …)` | Executor started |
| `Dispatching issue #N (phase: X, attempt: Y/Z)` | A coding agent is about to run (retry counter from state file) |
| `Heartbeat complete.` | Tick finished |
| `Issue #N exceeded max retries (M/Z).` | Local retry counter hit PRAUTO_MAX_RETRIES_PER_JOB; issue abandoned |
| `No coding agent available this wake.` | No agent passed the probe (auth/quota) |
| `Claude auth check failed.` | Claude CLI logged out (`claude auth login` fixes it) |
| `waiting for plan approval` | Waiting on a human, not quota |
| `quota-paused (claude). Waiting.` | Quota-paused; resumes next window |
| `Codex model/effort override is invalid. No dispatch this wake.` | Bad `PRAUTO_CODEX_MODEL`/`PRAUTO_CODEX_EFFORT` pair; no dispatch, no retry burned |

## Diagnosing a long integration-fix loop (Stage 3, pre-PR)

A tick that sits for hours in `Integration test fix loop: attempt N/10` while `git log` shows no
new commits is usually an environment flake, not a code problem — Stage 3 has **no flake
classifier**, so it re-rolls the same group with a fresh coding agent every attempt. Read it in
this order:

1. **Attempt timeline** — the loop posts one comment per attempt:
   `gh api "repos/<org>/<repo>/issues/<N>/comments?per_page=30" --jq '.[] | select(.body|test("integration test fix loop: attempt")) | "\(.created_at) \(.body)"'`
2. **Output check** — `git fetch -q origin <branch> && git log --since='<window>' --oneline master..origin/<branch>`.
   Zero commits across several attempts means nothing was branch-attributable to fix.
3. **What actually failed** — the per-attempt evidence is base64 **in the worker prompt**: the first
   record of the session transcript `~/.claude/projects/-<repo-slug>-I-<issue>/<session>.jsonl`
   (its `content` holds one fenced base64 block → `json.loads(base64.b64decode(...))` →
   `{failed_stages, test_output}`). Per-session cost/turns/result are in
   `.prauto/state/sessions/issue-<N>/<run>/agent-*.json`.
4. **Verdict** — a different, non-overlapping test set failing each attempt, with transport errors
   only (`Connect call failed`, `httpx.ConnectError`, `asyncpg ... connection was closed in the
   middle of operation`), on files the branch never touched = environment flake.

- **The worker's own `PRAUTO_TARGETED_VERIFICATION_JSON` pass does not end Stage 3.** Only Stage 5
  consumes it (`validate_targeted_verification`, keyed on `POST_PR_FAILED_STAGES`), so a worker
  attesting "369 passed on this head" changes nothing in the pre-PR loop.
- **Env-side triage**: an operator-side connection probe (SYN / idle-HOLD / psql QUERY), launched
  through `.prauto/scheduler/daemonize.py` so it survives the turn, separates a laptop↔LB drop from
  a healthy path; cluster node churn shows as
  `kubectl get events -A --field-selector reason=ScaleDown`.

## Manual tick

Run `bash .prauto/heartbeat.sh` directly from the repo root. Do NOT pre-set `PRAUTO_AGENT` — the
executor re-reads it from `config.local.env` and selects the agent itself. To watch a run without
spamming Slack, run the monitor in the foreground with `PRAUTO_MONITOR_DRY_RUN=1`.

## Pitfalls

- **Retry counting is state-file based, not GitHub-comment based.** The local counter
  (`.prauto/state/retry-count-<issue>.json`) increments only in the normal dispatch path —
  quota-pause cycles short-circuit before it, so quota deaths never burn retry slots. A
  resume is a continuation, not a new attempt. `PRAUTO_MAX_RETRIES_PER_JOB` defaults to 4.
- **A wake can legitimately end without `Heartbeat complete.`** Two early-exit paths return before
  that marker: `No coding agent available this wake.` (agent probe failed) and `Codex model/effort
  override is invalid. No dispatch this wake.` (a `PRAUTO_CODEX_MODEL`/`PRAUTO_CODEX_EFFORT` pair that
  failed preflight — both must be set together; valid models are `gpt-5.6`/`gpt-5.6-terra`/
  `gpt-5.6-luna`, valid efforts `low|medium|high|xhigh|max|ultra`, and bare aliases like `terra` are
  rejected). The monitor's classifier only recognizes the first one (`no-agent`); it reports the
  second as "task exited unexpectedly". Read the log tail instead of trusting that Slack line. Both
  are inert while `PRAUTO_AGENT=claude`.
- **A code-affecting branch gates on one full post-PR regression.** The executor provisions and
  *retains* its dev cluster through PR creation — tearing it down only at heartbeat exit — then runs
  the full static + unit + spot + api-wired + E2E suites against the exact pushed PR head. If that
  run fails, one turn-bounded coding-agent session fixes and tests only the failed stages; the
  executor pushes the commit, verifies the exact head, and independently retries just those stages.
  A targeted pass is review-ready without a second full regression. The final PR comment separates
  initial failures from targeted-retry passes; a diff confined to the executor's non-code exclusion
  set is exempt.
- **Do not do the tick's work in the supervisor.** Claim, phase derivation, dispatch, and
  finalize are the executor's. The supervisor only launches and monitors.
- **Detach via `launch.sh`, never `nohup &`.** The Hermes terminal tool blocks shell-level
  background wrappers AND tears down their process group (killpg) when the turn ends. `launch.sh`
  uses `daemonize.py`'s setsid double-fork, which escapes killpg (Hermes's own scheduler notes
  killpg "misses setsid grandchildren").
- **Both agents are login-based OAuth, not API tokens** — probe the CLI (`claude auth status`,
  `~/.codex/auth.json`), never inspect a token. A dry-run timeout is not exhaustion.
- **Quota-pause/resume is executor-owned.** The monitor only reports it; it never resumes or
  restarts a session.
- **`gh` identity is pinned by the executor** — it resolves `gh api user` once and asserts it
  against `PRAUTO_GITHUB_EXPECTED_ACTOR` (when set).
- **The monitor is self-terminating and idempotent** (`monitor.lock`). A re-fired tick that finds
  a live executor and a live monitor just reports status and ends.
- **A `gh pr create` GraphQL error after the PR already exists is recovered as a `[WARN]`**, not a
  hard failure — the executor checks for the PR by branch head before giving up.
- **The reviewer is requested when the PR reaches `prauto:review`**, not at PR creation.
- **A `[WARN]` naming a lock URL (`.../lock/status`) is a dev-env lock-endpoint skip**, not a
  branch failure — read the URL to tell a stale one from a genuinely down lock service.
- **A pre-PR static/unit failure is deferred to the post-PR gate**, not itself a branch regression
  verdict — the mandatory post-PR regression is the sole readiness authority.
- **Never shorten a stuck Stage 3 loop by faking its inputs.** Deleting/renaming
  `tests/integration/spot` (or breaking the deployed-API binding) makes `INTEG_EXIT=0` and lets the
  pipeline open a PR for a stage that never ran. Report instead: the loop is bounded (attempt 10 →
  `Max integration fix retries reached. Proceeding with current state.`) and the post-PR gate owns
  readiness.
- **Killing a mid-Stage-3 executor does not skip the stage.** The next wake re-derives
  `implementation` and restarts Stage 3 with a fresh attempt budget — strictly worse. Report the
  ETA (≈ attempt-average minutes × attempts left) instead of restarting.
- **A hard stop leaves the dev cluster provisioned.** The heartbeat's EXIT trap
  (`heartbeat.sh` `cleanup`) removes the live worktree first and then calls
  `teardown_provisioned_dev_env` → `uninstall.sh --delete-all`, which takes minutes. SIGTERM
  followed by SIGKILL a few seconds later truncates that teardown: the worktree is gone but
  `.prauto/state/dev-env-provisioned.json` and the cluster namespaces remain (up to the next
  wake's `recover_orphaned_dev_env()`, which re-attempts it).
  - To stop a running tick and free the cluster now: after killing the tree, run
    `bash helm-charts/bin/uninstall.sh --profile dev --env-file helm-charts/.env.dev \
    --no-question --delete-all` yourself, then `rm .prauto/state/dev-env-provisioned.json` and
    the stranded `heartbeat.lock`/`monitor.lock`. Give SIGTERM a few minutes if you want the trap
    to do the teardown instead.
  - That command tears down the dev *stack* only — the GKE cluster (e.g. `dev-env-02`) is
    pre-existing and shared; never delete it as part of stopping a tick unless explicitly told to.
- **The monitor's reporting channel is preflight-verified; a wrong profile home is loud, not
  silent.** `hermes send` resolves `PRAUTO_SLACK_TARGET` from ONE Hermes profile home, so the
  monitor resolves `PRAUTO_SCHEDULER_HERMES_HOME` (config.local.env) → inherited `HERMES_HOME` →
  default home, then checks the target with `hermes send --list <platform>` before it starts
  watching. An unresolvable target exits nonzero: the launcher reports
  `MONITOR_EXITED_IMMEDIATELY pid=… monitor_pid=… reason=monitor: cannot resolve Slack target …`,
  and the reason also lands in `.prauto/state/monitor-slack-unresolved`. Fix by pinning
  `PRAUTO_SCHEDULER_HERMES_HOME` to the profile that owns the channel — never by dropping the
  monitor. A send that fails twice (one retry, `PRAUTO_MONITOR_SEND_RETRY_SECS`) is appended to
  `.prauto/state/monitor-undelivered.log` instead of vanishing.
- **Launching the scheduler by hand from a plain shell loses Slack reporting unless the home is
  pinned.** The cron supervisor's child env carries `HERMES_HOME=<profile home>`; a manual
  `bash .prauto/scheduler/launch.sh` from a shell without it resolves the default home, where the
  target does not exist. Pinning `PRAUTO_SCHEDULER_HERMES_HOME` in config.local.env makes the
  launcher's reporting independent of how it was launched.
- **A worker session killed in-flight writes a 0-byte result artifact.**
  `.prauto/state/sessions/issue-<N>/<run>/agent-<session>.json` is written only after the session
  exits, so a SIGKILLed attempt leaves it empty — that attempt's `total_cost_usd`/`num_turns` are
  unrecoverable and per-attempt cost sums silently under-count it. The transcript under
  `~/.claude/projects/...` survives; the result file does not.
- **A logged-out Claude CLI passes the `claude auth status` gate but kills the wake at the dry-run.**
  `claude auth status` still exits 0 when logged out (`{"loggedIn": false, "authMethod": "none"}`),
  so `check_quota`'s auth gate passes and the wake dies at the JSON probe instead:
  `Claude dry-run failed (exit 0):` with empty stderr, then `No coding agent available this wake.`
  Verify with `claude auth status` (look at `loggedIn`, not the exit code) and fix with the
  interactive `claude auth login` — an empty `ANTHROPIC_API_KEY` in `config.local.env` is the
  normal state, not a fallback. Codex is the ready alternative: confirm its auth with
  `codex exec --json --sandbox workspace-write "Reply with exactly: OK"` (exit 0 + `item.completed`),
  then temporarily set `PRAUTO_AGENT="codex"` — note `--disallowedTools` tool enforcement is
  Claude-only, so fix sessions lose their `Agent`/`Workflow`/`Task` block.
- **Resuming a paused job leaves `next_run_at` in the past.** `hermes cron resume <id>` keeps the
  stale timestamp, so the job is immediately overdue and either the gateway ticker catches it up
  (`last_dispatch.kind = catch_up`) or `hermes -p <profile> cron run <id>` fires it synchronously
  (`Ran now: succeeded` prints after the supervisor turn completes). Run once from the repo root so
  the supervisor's `workdir` is the checkout; do not hand-run `heartbeat.sh` for the first wake.
- **The implementation phase needs the `Workflow` tool, which the Claude CLI registers only on explicit
  opt-in.** Claude Code gates dynamic workflows behind `enableWorkflows` (its schema: "Unset = default
  by plan"), and this account's plan default is off, so a bare `claude -p` session's tool list has no
  `Workflow` at all — `--allowedTools` cannot re-enable an unregistered tool. The implementation
  prompt's wf-minimal binding is then unsatisfiable, and a worker that obeys the prompt escalates with
  zero commits (`Implementation workflow escalated for issue #N. Abandoning.`, `prauto:failed`, branch
  head unchanged). The executor applies `CLAUDE_CODE_WORKFLOWS` (default 1; `PRAUTO_CLAUDE_WORKFLOWS=0`
  in `config.local.env` opts out) to the **implementation invocation and its quota-resume only**, not
  to every Claude session — so do not reason about a fix, analysis or pr-review session as though it
  had the tool. There is no Codex inline fallback for a Claude session: an absent `Workflow` tool in
  the implementation phase is a harness fault the prompt requires the worker to escalate on, because a
  self-driven substitute leaves no evidence of whether each stage's reviewer verdict was collected.
  Verify a live session with
  `claude -p "Reply with exactly: OK" --output-format stream-json --verbose --max-turns 1` and read the
  `system`/`init` event's `tools` — not the prompt's tool names. Fix sessions lose the tool
  through `FIX_DENY_TOOLS_EXTRA`'s `--disallowedTools` block (verified: absent even with the opt-in).
- **A retry counter that went DOWN is a refunded attempt, not corruption.** `claude -p` terminates a
  print-mode session once background tasks outlive its wait ceiling, exiting 0 with
  `Background tasks still running after <N>s; terminating.` on stderr — so the implementation session
  dies before wf-minimal reports, no `PRAUTO_WORKFLOW_OUTCOME` sentinel is emitted, and the stages that
  already ran have still committed. The executor classifies that from **its own wall-clock measurement
  of the session** — not from the stderr message above, and not from the worker's report. Both of those
  are worker-writable (the worker has a shell and runs as the executor's user), so neither can gate a
  refund; a session whose elapsed time reached the ceiling must have been terminated by the CLI. The ceiling is
  `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`, applied to the implementation invocation, default `14400000`
  (4h) — deliberately finite, since `0` waits forever and would both hold the executor PID lock for the
  full `PRAUTO_AGENT_TIMEOUT_SECS` and make this classification unreachable. Refunds are capped by
  `PRAUTO_MAX_REFUNDS_PER_JOB` (default 2) per ready-label lifecycle, and only the dispatch that
  consumed an attempt may refund one, so `PRAUTO_MAX_RETRIES_PER_JOB` still terminates the job.
- **The subagent tool is named `Task` from Claude Code 2.1.273 on (`Agent` through 2.1.271).** A
  whitelist entry for a name the CLI no longer exposes matches nothing, so `IMPLEMENTATION_ALLOWED_TOOLS`
  names both; the same rename makes a whitelist-only diagnosis wrong about what a session can do.
- **A workflow escalation is not necessarily a reviewer finding.** The sentinel is a single token, so
  the abandonment comment names both causes (a persisting REVISE after three fix passes, or an
  unrunnable workflow loop). Read the worker's own report before assuming a reviewer blocked the change.
- **A `spec`-stage ESCALATE is usually missing review evidence, not a spec defect.** Reviewer
  subagents run with `Read, Glob, Grep` and no shell, and their roles require the parent-supplied
  `Untrusted per-pass evidence` to carry the complete diff — status, staged/unstaged diffs,
  untracked inventory, `git diff --check` (`spec/AI_SCAFFOLD.md`). `wf-minimal` now makes each
  generator end its report with a fenced evidence block (`git status --porcelain`,
  `git show --stat --oneline HEAD`, full `git show HEAD`); before that it passed only the plan and
  the report, so a removal-scoped stage fail-closed and halted the run (issue #182, attempt 2).
  Read the finding text: "no diff supplied" is a harness fault — fix the workflow; a named
  spec↔impl contradiction is real and needs a code fix or a spec narrowing before a resume.
- **Resuming an escalated issue does not pick up base-branch fixes by itself.** `create_branch`
  reuses the existing `prauto/I-<n>` branch at its own head, and only PR finalize rebases onto
  `origin/$PRAUTO_BASE_BRANCH` — a commit landed on the base branch after the branch forked stays
  invisible to the resumed worktree. Rebase the branch and push it
  (`git worktree add /tmp/<n> prauto/I-<n> && git rebase origin/dev && git push --force-with-lease`)
  so the resumed stages actually run against the fix.
- **Resuming an escalated issue reuses its approved plan.** Escalation leaves the plan comment and its
  `go ahead` reply inside the current ready-label lifecycle, so
  `gh issue edit <N> --remove-label prauto:failed --add-label prauto:wip` (keeping the assignee) makes
  the next wake derive `implementation` and re-run that plan's stages for one retry slot. A full
  restart (remove all `prauto:` labels, then a fresh `prauto:ready`) moves the lifecycle anchor past
  the approval and forces a whole new analysis pass — use it only when the plan itself is wrong.
