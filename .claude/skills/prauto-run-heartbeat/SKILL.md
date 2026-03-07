---
name: prauto-run-heartbeat
description: Test-run the prauto heartbeat script and monitor its progress. Diagnoses and fixes script errors on failure.
disable-model-invocation: true
user-invocable: true
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

Run the pre-flight script:

```bash
.claude/skills/prauto-run-heartbeat/preflight.sh
```

If it exits non-zero, report the failures and stop. For a stale lock, ask the user whether to proceed or abort.

---

## Step 2 — Snapshot state before run

Before launching the heartbeat, take a **baseline snapshot** of `.prauto/state/sessions/` so you can detect new session directories during monitoring:

```bash
# Record what exists before the run
find .prauto/state/sessions/ -mindepth 1 -maxdepth 2 -type d 2>/dev/null | sort
```

Save this baseline mentally (directory names) for comparison in Step 4. The heartbeat creates per-issue session directories at `.prauto/state/sessions/issue-{N}/{uuid}/` for each issue it processes.

---

## Step 3 — Run heartbeat

Execute the heartbeat in the **background**, redirecting output to a **persistent log file** inside the state directory:

```bash
env -u CLAUDECODE bash -x .prauto/heartbeat.sh > .prauto/state/heartbeat.log 2>&1
```

Key points:
- `env -u CLAUDECODE` — **required** to avoid nested-run limit (the heartbeat internally invokes `claude` CLI).
- `bash -x` — enables trace output for monitoring.
- `> .prauto/state/heartbeat.log 2>&1` — **critical**: redirects all output (stdout + stderr/trace) to a persistent file inside the state directory (gitignored). Do NOT rely on the Bash tool's background task output capture — those temp files are ephemeral and get cleaned up before they can be read. The persistent log at `.prauto/state/heartbeat.log` survives task completion and is the **only reliable source** of trace output.

**Known issue — `claude -p` output invisible to Bash tool stdout:**
The `claude -p` CLI does **not** produce visible output in the Bash tool's stdout capture. Output only appears when redirected to a file. The heartbeat already handles this — `invoke_claude()` in `lib/claude.sh` redirects to a file inside the session directory and reads it back with `jq`. However, this means the log file will go **silent for minutes** during each `claude -p` invocation. This is expected, not a hang.

Note the background task ID for monitoring.

---

## Step 4 — Monitor progress

The heartbeat logs all key events to `.prauto/state/heartbeat.log` and writes artifacts to per-issue session directories under `.prauto/state/sessions/issue-{N}/{uuid}/`. Use the **log file as the primary monitoring source** and **session directories as completion signals**.

### Session directory structure

Each issue processed by the heartbeat gets a session directory:
```
.prauto/state/sessions/
  issue-{N}/
    {uuid}/                    # unique per heartbeat run
      claude-output-{pid}.json # raw Claude CLI output
      analysis.txt             # analysis phase output
      implementation.json      # implementation phase output
      integration-fix.json     # integration fix phase output
      review.json              # PR review phase output
      complete.json            # job completion record
      abandon.json             # job abandonment record
      squash-msg.txt           # squash commit message (temp)
```

### Primary: Persistent log file

Read the persistent log file at `.prauto/state/heartbeat.log` using `tail`:
```bash
tail -100 .prauto/state/heartbeat.log
```

Poll every **~20 seconds** until the background task exits. On each check:

1. **Lock file** — `test -f .prauto/state/heartbeat.lock` → script is running.
2. **Log file** — Parse `[INFO]`, `[WARN]`, and `[ERROR]` markers for phase transitions and key events. The heartbeat logs all decisions: issue discovery, phase routing, plan posting, implementation start/end, PR creation. Look for `Session dir:` lines to identify the active session directory.
3. **Session directories** — `find .prauto/state/sessions/ -mindepth 1 -maxdepth 2 -type d | sort` and compare to baseline:
   - **New `issue-{N}/{uuid}/` directory**: A new session was created for issue N.
   - **`analysis.txt` in session dir**: Analysis phase completed. Read and summarize the plan.
   - **`implementation.json` in session dir**: Implementation phase completed. Report session ID.
   - **`review.json` in session dir**: PR review phase completed.
   - **`complete.json` or `abandon.json`**: Job finished — read to determine outcome.

Log file notes:
- Parse `bash -x` trace lines (`+ command ...`) for supplementary detail.
- **Redact secrets**: The `bash -x` trace may print env var values (GH_TOKEN, ANTHROPIC_API_KEY). When summarizing output to the user, **never** include token/key values — replace them with `[REDACTED]`.
- **Expect long silences**: Each `claude -p` invocation (analysis, implementation, PR review) can run for several minutes. During this time the log file has **no new lines** — the `claude` process is running but its output goes to the session directory, not to stdout. Do **not** interpret silence as a hang. Check `heartbeat.lock` to confirm the process is still alive.
- **Do NOT use `TaskOutput`** or the background task's output file path — those are ephemeral and get cleaned up by Claude Code before they can be read. Always read `.prauto/state/heartbeat.log` instead.

