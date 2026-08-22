---
name: scaffold-bin-verdict-and-binding
description: scaffold/bin/*.sh is the non-Claude-Code review harness — its awk verdict scrape fails OPEN on a template restatement, it drops every .claude/agents frontmatter control, and AGENTS.md is NOT auto-loaded by Claude Code (measured)
metadata:
  type: project
---

Three measured facts about the agent-agnostic scaffold (`scaffold/roles/`, `scaffold/bin/`,
root `AGENTS.md`), all of which decide whether a security verdict is collected and honoured.

## 1. A text-scraped verdict fails open; wf-minimal.js's schema does not

`scaffold/bin/run-workflow.sh` extracts the verdict with

    awk '/^### Verdict/{found=1; next} found && /^(APPROVE|REVISE|ESCALATE)/{print $1; exit}'

**First match wins.** Every reviewer role file ends with an output-format code fence that
literally contains `### Verdict` followed by `APPROVE — ...` on the next line. A reviewer that
restates that template before its real verdict is parsed as APPROVE.

Measured end-to-end with a scripted fake `claude` on PATH: `reviewer`=APPROVE +
`security-reviewer`=REVISE-with-template-restatement → merged APPROVE, no fix pass, stderr
"All stages complete.", exit 0. The high-severity finding vanished silently.

Fail-*closed* directions that do hold (also measured): a bolded `**REVISE**`, an `## Verdict`
h2, or a trailing-period `REVISE.` all yield a token that `rank()`'s `*)` arm maps to ESCALATE.
Only the exact bare token `APPROVE` is dangerous.

**Why it matters:** `.claude/workflows/wf-minimal.js` — the file this script says it ports —
uses `REVIEW_SCHEMA` with `verdict: {enum: [APPROVE,REVISE,ESCALATE]}` structured output, so the
verdict cannot be confused with prose. The bash port replaced a typed field with a heuristic
scrape. Any future verdict-parsing code gets the same question: **can a reviewer's own prose
produce the token?** Prefer last-match, a unique sentinel, or a schema.

## 2. `run-stage.sh --agent claude` carries no frontmatter controls

It invokes exactly `claude -p "$prompt" --model "${model:-sonnet}" --output-format json` (argv
dumped and verified). Not passed: `disallowedTools: Write, Edit, NotebookEdit` (the read-only
property of all four evaluators), `model: opus` + `effort: xhigh` (evaluators default to
**sonnet** here), the PostToolUse ruff / Stop typecheck hooks, `memory: project`, `skills:`.

There is **no per-role permission axis in the script at all**. Today a bare `claude -p` cannot
Write (measured: "DENIED", no file created — the repo's `.claude/settings.json` has 60 allow
rules and none grant Edit/Write). So generators invoked this way cannot generate. The moment
anyone adds `--permission-mode acceptEdits` / `--allowedTools` to fix that, **evaluators get
write access in the same stroke** — generator ≠ reviewer isolation is gone. Check for that flag
on every future diff to `scaffold/bin/`.

## 3. Claude Code does NOT auto-load `AGENTS.md` (v2.1.239, measured behaviourally)

Temp dir + `claude -p "What is 2+2?"`:
- `CLAUDE.md` only ("begin with PPP-CLAUDE") → responds `PPP-CLAUDE`
- `AGENTS.md` only ("begin with QQQ-AGENTS") → **ignores it entirely** (not even a fallback)
- both present → `PPP-CLAUDE` only
- `CLAUDE.md` containing `@AGENTS.md` → `QQQ-AGENTS` fires ✅

So prose like "**Read `AGENTS.md` first**" in CLAUDE.md is a best-effort pointer, not a load.
Anything moved from CLAUDE.md to AGENTS.md leaves always-in-context and becomes read-if-noticed.
The `@AGENTS.md` import is the one-line fix. The binary string
"Claude Code hardcodes CLAUDE.md / AGENTS.md discovery." is misleading — do not trust it over
the behavioural test.

**How to apply:** on any diff that moves content between `CLAUDE.md` and `AGENTS.md`, list which
*rules* left auto-loaded context. The ones that matter: the security-reviewer invocation trigger
("when a generator's diff touches paths listed in ..."), generator ≠ reviewer, ESCALATE-halts-the-run,
and the skip-plan criteria. Those four are the whole review gate.

Related: [[reviewer-config-is-generator-writable]], [[per-run-workflow-fail-open]]
