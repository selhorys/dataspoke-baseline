export const meta = {
  name: 'wf-minimal',
  description: 'Simplest example of a dynamic agent-fleet workflow: per-stage generate → adversarial-review cycles from an approved plan (CLAUDE.md §Implementation Workflow steps 4-9)',
  whenToUse: 'After a human approves an implementation plan that names generator stages. args = {plan, stages, security?}.',
  phases: [
    { title: 'spec' },
    { title: 'backend' },
    { title: 'airflow-dag' },
    { title: 'test' },
    { title: 'frontend' },
    { title: 'k8s-helm' },
  ],
}

// args contract (supplied by the main agent from the approved plan):
//   plan:     string                  — the approved implementation plan, verbatim
//   stages:   (string | string[])[]   — generator stages in plan order; an inner array runs
//                                       concurrently (e.g. ["spec", ["backend","airflow-dag"],
//                                       "test", "frontend", "k8s-helm"]). The `spec` stage, when
//                                       present, leads so later stages read the updated spec.
//   security: string[]                — stages whose diff touches the sensitive paths listed in
//                                       .claude/agents/security-reviewer.md (decided at plan time)
// The harness may deliver args JSON-stringified; normalize before validating.
const ARGS = typeof args === 'string' ? JSON.parse(args) : args
if (!ARGS || typeof ARGS.plan !== 'string' || !Array.isArray(ARGS.stages)) {
  throw new Error('wf-minimal requires args {plan: string, stages: array, security?: string[]}')
}

const REVIEWER_FOR = { test: 'test-reviewer', spec: 'spec-reviewer' } // every other reviewed stage uses `reviewer`
const NO_REVIEW = ['k8s-helm'] // no review loop, per CLAUDE.md step 9
const RANK = { APPROVE: 0, REVISE: 1, ESCALATE: 2 }

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

APPROVED IMPLEMENTATION PLAN:
${ARGS.plan}

Implement your stage's scope from the plan, following your agent instructions (read the relevant specs first). End with your structured completion report.`
  if (!findings) return base
  return `${base}

FIX PASS — the reviewer returned these findings on the previous pass. Address each one: fix it, or dispute it with evidence in your completion report.
${JSON.stringify(findings, null, 2)}`
}

function reviewPrompt(stage, report) {
  return `Review the ${stage} generator's output per your agent instructions.

APPROVED IMPLEMENTATION PLAN:
${ARGS.plan}

GENERATOR COMPLETION REPORT:
${report}

Read every changed file yourself — do not trust the report's claims. Return verdict, findings, and a one-paragraph summary via structured output.`
}

// One review pass: `reviewer` (or `test-reviewer`), plus `security-reviewer` in
// parallel for security-flagged stages. Verdicts merge worst-of; findings concatenate.
async function reviewPass(stage, report, pass) {
  const reviewers = [REVIEWER_FOR[stage] || 'reviewer']
  if ((ARGS.security || []).includes(stage)) reviewers.push('security-reviewer')
  const results = (await parallel(reviewers.map(type => () =>
    agent(reviewPrompt(stage, report), {
      agentType: type,
      schema: REVIEW_SCHEMA,
      label: `${stage}:${pass}:${type}`,
      phase: stage,
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

// generate → review → [fix pass if REVISE, max 1 iteration] → re-review.
// REVISE persisting after the fix pass becomes ESCALATE (user decision required).
async function runStage(stage) {
  let report = await agent(genPrompt(stage, null), {
    agentType: stage, phase: stage, label: `${stage}:generate`,
  })
  if (report == null) return { stage, outcome: 'ESCALATE', summary: 'generator failed or was skipped' }
  if (NO_REVIEW.includes(stage)) return { stage, outcome: 'DONE', report }

  let review = await reviewPass(stage, report, 'review-1')
  if (review.verdict === 'REVISE') {
    log(`${stage}: REVISE — running fix pass (${review.findings.length} findings)`)
    const fixReport = await agent(genPrompt(stage, review.findings), {
      agentType: stage, phase: stage, label: `${stage}:fix-pass`,
    })
    report = fixReport ?? report
    review = await reviewPass(stage, report, 'review-2')
    if (review.verdict === 'REVISE') {
      review = { ...review, verdict: 'ESCALATE', summary: `findings persist after one fix pass: ${review.summary}` }
    }
  }
  return {
    stage,
    outcome: review.verdict === 'APPROVE' ? 'DONE' : 'ESCALATE',
    report,
    review,
  }
}

// Stage groups run in order; an inner array runs concurrently (the barrier between
// groups is required — later stages build on earlier ones). An ESCALATE finishes its
// group, then halts the run so later stages don't build on a broken base.
const results = []
let haltedAt = null
for (const group of ARGS.stages) {
  const stages = Array.isArray(group) ? group : [group]
  log(`stage group: ${stages.join(' + ')}`)
  const groupResults = (await parallel(stages.map(s => () => runStage(s)))).filter(Boolean)
  results.push(...groupResults)
  if (groupResults.some(r => r.outcome === 'ESCALATE')) {
    haltedAt = stages.join('+')
    break
  }
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
