#!/usr/bin/env bash

# capture-evidence.sh — PostToolUse hook for fakoli-state (Phase 5)
# Fires after every Bash tool call.
# Captures stdout/stderr/exit code of verification commands into a per-claim
# evidence buffer at .fakoli-state/.evidence-buffer/<claim-id>.json.
#
# Phase 5 fallback strategy: when the CLI subcommand is not yet available,
# write directly to the evidence buffer in shell. Wave 2 (guido) implements:
#   fakoli-state hook capture-evidence \
#     --command CMD --exit-code N \
#     --stdout-file PATH --stderr-file PATH \
#     --actor ACTOR
# Until then, the fallback path writes orphan.json (no active-claim lookup is
# attempted from shell; that lookup requires the CLI, which round-trips to
# state.db and cannot be done cheaply inside a < 200ms hook).
#
# Rules: no set -e, no piped grep, always exit 0, complete in < 200ms.
# Claude Code hook payload arrives on stdin as JSON.
# Relevant fields:
#   .tool_input.command    — the bash command that was run
#   .tool_response.stdout  — stdout of the command
#   .tool_response.stderr  — stderr of the command
#   .tool_response.exit_code — integer exit code
#   .session_id            — session identifier used as actor proxy

STATE_DIR=".fakoli-state"
EVIDENCE_DIR="${STATE_DIR}/.evidence-buffer"

# Fast-path: no project state, nothing to capture.
if [ ! -d "$STATE_DIR" ]; then
  exit 0
fi

# Read stdin payload (best-effort; failures are silent).
PAYLOAD=""
if [ -t 0 ]; then
  # stdin is a terminal — no payload (e.g. manual smoke-test invocation).
  PAYLOAD="{}"
else
  PAYLOAD=$(cat)
fi

# --- Extract fields via ONE python3 call ----------------------------------
# Previously this section spawned 7 python3 processes (1 bulk extraction +
# 6 separate parsers for individual field re-decoding). Greptile + Critic-1
# flagged the perf budget violation: on cold paths each python3 is 50-150ms,
# so the hook could hit the 5-second timeout mid-write leaving partial
# orphan.json. Now the extraction emits shell-sourceable assignments via
# shlex.quote() and shell `eval`s them in one round-trip.
if ! command -v python3 >/dev/null 2>&1; then
  exit 0
fi

# Payload via env var (not stdin): `python3 - <<'PYEOF'` reads its script
# from stdin (the heredoc), overriding any pipe redirect. Critic-4 caught
# this via a tightened hook-test assertion.
ASSIGNMENTS=$(HOOK_PAYLOAD="$PAYLOAD" python3 - <<'PYEOF' 2>/dev/null
import os, json, datetime, shlex

MAX_EXCERPT = 4000

def emit(name: str, value: str) -> None:
    # Use shlex.quote so embedded quotes/newlines/special chars survive the
    # subsequent shell `eval`. The output looks like: COMMAND='pytest -x'
    print(f"{name}={shlex.quote(value)}")

try:
    raw = os.environ.get('HOOK_PAYLOAD', '')
    d   = json.loads(raw) if raw.strip() else {}
    ti  = d.get('tool_input', {}) if isinstance(d, dict) else {}
    tr  = d.get('tool_response', {}) if isinstance(d, dict) else {}

    command    = ti.get('command') or ''
    exit_code  = tr.get('exit_code')
    stdout_raw = tr.get('stdout') or ''
    stderr_raw = tr.get('stderr') or ''
    actor      = d.get('session_id') or ''

    try:
        exit_code_int = int(exit_code) if exit_code is not None else 0
    except (ValueError, TypeError):
        exit_code_int = 0

    # tz-aware UTC (utcnow() was deprecated in 3.12, removed in 3.13).
    ts = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    emit('COMMAND',        command)
    emit('EXIT_CODE',      str(exit_code_int))
    emit('STDOUT_EXCERPT', stdout_raw[:MAX_EXCERPT])
    emit('STDERR_EXCERPT', stderr_raw[:MAX_EXCERPT])
    emit('ACTOR',          actor)
    emit('TIMESTAMP',      ts)
except Exception:
    # On any failure, emit empty assignments so the early-exit on $COMMAND
    # fires and the hook exits 0 cleanly.
    for name in ('COMMAND', 'EXIT_CODE', 'STDOUT_EXCERPT',
                 'STDERR_EXCERPT', 'ACTOR', 'TIMESTAMP'):
        print(f"{name}=''")
PYEOF
)

# Source the assignments. `eval` here is safe because shlex.quote() in the
# python emit() function ensures every value is single-quoted shell-safe.
eval "$ASSIGNMENTS"

# If we could not extract a command, nothing useful to do.
if [ -z "$COMMAND" ]; then
  exit 0
fi

# ---- Verification-command pattern matching --------------------------------
# Only capture evidence for known verification commands.
# This avoids filling the buffer with every incidental shell call.
# Patterns are matched as substrings anywhere in the command string.
# Phase 5 hardcoded set (Phase 6+ moves this to config):
#   pytest, ruff check, mypy, npm test, cargo test, bun test

IS_VERIFICATION=0

