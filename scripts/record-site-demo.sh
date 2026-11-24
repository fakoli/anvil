#!/usr/bin/env bash
# record-site-demo.sh — record the REAL anvil session that the website replays.
#
# The landing page (docs/index.md) plays docs/demo/session.json in an animated
# terminal. This script regenerates that file by running every command for
# real against this checkout, in a throwaway project under a temp directory.
#
#   docs/demo/session.json       the replayed session, one object per step
#   docs/demo/strict-gate.json   two extra steps showing the opt-in strict
#                                evidence gate (quoted on the landing page)
#
# Nothing is hand-edited. Each step's `output` is the captured stdout+stderr;
# the only transformations are the mechanical ones in scrub() below (strip
# ANSI, map the temp project path to ~/shortener, trim trailing whitespace).
# `act`, `label` and `comment` are captions written here; `display_lines` is a
# hint that lets the player shorten a long output, and the player says so.
#
# Requirements: bash, git, jq, column (util-linux), uv.
# No network and no LLM: planning uses the deterministic path, sync is off and
# API keys are unset. State never leaves the temp directory.
#
# Usage:  scripts/record-site-demo.sh
#         KEEP_WORK=1 scripts/record-site-demo.sh   # keep the temp project

set -uo pipefail   # deliberately no -e: three steps are EXPECTED to fail

ANVIL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$ANVIL_REPO/docs/demo"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/anvil-site-demo.XXXXXX")"
PROJECT="$WORK/shortener"            # shown as ~/shortener in session.json
APPENDIX="$WORK/strict/shortener"    # second throwaway project (strict gate)
RAW="$WORK/raw"

die() { printf 'record-site-demo: %s\n' "$*" >&2; exit 1; }
cleanup() { [ "${KEEP_WORK:-}" = 1 ] && printf 'kept %s\n' "$WORK" || rm -rf "$WORK"; }
trap cleanup EXIT

# ---------------------------------------------------------------- guards ----
for tool in git jq column uv; do
  command -v "$tool" >/dev/null || die "missing required tool: $tool"
done
[ -d "$ANVIL_REPO/bin" ] || die "not an anvil checkout: $ANVIL_REPO"
git -C "$WORK" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  && die "temp directory is inside a git work tree ($WORK); set TMPDIR elsewhere"

# ----------------------------------------------------------- environment ----
# Isolate every place anvil could read or write outside the temp directory.
while IFS= read -r var; do unset "$var"; done < <(compgen -e | grep -E '^ANVIL_' | grep -v '^ANVIL_REPO$')
unset CLAUDE_CODE_SESSION_ID CLAUDE_PLUGIN_ROOT            # harness session pins
unset ANTHROPIC_API_KEY OPENAI_API_KEY CUSTOM_LLM_API_KEY CUSTOM_LLM_BASE_URL
unset GITHUB_TOKEN GITHUB_REPOSITORY AWS_REGION AWS_DEFAULT_REGION

export ANVIL_ROOT="$PROJECT"                        # state at $PROJECT/.anvil (docs/how-to/state-location.md)
export ANVIL_KEYS_DIR="$WORK/home/keys"             # signing key (default: ~/.anvil/keys)
export ANVIL_TRUST_LIST="$WORK/home/trust.txt"      # trust list  (default: ~/.anvil/trust.txt)
export ANVIL_GLOBAL_CONFIG="$WORK/home/config.yaml" # absent on purpose: no global defaults
export PYTHONUNBUFFERED=1 NO_COLOR=1
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME="demo" GIT_AUTHOR_EMAIL="demo@example.com"
export GIT_COMMITTER_NAME="demo" GIT_COMMITTER_EMAIL="demo@example.com"

# Warm the engine environment once, up front, so no installer output can land
# in a recorded step; recorded commands then run with --no-sync. (`uv run`
# syncs inexactly, so it leaves any extras you have installed alone.)
uv run --project "$ANVIL_REPO/bin" anvil --version >/dev/null 2>&1 \
  || die "could not prepare the engine environment in $ANVIL_REPO/bin"

# What the session shows as plain `anvil` / `pytest`.
anvil()  { uv run --no-sync --project "$ANVIL_REPO/bin" anvil "$@"; }
pytest() { uv run --no-sync --project "$ANVIL_REPO/bin" pytest "$@"; }

