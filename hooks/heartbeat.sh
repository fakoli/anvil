#!/usr/bin/env bash
# heartbeat.sh — PostToolUse hook for anvil (B41).
#
# Renews the actor's active claim lease(s) on tool activity so a lazy lease stays
# fresh while real work is happening. Purely side-effecting and NON-BLOCKING.
#
# Rules (anvil hook contract — docs/design.md "Why hooks are non-blocking"):
# no set -e, no piped grep, always exit 0, complete in < 200ms.
#
# NOTE: mirrors the existing wrappers' LOCAL .anvil fast-path, so it is inert
# under the default HOME-workspace layout until B44 centralizes a
# home-workspace-aware fast-path across all hooks.

STATE_DIR=".anvil"

# Fast-path: no local project state, nothing to renew.
if [ ! -d "$STATE_DIR" ]; then
  exit 0
fi

# Without the plugin root we cannot locate the CLI safely (avoid resolving to a
# stray /bin/anvil); skip rather than risk an unrelated binary.
if [ -z "${CLAUDE_PLUGIN_ROOT:-}" ]; then
  exit 0
fi
CLI="${CLAUDE_PLUGIN_ROOT}/bin/anvil"
if [ ! -x "$CLI" ]; then
  exit 0
fi

# Read stdin + extract the session id as the actor proxy — mirrors the sibling
# hooks (record-file-change/capture-evidence), since claims are held under the
# harness-supplied identity, not the literal 'agent'.
PAYLOAD=""
if [ -t 0 ]; then
  PAYLOAD="{}"
else
  PAYLOAD=$(cat)
fi
ACTOR=""
if command -v python3 >/dev/null 2>&1; then
  ACTOR=$(HOOK_PAYLOAD="$PAYLOAD" python3 - <<'PYEOF' 2>/dev/null
import os, json
raw = os.environ.get('HOOK_PAYLOAD', '')
try:
    d = json.loads(raw) if raw.strip() else {}
except Exception:
    d = {}
print(str(d.get('session_id') or '') if isinstance(d, dict) else '')
PYEOF
)
fi

if [ -n "$ACTOR" ]; then
  "$CLI" hook heartbeat --actor "$ACTOR" >/dev/null 2>&1 || true
else
  "$CLI" hook heartbeat >/dev/null 2>&1 || true
fi

exit 0
