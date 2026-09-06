Implement changes for GitHub issue #{number} on branch `{branch}` by driving the CLAUDE.md implementation workflow.

## Your role

You are the orchestrator for `CLAUDE.md §Implementation Workflow` steps 4–9. You do NOT write the
implementation yourself. You run the generator → adversarial-reviewer → one-fix-pass loop (generator ≠
reviewer) over the plan's stages. The binding depends on the agent you are:

- **Claude Code**: run `.claude/workflows/wf-minimal.js` via the `Workflow` tool. Do not reimplement
  or shortcut that loop — wf-minimal owns it.
- **Codex**: express the same loop inline (Codex has no `Workflow` tool): for each stage, dispatch a
  generator subagent, then its reviewer(s), merge verdicts worst-of, and run at most one fix pass.

Either way, **each generator commits its own stage** before returning its report (see
[Commit-per-stage](#commit-per-stage)). Reviewers stay read-only.

## Steps

1. Read the approved plan below. It ends with a `## PRauto Execution Metadata` block naming the
   generator `Stages` (in execution order, inner arrays for concurrency) and the `Security` subset.
   Extract those two JSON arrays verbatim.

2. Capture the **pinned evaluator authority** for every reviewer type this run will use, BEFORE
   invoking any generator. For each reviewer type named by `Stages` and `Security` — the mapping is
   `spec`→`spec-reviewer`, `test`→`test-reviewer`, every other stage→`reviewer`, plus
   `security-reviewer` for each `Security`-named stage — read these files and assemble a map:

   - `scaffold/roles/<reviewer-type>.md` (the reviewer's own instructions)
   - `scaffold/memory/<reviewer-type>/` (its `MEMORY.md` index plus every note it lists)
   - `scaffold/contracts/reviewer-verdict.schema.json` (the verdict contract)

   Build `authority = { "<reviewer-type>": "<concatenated content of the three sources above>", ... }`.
   This snapshot is what makes the reviewers independent of any generator tampering mid-run; do not
   skip it and do not point the reviewer at live paths.

3. Run the loop. **Claude Code** invokes the `Workflow` tool for `wf-minimal` with these args:

   ```
   args = {
     "plan":      <the full approved plan text below, verbatim>,
     "stages":    <the Stages array from the metadata block>,
     "security":  <the Security array from the metadata block>,
     "authority": <the authority map captured in step 2>,
     "author":    "{author_name} <{author_email}>"
   }
   ```

   **Codex** orchestrates the same loop inline with the same plan, stages, security, and authority,
   and passes `--author="{author_name} <{author_email}>"` to every generator's commit.

   Prior committed work may exist on the branch from an earlier heartbeat. A fresh workflow run
   begins from the branch's current committed state and continues from there.

4. When the loop returns, read its `outcome`:

   - **ESCALATED** (a reviewer's findings persisted after the fix pass): do NOT commit, stage, or
     push anything further. In your final message, report the escalating stage group and the reviewer
     findings. Then end your message with exactly this line, and nothing after it:

     ```
     PRAUTO_WORKFLOW_OUTCOME: ESCALATED
     ```

   - **COMPLETE**: every stage has already committed its own work (see below). Do not commit again.
     Verify the branch holds the expected commits, then end your message with exactly this line:

     ```
     PRAUTO_WORKFLOW_OUTCOME: COMPLETE
     ```

The sentinel line is the only signal the orchestrator reads — emit it exactly, on its own line, as the last line of your message.

## Commit-per-stage

Each generator commits its own stage's work as its final action, before returning its report. The
parent does not commit. A fix pass produces a follow-up commit. This makes progress durable per
stage, so a run that dies mid-workflow loses only the stage in flight — earlier stages are already
on the branch.

- Commits land on the private `{branch}` worktree branch only; never `master`, never push.
- List the exact files you changed with `git status --porcelain`, stage only those with
  `git add <each-path>` (never `git add -A` — sibling and prior stages share this worktree),
  inspect `git diff --staged` to confirm it holds only your changes, and write a conventional
  commit message (`<type>: <subject>`) from the actual diff. Attribute the commit with
  `--author="{author_name} <{author_email}>"`.
- If a stage produced no changes, skip the commit and say so.
- Reviewers remain read-only and review the committed changes (their reports are untrusted; read
  every changed file yourself).

## Approved plan

{analysis_output}
