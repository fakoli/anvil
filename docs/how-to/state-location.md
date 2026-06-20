# Where anvil stores its state

anvil keeps everything for a project in one state directory — `state.db` (SQLite),
`config.yaml`, `events.jsonl`, `packets/`, the evidence buffer.

## Default: a shared workspace in your home directory

By default the state dir is a **per-project workspace under your home directory**,
keyed by the project's canonical git repo:

```
~/.anvil/workspaces/<key>/.anvil/
```

The key is the **main git worktree** (`git rev-parse --git-common-dir`), so **every
git worktree of a repo shares ONE state.db**. This is the fix for state getting
stranded inside an individual worktree: claim a task in one worktree, see it in
another, and removing a worktree never loses your project state.

`<key>` is the repo basename plus a short hash of its absolute path (e.g.
`app-1a2b3c4d`), so two different projects that share a basename never collide. (A
workspace created by an earlier anvil version under the **bare** basename is still
honored — anvil keeps resolving it so your existing state is never orphaned.)
Outside a git repo, the same basename+hash key is used on the directory's path.

## Resolution order

1. **`ANVIL_ROOT`** (env) — an explicit, literal override: state lives at
   `<ANVIL_ROOT>/.anvil/`. Used by CI, tests, and anyone who wants an exact path.
2. **`ANVIL_STATE_LAYOUT=local`** — opt out of the home workspace and keep state
   **in the repo** at `<cwd>/.anvil/` (the legacy behavior).
3. **Default** (`ANVIL_STATE_LAYOUT=workspace`) — the home workspace above.

The CLI and the MCP server resolve identically, so a host configures one project
and both surfaces agree.

## Finding / inspecting it

```bash
anvil status            # prints the resolved state dir + whether it's initialized
ls ~/.anvil/workspaces/ # all your project workspaces
```

## Migrating existing in-repo state

If you already have an in-repo `<repo>/.anvil/` (or `<repo>/bin/.anvil/`) from the
old layout, either:

- keep it: `export ANVIL_STATE_LAYOUT=local`, or
- migrate it into the home workspace with the built-in command:

```bash
anvil migrate-workspace        # dry run — reports source → target, writes nothing
anvil migrate-workspace --yes  # apply: copy the legacy .anvil/ into the workspace
```

It is **safe-first**: dry-run by default, never clobbers an existing home workspace
(that one stays authoritative), and **copies** (never moves) so the legacy dir
survives as a fallback. A SessionStart hook nudges you once if it detects legacy
in-repo state that hasn't been migrated.
