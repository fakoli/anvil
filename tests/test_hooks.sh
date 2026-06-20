#!/usr/bin/env bash
# test_hooks.sh — Smoke tests for check-claim.sh and record-file-change.sh
#
# Usage: bash tests/test_hooks.sh
# CI: add `bash plugins/anvil/tests/test_hooks.sh` to the test matrix.
#
# Strategy: run each hook script directly via bash (no CLAUDE_PLUGIN_ROOT set)
# so the CLI fallback is skipped and we test the shell logic paths.
# Assertions: exit code and presence/absence of stderr output.

set -u

PASS=0
FAIL=0

# Resolve script directory (hooks/ relative to this script's parent)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CHECK_CLAIM="${REPO_ROOT}/hooks/check-claim.sh"
RECORD_CHANGE="${REPO_ROOT}/hooks/record-file-change.sh"

_pass() {
  PASS=$((PASS + 1))
  printf 'PASS: %s\n' "$1"
}

_fail() {
  FAIL=$((FAIL + 1))
  printf 'FAIL: %s\n' "$1"
}

_assert_exit_zero() {
  local name="$1"
  local exit_code="$2"
  if [ "$exit_code" -eq 0 ]; then
    _pass "$name"
  else
    _fail "$name (exit code: $exit_code)"
  fi
}

# ---------------------------------------------------------------------------
# Setup: temporary project directory with .anvil/
# ---------------------------------------------------------------------------
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

UNINITIALIZED_DIR="${TMP_DIR}/no-state"
INITIALIZED_DIR="${TMP_DIR}/with-state"

mkdir -p "$UNINITIALIZED_DIR"
mkdir -p "${INITIALIZED_DIR}/.anvil"
touch "${INITIALIZED_DIR}/.anvil/events.jsonl"
touch "${INITIALIZED_DIR}/.anvil/state.db"

# ---------------------------------------------------------------------------
# Smoke test 1: check-claim.sh exits 0 when no .anvil/ exists
# ---------------------------------------------------------------------------
cd "$UNINITIALIZED_DIR" || exit 1
STDERR_OUT="$(bash "$CHECK_CLAIM" < /dev/null 2>&1)"
EXIT_CODE=$?
_assert_exit_zero "check-claim: exits 0 in uninitialized directory" "$EXIT_CODE"

# ---------------------------------------------------------------------------
# Smoke test 2: check-claim.sh exits 0 with valid JSON payload (no CLI)
# ---------------------------------------------------------------------------
PAYLOAD='{"tool_name":"Edit","tool_input":{"path":"src/foo.py"},"session_id":"test-session"}'
STDERR_OUT="$(printf '%s' "$PAYLOAD" | bash "$CHECK_CLAIM" 2>&1)"
EXIT_CODE=$?
_assert_exit_zero "check-claim: exits 0 with valid JSON payload in uninitialized dir" "$EXIT_CODE"

# ---------------------------------------------------------------------------
# Smoke test 3: check-claim.sh exits 0 with initialized dir (no CLI available)
# ---------------------------------------------------------------------------
cd "$INITIALIZED_DIR" || exit 1
PAYLOAD='{"tool_name":"Edit","tool_input":{"path":"src/app.py"},"session_id":"sess-abc"}'
STDERR_OUT="$(printf '%s' "$PAYLOAD" | bash "$CHECK_CLAIM" 2>&1)"
EXIT_CODE=$?
_assert_exit_zero "check-claim: exits 0 in initialized dir (no CLI)" "$EXIT_CODE"

# ---------------------------------------------------------------------------
# Smoke test 4: check-claim.sh exits 0 with absolute path outside project
# ---------------------------------------------------------------------------
cd "$INITIALIZED_DIR" || exit 1
PAYLOAD='{"tool_name":"Edit","tool_input":{"path":"/etc/hosts"},"session_id":"sess-abc"}'
STDERR_OUT="$(printf '%s' "$PAYLOAD" | bash "$CHECK_CLAIM" 2>&1)"
EXIT_CODE=$?
_assert_exit_zero "check-claim: exits 0 for absolute path outside project" "$EXIT_CODE"

# ---------------------------------------------------------------------------
# Smoke test 5: check-claim.sh exits 0 when stdin is a tty (no payload)
# ---------------------------------------------------------------------------
cd "$INITIALIZED_DIR" || exit 1
# Simulate terminal stdin by running in a subshell; the [ -t 0 ] check uses PAYLOAD={}
STDERR_OUT="$(bash "$CHECK_CLAIM" < /dev/null 2>&1)"
EXIT_CODE=$?
_assert_exit_zero "check-claim: exits 0 with empty/null payload" "$EXIT_CODE"

