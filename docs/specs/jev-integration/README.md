# Optional Jev integration

Status: implementation specification; authored 2026-09-20. Runtime acceptance
must be recorded separately. These documents do not approve Anvil state or
authorize enabling cloud processing for existing workloads.

See [validation and architecture review](VALIDATION.md) for implemented scope,
live synthetic measurements, extra applications, review findings, and limits.

## Read in this order

1. [Foundation and controls](foundation.md) — transport, privacy, switches,
   attribution, qualification, and rollback.
2. [Anvil semantic assurance](semantic-assurance.md) — PRD review, evidence
   triage, overclaim detection, and proof-contract preparation.
3. [Agent assistance](agent-assistance.md) — skill suggestions and context
   relevance ranking.
4. [Serving assistance](serving-assistance.md) — incident triage and voice
   intent classification.

This suite is authored in Anvil so its contracts can be reviewed together.
Anvil owns state and proof decisions; Anvil Serving owns its consumers and
operator configuration. Do not duplicate the PRDs across repositories or
modify private deployment state as part of source delivery. IDs are local to
each PRD; dependencies below are cross-document delivery dependencies, not
existing claimed tasks.

## Coverage and ownership

| Capability switch | Product consumer | Jev helps with | Jev cannot do |
|---|---|---|---|
| `prd_review` | Anvil PRD author | Find vague success/failure criteria | Approve or rewrite the PRD |
| `evidence_triage` | Anvil reviewer | Compare claimed outcome with reported observation | Verify authenticity or pass a proof gate |
| `proof_contracts` | Anvil task author | Suggest the missing category of observation | Generate or execute a test |
| `skill_suggestion` | Agent/Workbench user | Select relevant installed skill candidates | Install, invoke, or grant a skill authority |
| `context_ranking` | Workbench/agent consumer | Rank optional authorized context | Hide required instructions or expand access |
| `incident_triage` | Serving operator | Classify a bounded diagnostic excerpt | Restart, reroute, promote, or diagnose with certainty |
| `voice_intent` | Voice client | Label a finalized utterance | Execute a command or replace the selected model |

The seven switches cover the six original integrations plus a separate
proof-contract preparation use case. Overclaim detection is part of evidence
triage, not a second classifier with identical inputs.

## Delivery sequence

| Stage | Deliverable | Dependency | Required evidence |
|---|---|---|---|
| 1 | Default-off typed adapter and configuration | None | Offline transport/privacy/failure tests |
| 2 | Explicit advisory CLI and synthetic examples | Stage 1 | CLI tests, all seven capability fixtures |
| 3 | Native Anvil and Serving consumer seams | Stage 2 | Consumer attribution, ACL and stale-result tests |
| 4 | Qualification and adversarial review | Stages 1–3 | Frozen expected labels, raw synthetic results, failures |
| 5 | Source merge and article notes | Stage 4 | CI, independent reviews, confirmed merge |

Use the existing CLI, config, authorization, and evidence helpers. A separate
Jev daemon, generic provider framework, background scheduler, database schema,
or new dependency is not needed. CLI integration is useful on its own, but it
must not be described as a shipped Workbench UI or voice-pipeline integration.
Report actual coverage per consumer at handoff.

## Research basis and limits

Official sources inspected 2026-09-20:

- [Introduction](https://docs.typesafe.ai/introduction),
  [quickstart](https://docs.typesafe.ai/introduction/quickstart),
  [System One](https://docs.typesafe.ai/concepts/system-one), and
  [building with System One](https://docs.typesafe.ai/concepts/how-to-build-with-system-one).
- [API](https://docs.typesafe.ai/api), [models](https://docs.typesafe.ai/models),
  [patterns](https://docs.typesafe.ai/patterns),
  [confidence](https://docs.typesafe.ai/confidence), and
  [model limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13).
- [Skill suggestion](https://docs.typesafe.ai/cookbooks/skill_suggestion),
  [citation checking](https://docs.typesafe.ai/cookbooks/citation_check), and
  [legal/data terms](https://docs.typesafe.ai/legal).

Jev returns typed judgments, not generated prose or code. Choice and Score
include distribution-derived confidence; Noul is a truth probability, not a
separate confidence estimate. Questions in a request are independent. Explicit
state paths belong in question instructions; question IDs alone do not convey
meaning to the model. Arithmetic, dates, authenticity, and permissions stay
deterministic. Pin `jev-1.13.0`, not a moving latest alias.

Exploratory research used three synthetic requests, fourteen questions, and
2,712 input tokens. Observed request times were 172–229 ms. One initial
evidence fixture was ambiguous; a clarified follow-up matched four expected
labels. These are connectivity/examples, not held-out quality, calibrated
confidence, prompt-injection security, or production latency evidence.
Retain that failed/ambiguous case in subsequent evaluation rather than
reporting only the successful follow-up.

The published input price observed during research was $0.042 per million
tokens; the resulting approximately $0.000114 is an estimate, not a billing
receipt. Recheck pricing for later articles. Standard-service zero retention
is not established; enterprise zero-data-retention terms must not be assumed.

## Article notes, not publication

Capture the problem, code-level authority boundaries, why each integration was
chosen, actual before/after measurements, retained failures, rejected extra
use cases, and future-system implications. Separate mocked tests, live
synthetic measurements, source merge, installation, and live enablement.
Prepare notes for the author's `sekoudoumbouya` repository only; no draft
article or publication is requested. Never copy credentials, private logs,
personal infrastructure, or task content into public notes.
