export const meta = {
  name: 'wf-is-primary',
  description: 'criterion-met toggle label + is_primary dataset_filter column: spec → backend ∥ frontend → tests, each adversarially reviewed',
  phases: [
    { title: 'Spec', detail: 'spec agent updates the six column-list documents' },
    { title: 'Spec review', detail: 'spec-reviewer audits hierarchy and contradictions' },
    { title: 'Code', detail: 'backend (grammar + schema + sweep) and frontend concurrently' },
    { title: 'Code review', detail: 'reviewer + security-reviewer on backend, reviewer on frontend' },
    { title: 'Tests', detail: 'test agent across unit / spot / api-wired / vitest / e2e' },
    { title: 'Test review', detail: 'test-reviewer, two review-fix cycles' },
  ],
}

const PLAN = '/Users/soonmok/.claude/plans/sunny-fluttering-map.md'

// Every generator prompt carries these. The no-commit rule and the live-cluster
// allowlist are both standing project requirements, not per-run preferences.
const RULES = `
HARD RULES — these override anything you infer from the repo:
- Do NOT commit. Do NOT \`git add\` / stage. Do NOT create branches or stash.
  Leave every change unstaged in the working tree.
- Do NOT run any live-cluster command. The ONLY shell commands you may run are:
  \`uv run ruff\`, \`uv run mypy\`, \`uv run pytest tests/unit/...\`,
  \`pnpm -C src/frontend typecheck\`, \`pnpm -C src/frontend test\`,
  \`git diff\`, \`git status\`, \`rg\`/\`grep\`, \`ls\`, \`cat\`.
  Anything touching kubectl, helm, docker, install.sh, health-check.sh, or the
  integration/e2e suites is FORBIDDEN — the orchestrator verifies the cluster.
- Read the approved plan at ${PLAN} first. It is authoritative. Do not widen scope.
- Follow /Users/soonmok/Projects/selhorys/dataspoke-baseline/CLAUDE.md conventions.
`

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['APPROVE', 'REVISE', 'ESCALATE'] },
    summary: { type: 'string', description: 'One paragraph on the overall state of the work.' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          file: { type: 'string' },
          line: { type: 'integer' },
          severity: { type: 'string', enum: ['blocker', 'major', 'minor'] },
          finding: { type: 'string' },
          fix: { type: 'string' },
        },
        required: ['file', 'severity', 'finding', 'fix'],
        additionalProperties: false,
      },
    },
  },
  required: ['verdict', 'summary', 'findings'],
  additionalProperties: false,
}

function renderFindings(review) {
  return review.findings
    .map((f, i) => `${i + 1}. [${f.severity}] ${f.file}${f.line ? `:${f.line}` : ''}\n   Finding: ${f.finding}\n   Fix: ${f.fix}`)
    .join('\n\n')
}

/**
 * Run one generate → review → (fix if REVISE) cycle.
 *
 * reviewSpecs is a list of {agentType, label} so a stage can be judged by more
 * than one reviewer (backend gets reviewer + security-reviewer in parallel).
 * A REVISE from ANY reviewer triggers a single merged fix pass.
 */
