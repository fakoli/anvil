# Optional Jev advice

Jev is a TypeSafe-hosted typed decision aid. It is disabled by default and
never approves a PRD, satisfies a proof, executes a command, or selects your
planner/model. Ordinary Anvil commands keep working without it.

## Start with the switch

```bash
anvil jev status
anvil jev enable evidence_triage --allow-api
anvil jev evaluate evidence_triage --input selected-evidence.json --allow-export
anvil jev disable
```

The enable command updates the existing resolved project configuration, not
your repository or provider choice. `--allow-api` explicitly grants the
existing `llm_allow_api` permission; it does not enable planner fallback.
Each evaluation separately requires permission to export that selected input.
`anvil jev disable evidence_triage` removes only that capability; omitting the
name stops all new Jev requests. `--no-jev` disables a single evaluation/audit.

Provide `TYPESAFE_API_KEY` through the trusted CLI/service process environment
or its protected environment-file mechanism. The configuration stores only its
environment-variable name. Anvil does not search, source, or load a shared home
env file. Never put a key in a request JSON, command argument, PRD, or tracked
config. With Jev disabled, no key lookup or provider request occurs.

The `jev` settings block is loaded with normal project-over-global precedence;
the project block replaces the global block, rather than merging capability
lists. Supported fields are `enabled`, `capabilities`, pinned `model`,
`api_key_env`, and `timeout_seconds`. The default model is `jev-1.13.0`; moving
aliases are refused. Existing configs without the block remain disabled.
Enable/disable preserves other setting values and uses verified atomic
publication with recoverable displaced config files. Comments may be
normalized by YAML serialization.

## Choose a bounded question

`selected-evidence.json` can contain:

```json
{
  "claim": "The authenticated routed request returned the expected document.",
  "observation": "The backend process started. No routed response was recorded."
}
```

The result may identify insufficient semantic support. It does not establish
whether the observation really happened. Actual proof verification remains
the existing Anvil claim-bound artifact and review process.

| Capability | Exact input fields | Returned judgment |
|---|---|---|
| `prd_review` | `criteria: [{id, text}]` | Separate success Score and failure Noul per criterion |
| `evidence_triage` | `claim`, `observation` | `relation`: supports, contradicts, insufficient |
| `proof_contracts` | `claim` | Suggested missing observation category, or abstention |
| `skill_suggestion` | `intent`, `candidates: [{id, description}]` | `selection`: an eligible ID or none |
| `context_ranking` | `intent`, `candidates: [{id, text}]` | Relevance Score per optional candidate |
| `incident_triage` | `observation` | Closed diagnostic category, including unknown |
| `voice_intent` | `text` | Closed intent class, including unclear/unsupported |

Inputs reject unknown fields, duplicate/unsafe IDs, known credential patterns,
and oversized content. IDs start with a letter and contain only letters,
digits, underscores, or hyphens, up to 48 characters; `none` is reserved.
Each text is at most 4,096 characters, a PRD request has at most 16 criteria,
and a candidate list at most 24 items. The complete provider request is capped
at 32 KiB; overhead can make the effective content bound lower.
These limits do not imply that arbitrary permitted text is safe to export.

```bash
anvil jev enable prd_review --allow-api
anvil jev assess --file draft.md --allow-export --json
```

`assess` performs local parsing and deterministic readiness assessment, then
optionally sends only top-level acceptance criteria. It does not send the full
PRD or automatically select task-level criteria. To assess a chosen task's
criteria or more than 16 total criteria, deliberately select a bounded subset
using the `prd_review` JSON form. Nothing rewrites the PRD or its scores.

## Audit several selected items

```bash
anvil jev audit --input selected-audit.json --allow-export --json
```

The input is a list of 1–16 objects with exact fields `id`, `capability`, and
`input`. Each enabled item is one bounded request, in source order, with an
independent annotation. Disabled capabilities do not make a request. IDs are
unique ASCII letters/digits/underscores/hyphens, up to 64 characters.
The command does not discover additional files or auto-submit its results.
Changes to the selected file or effective policy invalidate returned advice;
a changed policy also stops the next item. Already transmitted bytes cannot
be recalled. `request_count` retains attempted calls even when advice is stale.

## Know when Jev was involved

Human output says “Jev advisory” and “not proof or approval.” JSON uses the
standard Anvil envelope and `data.schema: anvil.jev.annotation.v1`:

- `requested`: the capability was enabled and selected.
- `request_started`: an HTTP attempt began, including attempts that failed.
- `used`: a complete validated answer is available for this exact input.
- `status` and `reason`: disabled, blocked, unavailable, invalid response, or
  completed, with a bounded explanation code.
- `provider`, `model`, `input_digest`, `rubric_digest`, `usage`, and
  `elapsed_ms`: provenance and local measurements, not proof of correctness.

Choice answers contain `choice`, `probabilities`, and `confidence`; Score
answers contain fractional `score`, `probabilities`, and `confidence`; Noul
answers contain `noul`. Confidence is provider-reported, not locally calibrated.
No universal confidence cutoff is installed. A timeout/error never means “no
issues found.” Provider failures are advisory reports (exit zero); invalid CLI
inputs/configuration exit nonzero. Consumers must inspect `status` and `used`.

## Consumer boundary

`anvil jev bridge --json` reads one bounded stdin envelope containing `jev`,
`allow_api`, `allow_export`, `capability`, and `input`. It is a stateless local
process interface for an already trusted owner, such as Anvil Serving. It does
not load project config, initialize state, authenticate browser users, or grant
permission. The caller must enforce its own effective policy, authorization,
input selection, stale-result checks, and timeout. Never expose a web endpoint
that forwards caller-supplied permission/configuration fields into this bridge.

Serving owns its UI, transcript consent, resource access, and capability
switches. Browser code never receives the TypeSafe credential. Classification
cannot change the gateway route or invoke operational tools.

## Limits that matter

Jev can confidently misread text or accept a fabricated observation as
semantic support. Known-secret filtering is a safety net, not data-loss
prevention. Do not export private logs, full histories, secret files, or
unapproved customer data. Standard-service zero retention is not assumed.
There are no automatic retries, redirects, fallback providers, result caches,
or semantic acceptance gates.

The [PRD suite](../specs/jev-integration/README.md) records requirements,
ownership, future-system boundaries, and the distinction between source
delivery and live deployment. All switches remain off after installation.
