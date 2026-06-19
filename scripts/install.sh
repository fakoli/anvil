#!/bin/sh
# Anvil one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/fakoli/anvil/main/scripts/install.sh | sh -s -- <harness>
#
# Provisions a local anvil checkout (if one isn't already present, kept current on
# re-runs), then wires anvil into <harness> via `anvil install <harness> --write`.
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

# 2. Find an anvil checkout: the current repo if we're in one, else the cache.
#    On later runs we FORCE the cache to the latest main. The old fail-soft
#    `git pull` (warn-and-continue) let a shallow clone whose update failed keep
#    running STALE anvil, and stale anvil (pre native-install fix) corrupted
#    harness configs. Better to fail loud than to silently run an old, harmful one.
if [ -x "./bin/anvil" ] && [ -f "./.claude-plugin/plugin.json" ]; then
    ANVIL_DIR="$(pwd)"
elif [ -x "$CACHE_DIR/bin/anvil" ]; then
    ANVIL_DIR="$CACHE_DIR"
    if ! { git -C "$ANVIL_DIR" fetch --depth 1 --quiet "$REPO_URL" main \
           && git -C "$ANVIL_DIR" reset --hard --quiet FETCH_HEAD; }; then
        echo "error: couldn't update the cached anvil at $ANVIL_DIR." >&2
        echo "       check your network, or start clean:" >&2
        echo "         rm -rf \"$ANVIL_DIR\"  &&  re-run this installer." >&2
        exit 1
    fi
else
    echo "Cloning anvil into $CACHE_DIR ..." >&2
    git clone --depth 1 "$REPO_URL" "$CACHE_DIR"
    ANVIL_DIR="$CACHE_DIR"
fi

# 3. Wire it into the target harness. For codex/openclaw this drives the harness's
#    own CLI (it writes its own config); for the rest it merges the MCP config and
#    splices anvil's AGENTS.md as a removable block — all idempotent and reversible
#    with `anvil install <harness> --rollback`.
echo "Installing anvil for '$HARNESS' (from $ANVIL_DIR) ..." >&2
exec "$ANVIL_DIR/bin/anvil" install "$HARNESS" --write
