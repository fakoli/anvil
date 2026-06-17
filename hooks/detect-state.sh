#!/usr/bin/env bash

# detect-state.sh — SessionStart hook for fakoli-state
# Prints a one-line project state summary to the Claude Code session context.
# Rules: no set -e, no piped grep, always exit 0, complete in < 1 second.

STATE_DIR=".fakoli-state"

if [ ! -d "$STATE_DIR" ]; then
  echo "[fakoli-state] not initialized in this project — run \`fakoli-state init\` to start"
  exit 0
fi

# Detect language (same pattern as fakoli-flow/hooks/detect-context.sh)
DETECTED_LANG="unknown"
[ -f "Cargo.toml" ] && DETECTED_LANG="Rust"
[ -f "pyproject.toml" ] && DETECTED_LANG="Python"
[ -f "setup.py" ] && DETECTED_LANG="Python"
[ -f "package.json" ] && DETECTED_LANG="TypeScript"
[ -f "tsconfig.json" ] && DETECTED_LANG="TypeScript"

# Attempt to get live status from the CLI.
# Expected output format from `fakoli-state status --hook-format`:
#   active-claims:<N> ready-tasks:<N> blockers:<N> prd-status:<STATUS>
# Example: active-claims:2 ready-tasks:7 blockers:0 prd-status:approved
CLI="${CLAUDE_PLUGIN_ROOT}/bin/fakoli-state"

if [ -x "$CLI" ]; then
  STATUS_OUTPUT=$("$CLI" status --hook-format 2>&1)
  STATUS_EXIT=$?
  if [ "$STATUS_EXIT" -eq 0 ] && [ -n "$STATUS_OUTPUT" ]; then
    echo "[fakoli-state] Language: $DETECTED_LANG | $STATUS_OUTPUT"
    exit 0
  else
    # CLI present but returned non-zero or empty (Wave 2 not yet wired, DB locked, etc.)
    REASON=$(printf '%s' "$STATUS_OUTPUT" | head -1)
    if [ -z "$REASON" ]; then
      REASON="status check returned exit $STATUS_EXIT"
    fi
    echo "[fakoli-state] Language: $DETECTED_LANG | state present, status check unavailable: $REASON"
    exit 0
  fi
fi

# CLI not executable or not yet installed (Wave 2 still in progress)
echo "[fakoli-state] Language: $DETECTED_LANG | state present, CLI not available — install fakoli-state bin to enable status"
exit 0
