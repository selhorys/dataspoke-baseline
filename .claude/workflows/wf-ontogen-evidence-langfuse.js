export const meta = {
  name: 'wf-ontogen-evidence-langfuse',
  description: 'OntoGen: drop per-row debate-evidence JSON, add a Langfuse session Link column — spec→backend→frontend→test→k8s, generator+adversarial-reviewer pairs, double test-review loop',
  whenToUse: 'After the whimsical-doodling-cerf plan is approved. args = { planPath }.',
  phases: [
    { title: 'spec' },
    { title: 'backend' },
    { title: 'frontend' },
    { title: 'test' },
    { title: 'k8s-helm' },
  ],
}

const ARGS = typeof args === 'string' ? JSON.parse(args) : args
if (!ARGS || typeof ARGS.planPath !== 'string') {
  throw new Error('requires args { planPath: string }')
}
const PLAN_PATH = ARGS.planPath

// Stages run sequentially (barrier between each) — frontend reads backend's new run_id
// contract; tests read both; k8s last. Each entry: reviewer type, security flag, cycles.
const STAGES = [
  { name: 'spec',     reviewer: 'spec-reviewer', security: false, cycles: 1 },
  { name: 'backend',  reviewer: 'reviewer',      security: true,  cycles: 1 },
  { name: 'frontend', reviewer: 'reviewer',      security: true,  cycles: 1 },
  { name: 'test',     reviewer: 'test-reviewer', security: false, cycles: 2 }, // double loop
  { name: 'k8s-helm', reviewer: null,            security: false, cycles: 0 }, // no review
]

// Per-stage scope handed to each generator alongside the full plan.
const SCOPE = {
  spec: 'Spec layer only: API.md (drop result/{type}/{id}/attr + *AttrResponse; add run_id to row schema), FRONTEND_ONTOGEN.md (Confidence loses Evidence button; add 7th Evidence/Link column; update ASCII+table), BACKEND_LLM.md §Evidence shape (transcript lives in Langfuse, session=run_id), BACKEND_SCHEMA.md (ontogen tables: drop evidence, add run_id uuid NULL), USE_CASE_en/kr §UC3.',
  backend: 'Backend + DB schema: models.py + migrations/versions/001_initial_schema.py (swap evidence→run_id on ontogen_nodes/edges/triples); service.py (stop building/persisting transcript evidence; set run_id only on INSERT, never overwrite on reuse/update; add run_id to list_* row dicts; delete get_*_attr); routers/spoke/ontogen.py (remove 3 /attr routes); schemas/ontogen.py (add run_id to *Response, remove *AttrResponse). Keep gather_evidence() and the debate itself.',
  frontend: 'Frontend: runtime-config.ts add langfuseProjectId to RuntimeConfig + both branches (+test); app/layout.tsx inject langfuseProjectId from process.env.DATASPOKE_LANGFUSE_PROJECT_ID into window.__DATASPOKE_RUNTIME_CONFIG__ (cluster leg) (+layout.runtime-config.test.tsx); lib/api/ontogen.ts remove useOntogenItemAttr+OntogenItemAttrResponse, add run_id to row types; delete evidence-dialog.tsx; add shared evidence-link.tsx (run_id+langfuseUrl+langfuseProjectId → external Link target=_blank rel=noopener, else —); nodes/edges/triples-panel.tsx drop EvidenceDialog, add Evidence column after Created At, re-balance 7-column widths; update nodes-panel.test.tsx.',
  test: 'Tests: api_wired/test_uc3 (both variants) drop /attr + evidence.debate assertions + baseline snapshot, assert row.run_id == RUN_COMPLETE event run_id; spot remove persisted-evidence tests (keep input-evidence-gather); E2E uc3 + ground/result-table + COVERAGE.md Evidence-modal→Link. Run frontend Vitest + unit; api-wired/E2E run post-deploy (no cluster build yet).',
  'k8s-helm': 'Wire langfuseProjectId both legs: subchart frontend values.yaml (config.langfuseProjectId), configmap.yaml (DATASPOKE_LANGFUSE_PROJECT_ID), install.sh cluster --set-string frontend.config.langfuseProjectId AND local .env.local NEXT_PUBLIC_LANGFUSE_PROJECT_ID (default dataspoke-project).',
}

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
          file: { type: 'string' }, issue: { type: 'string' },
          expected: { type: 'string' }, suggestion: { type: 'string' },
        },
        required: ['severity', 'file', 'issue'],
      },
    },
    summary: { type: 'string' },
  },
  required: ['verdict', 'findings', 'summary'],
}

