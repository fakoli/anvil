---
name: resolve-loop
description: Drive backlog/roadmap items end-to-end, autonomously and in parallel. For each item — research the problem with multiple agents, judge approaches with /ponytail:ponytail-review, implement in an isolated git worktree, adversarially review for blind spots (pass/fail), then ship ONE PR per item (wait for CI + Greptile, address the review, merge). Use when the user says "run the loop", "resolve the backlog", "work the roadmap", "clear the issues", or asks for autonomous multi-item delivery on this repo.
---

# Resolve Loop

The repeatable harness for turning a backlog/roadmap item into a merged PR
without a human narrating each step. One item → one PR. Many items → run the
loop per item, in parallel, each in its own worktree so they never stomp.

This skill exists so the operator never has to re-describe the process. When
they say "run the loop on X", follow this verbatim.

## Operating contract

- **Autonomous, not noisy.** Make obvious calls yourself; surface only genuine
  key decisions (ambiguous scope, breaking/SPEC-FIRST changes, anything that
  needs the operator's product judgement). Batch questions; don't drip them.
- **Lazy first (ponytail).** The first solution that works and is correct wins.
  No speculative abstractions. The judging phase is explicitly a ponytail gate.
- **Hold breaking changes.** Implement SPEC-FIRST / breaking-change items, but
  do **not** auto-merge them — open the PR and get an explicit yes/no.
- **Report at the end.** PRs opened/merged, decisions taken, items held, things
  skipped + why.
- **Repo golden rules apply** (see `CLAUDE.md`): `uv` only; `env -u
  GITHUB_TOKEN gh …`; never commit secrets; one PR per item; merge only after CI
  green **and** Greptile addressed.

## The loop (per item)

Run these phases with the **Workflow** tool when an item is non-trivial (a
reference script ships beside this file:
[`resolve-item.workflow.js`](resolve-item.workflow.js) — read it, adapt the
`ITEM`, run it). Trivial mechanical items can skip straight to implement.

### 1. Frame the problem
Read the item's spec, acceptance criteria, and "likely files". State, in one
or two sentences, the actual problem to solve and what "done" means. If the
acceptance criteria are missing or contradictory, that's a key decision — ask.

### 2. Research (fan out)
Spawn **multiple** independent agents to propose implementation approaches —
each reads the relevant code and returns a concrete approach (files to touch,
sketch, risks, test plan). Diversity beats one agent iterated: e.g. a
minimal-diff angle, a reuse-existing-machinery angle, a correctness-first angle.

### 3. Judge (ponytail gate)
Run `/ponytail:ponytail-review` (or the `ponytail` reviewer) over the proposed
approaches. Pick the **laziest approach that is actually correct** — reuse
stdlib / existing engine code over new abstractions; shortest working diff. Kill
speculative flexibility here, before any code is written.

### 4. Implement (isolated worktree)
Implement the chosen approach in a **dedicated git worktree** so parallel items
never collide (`isolation: 'worktree'` in a Workflow agent, or `git worktree
add`). One item == one branch off `origin/main` == one worktree. Leave a
runnable check behind for non-trivial logic (a focused `test_*.py` or an
assert-based self-check) — lazy code without its check is unfinished.

### 5. Adversarial review (pass/fail)
Spawn fresh reviewers that did NOT write the code to hunt blind spots:
correctness bugs, missing edge cases, security, and over-engineering. Each
returns **PASS or FAIL** with evidence. On FAIL, loop back to step 4 with the
findings. Prefer ≥2 independent reviewers on risky changes; default to FAIL when
a reviewer is uncertain.

### 6. Ship (PR + Greptile)
See the PR protocol below. One PR per item.

## PR protocol (every PR)

1. Branch off `origin/main` (`git checkout -B <type>/<slug> origin/main`). Stage
   **named files only** — never `git add -A` (keeps secrets/scratch out).
2. Conventional-commit title; body explains the *why*. Push, open the PR with
   `env -u GITHUB_TOKEN gh pr create`.
3. **Wait for both checks**: the test job AND `Greptile Review`. Poll
   `env -u GITHUB_TOKEN gh pr checks <n>` until neither is `pending`.
4. **Read Greptile's review** (`gh pr view <n> --json reviews` + `gh api
   repos/<owner>/<repo>/pulls/<n>/comments`). For each finding:
   - **Real?** Fix it, push, let Greptile re-review.
   - **False positive / out of scope?** Reply on the inline comment explaining
     why, and record any deliberate deferral in `docs/tech-debt-backlog.md`
     (the repo's home for deferred review findings).
   Reply via `gh api -X POST repos/<owner>/<repo>/pulls/<n>/comments/<id>/replies`.
5. **Merge** only when CI is green and Greptile is addressed — squash:
   `env -u GITHUB_TOKEN gh pr merge <n> --squash --delete-branch`. Breaking /
   SPEC-FIRST items: stop here and get an explicit yes/no first.

## Running many items in parallel

- Give each item its own branch + worktree; never let two items write the same
  file concurrently. If two items must touch one file, sequence them.
- Pipeline, don't barrier: an item can be in review while another is still being
  implemented. PRs bake (CI + Greptile) asynchronously — open them, do other
  work, come back to merge.
- Respect dependency order from the backlog (`depends-on`). Land prerequisites
  first.

## Greptile bot

The reviewer posts as **`greptile-apps[bot]`** and surfaces as the
`Greptile Review` status check. It is established on this repo; its deferred
findings have historically been tracked in `docs/tech-debt-backlog.md` — keep
that convention.
