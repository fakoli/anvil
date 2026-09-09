# Execute Anvil work in your current harness

Anvil owns project state. The current harness owns reasoning, tool execution,
file edits, and model access. This works in a Codex or Claude subscription
session and in a harness connected to a locally served model. Selecting a
planning provider does not replace the model of an already running session.

## Keep model execution in the session

Read `anvil status` from the exact checkout and use its resolved config path:

```yaml
llm_provider: harness
llm_allow_api: false
llm_fallback: false
```

This explicit mode rejects nested LLM augmentation, including the automatic
planning backstop. The current session authors `## Tasks` in the selected PRD
source resolved by `anvil prd source-name --json`. After the required PRD review
and approval, use `anvil plan --no-llm`, `anvil score`, and `anvil review tasks`.
For oversized work, the current session authors smaller tasks in the source,
then re-parses and follows the review flow; do not use `expand --use-llm`.

The ordinary execution loop is the same on either kind of model:

1. Inspect `anvil next`, claim the chosen task, and use the branch/worktree and
   actor identity returned by the claim.
2. Fetch `anvil packet TASK_ID` and read the complete packet.
3. Implement in that worktree using the current harness's tools. Keep the lease
   current and follow its evidence-capture environment.
4. Run the packet's verification commands and submit the actual commands,
   changed files, and required typed proofs with `anvil submit TASK_ID`.
5. Stop at `needs_review`. Complete the repository's independent review gates;
   human confirmation is required before `anvil apply --approve`.

With MCP, use `get_next_task`, `claim_task`, `generate_work_packet`, and
`submit_completion_evidence`. These are the same state-engine operations;
there is no provider-specific execution endpoint and no model key requirement.
The [execute skill](https://github.com/fakoli/anvil/blob/main/skills/execute/SKILL.md)
carries the complete claim-to-evidence workflow.

## Harness connected to a local model

Configure the **harness** to use your existing local serving router and an
explicit model alias using that harness's own provider configuration. Keep
Anvil's CLI or MCP server available to it. Use the same `harness` config and
execution loop above. Anvil will not start a model server, change routes,
substitute a cloud model, or alter separately selected cloud sessions.

If instead you want Anvil itself to call a local OpenAI-compatible endpoint for
planning augmentation, that is an explicit API connection:

```yaml
llm_provider: custom
llm_allow_api: true
llm_fallback: false
custom_base_url: http://localhost:8000/v1
llm_model: your-explicit-local-alias
```

The URL is illustrative. Use your existing router's supported endpoint and
authentication. The `custom` adapter retains Chat Completions semantics; use
`openai` for native OpenAI Responses requests. See [LLM configuration](../llm.md).

## Qualification

The isolated `evals/cases/execute.yaml` case verifies artifact behavior, evidence
submission, and the final `needs_review` state using the real CLI. Run it with
Codex or Claude subscription execution as documented in
[behavioral evals](https://github.com/fakoli/anvil/blob/main/evals/README.md).
Offline integration tests verify the same state loop without invoking a model.
A local-model deployment still needs its own tool-use qualification against
its selected router and model; Anvil's portable state contract does not assert
that every local model can follow a work packet.