async function reviewCycle({ stage, genType, genPhase, revPhase, genPrompt, reviewPrompt, reviewSpecs, report }) {
  const runReviews = (specs, suffix, prompt) =>
    parallel(
      specs.map((spec) => () =>
        agent(`${prompt}\n\nGenerator's completion report:\n${report}`, {
          label: suffix ? `${spec.label}:${suffix}` : spec.label,
          phase: revPhase,
          agentType: spec.agentType,
          schema: VERDICT_SCHEMA,
        }),
      ),
    ).then((results) => results.map((r, i) => ({ spec: specs[i], review: r })))

  const isBogus = (r) => r && r.verdict === 'ESCALATE' && r.findings.length === 0

  let paired = await runReviews(reviewSpecs, '', reviewPrompt)

  // An ESCALATE carrying zero findings is an agent/quota failure, not a real
  // verdict — re-run that review once rather than halting the run. Retry the
  // specs that actually failed: selecting by index would re-run the wrong
  // reviewer and could leave a stage with no security verdict at all.
  const bogusPairs = paired.filter((p) => isBogus(p.review))
  if (bogusPairs.length) {
    log(`${stage}: ${bogusPairs.length} review(s) escalated with no findings — re-running exactly those`)
    const redone = await runReviews(bogusPairs.map((p) => p.spec), 'retry', reviewPrompt)
    paired = paired.filter((p) => !isBogus(p.review)).concat(redone)
  }

  // Fail closed. A null review means the agent died; treating a missing verdict
  // as consent would report an entirely unreviewed stage as approved.
  const missing = paired.filter((p) => !p.review)
  if (missing.length) {
    log(`${stage}: ${missing.length} reviewer(s) produced no verdict — halting rather than assuming approval`)
    return {
      stage,
      halted: true,
      reviews: [],
      reason: `no verdict from: ${missing.map((p) => p.spec.label).join(', ')}`,
    }
  }

  const reviews = paired.map((p) => p.review)

  const escalated = reviews.filter((r) => r.verdict === 'ESCALATE')
  if (escalated.length) {
    log(`${stage}: ESCALATE — halting this stage`)
    return { stage, halted: true, reviews }
  }

  const needFix = reviews.filter((r) => r.verdict === 'REVISE')
  if (!needFix.length) {
    log(`${stage}: APPROVED on the first pass`)
    return { stage, halted: false, reviews, fixed: false }
  }

  const merged = needFix.map((r) => renderFindings(r)).join('\n\n')
  log(`${stage}: REVISE — ${needFix.reduce((n, r) => n + r.findings.length, 0)} finding(s), running the single fix pass`)

  const fixReport = await agent(
    `${RULES}\n\nFIX PASS for the ${stage} stage. Your earlier work was reviewed and needs revision.\n\n` +
      `Address every finding below. Do not refactor anything the findings do not name.\n\n${merged}\n\n` +
      `Report what you changed per finding, and say explicitly if you disagree with one and why.`,
    { label: `${stage}:fix`, phase: genPhase, agentType: genType },
  )

  // Re-review with the FULL reviewer set, not just the first. A fix pass driven
  // by security findings must be re-checked by security-reviewer; having the
  // spec-compliance reviewer sign off on a security fix is how an unverified
  // change reaches the tree looking approved.
  const rechecked = await runReviews(
    reviewSpecs,
    're-review',
    `${reviewPrompt}\n\nThis is a RE-REVIEW after a fix pass. The findings raised by you or a peer reviewer were:\n${merged}\n\n` +
      `The generator's fix report:\n${fixReport}\n\nVerify each finding is genuinely resolved. Judge only from the files.`,
  )

  const noVerdict = rechecked.filter((p) => !p.review)
  if (noVerdict.length) {
    log(`${stage}: re-review produced no verdict from ${noVerdict.map((p) => p.spec.label).join(', ')} — halting`)
    return { stage, halted: true, reviews: rechecked.map((p) => p.review).filter(Boolean), fixed: true }
  }

  const stillBad = rechecked.filter((p) => p.review.verdict !== 'APPROVE')
  if (stillBad.length) {
    log(`${stage}: findings persist after the fix pass — escalating`)
    return { stage, halted: true, reviews: rechecked.map((p) => p.review), fixed: true }
  }
  log(`${stage}: approved after one fix pass by all ${reviewSpecs.length} reviewer(s)`)
  return { stage, halted: false, reviews: rechecked.map((p) => p.review), fixed: true }
}

// ── Stage A: spec ────────────────────────────────────────────────────────────

phase('Spec')

