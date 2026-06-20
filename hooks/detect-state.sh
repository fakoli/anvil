#!/usr/bin/env bash

# detect-state.sh — SessionStart hook for anvil.
# Prints a one-line project state summary to the session context.
# Rules: no set -e, no piped grep, always exit 0, complete in < 1 second.
#
# WORKSPACE-AWARE (B44): does NOT gate on a local ./.anvil dir — under the default
# HOME-workspace layout state lives in ~/.anvil/workspaces/<key>/, so the cwd has
# no .anvil. The CLI's workspace-aware `status --hook-format` is the source of
# truth (exit 0; prints "uninitialized" when there is no project).

# Language detection (cwd-relative — correct: it is about the repo you are in).
DETECTED_LANG="unknown"
[ -f "Cargo.toml" ] && DETECTED_LANG="Rust"
[ -f "pyproject.toml" ] && DETECTED_LANG="Python"
[ -f "setup.py" ] && DETECTED_LANG="Python"
[ -f "package.json" ] && DETECTED_LANG="TypeScript"
[ -f "tsconfig.json" ] && DETECTED_LANG="TypeScript"

# Legacy in-repo state present? (drives the migrate nudge below.)
LEGACY=""
if [ -d ".anvil" ] || [ -d "bin/.anvil" ]; then
  LEGACY="yes"
fi

CLI="${CLAUDE_PLUGIN_ROOT}/bin/anvil"
if [ ! -x "$CLI" ]; then
  echo "[anvil] Language: $DETECTED_LANG | CLI not available — install anvil bin to enable status"
  exit 0
fi

# Expected: "active-claims:<N> ready-tasks:<N> blockers:<N> prd-status:<STATUS>"
# or the literal "uninitialized" when there is no project for this dir.
STATUS_OUTPUT=$("$CLI" status --hook-format 2>&1)
STATUS_EXIT=$?

if [ "$STATUS_EXIT" -eq 0 ] && [ -n "$STATUS_OUTPUT" ] && [ "$STATUS_OUTPUT" != "uninitialized" ]; then
  echo "[anvil] Language: $DETECTED_LANG | $STATUS_OUTPUT"
  exit 0
fi

if [ "$STATUS_OUTPUT" = "uninitialized" ]; then
  if [ -n "$LEGACY" ]; then
    echo "[anvil] Language: $DETECTED_LANG | legacy in-repo .anvil found — run \`anvil migrate-workspace\` to move it into the home workspace"
  else
    echo "[anvil] not initialized in this project — run \`anvil init\` to start"
  fi
  exit 0
fi

# CLI present but status errored/empty (DB locked, bad env, etc.).
REASON=$(printf '%s' "$STATUS_OUTPUT" | head -1)
[ -z "$REASON" ] && REASON="status check returned exit $STATUS_EXIT"
echo "[anvil] Language: $DETECTED_LANG | status check unavailable: $REASON"
exit 0
