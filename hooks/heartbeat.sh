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

# B47 — do NOT pass a per-session identity here. The heartbeat MUST resolve the
# same actor that `anvil claim` did, or it renews zero leases (the claim is held
# under a different actor) and the lease silently expires mid-work. Passing no
# --actor lets the CLI's single resolver pick the same identity the claim used
# (resolve_actor: $ANVIL_ACTOR > $ANVIL_GATE_ACTOR > $USER > per-runner
# fingerprint > "agent"). Set $ANVIL_ACTOR in the loop env to pin it for a fleet.
# Drain a PIPED stdin (the harness may send a JSON payload we no longer use) so
# the writer never blocks — but never read from a TTY, which would hang.
if [ ! -t 0 ]; then
  cat >/dev/null 2>&1 || true
fi
"$CLI" hook heartbeat >/dev/null 2>&1 || true

exit 0