# ------------------------------------------------------------- recorder ----
STEPS_FILE=""; STEP_NO=0; LAST_RC=0; LAST_OUT=""

# step <act> <label> <comment> <cmd> [first:last]
#   act      chapter shown in the player's step list (Plan, Coordinate, ...)
#   label    short name for the step list
#   comment  one-sentence caption, shown as a `# comment` line in the terminal
#   cmd      run exactly as written (in this shell, cwd = the project); its
#            combined stdout+stderr and exit code are captured
#   first:last  optional: output lines [first, last) the player shows by default
step() {
  local act="$1" label="$2" comment="$3" cmd="$4" lines="${5:-}" out rc
  STEP_NO=$((STEP_NO + 1))
  out="$RAW/$(basename "$STEPS_FILE" .jsonl)-$(printf '%02d' "$STEP_NO").out"
  printf '\n# %s\n$ %s\n' "$comment" "$cmd"
  eval "$cmd" >"$out" 2>&1
  rc=$?
  cat "$out"
  [ "$rc" -eq 0 ] || printf '[exit %d]\n' "$rc"
  jq -cn --arg act "$act" --arg label "$label" --arg comment "$comment" --arg cmd "$cmd" \
        --argjson exit "$rc" --rawfile output "$out" --arg lines "$lines" \
        '{act: $act, label: $label, comment: $comment, cmd: $cmd, exit: $exit, output: $output}
         + (if $lines == "" then {} else {display_lines: ($lines | split(":") | map(tonumber))} end)' >>"$STEPS_FILE"
  LAST_RC=$rc; LAST_OUT=$out
}

expect_exit() { [ "$LAST_RC" -eq "$1" ] || die "step $STEP_NO: expected exit $1, got $LAST_RC"; }

# scrub: the ONLY post-processing applied to captured output.
build_json() {  # build_json <steps.jsonl> <out.json>
  jq -s --arg appendix "$APPENDIX" --arg project "$PROJECT" --arg work "$WORK" '
    def scrub:
        gsub("\u001b\\[[0-?]*[ -/]*[@-~]"; "")                        # ANSI CSI
      | gsub("\u001b\\][^\u0007\u001b]*(\u0007|\u001b\\\\)"; "")      # ANSI OSC
      | gsub("\r\n"; "\n")
      | split($appendix) | join("~/shortener")                        # abs path -> ~/shortener
      | split($project)  | join("~/shortener")
      | split($work)     | join("~")
      | sub("\\s+\\z"; "");                                           # trailing whitespace
    map(.output |= scrub)' "$1" >"$2"
}

# ------------------------------------------------------ project fixtures ----
new_repo() {  # new_repo <dir>: fresh git repo with one commit
  mkdir -p "$1" && cd "$1" || die "cannot create $1"
  git init -q -b main .
  printf '.anvil/\n__pycache__/\n.pytest_cache/\npytest.log\n' >.gitignore
  git add .gitignore && git commit -q -m "Initial commit"
}

write_prd() {  # the PRD the demo loads: 3 requirements, 1 feature, 2 tasks
  cat >.anvil/prd.md <<'PRD'
# Project: Shortener

## Summary

A tiny URL shortener library: turn a long URL into a short code and back.

## Goals

- Shorten any http(s) URL to a 7-character code.
- Resolve a code back to the original URL.

## Requirements

- R001: `shorten(url)` returns a deterministic 7-character code.
- R002: `resolve(code)` returns the original URL, or `None` for an unknown code.
- R003: Non-http(s) URLs are rejected with `ValueError`.

## Features

### F001: Shorten and resolve

Map URLs to short codes and back, in memory.

**Requirements:** R001, R002, R003

## Tasks

### T001: Implement shorten()

**Feature:** F001
**Priority:** high
**Likely files:** shortener.py, test_shortener.py

Hash the URL to a 7-character code; reject non-http(s) URLs.

**Acceptance criteria:**

- `shorten("https://example.com")` returns the same 7-character code every time.
- `shorten("ftp://example.com")` raises `ValueError`.

**Verification:**

- `pytest -q`

### T002: Implement resolve()

**Feature:** F001
**Priority:** medium
**Likely files:** shortener.py, test_shortener.py
**Dependencies:** T001

Look a code up and return the original URL.

**Acceptance criteria:**

- `resolve(shorten(url))` returns `url`.
- `resolve("nope")` returns `None`.

**Verification:**

- `pytest -q`
PRD
}

