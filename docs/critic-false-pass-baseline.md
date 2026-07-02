# Critic false-pass baseline (roadmap SL-2)

**The claim this measures:** anvil's review gate is only as trustworthy as its
critic. If the critic waves bad diffs through, every downstream "PASS" is a lie.
SL-2 makes that risk measurable: a fault-injection harness feeds a corpus of
*known-bad* diffs to the critic and counts how many it approves — the
**false-pass rate**. You cannot improve the critic until you can score it.

> Roadmap: [`docs/roadmap.md`](roadmap.md) § Wave 1 → **SL-2**. Acceptance: a
> reproducible script plus a committed baseline false-pass number in `docs/`.

## What "false pass" means

For each corpus case we know the ground truth (`bad` or `good`). A backend
returns a verdict per diff — `PASS` (the critic *approved* / waved it through)
or `FAIL` (the critic *rejected* it). The oracle is two pure ratios:

```
false_pass_rate = (# BAD cases the critic PASSED) / (# BAD cases)   <- the headline
false_fail_rate = (# GOOD cases the critic FAILED) / (# GOOD cases)
```

A **false pass** is the dangerous one: a defect the critic missed. The good
controls catch the opposite failure — a critic so trigger-happy it rejects clean
diffs (which would make a low false-pass rate meaningless).

## The corpus

Lives at [`benchmarks/critic_corpus/`](https://github.com/fakoli/anvil/tree/main/benchmarks/critic_corpus). Each case
is a tiny, self-evident `<id>.diff` plus a `<id>.json` carrying `id`, `label`,
`defect_class`, and `description`. The four bad classes are exactly the ones the
roadmap names; two good controls guard against over-rejection.

| id | label | defect_class | what it injects |
|---|---|---|---|
| `off_by_one` | bad | off-by-one | `range(len(x))` → `range(len(x) + 1)`; indexes past the end |
| `dropped_null_check` | bad | dropped-null-check | deletes the `if user is None` guard |
| `assertion_free_test` | bad | assertion-free-test | adds a `def test_*` that calls but never asserts |
| `deleted_assertion` | bad | deleted-assertion | deletes the only `assert` in a passing test |
| `good_bugfix` | good | none | a correct empty-list guard; should PASS |
| `good_test` | good | none | a real test that calls **and** asserts; should PASS |

## How to run

The harness lives at [`benchmarks/critic_falsepass.py`](https://github.com/fakoli/anvil/blob/main/benchmarks/critic_falsepass.py).
Run it from `bin/` so it uses the synced `anvil` venv (uv only):

```bash
cd bin
uv run python ../benchmarks/critic_falsepass.py                 # mock backend (default)
uv run python ../benchmarks/critic_falsepass.py --backend mock --json
uv run python ../benchmarks/critic_falsepass.py --backend api   # real critic (manual)
```

It is backend-agnostic. A *backend* is just a callable `Case -> Verdict`:

* **`mock`** — a deterministic, dependency-free rule-set (a cheap linter). It
  catches the classes a simple textual rule can see and is blind, *by design*,
  to ones needing semantic reasoning. Makes **no** API call. Its number is the
  committed self-test reference below.
* **`api`** — the real critic agent ([`agents/critic.md`](https://github.com/fakoli/anvil/blob/main/agents/critic.md)),
  an LLM that needs `ANTHROPIC_API_KEY` and is non-deterministic. It therefore
  **cannot** run in deterministic pytest CI. It is a documented stub that raises
  `NotImplementedError` until the Agent-SDK call is wired in — it never silently
  fabricates a number.

## Mock backend — committed self-test reference

The mock backend's rules are fixed, so its result is stable across runs and is
asserted by [`tests/test_critic_falsepass.py`](https://github.com/fakoli/anvil/blob/main/tests/test_critic_falsepass.py)
(no network, no LLM). The mock catches `deleted-assertion`, `dropped-null-check`,
and `assertion-free-test`, but has **no arithmetic rule**, so it waves the
`off_by_one` diff through — one false pass out of four bad cases:

```
backend: mock
false_pass_rate = 1/4 = 0.25   (off_by_one missed)
false_fail_rate = 0/2 = 0.0    (both good controls correctly PASS)
```

This is **not** a claim about the real critic. It is a fixture that proves the
harness arithmetic — and it documents the methodology a reader will re-apply to
the real critic's verdicts.

## Real critic baseline — TODO (run with API access)

> **PLACEHOLDER — not yet measured.** The real `false_pass_rate` for the LLM
> critic is **TBD**. Populate it by wiring `api_backend()` in
> `benchmarks/critic_falsepass.py` to invoke `agents/critic.md` via the Claude
> Agent SDK, then running:
>
> ```bash
> cd bin
> ANTHROPIC_API_KEY=... uv run python ../benchmarks/critic_falsepass.py --backend api --json
> ```
>
> Record the resulting `false_pass_rate` (and `false_fail_rate`), the date, the
> model id, and the corpus revision in the table below. Because the critic is
> non-deterministic, capture the number over several runs and note the spread —
> mirror the "reproducible-aggregate, not bit-identical" caveat from the
> coordination benchmark ([`benchmarks/README.md`](https://github.com/fakoli/anvil/blob/main/benchmarks/README.md)).

| date | model | corpus rev | false_pass_rate | false_fail_rate | notes |
|---|---|---|---|---|---|
| _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ | run `--backend api` to populate |
