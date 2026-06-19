# CLAUDE.md — developing anvil

Golden rules for agents working **on** this repo. Keep this file tiny: if a fact
can be inferred from the code, it does not belong here.

## Golden rules

- **Python via `uv` only.** Tests run from `bin/`: `cd bin && uv run pytest -q`.
  Never bare `pip`/`python`.
- **`gh` needs the token unset:** `env -u GITHUB_TOKEN gh …`. The ambient PAT
  lacks repo permissions; the keyring login works once it is unset.
- **Never commit secrets or anything confidential.** This is a public repo — no
  `.env`, keys, tokens, customer data, or internal-only paths in commits.
- **A version bump touches three files in lockstep** (`tests/test_version_sync.py`
  enforces it): `.claude-plugin/plugin.json`, `bin/pyproject.toml`,
  `bin/src/anvil/__init__.py`. Add a `CHANGELOG.md` entry. `marketplace.json`
  omits `version`, so it inherits from `plugin.json` — nothing to bump there.
- **One PR per item.** Merge only after CI is green **and** the Greptile review
  has landed and been addressed (fix real findings; reply on the ones you defer
  and record them in `docs/tech-debt-backlog.md`).

## Map

- Engine: `bin/src/anvil/` — CLI in `cli/`, MCP server in `mcp_server.py`.
  Entrypoints: `bin/anvil`, `bin/anvil-mcp`.
- `AGENTS.md` is the **end-user usage doc** that `anvil install <harness>` ships
  to other harnesses. Keep it usage-focused — it is not the place for dev rules.
- Planning: `docs/roadmap.md` (live) · `docs/backlog/anvil-backlog.md` (product
  backlog) · `docs/tech-debt-backlog.md` (deferred review findings).
- Cross-harness distribution: `bin/src/anvil/cli/install.py` + `packaging/`.