write_work() {  # what "agent-a" writes for T001, committed on its claim branch
  cat >shortener.py <<'PY'
"""A tiny in-memory URL shortener."""
import hashlib

_urls: dict[str, str] = {}


def shorten(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"not an http(s) URL: {url}")
    code = hashlib.sha256(url.encode()).hexdigest()[:7]
    _urls[code] = url
    return code
PY
  cat >test_shortener.py <<'PY'
import pytest

from shortener import shorten


def test_shorten_is_deterministic():
    code = shorten("https://example.com")
    assert len(code) == 7
    assert shorten("https://example.com") == code


def test_shorten_rejects_non_http():
    with pytest.raises(ValueError):
        shorten("ftp://example.com")
PY
  git add shortener.py test_shortener.py && git commit -q -m "T001: implement shorten()"
}

# ================================================================= main ====
START_EPOCH=$(date +%s)
mkdir -p "$WORK/home" "$RAW" "$OUT_DIR"

ANVIL_VERSION="$(anvil --version 2>&1)" || die "anvil is not runnable: $ANVIL_VERSION"
printf 'Recording with: %s\n' "$ANVIL_VERSION"

new_repo "$PROJECT"
STEPS_FILE="$RAW/session.jsonl"; STEP_NO=0; : >"$STEPS_FILE"

# -- Plan --------------------------------------------------------------------
step Plan "Start a project" \
  "Start a project: Anvil scaffolds its state, a SQLite database and an append-only event log" \
  'anvil init --name Shortener' 0:8
expect_exit 0
# Safety: state must have landed in the temp project, nowhere else.
[ -f "$PROJECT/.anvil/state.db" ] || die "state.db not in the temp project"
[ "$(anvil status --path-only)" = "$PROJECT/.anvil" ] || die "anvil resolved state outside the temp project"

write_prd   # (not a recorded step) author .anvil/prd.md

step Plan "Load and approve the PRD" \
  "Load the PRD (3 requirements, 2 tasks) and pass the two-step human review gate" \
  'anvil prd parse && anvil prd review && anvil prd review --approve'
expect_exit 0

step Plan "Plan, score, promote" \
  "Plan without an LLM: build the task graph, score it, promote tasks to ready" \
  'anvil plan && anvil score && anvil review tasks'
expect_exit 0

step Plan "Read the task graph" \
  "The task graph: T002 depends on T001, so Anvil lists only T001 as ready to claim" \
  'anvil graph'
expect_exit 0

# -- Coordinate --------------------------------------------------------------
step Coordinate "agent-a claims T001" \
  "agent-a claims T001: an exclusive lease, plus a git branch to work on" \
  'anvil claim T001 --actor agent-a'
expect_exit 0
CLAIM_ID="$(sed -n 's/^ *Claim ID: *\(C[0-9A-F]\{8\}\).*/\1/p' "$LAST_OUT")"
BRANCH="$(sed -n 's/^ *Branch: *\(.*\)$/\1/p' "$LAST_OUT")"
[ -n "$CLAIM_ID" ] && [ -n "$BRANCH" ] || die "could not read claim id / branch from claim output"

step Coordinate "agent-b is refused" \
  "agent-b tries to claim the same task and is refused" \
  'anvil claim T001 --actor agent-b'
expect_exit 1

# -- Prove -------------------------------------------------------------------
step Prove "Approve with no evidence" \
  "agent-a tries to approve its own task with no evidence on record: refused" \
  'anvil apply T001 --approve --reviewer agent-a'
expect_exit 1

step Prove "The gate names what is missing" \
  "The finish gate names exactly which proof is missing (exit 2 means block)" \
  'anvil gate-check --actor agent-a'
expect_exit 2

write_work  # (not a recorded step) agent-a writes shortener.py + test_shortener.py and commits

step Prove "Run the verification" \
  "agent-a does the work on its branch, then runs the task's verification command" \
  'pytest -q | tee pytest.log'