const specReport = await agent(
  `${RULES}

Execute **Stage A — spec** of the approved plan.

Add the \`is_primary\` column to the documented \`dataset_filter\` grammar and to the
\`dataset_registry\` schema, and document the \`criterion met:\` toggle label. Six
documents restate the column list verbatim and must all move together — the plan
lists them with line anchors:

- spec/API.md §\`dataset_filter\` grammar — new \`bool_col\`/\`bool\` productions,
  \`is_primary\` row in the column table, the never-swept default, updated example
- spec/feature/BACKEND_SCHEMA.md §\`dataset_registry\` — column row, partial index,
  and a clause disambiguating this from the unrelated \`dataset_node_map.is_primary\`
- spec/DATAHUB_INTEGRATION.md §Dataset attribute sync — third source row (siblings
  aspect, scrolled), extended GraphQL selection, three-branch derivation rule
- spec/feature/BACKEND.md — sweep step 3 and §Dataset resolution
- spec/USE_CASE_en.md and spec/USE_CASE_kr.md — column prose only, do NOT restructure
  the scenarios (Korean in plain -다/-한다 style)
- spec/feature/FRONTEND_GOVERNANCE.md — the ASCII mock gains the \`criterion met:\`
  prefix and the prose states the toggle group carries that visible label

API.md is priority 1: it is the canonical grammar definition and every other document
syncs to it. Write timeless present-tense reference prose — no "was added", no
"previously", no migration notes. Keep it terse; this is a column addition, not a
redesign.`,
  { label: 'spec', phase: 'Spec', agentType: 'spec' },
)

const specResult = await reviewCycle({
  stage: 'spec',
  genType: 'spec',
  genPhase: 'Spec',
  revPhase: 'Spec review',
  report: specReport,
  reviewSpecs: [{ agentType: 'spec-reviewer', label: 'review:spec' }],
  reviewPrompt: `Review the spec edits for the \`is_primary\` dataset_filter column and the
\`criterion met:\` toggle label against the approved plan at ${PLAN}.

Audit specifically:
- Does every one of the six documents carry the change, with no document left stating
  the old four-column list?
- Does anything contradict spec/API.md (priority 1)?
- Is the grammar production block internally consistent with the column table and the
  worked example?
- Is the \`dataset_node_map.is_primary\` vs \`dataset_registry.is_primary\`
  disambiguation actually present and clear?
- Any historical/changelog phrasing that violates the timeless-reference convention?
- Any bloat: verbatim code blocks, duplicated field tables, third-party detail that
  belongs behind a link?

Read the actual files. Do not take the generator's report at face value.`,
})

if (specResult.halted) {
  return { halted: 'spec', detail: specResult.reviews }
}

// ── Stages B + C: backend and frontend, concurrently ─────────────────────────

phase('Code')

