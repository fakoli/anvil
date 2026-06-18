#!/usr/bin/env bash

# record-file-change.sh — PostToolUse hook for anvil (Phase 4)
# Fires after Edit, Write, or NotebookEdit tool calls.
# Appends a file_changed event to .anvil/events.jsonl.
#
# Phase 4 strategy: prefer the CLI subcommand when available; fall back to a
# direct JSONL append in shell.  The direct-append path is intentionally simple
# — it is a well-formed JSON line that the replay engine can process.
# Wave 2 (guido) must implement: anvil hook record-file-change --file <PATH> --tool <TOOL> --actor <ACTOR>
#
# Rules: no set -e, no piped grep, always exit 0, complete in < 200ms.

STATE_DIR=".anvil"
EVENTS_FILE="${STATE_DIR}/events.jsonl"

# Fast-path: no project state, nothing to record.
if [ ! -d "$STATE_DIR" ]; then
  exit 0
fi

# Read and parse stdin payload (best-effort; failures are silent).
PAYLOAD=""
if [ -t 0 ]; then
  PAYLOAD="{}"
else
  PAYLOAD=$(cat)
fi

# Extract fields from the payload.
FILE_PATH=""
TOOL_NAME=""
ACTOR=""
if command -v python3 >/dev/null 2>&1; then
  EXTRACTED=$(printf '%s' "$PAYLOAD" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    ti = d.get('tool_input', {})
    path = ti.get('path') or ti.get('notebook_path') or ''
    tool = d.get('tool_name') or ''
    actor = d.get('session_id') or ''
    # Sanitise: strip newlines and quotes so they can't corrupt the JSONL line.
    def clean(s):
        return str(s).replace('\\n', ' ').replace('\\r', '').replace('\"', '')
    print(clean(path))
    print(clean(tool))
    print(clean(actor))
except Exception:
    print('')
    print('')
    print('')
" 2>/dev/null)
  # Read three lines from EXTRACTED.
  FILE_PATH=$(printf '%s\n' "$EXTRACTED" | sed -n '1p')
  TOOL_NAME=$(printf '%s\n' "$EXTRACTED" | sed -n '2p')
  ACTOR=$(printf '%s\n' "$EXTRACTED" | sed -n '3p')
fi

# If we have no file path, there's nothing useful to record.
if [ -z "$FILE_PATH" ]; then
  exit 0
fi

# ISO-8601 timestamp (UTC).  Prefer date with nanoseconds; fall back gracefully.
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null)
if [ -z "$TIMESTAMP" ]; then
  TIMESTAMP="unknown"
fi

# Prefer the CLI subcommand (guido Wave 2 implements this).
# CLI invocation shape for guido:
#   anvil hook record-file-change --file <PATH> --tool <TOOL> --actor <ACTOR>
CLI="${CLAUDE_PLUGIN_ROOT}/bin/anvil"

if [ -x "$CLI" ]; then
  "$CLI" hook record-file-change \
    --file "$FILE_PATH" \
    --tool "${TOOL_NAME:-unknown}" \
    --actor "${ACTOR:-unknown}" \
    >/dev/null 2>&1
  CLI_EXIT=$?
  if [ "$CLI_EXIT" -eq 0 ]; then
    exit 0
  fi
  # CLI returned non-zero (subcommand not yet implemented, DB locked, etc.).
  # Fall through to direct-append path.
fi

# Direct-append fallback: write a well-formed JSONL event line.
# The replay engine reads events.jsonl; this format must match Event model conventions.
# Action: "file_changed", entity_type: "file", entity_id: <path>.
# Escape the three interpolated strings minimally (no quotes or backslashes expected
# from Claude Code tool payloads, but be safe).
_escape_json() {
  # Replace backslash first, then double-quote, then control characters.
  printf '%s' "$1" | python3 -c "
import sys, json
print(json.dumps(sys.stdin.read())[1:-1], end='')
" 2>/dev/null || printf '%s' "$1"
}

ESCAPED_PATH=$(_escape_json "$FILE_PATH")
ESCAPED_TOOL=$(_escape_json "${TOOL_NAME:-unknown}")
ESCAPED_ACTOR=$(_escape_json "${ACTOR:-unknown}")
ESCAPED_TS=$(_escape_json "$TIMESTAMP")

EVENT_LINE="{\"action\":\"file_changed\",\"entity_type\":\"file\",\"entity_id\":\"${ESCAPED_PATH}\",\"actor\":\"${ESCAPED_ACTOR}\",\"tool\":\"${ESCAPED_TOOL}\",\"timestamp\":\"${ESCAPED_TS}\",\"source\":\"hook\"}"

# Append atomically-ish: write to a temp file, then append.
# True atomic append on HFS+/APFS requires flock; for Phase 4 the simple append
# is acceptable — race conditions between concurrent hooks are exceedingly rare.
printf '%s\n' "$EVENT_LINE" >> "$EVENTS_FILE" 2>/dev/null

exit 0