PYTEST_RC=$LAST_RC
expect_exit 0

step Prove "Capture the proof" \
  "The run is captured as a claim-bound proof: command, exit code, output hash. A harness hook does this automatically" \
  "ANVIL_CLAIM_ID=$CLAIM_ID anvil hook capture-evidence --command \"pytest -q\" --exit-code $PYTEST_RC --stdout-file pytest.log --actor agent-a"
expect_exit 0

step Prove "Submit evidence" \
  "agent-a submits evidence: the gate matches the proof and the claim is released" \
  'anvil submit T001 --commands "pytest -q" --files-changed shortener.py --files-changed test_shortener.py --actor agent-a'
expect_exit 0

step Prove "Approve and sign" \
  "A human reviewer approves: the task is done and a signed acceptance proof is written" \
  'anvil apply T001 --approve --reviewer human'
expect_exit 0

step Prove "Merge the branch" \
  "The approved branch lands on main" \
  "git switch -q main && git merge $BRANCH"
expect_exit 0

# -- Replay ------------------------------------------------------------------
step Replay "Read the event log" \
  "The ledger: every state change is one line in an append-only event log" \
  "jq -r '[.id, .action, .target_id, .actor] | @tsv' .anvil/events.jsonl | column -t"
expect_exit 0

step Replay "Rebuild from events" \
  "Rebuild state from the event log alone, into a scratch database" \
  'anvil replay --from-events .anvil/events.jsonl --into ../replay.db'
expect_exit 0

step Replay "Check replay integrity" \
  "Health check: replaying events.jsonl reproduces the live state.db" \
  'anvil doctor'
expect_exit 0

step Replay "Final status" \
  "Final status: T001 is done and T002 is unlocked for the next agent" \
  'anvil status'
expect_exit 0

MAIN_STEPS=$STEP_NO
build_json "$RAW/session.jsonl" "$OUT_DIR/session.json"

# ========================================================== strict gate =====
# The evidence gate at `apply --approve` is ADVISORY by default; `--strict`
# (or ANVIL_STRICT_EVIDENCE=1 / strict_evidence: true) makes it a hard gate.
# Same project, rebuilt from scratch in a second temp project; the setup
# commands are the ones recorded above, so here they run unrecorded.
export ANVIL_ROOT="$APPENDIX"
new_repo "$APPENDIX"
{
  anvil init --name Shortener && write_prd \
    && anvil prd parse && anvil prd review && anvil prd review --approve \
    && anvil plan && anvil score && anvil review tasks \
    && anvil claim T001 --actor agent-a && write_work
} >"$RAW/strict-setup.log" 2>&1 || { cat "$RAW/strict-setup.log" >&2; die "strict-gate setup failed"; }

STEPS_FILE="$RAW/strict-gate.jsonl"; STEP_NO=0; : >"$STEPS_FILE"

step Prove "Submit without the proof" \
  "agent-a submits without running the tests: accepted for review, but the gate reports the missing proof" \
  'anvil submit T001 --commands "pytest -q" --files-changed shortener.py --files-changed test_shortener.py --actor agent-a'
expect_exit 0

step Prove "Strict approval is refused" \
  "With the strict evidence gate on, approval is refused until the proof exists" \
  'anvil apply T001 --approve --strict --reviewer human'
expect_exit 1

build_json "$RAW/strict-gate.jsonl" "$OUT_DIR/strict-gate.json"

# ============================================================== wrap up ====
# Sanity: nothing private or machine-specific may survive in the deliverables.
for f in "$OUT_DIR/session.json" "$OUT_DIR/strict-gate.json"; do
  jq -e 'type == "array" and all(.[]; has("comment") and has("cmd") and has("exit") and has("output"))' "$f" >/dev/null \
    || die "$f is not a well-formed step array"
  if grep -qF -e "$WORK" -e "$HOME" "$f"; then die "$f still contains an absolute local path"; fi
done

printf '\nRecorded %d + %d steps in %ds with %s\n' \
  "$MAIN_STEPS" "$STEP_NO" "$(( $(date +%s) - START_EPOCH ))" "$ANVIL_VERSION"
printf '  %s\n  %s\n' "$OUT_DIR/session.json" "$OUT_DIR/strict-gate.json"
