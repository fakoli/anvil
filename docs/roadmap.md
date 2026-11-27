# Roadmap

What Anvil is working toward, in the order it is planned. Shipped work is in
the [changelog](https://github.com/fakoli/anvil/blob/main/CHANGELOG.md).

## Now: proof integrity

A proof today records what a hook reported. The next step is a record Anvil
can check for itself.

- **Independent re-execution.** A new `anvil verify <task>` re-runs each
  recorded command in a clean checkout and countersigns the acceptance proof
  when the results match.
- **A strict evidence gate by default.** Today the gate is advisory unless you
  set `strict_evidence: true` or the task declares an evidence contract.
- **Published measurements.** How often an incomplete or fabricated submission
  gets through each gate mode, and how often the reviewing agent passes work
  it should not.
- **Portable proofs.** Export an acceptance proof to an existing proof format,
  so it can be checked without Anvil.

## Next: more places to sync

- Linear and Monday sync providers. GitHub Issues is the only provider today.
- Webhook-based sync through an opt-in listener that you start yourself.
- Conflict resolutions that apply immediately instead of on the next pass.

## Later

- Jira and GitHub Projects sync providers.
- `anvil snapshot`: local database snapshots with retention and restore.
- Sync tools on the MCP server, so an agent does not have to shell out.

## Not planned

- A background daemon that Anvil depends on. The core loop stays a CLI plus
  an MCP server that lives only as long as the agent session.
- A hosted service or account system.
- Real-time collaborative editing.

## Have a say

Open an [issue on GitHub](https://github.com/fakoli/anvil/issues) to propose
something or to argue for a different order. To build a sync provider now, the
[provider guide](sync-providers.md) walks through one.
