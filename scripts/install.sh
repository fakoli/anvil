#!/bin/sh
# Anvil one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/fakoli/anvil/main/scripts/install.sh | sh -s -- <harness>
#
# Provisions a local anvil checkout (if one isn't already present), then wires
# its MCP server + AGENTS.md into <harness> via `anvil install <harness> --write`.
# Run from inside an anvil checkout and it uses that checkout directly.
#
# Prerequisite: `uv` (https://docs.astral.sh/uv/) — the wrappers self-sync deps.
set -eu

REPO_URL="https://github.com/fakoli/anvil.git"
CACHE_DIR="${ANVIL_SRC:-$HOME/.anvil-src}"

usage() {
    echo "Usage: install.sh <harness>" >&2
    echo "  harness: codex | cursor | windsurf | cline | vscode | zed | copilot |" >&2
    echo "           gemini | opencode | roo | amp | continue | goose | openhands |" >&2
    echo "           openclaw | claude-code" >&2
    exit 2
}

HARNESS="${1:-}"
[ -n "$HARNESS" ] || usage

# 1. uv is the only prerequisite.
if ! command -v uv >/dev/null 2>&1; then
    echo "anvil needs 'uv' — install from https://docs.astral.sh/uv/ then re-run." >&2
    exit 1
fi

# 2. Find an anvil checkout: the current repo if we're in one, else the cache
#    (clone on first run, fast-forward on later runs).
if [ -x "./bin/anvil" ] && [ -f "./.claude-plugin/plugin.json" ]; then
    ANVIL_DIR="$(pwd)"
elif [ -x "$CACHE_DIR/bin/anvil" ]; then
    ANVIL_DIR="$CACHE_DIR"
    git -C "$ANVIL_DIR" pull --ff-only --quiet \
        || echo "warning: couldn't update cached anvil at $ANVIL_DIR — using it as-is." >&2
else
    echo "Cloning anvil into $CACHE_DIR ..." >&2
    git clone --depth 1 "$REPO_URL" "$CACHE_DIR"
    ANVIL_DIR="$CACHE_DIR"
fi

# 3. Wire it into the target harness (idempotent JSON/TOML merge + AGENTS.md).
echo "Installing anvil for '$HARNESS' (from $ANVIL_DIR) ..." >&2
exec "$ANVIL_DIR/bin/anvil" install "$HARNESS" --write
