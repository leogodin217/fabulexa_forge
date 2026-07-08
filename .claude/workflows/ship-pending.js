export const meta = {
  name: 'ship-pending',
  description:
    'Autonomous sprint implementation loop (step-1 spike). Runs an already-planned sprint (spec.md + state.yaml) through per-phase implement -> gate -> review -> fix -> demo -> commit, then a sprint-level review fix loop, demos-twice, and completion checks, inside an EXISTING worktree. All shell work routes through a neutral ops-gate agent (workflow scripts cannot run bash). Does NOT do pre-flight (worktree create / uv sync / baseline) or the ACCEPT/FIX gate -- the /ship-pending skill owns those in the main loop.',
  phases: [
    { title: 'Implement' },
    { title: 'Gate' },
    { title: 'Review' },
    { title: 'Fix' },
    { title: 'Demo' },
    { title: 'Analyze' },
    { title: 'Commit' },
    { title: 'Sprint review' },
    { title: 'Finalize' },
  ],
}

// ---------------------------------------------------------------------------
// args (parsed + validated by the skill in the main loop, then passed verbatim)
//   { sprint, worktree, parent, specPath,
//     phases: [{ n, name, demo }],          // from state.yaml, in order
//     gates:  ["cd packages/... && make test", ...],
//     today:  "YYYY-MM-DD" }                 // Date.now() is forbidden in scripts
// ---------------------------------------------------------------------------
const _args = typeof args === 'string' ? JSON.parse(args) : args
const { sprint, worktree, parent, specPath, phases, gates, today } = _args

// ---- structured-output schemas (validated at the tool layer; no text parsing) ----

const IMPL = {
  type: 'object',
  additionalProperties: false,
  required: ['files', 'demo', 'precommit', 'testsPass', 'testsFail', 'producesSimData', 'decisions'],
  properties: {
    files: { type: 'array', items: { type: 'string' }, description: 'Every path created/modified (impl + tests + demo), relative to worktree root.' },
    demo: { type: 'string', description: 'Demo path created; must equal this phase demo from state.yaml.' },
    precommit: { type: 'string', enum: ['PASS', 'FAIL'] },
    precommitReason: { type: 'string', description: 'Short reason when precommit=FAIL; empty otherwise.' },
    testsPass: { type: 'integer' },
    testsFail: { type: 'integer' },
    producesSimData: { type: 'boolean', description: 'True iff the demo emits simulation output worth a data-analyst pass.' },
    decisions: { type: 'array', items: { type: 'string' }, description: '1-3 key implementation decisions, one line each.' },
  },
}

const GATE = {
  type: 'object',
  additionalProperties: false,
  required: ['passed', 'ranCommands'],
  properties: {
    passed: { type: 'boolean' },
    ranCommands: { type: 'array', items: { type: 'string' } },
    failingCommand: { type: 'string', description: 'Command that exited non-zero; empty if passed.' },
    output: { type: 'string', description: 'Last ~60 lines of the failing command; empty if passed.' },
  },
}

const REVIEW = {
  type: 'object',
  additionalProperties: false,
  required: ['verdict', 'precommit', 'findings', 'raw'],
  properties: {
    verdict: { type: 'string', enum: ['APPROVED', 'REVISIONS_NEEDED'] },
    precommit: { type: 'string', enum: ['PASS', 'FAIL'] },
    findings: { type: 'array', items: { type: 'string' }, description: 'Each finding verbatim ([file:line] or [pre-commit] + text). Empty when APPROVED.' },
    raw: { type: 'string', description: 'Full reviewer text, verbatim -- handed to the fixer unfiltered.' },
  },
}

const FIX = {
  type: 'object',
  additionalProperties: false,
  required: ['resolutions', 'files', 'testsPass', 'testsFail', 'precommit'],
  properties: {
    resolutions: { type: 'array', items: { type: 'string' }, description: 'Per finding: "FIXED: ..." or "DISPUTED: <reason>".' },
    files: { type: 'array', items: { type: 'string' } },
    testsPass: { type: 'integer' },
    testsFail: { type: 'integer' },
    precommit: { type: 'string', enum: ['PASS', 'FAIL'] },
  },
}

const DEMO = {
  type: 'object',
  additionalProperties: false,
  required: ['passed'],
  properties: {
    passed: { type: 'boolean' },
    output: { type: 'string', description: 'Truncated output; traceback tail on failure.' },
  },
}