const codeResults = await parallel([
  async () => {
    const report = await agent(
      `${RULES}

Execute **Stage B — backend** of the approved plan. The specs from Stage A are already
updated in the working tree; read them (spec/API.md §\`dataset_filter\` grammar,
spec/feature/BACKEND_SCHEMA.md §\`dataset_registry\`, spec/DATAHUB_INTEGRATION.md
§Dataset attribute sync) as your contract.

Seven files, in this order:

1. src/shared/dataset_filter.py — add a third column class \`_BOOL_COLUMNS\` alongside
   \`_SCALAR_COLUMNS\`/\`_ARRAY_COLUMNS\`, mapping "is_primary" to
   \`DatasetRegistry.is_primary\`. New frozen AST node \`BoolEquals(column, value: bool)\`,
   exported in \`__all__\`. Parser: after \`bool_col =\`, accept the bare words true/false
   case-insensitively; a QUOTED string there is a syntax error carrying the character
   position. Extend the two existing wrong-kind-column error messages so
   \`'x' IN is_primary\` and \`is_primary = 'true'\` each name the right kind. Compile to
   \`column.is_(True)\` / \`column.is_(False)\`. Add the \`_format_predicate\` branch
   rendering \`is_primary = true\` / \`is_primary = false\` lowercase. Update the module
   docstring grammar block in lockstep.

   CRITICAL SECURITY INVARIANT: this module must still contain no f-string,
   %-formatting, str.format or sqlalchemy.text() anywhere on the compile path. Every
   literal is a bound parameter; every column identifier comes from the fixed maps.

2. src/shared/db/models.py — DatasetRegistry gains
   \`is_primary: Mapped[bool]\`, Boolean, nullable=False, default=True,
   server_default=text("true"). Add
   \`Index("ix_dataset_registry_not_primary", "is_primary", postgresql_where=text("NOT is_primary"))\`
   to __table_args__ (partial: only the false side is selective).

3. migrations/versions/001_initial_schema.py — fold the column and the partial index
   into the squashed revision. Do NOT create a new revision file.

4. src/shared/datahub/client.py::get_dataset_attributes — extend the GraphQL selection
   inside the mandatory \`... on Dataset\` fragment with
   \`siblings { isPrimary siblings { urn } }\`. Change the return type from
   \`dict[str, tuple[list[str], list[str]]]\` to \`dict[str, DatasetAttributeRead]\`,
   a new module-level frozen dataclass carrying
   (tag_urns, glossary_term_urns, is_primary). Derivation, exactly:
     - \`siblings\` absent or null            -> is_primary = True
     - \`siblings.siblings\` empty list       -> is_primary = True
     - otherwise                            -> is_primary = bool(isPrimary)
   Apply the same shape-check-don't-duck-type discipline as the surrounding code: a
   malformed \`siblings\` object degrades to True, never raises.

5. src/shared/db/registry.py — \`DatasetAttributes\` gains \`is_primary: bool\`;
   \`upsert_dataset_attributes\` adds it to BOTH the on-conflict set_ and the insert
   payload. Leave the never-blank and never-register-an-unseen-URN invariants intact.

6. src/backend/ingestion/service.py::_sync_dataset_attributes — thread is_primary from
   the read into the DatasetAttributes record.

7. src/api/schemas/_dataset_filter.py — extend DATASET_FILTER_FIELD_DESCRIPTION with the
   boolean column and its unquoted literal form.

Do NOT touch src/backend/_dataset_filter.py, the measurers, or any consumer service —
they all go through filter_clause and absorb the new node.

Run \`uv run ruff check\` and \`uv run ruff format\` on the files you edit, and
\`uv run pytest tests/unit/shared/test_dataset_filter.py\` to see what the existing suite
says (some failures are expected until Stage D updates the tests — report them, do not
edit tests).

Report: what you changed per file, the exact grammar productions you implemented, and
anything in the plan you could not do.`,
      { label: 'backend', phase: 'Code', agentType: 'backend' },
    )

    return await reviewCycle({
      stage: 'backend',
      genType: 'backend',
      genPhase: 'Code',
      revPhase: 'Code review',
      report,
      reviewSpecs: [
        { agentType: 'reviewer', label: 'review:backend' },
        { agentType: 'security-reviewer', label: 'security:backend' },
      ],
      reviewPrompt: `Review the backend implementation of the \`is_primary\` dataset_filter column
against the approved plan at ${PLAN} and the updated specs in the working tree
(spec/API.md §\`dataset_filter\` grammar, spec/feature/BACKEND_SCHEMA.md
§\`dataset_registry\`, spec/DATAHUB_INTEGRATION.md §Dataset attribute sync).

Weight these highest:
- **SQL injection surface.** src/shared/dataset_filter.py must contain no f-string,
  %-formatting, str.format or sqlalchemy.text() on the compile path. Every literal
  bound, every identifier from a fixed map. Verify by reading, not by trusting.
- Does \`is_primary = 'true'\` (quoted) actually raise with a character position rather
  than parsing? Does \`'x' IN is_primary\` produce a sensible wrong-kind message?
- Is the DataHub derivation rule exactly three branches as specified, and does a
  malformed \`siblings\` payload degrade to True instead of raising?
- Is the migration folded into 001 rather than added as a new revision?
- Does the on-conflict set_ in upsert_dataset_attributes include is_primary (a missing
  entry means the value never refreshes after first insert — silent staleness)?
- Does the column default (NOT NULL DEFAULT true) match the spec, and is the index
  genuinely partial?
- Any consumer service or measurer touched that the plan said to leave alone?

Read every changed file. Do not take the generator's report at face value.`,
    })
  },

  async () => {
    const report = await agent(
      `${RULES}

Execute **Stage C — frontend** of the approved plan. Two small, surgical edits:

1. src/frontend/components/governance/metric-dataset-table.tsx — the three verdict
   checkboxes currently render as bare words true / false / unknown with no visible
   label. Add a visible \`criterion met:\` label immediately before them, inside the
   existing \`role="group"\` flex row, styled to match the checkbox labels
   (text-xs font-medium, muted foreground).

   CRITICAL: keep each checkbox's \`aria-label={verdict}\` exactly as it is, and do not
   let the new label become the accessible name of any checkbox. Existing tests query
   \`getByRole("checkbox", { name: verdict })\` for each of true/false/unknown and those
   queries must still resolve. Leave the \`met criterion\` column header unchanged.

2. src/frontend/components/dataset-filter-guide.tsx — add the \`bool_col := is_primary\`
   line to the GRAMMAR block (matching the production now in spec/API.md) and an
   \`is_primary\` entry to the COLUMNS table with kind "bool", described as true when the
   dataset is the primary sibling or has no sibling. Follow the existing entry shape.

Do NOT touch src/frontend/lib/dataset-filter-format.ts or metagen-filter-summary.ts —
both are purely lexical and grammar-agnostic.

Run \`pnpm -C src/frontend typecheck\`. Do not edit test files; Stage D owns those.

Report what you changed and the exact label markup you produced.`,
      { label: 'frontend', phase: 'Code', agentType: 'frontend' },
    )

    return await reviewCycle({
      stage: 'frontend',
      genType: 'frontend',
      genPhase: 'Code',
      revPhase: 'Code review',
      report,
      reviewSpecs: [{ agentType: 'reviewer', label: 'review:frontend' }],
      reviewPrompt: `Review the frontend changes against the approved plan at ${PLAN} and
spec/feature/FRONTEND_GOVERNANCE.md (updated in the working tree this run).

Check specifically:
- Does the \`criterion met:\` label render visibly, and is it OUTSIDE every
  \`<label>\` that wraps a Checkbox? If it landed inside one, it would change that
  checkbox's accessible name and break every
  \`getByRole("checkbox", { name: "true" })\` query in the Vitest and Playwright suites.
  Trace this carefully — it is the one way this change can silently break tests.
- Is the \`met criterion\` column header genuinely unchanged?
- Does the dataset-filter-guide entry match the grammar production in spec/API.md?
- Were dataset-filter-format.ts or metagen-filter-summary.ts touched? They should not be.
- Any scope creep beyond the two files.

Read the actual files.`,
    })
  },
])