# ---------------------------------------------------------------------------
# Smoke test 6: record-file-change.sh exits 0 in uninitialized directory
# ---------------------------------------------------------------------------
cd "$UNINITIALIZED_DIR" || exit 1
STDERR_OUT="$(bash "$RECORD_CHANGE" < /dev/null 2>&1)"
EXIT_CODE=$?
_assert_exit_zero "record-file-change: exits 0 in uninitialized directory" "$EXIT_CODE"

# ---------------------------------------------------------------------------
# Smoke test 7: record-file-change.sh exits 0 with valid JSON payload
# ---------------------------------------------------------------------------
PAYLOAD='{"tool_name":"Edit","tool_input":{"path":"src/bar.py"},"session_id":"sess-xyz"}'
STDERR_OUT="$(printf '%s' "$PAYLOAD" | bash "$RECORD_CHANGE" 2>&1)"
EXIT_CODE=$?
_assert_exit_zero "record-file-change: exits 0 with valid JSON payload in uninitialized dir" "$EXIT_CODE"

# ---------------------------------------------------------------------------
# Smoke test 8: record-file-change.sh appends to events.jsonl (fallback path)
# ---------------------------------------------------------------------------
cd "$INITIALIZED_DIR" || exit 1
PAYLOAD='{"tool_name":"Write","tool_input":{"path":"src/new_file.py"},"session_id":"sess-append"}'
printf '%s' "$PAYLOAD" | bash "$RECORD_CHANGE" 2>/dev/null
EXIT_CODE=$?
_assert_exit_zero "record-file-change: exits 0 in initialized dir (fallback JSONL append)" "$EXIT_CODE"

EVENTS_FILE="${INITIALIZED_DIR}/.anvil/events.jsonl"
if [ -f "$EVENTS_FILE" ] && grep -q "file_changed\|new_file.py" "$EVENTS_FILE" 2>/dev/null; then
  _pass "record-file-change: events.jsonl contains expected content after append"
else
  # Critic-4 flagged this branch as always-passing (false confidence). Now
  # asserts properly: if neither CLI nor python3 fallback wrote the audit
  # entry, the hook's write contract is broken.
  _fail "record-file-change: events.jsonl was not written or missing expected content"
fi

# ---------------------------------------------------------------------------
# Smoke test 9: record-file-change.sh exits 0 with empty JSON payload
# ---------------------------------------------------------------------------
PAYLOAD="{}"
STDERR_OUT="$(printf '%s' "$PAYLOAD" | bash "$RECORD_CHANGE" 2>&1)"
EXIT_CODE=$?
_assert_exit_zero "record-file-change: exits 0 with empty JSON (no file path → fast-path skip)" "$EXIT_CODE"

# ---------------------------------------------------------------------------
# Smoke test 10: check-claim.sh exits 0 with malformed JSON (graceful degradation)
# ---------------------------------------------------------------------------
cd "$INITIALIZED_DIR" || exit 1
MALFORMED="NOT VALID JSON"
STDERR_OUT="$(printf '%s' "$MALFORMED" | bash "$CHECK_CLAIM" 2>&1)"
EXIT_CODE=$?
_assert_exit_zero "check-claim: exits 0 with malformed JSON payload" "$EXIT_CODE"

# ---------------------------------------------------------------------------
# Phase 5 setup: capture-evidence.sh path + per-test state dirs
# ---------------------------------------------------------------------------
CAPTURE_EVIDENCE="${REPO_ROOT}/hooks/capture-evidence.sh"

# A fresh initialized dir for capture-evidence tests (separate from above to
# avoid interference with record-file-change tests).
CE_DIR="${TMP_DIR}/ce-test"
mkdir -p "${CE_DIR}/.anvil"
touch "${CE_DIR}/.anvil/state.db"

# ---------------------------------------------------------------------------
# Smoke test 11: capture-evidence.sh exits 0 outside any project (.anvil/ absent)
# ---------------------------------------------------------------------------
cd "$UNINITIALIZED_DIR" || exit 1
EXIT_CODE=0
bash "$CAPTURE_EVIDENCE" < /dev/null 2>/dev/null
EXIT_CODE=$?
_assert_exit_zero "capture-evidence: exits 0 outside .anvil/ project" "$EXIT_CODE"

# ---------------------------------------------------------------------------
# Smoke test 12: capture-evidence.sh exits 0 with stdin tty (no payload)
# ---------------------------------------------------------------------------
cd "$CE_DIR" || exit 1
EXIT_CODE=0
bash "$CAPTURE_EVIDENCE" < /dev/null 2>/dev/null
EXIT_CODE=$?
_assert_exit_zero "capture-evidence: exits 0 with null stdin (no payload)" "$EXIT_CODE"

