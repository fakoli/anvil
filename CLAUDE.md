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
- **A version bump touches three core files in lockstep** (`tests/test_version_sync.py`
  enforces it): `.claude-plugin/plugin.json`, `bin/pyproject.toml`,
  `bin/src/anvil/__init__.py` — **and** the per-harness packaging manifests that
  `tests/test_install_manifests.py` pins to `anvil.__version__`:
  `packaging/codex/.codex-plugin/plugin.json`,
  `packaging/codex/.agents/plugins/marketplace.json`, and
  `packaging/gemini/gemini-extension.json`. Add a `CHANGELOG.md` entry. The root
  `marketplace.json` omits `version`, so it inherits from `plugin.json` — nothing
  to bump there.
- **Bump only when you publish, not per commit.** Claude Code pins plugin pickups
  to the `version` string: an unchanged version means `/plugin marketplace update`
  is a no-op and users keep running stale code, however many commits landed. So
  bump the **patch** each time you *publish* — i.e. a backlog list is done and you
  want folks to pick it up (`0.0.8 → 0.0.9`); reserve **minor/major** for bigger
  releases. Day-to-day commits between publishes change no version file. To
  publish, promote the `CHANGELOG.md` `## [Unreleased]` block to the new version.
- **One PR per item.** Merge only after CI is green **and** both automated
  reviewers — **Greptile** and **GitHub Copilot** — have landed and been addressed
  (fix real findings; reply on the ones you defer and record them in
  `docs/tech-debt-backlog.md`). Wait for both before merging; a PR can sit
  `BLOCKED` until the second review posts. **Re-reviews are not automatic** — a
  push does not re-trigger Greptile or Copilot. A finding fixed with a small,
  direct change needs no re-review. For a **substantial or multi-line fix**,
  request a re-review so the bots re-check the changed code: **Greptile** via an
  `@greptile review` comment; **Copilot** by re-requesting its review on the PR
  (re-request review / `@copilot`).

## Map

- Engine: `bin/src/anvil/` — CLI in `cli/`, MCP server in `mcp_server.py`.
  Entrypoints: `bin/anvil`, `bin/anvil-mcp`.
- `AGENTS.md` is the **end-user usage doc** that `anvil install <harness>` ships
  to other harnesses. Keep it usage-focused — it is not the place for dev rules.
- Planning: `docs/roadmap.md` (live) · `docs/backlog/anvil-backlog.md` (product
  backlog) · `docs/tech-debt-backlog.md` (deferred review findings).
- Cross-harness distribution: `bin/src/anvil/cli/install.py` + `packaging/`.
