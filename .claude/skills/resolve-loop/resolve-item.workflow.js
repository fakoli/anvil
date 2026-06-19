// resolve-item.workflow.js — reference harness for the resolve-loop skill.
//
// Drives ONE backlog/roadmap item through: research (fan out) -> judge
// (ponytail gate) -> [optional] implement in an isolated worktree ->
// adversarial review (pass/fail). Returns a vetted plan (and, if a worktree
// path is supplied, an implemented + reviewed result). The CALLER handles the
// PR + Greptile + merge per the skill's PR protocol.
//
// Run it:  Workflow({ scriptPath: "<this file>", args: ITEM })
//   ITEM = {
//     id, title, problem, acceptance, likely_files: [..],
//     worktree?: "/abs/path/to/worktree"   // omit to stop after the vetted plan
//   }
//
// It is a TEMPLATE — read it, tweak the agent counts / prompts for the item at
// hand, then run. Plain JS (no TS). Adapt freely; don't treat it as frozen.

export const meta = {
  name: 'resolve-item',
  description: 'Research -> judge (ponytail) -> implement-in-worktree -> adversarial review for one backlog item',
  phases: [
    { title: 'Research' },
    { title: 'Judge' },
    { title: 'Implement' },
    { title: 'Review' },
  ],
}

const item = args || {}
const ctx =
  `ITEM ${item.id || ''}: ${item.title || ''}\n` +
  `Problem: ${item.problem || '(read the spec)'}\n` +
  `Acceptance: ${item.acceptance || '(from the backlog)'}\n` +
  `Likely files: ${(item.likely_files || []).join(', ') || '(discover them)'}\n` +
  `Repo golden rules: uv only; never commit secrets; smallest correct diff; leave a runnable check.`

const APPROACH = {
  type: 'object', additionalProperties: false,
  required: ['angle', 'sketch', 'files', 'risks', 'test_plan', 'diff_size'],
  properties: {
    angle: { type: 'string' },
    sketch: { type: 'string', description: 'Concrete approach: what changes, where, how.' },
    files: { type: 'array', items: { type: 'string' } },
    risks: { type: 'string' },
    test_plan: { type: 'string' },
    diff_size: { type: 'string', enum: ['xs', 's', 'm', 'l', 'xl'] },
  },
}

const CHOICE = {
  type: 'object', additionalProperties: false,
  required: ['chosen_angle', 'why', 'plan', 'cut', 'test_plan'],
  properties: {
    chosen_angle: { type: 'string' },
    why: { type: 'string', description: 'Why this is the laziest correct option.' },
    plan: { type: 'string', description: 'Step-by-step implementation plan.' },
    cut: { type: 'string', description: 'Speculative/over-engineered bits explicitly cut.' },
    test_plan: { type: 'string' },
  },
}

const VERDICT = {
  type: 'object', additionalProperties: false,
  required: ['verdict', 'findings'],
  properties: {
    verdict: { type: 'string', enum: ['PASS', 'FAIL'] },
    findings: { type: 'array', items: {
      type: 'object', additionalProperties: false,
      required: ['severity', 'detail'],
      properties: {
        severity: { type: 'string', enum: ['blocker', 'should-fix', 'nit'] },
        detail: { type: 'string' },
      },
    } },
  },
}

// 1. Research — diverse angles, in parallel.
phase('Research')
const ANGLES = [
  'minimal-diff: the smallest change that satisfies the acceptance criteria',
  'reuse-existing: solve it by reusing engine code / stdlib already in the repo, adding as little as possible',
  'correctness-first: the approach least likely to have edge-case or concurrency bugs, even if slightly larger',
]
const approaches = (await parallel(ANGLES.map(a => () =>
  agent(
    `Propose an implementation approach for this item, from the "${a}" angle.\n\n${ctx}\n\n` +
    `Read the actual code in the likely files first. Return a concrete approach.`,
    { label: `research:${a.split(':')[0]}`, phase: 'Research', schema: APPROACH }
  )
))).filter(Boolean)

// 2. Judge — the ponytail gate. Pick the laziest correct approach.
phase('Judge')
const choice = await agent(
  `You are the ponytail reviewer (lazy senior dev: efficient, not careless). ` +
  `Given these candidate approaches, pick the LAZIEST one that is actually correct ` +
  `— reuse over new code, stdlib over deps, shortest working diff — and cut speculative flexibility.\n\n` +
  `${ctx}\n\nCandidates:\n${JSON.stringify(approaches, null, 2)}\n\n` +
  `Return the chosen angle, why it's the lazy-correct pick, a step-by-step plan, what you cut, and the test plan.`,
  { label: 'judge:ponytail', phase: 'Judge', schema: CHOICE }
)

// Stop here with a vetted plan unless a worktree was supplied to implement in.
if (!item.worktree) {
  return { stage: 'vetted-plan', item: { id: item.id, title: item.title }, approaches, choice }
}

// 3. Implement — in the caller-supplied worktree (isolation: each item its own).
phase('Implement')
const impl = await agent(
  `Implement this vetted plan by editing files in the git worktree at ${item.worktree}. ` +
  `Edit files under that path only. Follow the plan exactly; keep the diff minimal; ` +
  `add the test from the test plan. Do NOT commit, push, or open a PR — just make the edits.\n\n` +
  `${ctx}\n\nPlan:\n${JSON.stringify(choice, null, 2)}\n\n` +
  `Return a summary of what you changed and the test you added.`,
  { label: 'implement', phase: 'Implement' }
)

// 4. Adversarial review — fresh eyes, pass/fail, until clean or 2 rounds.
phase('Review')
const MAX_ROUNDS = 2
let verdict = null
for (let round = 0; round < MAX_ROUNDS; round++) {
  const reviews = (await parallel(['correctness', 'over-engineering + edge cases'].map(lens => () =>
    agent(
      `Adversarially review the uncommitted diff in the worktree at ${item.worktree} ` +
      `(run \`git -C ${item.worktree} diff\`) through the "${lens}" lens. You did NOT write this code. ` +
      `Hunt blind spots, missing edge cases, and over-engineering. Return PASS or FAIL with findings. ` +
      `Default to FAIL if uncertain.\n\n${ctx}`,
      { label: `review:${lens.split(' ')[0]}:r${round}`, phase: 'Review', schema: VERDICT }
    )
  ))).filter(Boolean)
  const blockers = reviews.flatMap(r => r.findings).filter(f => f.severity === 'blocker')
  verdict = { round, pass: reviews.every(r => r.verdict === 'PASS') && blockers.length === 0, reviews }
  if (verdict.pass) break
  // Only apply fixes if another review round will follow — otherwise the
  // worktree would end up ahead of the FAIL verdict we return, with the fix
  // never re-reviewed. On the last round we return the honest FAIL and let the
  // caller decide (re-run the loop, or intervene).
  if (round < MAX_ROUNDS - 1) {
    await agent(
      `Fix these review findings in the worktree at ${item.worktree}. Keep the diff minimal.\n\n` +
      `${JSON.stringify(reviews.flatMap(r => r.findings), null, 2)}`,
      { label: `fix:r${round}`, phase: 'Review' }
    )
  }
}

return { stage: 'implemented', item: { id: item.id, title: item.title }, choice, impl, verdict }