const codeHalted = codeResults.filter(Boolean).filter((r) => r.halted)
if (codeHalted.length) {
  return { halted: codeHalted.map((r) => r.stage), detail: codeHalted }
}

// ── Stage D: tests, two review-fix cycles ────────────────────────────────────

phase('Tests')

let testReport = await agent(
  `${RULES}

Execute **Stage D — tests** of the approved plan. The spec, backend and frontend changes
are already in the working tree. Extend the EXISTING test files listed below — do not
create parallel new files where a home already exists.

unit:
- tests/unit/shared/test_dataset_filter.py — the main body of work. Boolean predicate
  parse + compile (\`is_primary = true\`, \`IS_PRIMARY = TRUE\`, \`is_primary = false\`);
  rejection with character positions of \`is_primary = 'true'\`, \`'x' IN is_primary\`,
  \`is_primary IN ('true')\`; composition with AND/OR and inside nested parens;
  format_filter canonical rendering; one entry in the existing injection battery.
- tests/unit/shared/db/test_models.py — is_primary type/nullability/default in the
  attribute-column assertion.
- tests/unit/shared/db/test_registry.py — is_primary in the upsert payload key list AND
  in the on-conflict refresh set (a missing set_ entry is the silent-staleness bug).
- tests/unit/shared/datahub/test_client.py — the derivation truth table: siblings aspect
  absent, empty sibling list, isPrimary true, isPrimary false, malformed siblings object.
- tests/unit/backend/ingestion/test_service.py — the sweep threads is_primary through.
- tests/unit/api/schemas/test_dataset_filter.py — 422 INVALID_DATASET_FILTER envelope
  with detail.position for the quoted-boolean case.

spot integration:
- tests/integration/spot/test_dataset_attribute_sync.py — is_primary populated by the
  sweep; a dataset_filter using it resolves against synced attributes.

api-wired integration:
- tests/integration/api_wired/test_uc5_01_governance.py — a metric whose dataset_filter
  uses is_primary. IMPORTANT: the dev DataHub seed has NO siblings, so the test must
  first emit a \`siblings\` aspect onto a resolvable example_db.catalog.* dataset via the
  DataHub client and then run the sweep. Without that the false branch is unreachable
  and the test is vacuous. Use resolvable catalog.* URNs only.

frontend:
- src/frontend/components/governance/metric-dataset-table.test.tsx — the
  \`criterion met:\` label renders, and all three
  \`getByRole("checkbox", { name: verdict })\` queries still resolve.

e2e:
- tests/e2e/ground/governance/metric-datasets-panel.spec.ts — the label is visible in
  the Datasets panel.

Conventions that matter here:
- Assertions derive from SPEC INVARIANTS, not from whatever the implementation happens
  to do. If the implementation and the spec disagree, report it — do not pin the test to
  the implementation.
- Inline API payloads for readability; do not hide them behind helpers.
- Spot tests only where api-wired's pipeline cannot naturally reach the state.

You may run \`uv run pytest\` on the tests/unit/ files you touch, and
\`pnpm -C src/frontend test\`. You may NOT run the integration or e2e suites — those need
a live cluster and the orchestrator runs them.

Report per file what you added and any spec/implementation disagreement you found.`,
  { label: 'test', phase: 'Tests', agentType: 'test' },
)