const ANALYSIS = {
  type: 'object',
  additionalProperties: false,
  required: ['verdict'],
  properties: {
    verdict: { type: 'string', enum: ['PASS', 'ISSUES_FOUND'] },
    notes: { type: 'string' },
  },
}

const COMMIT = {
  type: 'object',
  additionalProperties: false,
  required: ['committed', 'sha', 'untrackedClean'],
  properties: {
    committed: { type: 'boolean' },
    sha: { type: 'string' },
    untrackedClean: { type: 'boolean', description: 'True iff no sprint-scoped untracked files remain after commit.' },
    note: { type: 'string', description: 'Any anomaly (e.g. an untracked deliverable remained).' },
  },
}

const SPRINT_REVIEW = {
  type: 'object',
  additionalProperties: false,
  required: ['recommendation', 'findings', 'raw'],
  properties: {
    recommendation: { type: 'string', enum: ['APPROVED', 'REVISIONS_NEEDED'] },
    findings: { type: 'array', items: { type: 'string' } },
    raw: { type: 'string' },
  },
}

const CHECKS = {
  type: 'object',
  additionalProperties: false,
  required: ['clean'],
  properties: {
    clean: { type: 'boolean' },
    output: { type: 'string', description: 'Untracked files, "# Future:" hits, or bare "    pass" hits, if any.' },
  },
}

// ---- prompt builders (faithful to implement-sprint templates, minus the text
//      OUTPUT-FORMAT blocks: the schema replaces those) ----

const wd = `Working directory: ${worktree}\nAll paths absolute or relative to the worktree root. Invoke Python via \`uv run python\` from the worktree root.`

const implPrompt = (p) => `Implement Phase ${p.n}: ${p.name} for the ${sprint} sprint.
${wd}

Sprint spec: ${specPath}
Read the spec, focus on Phase ${p.n}. Read source files as needed (LSP tools, not Grep, for code structure).

QUALITY RULES (mandatory):
- Module-level functions, not inner closures -- helpers must be independently testable.
- Extract shared logic (DRY); no copy-paste-and-modify.
- Deliver every Phase ${p.n} success criterion in the spec; missing examples/configs count as failures.
- TYPE_CHECKING for type-only imports; remove stale/scaffolding comments.
- Tests go in the directory matching the code under test. Never a tests/sprints/ dir or sprint-named test file.
- A change that forces the SAME edit across many files (config-field migration, rename, signature ripple) is a CODEMOD job: write and run a script (libcst/ast), do not hand-edit file by file -- that overflows context on large sweeps.

DELIVERABLES (mandatory):
- All files in the Phase ${p.n} Files table (impl + tests).
- The demo at exactly: ${p.demo} -- create it before returning; do not defer.
- Pre-commit clean on the files you touched: \`pre-commit run --files <touched>\` until exit 0 (max 3 runs).

ITERATION LIMITS: max 3 attempts per failing test; max 3 per demo fix; never read the same file more than twice.

Report your result via the structured-output tool: files, demo path, precommit PASS/FAIL, test pass/fail counts, whether the demo emits simulation data, and 1-3 key decisions.`

const gatePrompt = (tag) => `Run the ${sprint} test gates (${tag}). Mechanical gate run -- you are a NEUTRAL runner, not a judge.
${wd}

Execute each command EXACTLY ONCE, in order, from the worktree root. Stop at the first non-zero exit:
${gates.map((g, i) => `  ${i + 1}. ${g}`).join('\n')}

Do NOT read source to diagnose, do NOT fix anything, do NOT retry with variant flags. Run, capture, report.
On failure: report the failing command and the last ~60 lines of its output.`

const reviewPrompt = (p) => `Review Phase ${p.n}: ${p.name} of the ${sprint} sprint. Fresh eyes; Phase ${p.n} only.
${wd}

Spec: ${specPath}. Inspect this phase's changes via \`git diff HEAD\`.
PRE-COMMIT: run \`pre-commit run --files <files in the diff>\` ONCE. Any non-zero exit (real violation OR auto-fix modification) is a finding. Do not re-run, do not fix.
Do NOT flag exported-but-unused symbols/migrations the spec assigns to a LATER phase (cross-check later-phase Files tables) -- end-state Principle #8 is enforced at sprint review, not here.

Return verdict APPROVED or REVISIONS_NEEDED, precommit PASS/FAIL, every finding verbatim, and raw = your full review text.`