const NO_COMMIT = 'Do NOT commit, stage, or push any changes — leave edits in the working tree.'

function genPrompt(s, findings) {
  const base = `You are the ${s.name} generator in CLAUDE.md §Implementation Workflow.

APPROVED PLAN: read it IN FULL before doing anything — ${PLAN_PATH}

YOUR STAGE SCOPE:
${SCOPE[s.name]}

Read the relevant specs first, then implement only your stage's scope. ${NO_COMMIT}
End with your structured completion report.`
  if (!findings) return base
  return `${base}

FIX PASS — the reviewer returned these findings. Address each: fix it, or dispute with evidence.
${JSON.stringify(findings, null, 2)}`
}

function reviewPrompt(s, report) {
  return `Review the ${s.name} generator's output per your agent instructions.

APPROVED PLAN: read it IN FULL at ${PLAN_PATH}

GENERATOR COMPLETION REPORT:
${report}

Read every changed file yourself — do not trust the report. Return verdict, findings, and a
one-paragraph summary via structured output.`
}

async function reviewPass(s, report, pass) {
  const reviewers = [s.reviewer]
  if (s.security) reviewers.push('security-reviewer')
  const results = (await parallel(reviewers.map(type => () =>
    agent(reviewPrompt(s, report), {
      agentType: type, schema: REVIEW_SCHEMA, label: `${s.name}:${pass}:${type}`, phase: s.name,
    })
  ))).filter(Boolean)
  if (results.length < reviewers.length) {
    return { verdict: 'ESCALATE', findings: [], summary: 'a reviewer failed to produce a verdict' }
  }
  return {
    verdict: results.reduce((w, r) => (RANK[r.verdict] > RANK[w] ? r.verdict : w), 'APPROVE'),
    findings: results.flatMap(r => r.findings),
    summary: results.map(r => r.summary).join(' | '),
  }
}

// generate → up to `cycles` review-fix iterations. A REVISE still standing after the last
// allotted fix pass becomes ESCALATE. cycles:0 ⇒ no review (k8s-helm). cycles:2 ⇒ the test
// double-loop (cycle 2 catches impl-pinning introduced by cycle 1's fixes).
async function runStage(s) {
  let report = await agent(genPrompt(s, null), {
    agentType: s.name, phase: s.name, label: `${s.name}:generate`,
  })
  if (report == null) return { stage: s.name, outcome: 'ESCALATE', summary: 'generator failed/skipped' }
  if (s.cycles === 0) return { stage: s.name, outcome: 'DONE', report }

  let review = await reviewPass(s, report, 'review-1')
  for (let cycle = 1; cycle <= s.cycles && review.verdict === 'REVISE'; cycle++) {
    log(`${s.name}: REVISE — fix pass ${cycle}/${s.cycles} (${review.findings.length} findings)`)
    const fixed = await agent(genPrompt(s, review.findings), {
      agentType: s.name, phase: s.name, label: `${s.name}:fix-${cycle}`,
    })
    report = fixed ?? report
    review = await reviewPass(s, report, `review-${cycle + 1}`)
  }
  if (review.verdict === 'REVISE') {
    review = { ...review, verdict: 'ESCALATE', summary: `findings persist after ${s.cycles} fix pass(es): ${review.summary}` }
  }
  return { stage: s.name, outcome: review.verdict === 'APPROVE' ? 'DONE' : 'ESCALATE', report, review }
}

const results = []
let haltedAt = null
for (const s of STAGES) {
  log(`stage: ${s.name}`)
  const r = await runStage(s)
  results.push(r)
  if (r.outcome === 'ESCALATE') { haltedAt = s.name; break }
}

return {
  outcome: haltedAt ? `ESCALATED at ${haltedAt} — user decision required` : 'COMPLETE',
  stages: results.map(r => ({
    stage: r.stage, outcome: r.outcome,
    verdict: r.review ? r.review.verdict : 'n/a',
    findings: r.review ? r.review.findings : [],
    summary: r.review ? r.review.summary : (r.summary || ''),
  })),
}