# ---------------------------------------------------------------------------
# Smoke test 13: capture-evidence.sh exits 0 for non-verification command
# (incidental shell calls must not be captured)
# ---------------------------------------------------------------------------
cd "$CE_DIR" || exit 1
PAYLOAD='{"tool_input":{"command":"echo hello"},"tool_response":{"stdout":"hello\n","stderr":"","exit_code":0},"session_id":"sess-ce"}'
printf '%s' "$PAYLOAD" | bash "$CAPTURE_EVIDENCE" 2>/dev/null
EXIT_CODE=$?
_assert_exit_zero "capture-evidence: exits 0 for non-verification command (echo)" "$EXIT_CODE"

# And orphan.json should NOT have been written for this non-verification command.
CE_ORPHAN="${CE_DIR}/.anvil/.evidence-buffer/orphan.json"
if [ ! -f "$CE_ORPHAN" ]; then
  _pass "capture-evidence: orphan.json NOT written for non-verification command"
else
  # File may have been written by a previous sub-test accidentally; check content.
  if grep -q "echo hello" "$CE_ORPHAN" 2>/dev/null; then
    _fail "capture-evidence: orphan.json written for non-verification command 'echo hello'"
  else
    _pass "capture-evidence: orphan.json did not capture non-verification command"
  fi
fi

# ---------------------------------------------------------------------------
# Smoke test 14: capture-evidence.sh exits 0 for a verification command (pytest)
# and writes to orphan.json when no active claim
# ---------------------------------------------------------------------------
CE2_DIR="${TMP_DIR}/ce-test2"
mkdir -p "${CE2_DIR}/.anvil"
touch "${CE2_DIR}/.anvil/state.db"

cd "$CE2_DIR" || exit 1
PAYLOAD='{"tool_input":{"command":"pytest tests/ -v"},"tool_response":{"stdout":"5 passed","stderr":"","exit_code":0},"session_id":"sess-ce2"}'
printf '%s' "$PAYLOAD" | bash "$CAPTURE_EVIDENCE" 2>/dev/null
EXIT_CODE=$?
_assert_exit_zero "capture-evidence: exits 0 for verification command (pytest)" "$EXIT_CODE"

CE2_ORPHAN="${CE2_DIR}/.anvil/.evidence-buffer/orphan.json"
if [ -f "$CE2_ORPHAN" ] && grep -q "pytest" "$CE2_ORPHAN" 2>/dev/null; then
  _pass "capture-evidence: orphan.json written with pytest command captured"
else
  # Critic-4 flagged this as always-passing. The orphan write IS the
  # contract when no active claim exists; if it didn't fire, the hook
  # is broken.
  _fail "capture-evidence: orphan.json missing or did not capture pytest command"
fi

# ---------------------------------------------------------------------------
# Regression test (CL-1): check-claim.sh invokes the per-file CLI subcommand
# `hook check-claim --file <PATH> --actor <ACTOR>` rather than the old
# `status --hook-format` count-based check.  We stub a fake CLI binary that
# records the arguments it was called with and assert the shell hook called
# the right subcommand with the right flags.
# ---------------------------------------------------------------------------
CC_DIR="${TMP_DIR}/cc-cli-stub"
mkdir -p "${CC_DIR}/.anvil"
touch "${CC_DIR}/.anvil/state.db"

STUB_ROOT="${TMP_DIR}/cc-plugin"
mkdir -p "${STUB_ROOT}/bin"
STUB_CLI="${STUB_ROOT}/bin/anvil"
STUB_LOG="${TMP_DIR}/cc-cli-args.log"

# Embed the absolute log path into the stub via heredoc expansion (unquoted
# delimiter) so the stub records to a known file regardless of caller env.
cat > "$STUB_CLI" <<STUB
#!/usr/bin/env bash
# Test stub for anvil CLI — record argv to ${STUB_LOG} and exit 0.
printf '%s\n' "\$*" >> "${STUB_LOG}"
exit 0
STUB
chmod +x "$STUB_CLI"

cd "$CC_DIR" || exit 1
: > "$STUB_LOG"
PAYLOAD='{"tool_name":"Edit","tool_input":{"path":"src/foo.py"},"session_id":"sess-cl1"}'
printf '%s' "$PAYLOAD" | CLAUDE_PLUGIN_ROOT="$STUB_ROOT" bash "$CHECK_CLAIM" 2>/dev/null
EXIT_CODE=$?
_assert_exit_zero "check-claim (CL-1): invokes CLI and exits 0" "$EXIT_CODE"

# The stub should have been called exactly once with the per-file subcommand.
if grep -q '^hook check-claim --file src/foo.py --actor sess-cl1$' "$STUB_LOG" 2>/dev/null; then
  _pass "check-claim (CL-1): invoked 'hook check-claim --file <PATH> --actor <ACTOR>'"
else
  _fail "check-claim (CL-1): did not invoke per-file CLI subcommand (log: $(cat "$STUB_LOG"))"
fi

