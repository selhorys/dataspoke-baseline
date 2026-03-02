---
name: prauto-run-heartbeat
description: Test-run the prauto heartbeat script and monitor its progress. Diagnoses and fixes script errors on failure.
allowed-tools: Bash(env *), Bash(which *), Bash(date *), Bash(test *), Bash(ls *), Bash(cat *), Bash(tail *), Bash(ps *), Read, Edit, Glob, Grep
---

## Overview

Test-runs `.prauto/heartbeat.sh` inside a Claude Code session and monitors its execution.
Two separate concerns run simultaneously:

| # | Concern | Auth context | What it does |
|---|---------|-------------|--------------|
| 1 | **Heartbeat execution** | `.prauto/config.local.env` (GH_TOKEN, git author) | The heartbeat script runs as a background subprocess. It loads its own credentials and spawns nested Claude CLI invocations. |
| 2 | **Monitoring & fixing** | Local Claude session (your auth) | This Claude session watches the output, diagnoses errors, fixes scripts, and re-runs. |

Because the heartbeat spawns `claude` CLI internally, the `CLAUDECODE` env var must be unset to avoid the nested-run limit.

---

## Step 1 — Pre-flight checks

1. Verify `.prauto/config.local.env` exists: `test -f .prauto/config.local.env`
   - Do **NOT** read its contents — it contains secrets (GH_TOKEN, ANTHROPIC_API_KEY).
   - If missing, tell the user to create it from `.prauto/config.local.env.example` and stop.
2. Verify `.prauto/heartbeat.sh` exists and is executable.
3. Check for stale lock: if `.prauto/state/heartbeat.lock` exists, warn the user that another heartbeat may be running. Ask whether to proceed or abort.
4. Verify required CLI tools are available: `which claude && which gh && which git && which jq`

---

## Step 2 — Snapshot state before run

Before launching the heartbeat, take a **baseline snapshot** of `.prauto/state/` so you can detect changes during monitoring:

```bash
# Record what exists before the run
ls -la .prauto/state/current-job.json 2>/dev/null   # may not exist
ls -la .prauto/state/sessions/ 2>/dev/null           # list existing session files
ls -la .prauto/state/history/ 2>/dev/null             # list existing history files
```

Save this baseline mentally (file names, modification times) for comparison in Step 4.

---

## Step 3 — Run heartbeat

Execute the heartbeat in the **background**, redirecting output to a **persistent log file**:

```bash
env -u CLAUDECODE bash -x .prauto/heartbeat.sh > /tmp/prauto-heartbeat.log 2>&1
```

Key points:
- `env -u CLAUDECODE` — **required** to avoid nested-run limit (the heartbeat internally invokes `claude` CLI).
- `bash -x` — enables trace output for monitoring.
- `> /tmp/prauto-heartbeat.log 2>&1` — **critical**: redirects all output (stdout + stderr/trace) to a persistent file. Do NOT rely on the Bash tool's background task output capture — those temp files are ephemeral and get cleaned up before they can be read. The persistent log at `/tmp/prauto-heartbeat.log` survives task completion and is the **only reliable source** of trace output.

**Known issue — `claude -p` output invisible to Bash tool stdout:**
The `claude -p` CLI does **not** produce visible output in the Bash tool's stdout capture. Output only appears when redirected to a file (e.g., `> /tmp/out.txt`). The heartbeat already handles this — `invoke_claude()` in `lib/claude.sh` redirects to a temp file and reads it back with `jq`. However, this means the log file will go **silent for minutes** during each `claude -p` invocation. This is expected, not a hang.

Note the background task ID for monitoring.

---

## Step 4 — Monitor progress

The heartbeat writes structured state to `.prauto/state/` as it runs. Use **state files as the primary monitoring source** and the background task output as a secondary signal.

### Primary: Watch `.prauto/state/` files

Poll every **~20 seconds** until the background task exits. On each check:

1. **Lock file** — `test -f .prauto/state/heartbeat.lock` → script is running.
2. **Job file** — Read `.prauto/state/current-job.json` (if it exists) and compare to the baseline snapshot:
   - **New file appeared**: Heartbeat claimed an issue. Report the issue number and title.
   - **`phase` changed**: Report the phase transition (e.g., `analysis` → `plan-approval` → `implementation` → `pr`, or `pr-review` → `pr` for reviewer-feedback cycles). Note: `cross_check_phase()` may skip phases forward (e.g., `analysis` → `pr`) if GitHub state is ahead of the local cache.
   - **`session_id` changed**: A Claude CLI session started or resumed.
   - **`retries` incremented**: A retry occurred.
   - **`last_heartbeat` updated**: Heartbeat is alive and progressing.
3. **Session files** — `ls .prauto/state/sessions/` and compare to baseline:
   - **New `analysis-I-*.txt`**: Analysis phase completed. Read and summarize the plan.
   - **New `impl-I-*.json`**: Implementation phase completed. Report session ID.
   - **New `review-I-*.json`**: PR review phase completed.
4. **History files** — `ls .prauto/state/history/` and compare to baseline:
   - **New file**: Job completed (or was abandoned). Read to determine outcome.
5. **Job file disappeared**: Either `complete_job()` or `abandon_job()` was called — check history.

### Secondary: Persistent log file

Read the persistent log file at `/tmp/prauto-heartbeat.log` using `tail`:
```bash
tail -100 /tmp/prauto-heartbeat.log
```
- Parse `bash -x` trace lines (`+ command ...`) for supplementary detail.
- Watch for `[INFO]`, `[WARN]`, and `[ERROR]` markers.
- **Redact secrets**: The `bash -x` trace may print env var values (GH_TOKEN, ANTHROPIC_API_KEY). When summarizing output to the user, **never** include token/key values — replace them with `[REDACTED]`.
- **Expect long silences**: Each `claude -p` invocation (analysis, implementation, PR review) can run for several minutes. During this time the log file has **no new lines** — the `claude` process is running but its output goes to an internal temp file, not to stdout. Do **not** interpret silence as a hang. Use state files to confirm the process is still alive (`heartbeat.lock` exists, `current-job.json` `last_heartbeat` updates).
- **Do NOT use `TaskOutput`** or the background task's output file path — those are ephemeral and get cleaned up by Claude Code before they can be read. Always read `/tmp/prauto-heartbeat.log` instead.

### Non-standard heartbeat patterns

The monitor may observe patterns that don't follow the typical claim→analyze→implement→PR lifecycle:

1. **Orphan recovery** (Step 5.1): `current-job.json` appears with a non-`analysis` phase (derived from GitHub state), and the heartbeat exits immediately. No session files are created. The phase is inferred from GitHub signals: PR exists → `pr`, plan approved → `implementation`, plan posted → `plan-approval`, nothing → `analysis`. The *next* heartbeat will resume work normally.

2. **Squash-finalize** (Step 5.5): No `current-job.json` at all. Lock appears and disappears. Only visible in `/tmp/prauto-heartbeat.log` — look for `[INFO] Squash-finalizing` markers. This is a stateless operation: the heartbeat rebases, squashes commits, generates a commit message via Claude, force-pushes, and swaps `prauto:review` → `prauto:done` labels.

3. **Stale job cleanup** (Step 5 validation): `current-job.json` disappears and a new history file appears, but no session files were created. This means GitHub validation (issue closed, `prauto:wip` removed, or reassigned to another worker) triggered a stale-job completion via `complete_job`.

4. **Cross-check phase jump** (Step 5 resume): When monitoring a resume, the phase in `current-job.json` may jump forward (e.g., `analysis` → `pr`) because `cross_check_phase()` detected GitHub state ahead of the local cache (e.g., a PR already exists for the branch).

### State-based milestones to report