const fixPrompt = (label, rawFindings) => `Fix review findings for ${label} of the ${sprint} sprint.
${wd}

Spec: ${specPath}. Read it for context, then address the findings below.

REVIEWER FINDINGS (verbatim -- address every one):
${rawFindings}

RULES: address every finding; if one is genuinely wrong, mark it DISPUTED with a one-line reason (do not silently skip); run the tests after fixing; change nothing beyond what the findings require; re-run pre-commit on touched files until exit 0 (max 3).

Report: per-finding resolution (FIXED / DISPUTED+reason), files modified, test pass/fail counts, precommit PASS/FAIL.`

const demoPrompt = (demo) => `Run one demo and report exit status. Mechanical -- do not fix anything.
${wd}
Command: uv run python ${demo}
passed = (exit code == 0). On failure include the traceback tail.`

const analyzePrompt = (p) => `Analyze the simulation output produced by the Phase ${p.n} demo of the ${sprint} sprint.
${wd}
Demo: ${p.demo}. Run your standard data-realism validation workflow.
Return verdict PASS or ISSUES_FOUND with brief notes.`

const commitPrompt = (p, impl, cycles) => {
  const next = p.isLast ? p.n : p.n + 1
  const cyclesNote = cycles > 0 ? ` after ${cycles} fix cycle(s)` : ''
  return `Commit Phase ${p.n} of the ${sprint} sprint. MECHANICAL ONLY -- do not evaluate code quality.
${wd}

1. Edit docs/sprints/${sprint}/state.yaml: set phases.${p.n}.status to "complete"; set current_phase to ${next}.
2. \`git add\` ONLY these explicit paths (never -A):
     docs/sprints/${sprint}/state.yaml
${impl.files.map((f) => `     ${f}`).join('\n')}
3. Commit, message:
     Sprint ${sprint} - Phase ${p.n}: ${p.name}

     - Tests: PASS
     - Pre-commit: PASS
     - Review: APPROVED${cyclesNote}

     Co-Authored-By: Claude <noreply@anthropic.com>
4. Attach a git note on refs/notes/agent/sprint with JSON:
     {"sprint":"${sprint}","phase":${p.n},"review_cycles":${cycles},"decisions":${JSON.stringify(impl.decisions)},"files":${JSON.stringify(impl.files)}}
5. Verify: \`git status --porcelain | grep '^??'\`. If any untracked file sits under a reported path, set untrackedClean=false and name it.

Report committed, the commit sha, and untrackedClean.`
}

// Inlines the /review-sprint protocol (canonical source:
// .claude/skills/review-sprint/SKILL.md). A subagent cannot invoke the Skill
// tool, so the protocol is embedded here; the agent may Read the skill for full
// gate detail. /review-sprint is itself a single reviewer-agent task, so this is
// a faithful inline, not a collapse of a fan-out.
const sprintReviewPrompt = () => `Fresh-eyes SPRINT-LEVEL review of the ${sprint} sprint. This inlines the /review-sprint protocol -- canonical source .claude/skills/review-sprint/SKILL.md; read it for full gate detail.
${wd}
Diff base: \`git merge-base HEAD ${parent}\`. Spec: ${specPath}.

CONTEXT (mandatory): load tier-1 (CLAUDE.md principles #7/#8 + Anti-Patterns; ${specPath}; changed files vs the diff base; their test files). Before gates 2, 4, and 7 ALSO load tier-2 (sibling source files + existing tests in every touched package) -- duplicate-helper and test-value detection are unauditable without it. Fresh on INTENT, not on existing code. Use LSP tools, not Grep, to trace defs/callers.

Run all 10 gates. Never collapse a gate to a bare PASS -- every gate gets a one-sentence Note even when clean:
  1. Dead code -- scaffolding ("# Future:" / "# TODO:" / bare \`pass\`), inert self-renames (\`+ foo = bar\` with no later divergence), 1-2 element module constants used once, and sprint-added public symbols with no production caller (the reference chain must terminate outside tests/demos/__init__ re-exports).
  2. Consistency / DRY (needs tier-2) -- every new top-level def/class/const vs pre-existing siblings; flag bodies <30% different, one-call-site helpers (inline), cross-shape helper reuse.
  3. Test names -- does each changed test verify what its name claims (order / deterministic / error lies)?
  4. Test value (needs tier-2) -- >=3 tests differing only in literals -> parametrize; weak assertions (\`len>0\`, \`is not None\`, \`== == \`, isinstance-only) on deterministic fixtures -> exact-value.
  5. Coverage -- new files <85%; uncovered error paths.
  6. Type-ignore density -- >1 per file, or >=3 same-shape across the diff -> centralize via a test helper (never loosen the production signature).
  7. Spec <-> codebase, BOTH directions: 7a read sprint git notes (refs/notes/agent/sprint) for the implementer's decisions; 7b spec->impl (signatures/docstrings/raises match; impl worse than spec = bug); 7c impl->spec (did the spec prescribe a helper/const/fixture the package already had? flag as a spec-time miss even if faithfully built).
  8. Workspace -- untracked files not in .gitignore.
  9. Pre-commit -- \`pre-commit run --all-files\`; any failure is a finding.
  10. Demos -- run every phase demo TWICE; flag failure or output drift between runs.

OUTPUT: write docs/sprints/${sprint}/review.md (header \`**Date:** ${today}\`, \`**Reviewer:** Claude (fresh eyes, tier-2 context loaded)\`) using the Severity (clean / observations / blockers) + Findings + Notes schema -- one row per gate, Notes populated even when clean. Do NOT file \`finding\` notes; review.md is the only output. Be skeptical: a spec passing does not imply the spec was right (gate 7c).

Then return the structured result. ROUTING: recommendation=APPROVED ONLY if every gate is clean (zero blockers AND zero observations). ANY finding -- blocker OR observation (what /review-sprint calls APPROVED-WITH-NOTES) -- maps to REVISIONS_NEEDED, because this loop binds every finding to a fix. findings = every finding verbatim; raw = the review.md body.`