case "$COMMAND" in
  *pytest*)       IS_VERIFICATION=1 ;;
  *"ruff check"*) IS_VERIFICATION=1 ;;
  *mypy*)         IS_VERIFICATION=1 ;;
  *"npm test"*)   IS_VERIFICATION=1 ;;
  *"cargo test"*) IS_VERIFICATION=1 ;;
  *"bun test"*)   IS_VERIFICATION=1 ;;
esac

# Not a verification command — silent exit; do not pollute the buffer.
if [ "$IS_VERIFICATION" -eq 0 ]; then
  exit 0
fi

# ---- Try the CLI subcommand first (guido Wave 2 implements this) ----------
# CLI invocation shape for guido:
#   fakoli-state hook capture-evidence \
#     --command CMD --exit-code N \
#     --stdout-file PATH --stderr-file PATH \
#     --actor ACTOR
#
# The hook passes --stdout-file / --stderr-file (temp files) rather than
# inlining content because excerpts can be multi-line and avoid quoting issues.

CLI="${CLAUDE_PLUGIN_ROOT}/bin/fakoli-state"

if [ -x "$CLI" ]; then
  STDOUT_TMP=$(mktemp 2>/dev/null) || STDOUT_TMP=""
  STDERR_TMP=$(mktemp 2>/dev/null) || STDERR_TMP=""

  if [ -n "$STDOUT_TMP" ] && [ -n "$STDERR_TMP" ]; then
    printf '%s' "$STDOUT_EXCERPT" > "$STDOUT_TMP" 2>/dev/null
    printf '%s' "$STDERR_EXCERPT" > "$STDERR_TMP" 2>/dev/null

    "$CLI" hook capture-evidence \
      --command "$COMMAND" \
      --exit-code "${EXIT_CODE:-0}" \
      --stdout-file "$STDOUT_TMP" \
      --stderr-file "$STDERR_TMP" \
      --actor "${ACTOR:-unknown}" \
      >/dev/null 2>&1
    CLI_EXIT=$?

    rm -f "$STDOUT_TMP" "$STDERR_TMP" 2>/dev/null

    if [ "$CLI_EXIT" -eq 0 ]; then
      exit 0
    fi
    # CLI returned non-zero (subcommand not yet implemented, DB locked, etc.).
    # Fall through to the direct-write fallback.
  else
    rm -f "$STDOUT_TMP" "$STDERR_TMP" 2>/dev/null
    # mktemp failed — fall through to direct-write fallback.
  fi
fi

# ---- Direct-write fallback (Phase 5) -------------------------------------
# The CLI is absent or its capture-evidence subcommand is not yet implemented.
#
# Active-claim lookup from shell would require shelling out to the CLI again
# (or reading state.db directly, which we must never do).  For Phase 5 we
# always write to orphan.json so no evidence is lost.  The user can attach
# orphan evidence to a claim later via `fakoli-state submit --output-file`.
#
# When the CLI subcommand (guido Wave 2) is wired, it will:
#   1. Look up the active claim for --actor in state.db.
#   2. Write to .evidence-buffer/<claim-id>.json if a claim is found.
#   3. Fall back to orphan.json if no active claim exists for that actor.

mkdir -p "$EVIDENCE_DIR" 2>/dev/null

EVIDENCE_FILE="${EVIDENCE_DIR}/orphan.json"

# Build and append the JSON record via python3 to handle escaping safely.
# Pass all values via environment variables to avoid shell-quoting issues.
HOOK_COMMAND="$COMMAND" \
HOOK_EXIT_CODE="${EXIT_CODE:-0}" \
HOOK_STDOUT="$STDOUT_EXCERPT" \
HOOK_STDERR="$STDERR_EXCERPT" \
HOOK_ACTOR="${ACTOR:-unknown}" \
HOOK_TIMESTAMP="${TIMESTAMP:-}" \
HOOK_EVIDENCE_FILE="$EVIDENCE_FILE" \
python3 - <<'PYEOF' 2>/dev/null
import os, json

command    = os.environ.get('HOOK_COMMAND', '')
exit_code  = os.environ.get('HOOK_EXIT_CODE', '0')
stdout_ex  = os.environ.get('HOOK_STDOUT', '')
stderr_ex  = os.environ.get('HOOK_STDERR', '')
actor      = os.environ.get('HOOK_ACTOR', 'unknown')
timestamp  = os.environ.get('HOOK_TIMESTAMP', '')
evidence_f = os.environ.get('HOOK_EVIDENCE_FILE', '')

try:
    exit_code_int = int(exit_code)
except (ValueError, TypeError):
    exit_code_int = 0

record = {
    'timestamp':      timestamp,
    'command':        command,
    'exit_code':      exit_code_int,
    'stdout_excerpt': stdout_ex,
    'stderr_excerpt': stderr_ex,
    'actor':          actor,
    'note':           'orphan — no active claim found at capture time; pass this file via: fakoli-state submit TASK_ID --output-file <THIS_FILE>',
}

line = json.dumps(record)
if evidence_f:
    with open(evidence_f, 'a') as fh:
        fh.write(line + '\n')
PYEOF

exit 0
