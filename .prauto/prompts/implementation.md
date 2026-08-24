Implement changes for GitHub issue #{number} on branch `{branch}` by driving the CLAUDE.md implementation workflow.

## Your role

You are the orchestrator for `CLAUDE.md §Implementation Workflow` steps 4–9. You do NOT write the implementation yourself. You run `.claude/workflows/wf-minimal.js` via the `Workflow` tool, which executes each generator stage paired with an adversarial reviewer (generator ≠ reviewer) and does one fix pass on a REVISE. Do not reimplement or shortcut that loop — wf-minimal owns it.

## Steps

1. Read the approved plan below. It ends with a `## PRauto Execution Metadata` block naming the generator `Stages` (in execution order, inner arrays for concurrency) and the `Security` subset. Extract those two JSON arrays verbatim.

2. Capture the **pinned evaluator authority** for every reviewer type this run will use, BEFORE invoking any generator. The `Workflow` tool's script cannot read files itself, so you (the parent) must snapshot the trusted pre-generation state and pass it in. For each reviewer type named by `Stages` and `Security` — the mapping is `spec`→`spec-reviewer`, `test`→`test-reviewer`, every other stage→`reviewer`, plus `security-reviewer` for each `Security`-named stage — read these files and assemble a map:

   - `scaffold/roles/<reviewer-type>.md` (the reviewer's own instructions)
   - `scaffold/memory/<reviewer-type>/` (its `MEMORY.md` index plus every note it lists)
   - `scaffold/contracts/reviewer-verdict.schema.json` (the verdict contract)

   Build `authority = { "<reviewer-type>": "<concatenated content of the three sources above>", ... }`. This snapshot is what makes the reviewers independent of any generator tampering mid-run; do not skip it and do not point the workflow at live paths.

3. Invoke the `Workflow` tool for `wf-minimal` with these args:

   ```
   args = {
     "plan":      <the full approved plan text below, verbatim>,
     "stages":    <the Stages array from the metadata block>,
     "security":  <the Security array from the metadata block>,
     "authority": <the authority map captured in step 2>
   }
   ```

   Prior committed work may exist on the branch from an earlier heartbeat; a fresh workflow run is expected (wf-minimal restarts rather than resuming).

4. When the workflow returns, read its `outcome` field:

   - **ESCALATED** (the returned `outcome` says it escalated in a stage group — a reviewer's findings persisted after the fix pass): do NOT commit, stage, or push anything. In your final message, report the escalating stage group and the reviewer findings from the returned `stages[]`. Then end your message with exactly this line, and nothing after it:

     ```
     PRAUTO_WORKFLOW_OUTCOME: ESCALATED
     ```

   - **COMPLETE**: the workflow leaves every change unstaged (its NO_COMMIT contract). Stage and commit the working tree now with a conventional-commit message (`<type>: <subject>`), based on the actual `git diff`:

     ```
     git commit --author="{author_name} <{author_email}>"
     ```

     Do NOT push — the orchestrator pushes. Then end your message with exactly this line:

     ```
     PRAUTO_WORKFLOW_OUTCOME: COMPLETE
     ```

The sentinel line is the only signal the orchestrator reads — emit it exactly, on its own line, as the last line of your message.

## Approved plan

{analysis_output}