# The stub MUST NOT have been called with the legacy status --hook-format path.
if grep -q 'status --hook-format' "$STUB_LOG" 2>/dev/null; then
  _fail "check-claim (CL-1): still calls legacy 'status --hook-format' (log: $(cat "$STUB_LOG"))"
else
  _pass "check-claim (CL-1): legacy 'status --hook-format' invocation removed"
fi

# ---------------------------------------------------------------------------
# Smoke test 15: capture-evidence.sh exits 0 for exit_code=1 verification command
# (hook must never block even when the test run itself failed)
# ---------------------------------------------------------------------------
CE3_DIR="${TMP_DIR}/ce-test3"
mkdir -p "${CE3_DIR}/.anvil"
touch "${CE3_DIR}/.anvil/state.db"

cd "$CE3_DIR" || exit 1
PAYLOAD='{"tool_input":{"command":"pytest tests/ -v"},"tool_response":{"stdout":"1 failed","stderr":"FAILED test_foo.py","exit_code":1},"session_id":"sess-ce3"}'
printf '%s' "$PAYLOAD" | bash "$CAPTURE_EVIDENCE" 2>/dev/null
EXIT_CODE=$?
_assert_exit_zero "capture-evidence: exits 0 even when captured command exit_code=1" "$EXIT_CODE"

# ===========================================================================
# heartbeat.sh — PostToolUse lease heartbeat (B41). Non-blocking: always exit 0.
# ===========================================================================
HEARTBEAT="${REPO_ROOT}/hooks/heartbeat.sh"

cd "$UNINITIALIZED_DIR" || exit 1
bash "$HEARTBEAT" < /dev/null 2>/dev/null
_assert_exit_zero "heartbeat: exits 0 outside .anvil/ project" "$?"

HB_DIR="${TMP_DIR}/hb-test"
mkdir -p "${HB_DIR}/.anvil"
touch "${HB_DIR}/.anvil/state.db"
cd "$HB_DIR" || exit 1
bash "$HEARTBEAT" < /dev/null 2>/dev/null
_assert_exit_zero "heartbeat: exits 0 in initialized dir (no CLI / no claim)" "$?"

# ===========================================================================
# detect-state.sh — SessionStart hook (B44: workspace-aware, no local-.anvil gate).
# Always exit 0; output is driven by the workspace-aware CLI status, not a cwd dir.
# ===========================================================================
DETECT_STATE="${REPO_ROOT}/hooks/detect-state.sh"

# Plain dir, no CLI on PATH (CLAUDE_PLUGIN_ROOT unset → CLI not found) → exit 0.
cd "$UNINITIALIZED_DIR" || exit 1
( unset CLAUDE_PLUGIN_ROOT; bash "$DETECT_STATE" >/dev/null 2>&1 )
_assert_exit_zero "detect-state: exits 0 with no CLI available" "$?"

# Legacy in-repo .anvil present (no CLI) → still exit 0 (non-blocking contract).
DS_DIR="${TMP_DIR}/ds-test"
mkdir -p "${DS_DIR}/.anvil"
cd "$DS_DIR" || exit 1
( unset CLAUDE_PLUGIN_ROOT; bash "$DETECT_STATE" >/dev/null 2>&1 )
_assert_exit_zero "detect-state: exits 0 with legacy .anvil present" "$?"

# Stub CLI reporting "uninitialized" + a legacy .anvil present → migrate nudge.
DS_STUB_ROOT="${TMP_DIR}/ds-plugin"
mkdir -p "${DS_STUB_ROOT}/bin"
cat > "${DS_STUB_ROOT}/bin/anvil" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "${DS_STATUS:-uninitialized}"
exit 0
STUB
chmod +x "${DS_STUB_ROOT}/bin/anvil"

cd "$DS_DIR" || exit 1  # has a legacy .anvil from above
DS_OUT=$(CLAUDE_PLUGIN_ROOT="$DS_STUB_ROOT" DS_STATUS="uninitialized" bash "$DETECT_STATE" 2>/dev/null)
if printf '%s' "$DS_OUT" | grep -q 'migrate-workspace'; then
  _pass "detect-state: legacy + uninitialized → nudges 'anvil migrate-workspace'"
else
  _fail "detect-state: expected migrate nudge (got: $DS_OUT)"
fi

# Stub CLI reporting a live status line → prints the state banner.
DS_OUT=$(CLAUDE_PLUGIN_ROOT="$DS_STUB_ROOT" DS_STATUS="active-claims:1 ready-tasks:2 blockers:0 prd-status:approved" bash "$DETECT_STATE" 2>/dev/null)
if printf '%s' "$DS_OUT" | grep -q 'active-claims:1'; then
  _pass "detect-state: status line → prints the state banner"
else
  _fail "detect-state: expected status banner (got: $DS_OUT)"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
printf '\n'
printf 'Results: %d passed, %d failed\n' "$PASS" "$FAIL"

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
