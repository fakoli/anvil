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

# Extract fields and build the complete JSON event line in a single python3 pass.
# json.dumps handles all escaping (backslashes, quotes, newlines, unicode) so the
# shell never hand-builds JSON and backslash payloads cannot corrupt the JSONL log.
EVENT_LINE=""
FILE_PATH=""
TOOL_NAME=""
ACTOR=""
if command -v python3 >/dev/null 2>&1; then
  # Single python3 pass: extract fields AND build the complete JSON event line.
  # Emits shell-sourceable assignments via shlex.quote() so embedded newlines,
  # backslashes, quotes, and unicode in any field cannot corrupt the JSONL log
  # or break shell variable assignment.  Mirrors the pattern in capture-evidence.sh.
  ASSIGNMENTS=$(HOOK_PAYLOAD="$PAYLOAD" python3 - <<'PYEOF' 2>/dev/null
import os, json, datetime, shlex

def emit(name, value):
    print(f"{name}={shlex.quote(str(value))}")

try:
    raw  = os.environ.get('HOOK_PAYLOAD', '')
    d    = json.loads(raw) if raw.strip() else {}
    ti   = d.get('tool_input', {}) if isinstance(d, dict) else {}
    path = str(ti.get('path') or ti.get('notebook_path') or '')
    tool = str(d.get('tool_name') or 'unknown')
    actor = str(d.get('session_id') or 'unknown')
    ts   = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    if path:
        record = {
            'action':      'file_changed',
            'entity_type': 'file',
            'entity_id':   path,
            'actor':       actor,
            'tool':        tool,
            'timestamp':   ts,
            'source':      'hook',
        }
        emit('FILE_PATH',  path)
        emit('TOOL_NAME',  tool)
        emit('ACTOR',      actor)
        emit('EVENT_LINE', json.dumps(record))
    else:
        emit('FILE_PATH',  '')
        emit('TOOL_NAME',  '')
        emit('ACTOR',      '')
        emit('EVENT_LINE', '')
except Exception:
    for name in ('FILE_PATH', 'TOOL_NAME', 'ACTOR', 'EVENT_LINE'):
        print(f"{name}=''")
PYEOF
  )
  eval "$ASSIGNMENTS"
fi

# If we have no file path, there's nothing useful to record.
if [ -z "$FILE_PATH" ]; then
  exit 0
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

# Direct-append fallback: EVENT_LINE was built by json.dumps inside the python3
# pass above — backslashes, quotes, newlines, and unicode are all properly escaped.
# No hand-rolled JSON interpolation occurs here.

# Append atomically-ish: write to a temp file, then append.
# True atomic append on HFS+/APFS requires flock; for Phase 4 the simple append
# is acceptable — race conditions between concurrent hooks are exceedingly rare.
printf '%s\n' "$EVENT_LINE" >> "$EVENTS_FILE" 2>/dev/null

exit 0