| State change | Meaning |
|-------------|---------|
| `heartbeat.lock` appears | Script started, lock acquired |
| `current-job.json` created | Issue claimed, job started |
| `current-job.json` created with phase != `analysis` + immediate exit | Orphan recovery — job rebuilt from GitHub state, will resume next heartbeat |
| `current-job.json` phase = `analysis` | Analysis phase running |
| New `sessions/analysis-I-*.txt` | Analysis complete — summarize the plan |
| `current-job.json` phase = `plan-approval` | Plan posted to issue, awaiting approval |
| `current-job.json` phase = `implementation` | Implementation phase running |
| New `sessions/impl-I-*.json` | Implementation complete |
| `current-job.json` phase = `pr-review` | Addressing PR reviewer feedback |
| `current-job.json` phase = `pr` | PR creation/push phase |
| `current-job.json` disappeared + new history file | Job completed |
| `current-job.json` disappeared + new history, no session files created | GitHub validation found stale issue — job completed without work |
| No `current-job.json` created but lock appeared/disappeared | Squash-finalize (stateless — no job file) |
| `heartbeat.lock` disappeared | Script finished |

---

## Step 5 — Handle outcome

### On success (exit code 0)

Report a completion summary using **state files** as the source of truth:
- Read `current-job.json` (if still present) to report phase and status.
- Compare `state/sessions/` and `state/history/` to the baseline from Step 2 to identify what was created.
- If a new `analysis-I-*.txt` was created, read and summarize the plan.
- If a new history file was created, the job completed — report the outcome.
- Total duration and any warnings encountered during execution.

### On failure (non-zero exit code)

Perform up to **3 retry cycles**:

1. **Diagnose**: Read `/tmp/prauto-heartbeat.log` for error details. Also check state files — a partially-created `current-job.json` or missing expected session file can indicate where the failure occurred. **Note**: If `CLAUDE_OUTPUT` is empty in the error trace, this may be caused by the `claude -p` Bash tool stdout capture issue (see Step 3). Check `invoke_claude()` in `lib/claude.sh` — it redirects to a temp file; verify the file redirect and `jq` parsing are working.
2. **Locate**: Map the error to a source file in `.prauto/` — typically one of:
   - `heartbeat.sh` — main orchestrator
   - `lib/helpers.sh` — logging, config loading
   - `lib/state.sh` — job state, locking
   - `lib/quota.sh` — token quota
   - `lib/issues.sh` — issue discovery, claiming
   - `lib/claude.sh` — Claude CLI invocation
   - `lib/git-ops.sh` — branch creation, worktree, push
   - `lib/pr.sh` — PR creation, feedback, squash-finalize
   - `lib/phases.sh` — phase-specific handlers
   - `prompts/*.md` — prompt templates
3. **Analyze**: Read the relevant source file. Understand the root cause.
4. **Fix**: Edit the source file to resolve the issue.
   - **NEVER modify `.prauto/config.local.env`** — if the error is credentials/config-related, report to user and stop.
   - **NEVER modify `.prauto/config.env`** unless it's clearly a bug in the shared config (not a config value issue).
5. **Re-run**: Launch the heartbeat again (overwrite the previous log):
   ```bash
   env -u CLAUDECODE bash -x .prauto/heartbeat.sh > /tmp/prauto-heartbeat.log 2>&1
   ```
   The re-run **automatically** uses credentials from `.prauto/config.local.env` — no manual credential handling needed.
6. **Monitor**: Return to Step 4.

If all 3 retries fail with the same or new errors, report the persistent failure and suggest manual intervention.

---

## Step 6 — Final report

```
## Heartbeat Test Run — <timestamp>

**Status**: Success / Failed (after N retries)
**Action taken**: <what the heartbeat did>
**Duration**: <total elapsed time>

### Execution log
<brief chronological summary of key events>

### Fixes applied (if any)
- `<file>:<line>` — <description of fix>

### Errors (if unresolved)
- <error description>
- <suggested manual fix>
```

---

## Constraints

- **Never read `.prauto/config.local.env`** — contains GH_TOKEN and ANTHROPIC_API_KEY.
- **Redact secrets** in all output shown to user — `bash -x` traces may expose env var values.
- **Always use `env -u CLAUDECODE`** for every heartbeat invocation, including retries after fixes.
- The heartbeat is idempotent (PID-based locking). Multiple runs are safe — a concurrent run will simply exit if another holds the lock.
- If the lock file is stale (process no longer running), the script handles this automatically.
