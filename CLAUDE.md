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
- **Bump the version with `scripts/release.py` — never by hand.** A version bump
  touches many files in lockstep and is easy to under-do; the helper does all of
  it from one command and works the same under Claude, Codex, or a bare shell
  (stdlib only, no `uv` needed):

  ```bash
  python3 scripts/release.py minor --dry-run   # preview every planned edit
  python3 scripts/release.py minor             # apply + auto-run the sync tests
  ```

  It rewrites the three core files (`tests/test_version_sync.py` enforces them:
  `.claude-plugin/plugin.json`, `bin/pyproject.toml`, `bin/src/anvil/__init__.py`)
  **and** the per-harness packaging manifests
  (`tests/test_install_manifests.py` pins them to `anvil.__version__`:
  `packaging/codex/.codex-plugin/plugin.json`,
  `packaging/codex/.agents/plugins/marketplace.json`,
  `packaging/gemini/gemini-extension.json`, and the OpenClaw manifests
  `packaging/openclaw/plugin/openclaw.plugin.json` +
  `.../package.json`), promotes the `CHANGELOG.md` `## [Unreleased]` block, and
  refreshes the **user-facing version docs** that no test guards (the `README.md`
  badge + "Beta — vX.Y.Z" lines and the `anvil X.Y.Z (schema N)` examples in
  `docs/how-to/getting-started.md`, `docs/cli-reference.md`,
  `docs/architecture.md` — the schema number is re-synced to
  `state/schema.py`'s `SCHEMA_VERSION`). It leaves historical snapshots
  (`docs/archive/**`, `benchmarks/RESULTS.md`, old `CHANGELOG.md` entries)
  untouched, and the root `marketplace.json` inherits its version from
  `plugin.json`. The pinned set lives in the script (and `tests/test_release_helper.py`
  guards it) — when a manifest is added there, add it in both places.
- **Bump only when you publish, not per commit.** Claude Code pins plugin pickups
  to the `version` string: an unchanged version means `/plugin marketplace update`
  is a no-op and users keep running stale code, however many commits landed. So
  bump the **patch** each time you *publish* — i.e. a backlog list is done and you
  want folks to pick it up (`0.0.8 → 0.0.9`); reserve **minor/major** for bigger
  releases. Day-to-day commits between publishes change no version file. To
  publish, promote the `CHANGELOG.md` `## [Unreleased]` block to the new version.
- **One PR per item.** Merge only after CI is green **and** the automated
  reviewer — **GitHub Copilot** — has landed and been addressed (fix real
  findings; reply on the ones you defer and record them in
  `docs/tech-debt-backlog.md`). A PR can sit `BLOCKED` until the review posts.
  **Greptile is disabled** (turned off 2026-06-20 to avoid fees) — do **not** wait
  for it or post `@greptile review`; Copilot is the only bot gate now.
  **Re-reviews are not automatic** — a push does not re-trigger Copilot. A finding
  fixed with a small, direct change needs no re-review. For a **substantial or
  multi-line fix**, re-request Copilot's review on the PR (re-request review /
  `@copilot`) so it re-checks the changed code.

## Map

- Engine: `bin/src/anvil/` — CLI in `cli/`, MCP server in `mcp_server.py`.
  Entrypoints: `bin/anvil`, `bin/anvil-mcp`.
- `AGENTS.md` is the **end-user usage doc** that `anvil install <harness>` ships
  to other harnesses. Keep it usage-focused — it is not the place for dev rules.
- Planning: `docs/roadmap.md` (live) · `docs/backlog/anvil-backlog.md` (product
  backlog) · `docs/tech-debt-backlog.md` (deferred review findings).
- Cross-harness distribution: `bin/src/anvil/cli/install.py` + `packaging/`.
