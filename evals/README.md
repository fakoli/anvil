# Anvil behavioral evaluations

These opt-in checks run a real Codex or Claude agent in a disposable Anvil
project, then verify Anvil's own state and the resulting artifact. They consume
subscription capacity and never run in ordinary CI.

```bash
RUN_BEHAVIORAL_EVALS=1 uv run --project bin python evals/run.py \
  evals/cases/start_prd.yaml --provider codex --model gpt-6-astra \
  --reasoning-effort high --report /tmp/anvil-start-prd-report.json
RUN_BEHAVIORAL_EVALS=1 uv run --project bin python evals/run.py \
  evals/cases/execute.yaml --provider codex --report /tmp/anvil-execute-report.json
RUN_BEHAVIORAL_EVALS=1 uv run --project bin python evals/run.py \
  evals/cases/execute.yaml --provider claude
```

The provider must have an existing subscription login. The shared adapter
checks public CLI auth status, ignores ambient user settings, and removes or
masks API credentials and provider overrides in child environments. It does not
read credential files, request a key, or change a running harness's provider.
Claude's SDK is a core dependency; Codex execution uses its installed CLI.

Cases:

- `start_prd.yaml`: author and parse a PRD; require draft status, required
  sections, and the parse event.
- `execute.yaml`: claim a ready task, implement a program, verify it, and submit
  evidence. Require `needs_review`, a submission event, and independently
  verified program output. Never automatically accept the task.

The runner uses `uv run --project <checkout>/bin` with its working directory and
`ANVIL_ROOT` pinned to the scratch project. It puts that checkout's `anvil` on
the agent PATH. State and artifacts are removed on exit. Reports contain case,
provider/model selection, latency, available token counts, and assertion
results; they exclude prompts, account metadata, and credentials. Codex token
counts measure subscription usage, not billable API dollars.

The same state operations work when the current harness uses a locally served
model. Configure that connection in the harness and use Anvil's `harness`
provider mode; see [the execution guide](../docs/how-to/execute-in-your-harness.md).
The subscription drivers here do not qualify a particular local-model deployment.

Add YAML cases with `prompt` (or interview answers), optional `prd_source`,
`config`, and `setup_commands`, plus deterministic assertions. The production
CLI performs every state mutation. Static and offline contract tests in
`tests/` remain the ordinary CI gate; live tests require
`RUN_BEHAVIORAL_EVALS=1` at both runner and driver entry points.
