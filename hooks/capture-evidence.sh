#!/usr/bin/env bash

# capture-evidence.sh — PostToolUse hook for anvil (Phase 5)
# Fires after every Bash tool call.
# Captures stdout/stderr/exit code of verification commands into a per-claim
# evidence buffer at .anvil/.evidence-buffer/<claim-id>.json.
#
# Phase 5 fallback strategy: when the CLI subcommand is not yet available,
# write directly to the evidence buffer in shell. Wave 2 (guido) implements:
#   anvil hook capture-evidence \
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

STATE_DIR=".anvil"
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

# Single python3 pass: extract fields AND pre-build the JSON evidence line.
# Emits shell-sourceable assignments (shlex.quote) for COMMAND/IS_VERIFICATION/
# ACTOR/EXIT_CODE/TIMESTAMP, plus a ready-to-append EVIDENCE_LINE so the
# fallback path needs no second interpreter spawn.
ASSIGNMENTS=$(HOOK_PAYLOAD="$PAYLOAD" python3 - <<'PYEOF' 2>/dev/null
import os, json, datetime, shlex

MAX_EXCERPT = 4000

VERIFICATION_PATTERNS = ('pytest', 'ruff check', 'mypy', 'npm test', 'cargo test', 'bun test')

def emit(name: str, value: str) -> None:
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

    is_verification = int(any(p in command for p in VERIFICATION_PATTERNS))

    stdout_ex = stdout_raw[:MAX_EXCERPT]
    stderr_ex = stderr_raw[:MAX_EXCERPT]

    # Pre-build the JSON evidence line so the fallback path needs no second spawn.
    record = {
        'timestamp':      ts,
        'command':        command,
        'exit_code':      exit_code_int,
        'stdout_excerpt': stdout_ex,
        'stderr_excerpt': stderr_ex,
        'actor':          actor,
        'note':           'orphan — no active claim found at capture time; pass this file via: anvil submit TASK_ID --output-file <THIS_FILE>',
    }
    evidence_line = json.dumps(record)

    emit('COMMAND',        command)
    emit('EXIT_CODE',      str(exit_code_int))
    emit('STDOUT_EXCERPT', stdout_ex)
    emit('STDERR_EXCERPT', stderr_ex)
    emit('ACTOR',          actor)
    emit('TIMESTAMP',      ts)
    emit('IS_VERIFICATION', str(is_verification))
    emit('EVIDENCE_LINE',  evidence_line)
except Exception:
    # On any failure, emit empty assignments so the early-exit on $COMMAND
    # fires and the hook exits 0 cleanly.
    for name in ('COMMAND', 'EXIT_CODE', 'STDOUT_EXCERPT',
                 'STDERR_EXCERPT', 'ACTOR', 'TIMESTAMP', 'IS_VERIFICATION',
                 'EVIDENCE_LINE'):
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
# IS_VERIFICATION was computed in the python3 extraction pass above.
# Only capture evidence for known verification commands (pytest, ruff check,
# mypy, npm test, cargo test, bun test) to avoid polluting the buffer.
# Phase 6+ moves the hardcoded pattern list to config.

# Not a verification command — silent exit; do not pollute the buffer.
if [ "${IS_VERIFICATION:-0}" -eq 0 ]; then
  exit 0
fi

# ---- Try the CLI subcommand first (guido Wave 2 implements this) ----------
# CLI invocation shape for guido:
#   anvil hook capture-evidence \
#     --command CMD --exit-code N \
#     --stdout-file PATH --stderr-file PATH \
#     --actor ACTOR
#
# The hook passes --stdout-file / --stderr-file (temp files) rather than
# inlining content because excerpts can be multi-line and avoid quoting issues.

CLI="${CLAUDE_PLUGIN_ROOT}/bin/anvil"

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
# orphan evidence to a claim later via `anvil submit --output-file`.
#
# When the CLI subcommand (guido Wave 2) is wired, it will:
#   1. Look up the active claim for --actor in state.db.
#   2. Write to .evidence-buffer/<claim-id>.json if a claim is found.
#   3. Fall back to orphan.json if no active claim exists for that actor.

mkdir -p "$EVIDENCE_DIR" 2>/dev/null

EVIDENCE_FILE="${EVIDENCE_DIR}/orphan.json"

# EVIDENCE_LINE was pre-built as a valid JSON string by the extraction python3
# pass above — no second interpreter spawn needed.
if [ -n "$EVIDENCE_LINE" ]; then
  printf '%s\n' "$EVIDENCE_LINE" >> "$EVIDENCE_FILE" 2>/dev/null
fi

exit 0
