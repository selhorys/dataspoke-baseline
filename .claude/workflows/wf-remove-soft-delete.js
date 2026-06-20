export const meta = {
  name: 'wf-remove-soft-delete',
  description: 'Remove soft-delete: UC2 validation hard-delete+cascade, UC3 ontogen seed enabled/disabled lifecycle. Per-stage generate→adversarial-review with double test loop.',
  whenToUse: 'After plan mossy-noodling-papert.md is approved. args = {planPath, stages, security}.',
  phases: [
    { title: 'spec' },
    { title: 'backend' },
    { title: 'frontend' },
    { title: 'test' },
  ],
}

// args: { planPath: string, stages: (string|string[])[], security: string[] }
const ARGS = typeof args === 'string' ? JSON.parse(args) : args
if (!ARGS || typeof ARGS.planPath !== 'string' || !Array.isArray(ARGS.stages)) {
  throw new Error('requires args {planPath: string, stages: array, security?: string[]}')
}

const REVIEWER_FOR = { test: 'test-reviewer', spec: 'spec-reviewer' }
const RANK = { APPROVE: 0, REVISE: 1, ESCALATE: 2 }
const NO_COMMIT = 'Do NOT commit, stage (git add), or push anything — leave all changes unstaged in the working tree for the user to review.'

const REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['APPROVE', 'REVISE', 'ESCALATE'] },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
          file: { type: 'string' },
          issue: { type: 'string' },
          expected: { type: 'string' },
          suggestion: { type: 'string' },
        },
        required: ['severity', 'file', 'issue'],
      },
    },
    summary: { type: 'string' },
  },
  required: ['verdict', 'findings', 'summary'],
}

function genPrompt(stage, findings) {
  const base = `You are the ${stage} generator in CLAUDE.md §Implementation Workflow.

Read the APPROVED IMPLEMENTATION PLAN first — it is at: ${ARGS.planPath}
Read it in full before doing anything, then read the specs/files it names for your stage.

Implement ONLY your stage's scope from that plan, following your agent instructions and the
project conventions in CLAUDE.md (timeless spec wording; no migration shims — pre-release;
fold schema changes into the single squashed alembic revision 001). ${NO_COMMIT}

End with your structured completion report: what you changed, by file, and any deviations.`
  if (!findings) return base
  return `${base}

FIX PASS — the reviewer returned these findings on the previous pass. Address each: fix it,
or dispute it with concrete evidence in your completion report.
${JSON.stringify(findings, null, 2)}`
}

function reviewPrompt(stage, report, note) {
  return `Review the ${stage} generator's output per your agent instructions.

APPROVED IMPLEMENTATION PLAN: ${ARGS.planPath} (read it in full first).
${note || ''}
GENERATOR COMPLETION REPORT:
${report}

Read every changed file yourself — do not trust the report's claims. Assertions must derive
from the plan/spec invariants, not from current implementation behavior. Return verdict,
findings, and a one-paragraph summary via structured output. You are read-only; do not edit.`
}

async function reviewPass(stage, report, pass, note) {
  const reviewers = [REVIEWER_FOR[stage] || 'reviewer']
  if ((ARGS.security || []).includes(stage)) reviewers.push('security-reviewer')
  const results = (await parallel(reviewers.map(type => () =>
    agent(reviewPrompt(stage, report, note), {
      agentType: type, schema: REVIEW_SCHEMA, label: `${stage}:${pass}:${type}`, phase: stage,
    })
  ))).filter(Boolean)
  if (results.length < reviewers.length) {
    return { verdict: 'ESCALATE', findings: [], summary: 'a reviewer failed to produce a verdict' }
  }
  return {
    verdict: results.reduce((worst, r) => (RANK[r.verdict] > RANK[worst] ? r.verdict : worst), 'APPROVE'),
    findings: results.flatMap(r => r.findings),
    summary: results.map(r => r.summary).join(' | '),
  }
}

// One generate → review → [fix-if-REVISE, max 1 fix] cycle. Returns {report, review}.
async function reviewFixCycle(stage, report, cyclePass, note) {
  let review = await reviewPass(stage, report, `${cyclePass}-review`, note)
  if (review.verdict === 'REVISE') {
    log(`${stage} [${cyclePass}]: REVISE — fix pass (${review.findings.length} findings)`)
    const fixReport = await agent(genPrompt(stage, review.findings), {
      agentType: stage, phase: stage, label: `${stage}:${cyclePass}-fix`,
    })
    report = fixReport ?? report
    review = await reviewPass(stage, report, `${cyclePass}-rereview`, note)
    if (review.verdict === 'REVISE') {
      review = { ...review, verdict: 'ESCALATE', summary: `findings persist after fix pass: ${review.summary}` }
    }
  }
  return { report, review }
}

async function runStage(stage) {
  let report = await agent(genPrompt(stage, null), {
    agentType: stage, phase: stage, label: `${stage}:generate`,
  })
  if (report == null) return { stage, outcome: 'ESCALATE', summary: 'generator failed or was skipped' }

  // Test stage runs the review→fix cycle TWICE (feedback_double_loop_review_fix):
  // cycle 2 catches new implementation-pinning introduced by cycle 1's fixes.
  if (stage === 'test') {
    const c1 = await reviewFixCycle(stage, report, 'cycle1')
    report = c1.report
    if (c1.review.verdict === 'ESCALATE') return { stage, outcome: 'ESCALATE', report, review: c1.review }
    const c2 = await reviewFixCycle(stage, report, 'cycle2',
      'This is the SECOND adversarial cycle — scrutinize especially for assertions newly pinned to implementation behavior by the prior fix pass.')
    return { stage, outcome: c2.review.verdict === 'APPROVE' ? 'DONE' : 'ESCALATE', report: c2.report, review: c2.review }
  }

  const c = await reviewFixCycle(stage, report, 'cycle1')
  return { stage, outcome: c.review.verdict === 'APPROVE' ? 'DONE' : 'ESCALATE', report: c.report, review: c.review }
}

const results = []
let haltedAt = null
for (const group of ARGS.stages) {
  const stages = Array.isArray(group) ? group : [group]
  log(`stage group: ${stages.join(' + ')}`)
  const groupResults = (await parallel(stages.map(s => () => runStage(s)))).filter(Boolean)
  results.push(...groupResults)
  if (groupResults.some(r => r.outcome === 'ESCALATE')) { haltedAt = stages.join('+'); break }
}

return {
  outcome: haltedAt ? `ESCALATED in stage group ${haltedAt} — user decision required` : 'COMPLETE',
  stages: results.map(r => ({
    stage: r.stage,
    outcome: r.outcome,
    verdict: r.review ? r.review.verdict : 'n/a',
    findings: r.review ? r.review.findings : [],
    summary: r.review ? r.review.summary : (r.summary || ''),
    report: r.report || '',
  })),
}
