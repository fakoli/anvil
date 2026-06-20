#!/usr/bin/env bash
# heartbeat.sh — PostToolUse hook for anvil (B41).
#
# Renews the actor's active claim lease(s) on tool activity so a lazy lease stays
# fresh while real work is happening. Purely side-effecting and NON-BLOCKING.
#
# Rules (anvil hook contract — docs/design.md "Why hooks are non-blocking"):
# no set -e, no piped grep, always exit 0, complete in < 200ms.
#
# NOTE: like anvil's other bundled hooks, this fast-paths on a LOCAL .anvil and
# is therefore inert under the default HOME-workspace layout — B44 centralizes a
# home-workspace-aware fast-path across all hooks.

STATE_DIR=".anvil"

# Fast-path: no local project state, nothing to renew.
if [ ! -d "$STATE_DIR" ]; then
  exit 0
fi

CLI="${CLAUDE_PLUGIN_ROOT}/bin/anvil"
if [ -x "$CLI" ]; then
  # actor + cwd default inside the verb ($ANVIL_GATE_ACTOR/agent; current dir).
  "$CLI" hook heartbeat >/dev/null 2>&1 || true
fi

exit 0
