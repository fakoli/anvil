#!/usr/bin/env bash

# check-claim.sh — PreToolUse hook for anvil (Phase 4)
# Fires before Edit, Write, or NotebookEdit tool calls.
# Warns (non-blocking) when there are active claims and the agent modifies a file,
# so the agent can verify the file is in scope before proceeding.
#
# Rules: no set -e, no piped grep, always exit 0, complete in < 200ms.
# Performance: only shells out to the CLI when .anvil/ exists; otherwise fast-paths.
#
# Claude Code hook payload arrives on stdin as JSON.
# Relevant fields (Phase 4 minimal extraction):
#   .tool_input.path   — file being modified (Edit, Write)
#   .tool_name         — e.g. "Edit", "Write", "NotebookEdit"
#   .session_id        — session identifier (used as actor proxy when no claim actor available)

# Fast-path: no anvil state anywhere, nothing to check. anvil's DEFAULT layout
# is the HOME workspace (~/.anvil/workspaces/<key>/); the in-repo .anvil/ (or
# bin/.anvil/) is opt-in only. Fast-path out only when NONE of those exist.
if [ ! -d ".anvil" ] && [ ! -d "bin/.anvil" ] && [ ! -d "${HOME:-/nonexistent}/.anvil/workspaces" ]; then
  exit 0
fi

# Read and parse stdin payload (best-effort; failures are silent).
PAYLOAD=""
if [ -t 0 ]; then
  # stdin is a terminal — no payload (e.g. manual smoke-test invocation)
  PAYLOAD="{}"
else
  PAYLOAD=$(cat)
fi

# Extract the file path and actor in a single python3 call.
# FILE_PATH: .tool_input.path (Edit/Write) or .tool_input.notebook_path (NotebookEdit).
# ACTOR: .session_id (used as actor proxy).
FILE_PATH=""
ACTOR=""
if command -v python3 >/dev/null 2>&1; then
  EXTRACTED=$(printf '%s' "$PAYLOAD" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    ti = d.get('tool_input', {})
    print(ti.get('path') or ti.get('notebook_path') or '')
    print(d.get('session_id') or '')
except Exception:
    print('')
    print('')
" 2>/dev/null)
  FILE_PATH=$(printf '%s\n' "$EXTRACTED" | sed -n '1p')
  ACTOR=$(printf '%s\n' "$EXTRACTED" | sed -n '2p')
fi

# Normalise: if FILE_PATH is empty, we can't check anything — exit silently.
if [ -z "$FILE_PATH" ]; then
  exit 0
fi

# If the file is outside the project tree (absolute path not under cwd), skip silently.
# We detect "outside" by checking whether FILE_PATH starts with / AND does not start
# with the resolved cwd.  Relative paths are always considered in-project.
if [ "${FILE_PATH#/}" != "$FILE_PATH" ]; then
  # FILE_PATH is absolute
  CWD="$(pwd)"
  if [ "${FILE_PATH#"$CWD/"}" = "$FILE_PATH" ] && [ "$FILE_PATH" != "$CWD" ]; then
    # Absolute path that does not reside under cwd — skip silently.
    exit 0
  fi
fi

# Shell out to the per-file CLI hook (CL-1).  The Phase 5 CLI subcommand
# `anvil hook check-claim --file <PATH> --actor <ACTOR>` performs the
# real per-file scope check against `expected_files` on every active claim
# and only warns when FILE is in another agent's claim — superseding the
# Phase 4 coarse "any active claim → warn" approach.
CLI="${CLAUDE_PLUGIN_ROOT:-/nonexistent}/bin/anvil"

if [ ! -x "$CLI" ]; then
  # CLI not available — degrade silently; never break the session.
  exit 0
fi

ACTOR_FOR_CLI="${ACTOR:-unknown}"
# The CLI hook prints any per-file warnings to stderr and always exits 0; we
# let stderr flow through to the user's terminal unchanged, discard stdout
# (the subcommand has none in normal operation), and ignore any non-zero
# exit so the hook never blocks the tool.
"$CLI" hook check-claim --file "$FILE_PATH" --actor "$ACTOR_FOR_CLI" >/dev/null || true

exit 0