const TEST_REVIEW_PROMPT = `Review the tests added for the \`is_primary\` dataset_filter column and the
\`criterion met:\` toggle label, against the approved plan at ${PLAN} and the specs in
the working tree.

The question you are answering is: do these assertions derive from spec invariants, or
do they merely re-state what the current implementation happens to do?

Audit specifically:
- Any assertion that pins a value the spec does not fix (exact error message wording,
  incidental ordering, an internal helper's return shape).
- The api-wired UC5 test: does it ACTUALLY create a sibling relationship in DataHub
  before asserting on is_primary = false? A test that asserts the false branch without
  seeding a siblings aspect is vacuous — everything is primary by default.
- Coverage of the three derivation branches (aspect absent / empty list / explicit
  isPrimary) and of the on-conflict refresh path in upsert_dataset_attributes.
- Does the quoted-boolean rejection test assert the error CODE and the presence of a
  position, rather than the exact prose of the message?
- Missing negative cases: is_primary used with IN, with a quoted value, on the array side.
- Test-only workarounds are acceptable; a workaround papering over a real src/ bug is not
  — if you find one, that is a blocker finding naming the src/ file.

Read the actual test files and the code under test.`

const testCycle1 = await reviewCycle({
  stage: 'test',
  genType: 'test',
  genPhase: 'Tests',
  revPhase: 'Test review',
  report: testReport,
  reviewSpecs: [{ agentType: 'test-reviewer', label: 'review:test' }],
  reviewPrompt: TEST_REVIEW_PROMPT,
})

if (testCycle1.halted) {
  return { halted: 'test', detail: testCycle1.reviews }
}

// Second cycle. Its purpose is narrow: a fix pass driven by cycle 1's findings
// routinely introduces fresh implementation-pinning, and only a second read
// catches it.
log('test: running the second review-fix cycle')

const testCycle2 = await reviewCycle({
  stage: 'test',
  genType: 'test',
  genPhase: 'Tests',
  revPhase: 'Test review',
  report: `Cycle 1 is complete. Its verdict summary:\n${testCycle1.reviews.map((r) => r.summary).join('\n')}`,
  reviewSpecs: [{ agentType: 'test-reviewer', label: 'review:test:cycle2' }],
  reviewPrompt:
    TEST_REVIEW_PROMPT +
    `\n\nThis is the SECOND review cycle. A fix pass has already run against an earlier
round of findings. Weight most heavily the failure mode that fix pass typically
introduces: assertions rewritten to match observed implementation output rather than
the spec invariant. Re-read the tests that changed.`,
})

return {
  spec: specResult.reviews.map((r) => r.verdict),
  code: codeResults.filter(Boolean).map((r) => ({ stage: r.stage, verdicts: r.reviews.map((v) => v.verdict) })),
  test: {
    cycle1: testCycle1.reviews.map((r) => r.verdict),
    cycle2: testCycle2.reviews.map((r) => r.verdict),
    halted: testCycle2.halted || false,
  },
}