const allDemosPrompt = (tag) => `Run EVERY phase demo once, in order (${tag}). Mechanical.
${wd}
${phases.map((p) => `  uv run python ${p.demo}`).join('\n')}
passed = all exit 0. On any failure, name the demo and include the traceback tail.`

const completionChecksPrompt = () => `Run completion checks in the worktree. clean = all three produce NO output.
${wd}
  1. git status --porcelain | grep '^??'
  2. grep -rn "# Future:" src/
  3. grep -rn "    pass$" src/
Report any hits. Do not fix anything.`

// ---------------------------------------------------------------------------
// orchestration -- a sequential for-await over phases. NOT pipeline(): phase
// N+1 must not start until phase N has committed in the worktree.
// ---------------------------------------------------------------------------

const phaseResults = []

function halt(reason, phaseNum, detail) {
  log(`HALT at phase ${phaseNum}: ${reason}`)
  return { status: 'halted', reason, phase: phaseNum, detail, phasesCompleted: phaseResults }
}

// All agent() calls run inside run(). A thrown agent error -- e.g. an implementer
// exhausting its context window ("Prompt is too long"), which the runtime cannot
// then turn into a StructuredOutput result -- is caught below and converted to a
// graceful halt that preserves already-committed phases, instead of crashing the
// whole workflow with an uncaught exception.
async function run() {
for (let i = 0; i < phases.length; i++) {
  const p = { ...phases[i], isLast: i === phases.length - 1 }
  log(`Phase ${p.n}: ${p.name} -- implementing`)

  const impl = await agent(implPrompt(p), { agentType: 'implementer', label: `impl P${p.n}`, phase: 'Implement', schema: IMPL })

  let gate = await agent(gatePrompt(`P${p.n}`), { agentType: 'ops-gate', label: `gate P${p.n}`, phase: 'Gate', schema: GATE })
  if (!gate.passed) return halt('test-gate', p.n, gate)

  let review = await agent(reviewPrompt(p), { agentType: 'reviewer', label: `review P${p.n}`, phase: 'Review', schema: REVIEW })

  const findingsSeen = []
  const fixResolutions = []
  let cycles = 0
  while (review.verdict === 'REVISIONS_NEEDED' && cycles < 3) {
    cycles += 1
    findingsSeen.push(...review.findings)
    log(`Phase ${p.n}: fix cycle ${cycles}/3 (${review.findings.length} finding(s))`)

    const fix = await agent(fixPrompt(`Phase ${p.n}: ${p.name}`, review.raw), { agentType: 'implementer', label: `fix P${p.n}.${cycles}`, phase: 'Fix', schema: FIX })
    fixResolutions.push(...fix.resolutions)

    gate = await agent(gatePrompt(`P${p.n} re-gate`), { agentType: 'ops-gate', label: `gate P${p.n}.${cycles}`, phase: 'Gate', schema: GATE })
    if (!gate.passed) return halt('test-gate-after-fix', p.n, gate)

    review = await agent(reviewPrompt(p), { agentType: 'reviewer', label: `review P${p.n}.${cycles}`, phase: 'Review', schema: REVIEW })
  }
  if (review.verdict !== 'APPROVED') return halt('fix-cycles-exhausted', p.n, { findingsSeen, lastReview: review.raw })

  const demo = await agent(demoPrompt(p.demo), { agentType: 'ops-gate', label: `demo P${p.n}`, phase: 'Demo', schema: DEMO })
  if (!demo.passed) return halt('demo', p.n, demo)

  let analysis = null
  if (impl.producesSimData) {
    analysis = await agent(analyzePrompt(p), { agentType: 'data-analyst', label: `analyze P${p.n}`, phase: 'Analyze', schema: ANALYSIS })
  }

  const commit = await agent(commitPrompt(p, impl, cycles), { agentType: 'ops-gate', label: `commit P${p.n}`, phase: 'Commit', schema: COMMIT })
  if (!commit.committed || !commit.untrackedClean) return halt('commit', p.n, commit)

  phaseResults.push({
    phase: p.n, name: p.name, files: impl.files, decisions: impl.decisions,
    reviewCycles: cycles, findingsSeen, fixResolutions,
    analysis: analysis ? analysis.verdict : 'n/a', sha: commit.sha,
  })
}

// ---- post-implementation: sprint-level review with its own bounded fix loop ----

log('All phases committed -- running sprint-level review')
let sr = await agent(sprintReviewPrompt(), { agentType: 'reviewer', label: 'sprint-review', phase: 'Sprint review', schema: SPRINT_REVIEW })
let srCycles = 0
const srResolutions = []
while (sr.recommendation === 'REVISIONS_NEEDED' && srCycles < 3) {
  srCycles += 1
  log(`Sprint review: fix cycle ${srCycles}/3 (${sr.findings.length} finding(s))`)

  const fix = await agent(fixPrompt(`the ${sprint} sprint (review cleanup)`, sr.raw), { agentType: 'implementer', label: `sprint-fix.${srCycles}`, phase: 'Fix', schema: FIX })
  srResolutions.push(...fix.resolutions)

  const g = await agent(gatePrompt('post-sprint-fix'), { agentType: 'ops-gate', label: `gate sr.${srCycles}`, phase: 'Gate', schema: GATE })
  if (!g.passed) return halt('gate-after-sprint-fix', 'sprint', g)

  // commit the cleanup (explicit paths come from the fixer)
  await agent(
    `Commit sprint review cleanup for ${sprint}. MECHANICAL. ${wd}\n` +
      `git add ONLY: ${fix.files.join(' ')}\nCommit message: "Sprint ${sprint} - review cleanup".\n` +
      `Verify no sprint-scoped untracked files remain. Report committed, sha, untrackedClean.`,
    { agentType: 'ops-gate', label: `commit sr.${srCycles}`, phase: 'Commit', schema: COMMIT },
  )

  sr = await agent(sprintReviewPrompt(), { agentType: 'reviewer', label: `sprint-review.${srCycles}`, phase: 'Sprint review', schema: SPRINT_REVIEW })
}
if (sr.recommendation !== 'APPROVED') return halt('sprint-review-exhausted', 'sprint', { findings: sr.findings, raw: sr.raw, srResolutions })

// ---- finalize: demos twice + completion checks ----

log('Sprint review APPROVED -- demos x2 + completion checks')
const demosRun1 = await agent(allDemosPrompt('run 1 of 2'), { agentType: 'ops-gate', label: 'demos x2 (1)', phase: 'Finalize', schema: DEMO })
const demosRun2 = await agent(allDemosPrompt('run 2 of 2'), { agentType: 'ops-gate', label: 'demos x2 (2)', phase: 'Finalize', schema: DEMO })
const checks = await agent(completionChecksPrompt(), { agentType: 'ops-gate', label: 'completion checks', phase: 'Finalize', schema: CHECKS })

return {
  status: 'ready-for-decision', // the skill presents this and handles ACCEPT / FIX
  sprint,
  worktree,
  parent,
  phases: phaseResults,
  sprintReview: { cycles: srCycles, resolutions: srResolutions },
  demosTwice: { run1: demosRun1.passed, run2: demosRun2.passed },
  completionChecks: checks,
}
} // end run()

try {
  return await run()
} catch (e) {
  const msg = e && e.message ? e.message : String(e)
  return halt('agent-error', 'unknown', {
    error: msg,
    note:
      'an agent threw (likely context overflow / StructuredOutput failure). A phase whose scope is a codebase-wide migration sweep can exhaust a single ' +
      'implementer; split it at the planning layer. Already-committed phases are in phasesCompleted.',
  })
}
