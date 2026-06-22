#!/bin/sh
# Anvil one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/fakoli/anvil/main/scripts/install.sh | sh -s -- <harness>
#
# Installs the `anvil` CLI + `anvil-mcp` server from PyPI with `uv tool`, then
# wires anvil into <harness> via `anvil install <harness> --write` (idempotent;
# reversible with `anvil install <harness> --rollback`).
#
# Prerequisite: uv (https://docs.astral.sh/uv/). No checkout needed.
set -eu

PACKAGE="anvil-state"

print_usage() {
    echo "Usage: install.sh <harness>"
    echo "  harness: codex | cursor | windsurf | cline | vscode | zed | copilot |"
    echo "           gemini | opencode | roo | amp | continue | goose | openhands |"
    echo "           openclaw | claude-code"
}

# A usage *error* (bad/missing args): help to stderr, non-zero exit. An explicit
# `-h`/`--help` request instead prints to stdout and exits 0 (see the arg loop).
usage() { print_usage >&2; exit 2; }

HARNESS=""
for arg in "$@"; do
    case "$arg" in
        -h|--help) print_usage; exit 0 ;;  # explicit help → stdout, success
        -*) echo "unknown option: $arg" >&2; usage ;;
        *) if [ -z "$HARNESS" ]; then HARNESS="$arg"; else usage; fi ;;
    esac
done
[ -n "$HARNESS" ] || usage

# Claude Code installs anvil as a plugin (skills + hooks + MCP), NOT via
# `anvil install` — which this script would call and which rejects 'claude-code'.
# Redirect to the real path instead of failing with a confusing "unknown harness".
if [ "$HARNESS" = "claude-code" ] || [ "$HARNESS" = "claude" ]; then
    echo "Claude Code installs anvil as a plugin, not through this script. Run inside Claude Code:"
    echo "    /plugin marketplace add fakoli/anvil"
    echo "    /plugin install anvil@anvil"
    exit 0
fi

# uv is the only prerequisite (it fetches the published package + its deps).
if ! command -v uv >/dev/null 2>&1; then
    echo "anvil needs 'uv' — install from https://docs.astral.sh/uv/ then re-run." >&2
    exit 1
fi

# Install (or upgrade) the anvil CLI + MCP server from PyPI. `uv tool install`
# places `anvil` and `anvil-mcp` in uv's tool-bin dir; -U keeps re-runs current.
echo "Installing $PACKAGE (anvil CLI + anvil-mcp) via uv tool ..." >&2
uv tool install --upgrade "$PACKAGE"

# Resolve the launcher. uv's tool-bin dir (usually ~/.local/bin) MUST be on PATH
# so the harness can later launch `anvil-mcp` from the MCP config we write — warn
# if it isn't, but still complete the wiring via the absolute path.
ANVIL="anvil"
if ! command -v anvil >/dev/null 2>&1; then
    BIN_DIR="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
    ANVIL="$BIN_DIR/anvil"
    echo "note: $BIN_DIR is not on your PATH. Add it so your harness can launch" >&2
    echo "      the MCP server (anvil-mcp), e.g.:" >&2
    echo "        export PATH=\"$BIN_DIR:\$PATH\"" >&2
fi

# Wire anvil into the target harness. For codex/openclaw this drives the harness's
# own CLI; for the rest it merges the MCP config and splices anvil's AGENTS.md as a
# removable block — all idempotent and reversible with `--rollback`.
echo "Installing anvil for '$HARNESS' ..." >&2
exec "$ANVIL" install "$HARNESS" --write
