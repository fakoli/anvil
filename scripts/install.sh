#!/bin/sh
# Anvil one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/fakoli/anvil/main/scripts/install.sh | sh -s -- <harness> [--path]
#
# Provisions a local anvil checkout (if one isn't already present, kept current on
# re-runs), then wires anvil into <harness> via `anvil install <harness> --write`.
# Pass --path to also symlink `anvil` into ~/.local/bin (override: ANVIL_BIN_DIR).
# Run from inside an anvil checkout and it uses that checkout directly.
#
# Prerequisite: `uv` (https://docs.astral.sh/uv/) — the wrappers self-sync deps.
set -eu

REPO_URL="https://github.com/fakoli/anvil.git"
CACHE_DIR="${ANVIL_SRC:-$HOME/.anvil-src}"
PATH_DIR="${ANVIL_BIN_DIR:-$HOME/.local/bin}"

print_usage() {
    echo "Usage: install.sh <harness> [--path]"
    echo "  harness: codex | cursor | windsurf | cline | vscode | zed | copilot |"
    echo "           gemini | opencode | roo | amp | continue | goose | openhands |"
    echo "           openclaw | claude-code"
    echo "  --path:  also symlink 'anvil' into $PATH_DIR so you can run it"
    echo "           globally (opt-in, idempotent, never clobbers an existing anvil)"
}

# A usage *error* (bad/missing args): help to stderr, non-zero exit. An explicit
# `-h`/`--help` request instead prints to stdout and exits 0 (see the arg loop).
usage() { print_usage >&2; exit 2; }

# Opt-in: symlink the checkout's launcher into a PATH dir so `anvil` works
# globally. Idempotent (re-linking the same target is a no-op) and never clobbers
# a pre-existing `anvil` the user put there themselves.
link_into_path() {
    src="$1/bin/anvil"
    dest="$PATH_DIR/anvil"
    mkdir -p "$PATH_DIR"
    if [ -L "$dest" ] && [ "$(readlink "$dest" 2>/dev/null)" = "$src" ]; then
        echo "anvil already linked at $dest" >&2
    elif [ -e "$dest" ] || [ -L "$dest" ]; then
        echo "warning: $dest already exists — leaving it untouched." >&2
        echo "         remove it and re-run with --path to (re)link this checkout." >&2
        return 0
    else
        ln -s "$src" "$dest"
        echo "Linked anvil -> $dest" >&2
    fi
    case ":$PATH:" in
        *":$PATH_DIR:"*) : ;;
        *) echo "note: $PATH_DIR isn't on your PATH yet. Add it, e.g.:" >&2
           echo "        export PATH=\"$PATH_DIR:\$PATH\"" >&2 ;;
    esac
}

HARNESS=""
ADD_TO_PATH=""
for arg in "$@"; do
    case "$arg" in
        --path) ADD_TO_PATH=1 ;;
        -h|--help) print_usage; exit 0 ;;  # explicit help → stdout, success
        -*) echo "unknown option: $arg" >&2; usage ;;
        *) if [ -z "$HARNESS" ]; then HARNESS="$arg"; else usage; fi ;;
    esac
done
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

# 3. Optionally put `anvil` on PATH (before the install so its exit code is the
#    one we exec into below).
[ -z "$ADD_TO_PATH" ] || link_into_path "$ANVIL_DIR"

# 4. Wire it into the target harness. For codex/openclaw this drives the harness's
#    own CLI (it writes its own config); for the rest it merges the MCP config and
#    splices anvil's AGENTS.md as a removable block — all idempotent and reversible
#    with `anvil install <harness> --rollback`.
echo "Installing anvil for '$HARNESS' (from $ANVIL_DIR) ..." >&2
exec "$ANVIL_DIR/bin/anvil" install "$HARNESS" --write
