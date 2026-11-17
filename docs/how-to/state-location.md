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
`app-1a2b3c4d`), so two different projects that share a basename never collide.
Outside a git repo, the same basename+hash key is used on the directory's path.

A workspace created by an early anvil version under the **bare** basename
(`~/.anvil/workspaces/app/`) is honored only when it carries an owner marker
(`.anvil/workspace-owner.json`) naming this project's canonical root. A bare-name
directory is keyed on the name alone, so without that binding any checkout called
`app` would read and write another project's state. An unmarked one is never
adopted implicitly — see [Adopting a legacy bare-name workspace](#adopting-a-legacy-bare-name-workspace).

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

## Adopting a legacy bare-name workspace

If a project that used an early anvil release suddenly reports "not initialized",
its history is probably in a bare-name workspace that has no owner marker yet. The
"not initialized" message (CLI and SessionStart) names the directory when one
exists. Bind it to the project explicitly:

```bash
anvil migrate-workspace --adopt-legacy        # dry run — shows the workspace, its
                                              # last-written time, and the project root
anvil migrate-workspace --adopt-legacy --yes  # write the owner marker
```

Adoption writes one small file and copies or moves nothing. It refuses when the
workspace is already bound to a different project, when its marker is not a valid
regular file, or when this project already has a hashed-key workspace (anvil will
not pick between two real workspaces — move one aside first). Check the reported
last-written time before confirming: the directory name is the only thing that
ties an unmarked workspace to a project.