### Heartbeat patterns

The heartbeat processes **all** claimed issues in a single run (oldest first). You may see multiple phase transitions in one execution. Key patterns:

1. **New issue claimed** (Step 5): Log shows `Claimed issue #N`. The heartbeat then re-fetches all claimed issues to include the new one in the processing loop.

2. **WIP issue processing** (Step 6 loop): Log shows `WIP #N: phase=<phase>`. A heartbeat marker comment is posted, a worktree is created, and the phase handler runs. After completion, the worktree is cleaned up before moving to the next issue.

3. **Squash-finalize** (Step 6 loop, prauto:review issues): Log shows `Squash-finalizing PR #N for issue #M`. The heartbeat rebases, squashes commits, generates a commit message via Claude, force-pushes, and swaps `prauto:review` → `prauto:done` labels.

4. **Plan-approval wait** (Step 6 loop): Log shows `waiting for plan approval. Skipping.` No session files created. No heartbeat marker comment posted (retries not counted for waiting). The loop continues to the next issue.

5. **PR review feedback** (Step 6 loop, prauto:review issues): Log shows `Addressing reviewer feedback on PR #N`. A worktree is created, Claude addresses comments, pushes, and posts a feedback-addressed marker.

### Milestones to report

| Signal | Meaning |
|--------|---------|
| `heartbeat.lock` appears | Script started, lock acquired |
| Log: `[INFO] Session dir: ...issue-{N}/{uuid}` | Session directory created for issue N |
| Log: `[INFO] Claimed issue #N` | New issue claimed (Step 5) |
| Log: `[INFO] Open issue limit reached` | Skipped new pickup (at limit) |
| Log: `[INFO] WIP #N: phase=<phase>` | Processing a WIP issue (Step 6 loop) |
| Log: `[INFO] Starting analysis phase` | Analysis running for current issue |
| `analysis.txt` in session dir | Analysis complete — summarize the plan |
| Log: `[INFO] Plan posted` | Plan awaiting approval |
| Log: `[INFO] Starting implementation phase` | Implementation running for current issue |
| `implementation.json` in session dir | Implementation complete |
| Log: `[INFO] Integration test fix loop: attempt` | Integration fix loop running |
| Log: `[INFO] Integration tests passed` | Integration tests passed |
| `integration-fix.json` in session dir | Integration fix session complete |
| Log: `[INFO] Squash-finalizing PR #N` | Squash-finalize running for review issue |
| Log: `[INFO] Addressing reviewer feedback` | PR review running for review issue |
| Log: `[INFO] All claimed issues checked` | Processing loop finished |
| `complete.json` / `abandon.json` in session dir | Job finished |
| `heartbeat.lock` disappeared | Script finished |

---

## Step 5 — Handle outcome

### On success (exit code 0)

Report a completion summary using **log + session directory artifacts** as the source of truth:
- Read `.prauto/state/heartbeat.log` for a chronological summary of key events.
- Compare `state/sessions/` to the baseline from Step 2 to identify new session directories.
- For each new session dir (`issue-{N}/{uuid}/`), check for `analysis.txt`, `implementation.json`, `review.json`, `complete.json`, or `abandon.json`.
- If `analysis.txt` exists, read and summarize the plan.
- If `complete.json` or `abandon.json` exists, the job finished — report the outcome.
- Total duration and any warnings encountered during execution.

### On failure (non-zero exit code)

Perform up to **3 retry cycles**:

1. **Diagnose**: Read `.prauto/state/heartbeat.log` for error details. Also check session directories — a missing expected artifact file can indicate where the failure occurred. The `claude-output-{pid}.json` file in the session dir contains raw Claude CLI output for debugging. **Note**: If `CLAUDE_OUTPUT` is empty in the error trace, this may be caused by the `claude -p` Bash tool stdout capture issue (see Step 3). Check `invoke_claude()` in `lib/claude.sh` — it redirects to the session directory; verify the file redirect and `jq` parsing are working.
2. **Locate**: Map the error to a source file in `.prauto/` — typically one of:
   - `heartbeat.sh` — main orchestrator
   - `lib/helpers.sh` — logging, config loading
   - `lib/state.sh` — job state, locking
   - `lib/quota.sh` — token quota
   - `lib/issues.sh` — issue discovery, claiming, WIP detection
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
   env -u CLAUDECODE bash -x .prauto/heartbeat.sh > .prauto/state/heartbeat.log 2>&1
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
