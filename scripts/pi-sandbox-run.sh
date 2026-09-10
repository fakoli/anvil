#!/bin/sh
# pi-sandbox-run.sh — fail-closed launcher for unattended pi children.
#
# Usage:
#   scripts/pi-sandbox-run.sh --profile <name> --workspace <dir> \
#     (--task-file <file> | --task <text>) [--allowlist <path>] [--pi <bin>] \
#     [--offline] [--dry-run]
#
# What it guarantees (M1; containerization lands in M4):
#   1. The allowlist validates against packaging/pi/sandbox/allowlist.schema.json.
#   2. Every extension pin in the profile is verified (sha256 for path:/npm:,
#      ref->commit for git:) BEFORE anything launches; any mismatch or missing
#      pin aborts the run. Unreachable registry => abort (fail closed).
#   3. The composed argv uses pi's strict recipe: --no-extensions (explicit -e
#      paths only), --tools strict allowlist, --no-skills, -na (ignore
#      project-local settings), --mode json.
#   4. PI_CODING_AGENT_DIR points at a fresh per-run directory, so no user or
#      project extensions, skills, or settings can load regardless of cwd.
#
# Exit codes: 0 ok · 1 pi exited non-zero (forwarded) · 2 policy/usage error ·
# 3 pin mismatch · 4 verification dependency unavailable.

set -u

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
allowlist_default="$repo_root/packaging/pi/sandbox/allowlist.json"

usage() {
  echo "usage: $0 --profile <name> --workspace <dir> (--task-file <file>|--task <text>) [--allowlist <path>] [--pi <bin>] [--offline] [--dry-run]" >&2
  exit 2
}

PROFILE=""; WORKSPACE=""; TASK_FILE=""; TASK=""; ALLOWLIST="$allowlist_default"; PI_BIN="pi"; OFFLINE=""; DRY_RUN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) [ $# -ge 2 ] || usage; PROFILE="$2"; shift 2 ;;
    --workspace) [ $# -ge 2 ] || usage; WORKSPACE="$2"; shift 2 ;;
    --task-file) [ $# -ge 2 ] || usage; TASK_FILE="$2"; shift 2 ;;
    --task) [ $# -ge 2 ] || usage; TASK="$2"; shift 2 ;;
    --allowlist) [ $# -ge 2 ] || usage; ALLOWLIST="$2"; shift 2 ;;
    --pi) [ $# -ge 2 ] || usage; PI_BIN="$2"; shift 2 ;;
    --offline) OFFLINE="--offline"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) usage ;;
  esac
done
[ -n "$PROFILE" ] || usage
[ -n "$WORKSPACE" ] || usage
{ [ -n "$TASK_FILE" ] || [ -n "$TASK" ]; } || usage
[ -f "$ALLOWLIST" ] || { echo "pi-sandbox-run: allowlist not found: $ALLOWLIST" >&2; exit 2; }
[ -d "$WORKSPACE" ] || { echo "pi-sandbox-run: workspace not found: $WORKSPACE" >&2; exit 2; }
[ -n "$TASK_FILE" ] && [ ! -f "$TASK_FILE" ] && { echo "pi-sandbox-run: task file not found: $TASK_FILE" >&2; exit 2; }

POLICY="$script_dir/pi-sandbox-policy.mjs"
NODE_BIN=${NODE_BIN:-node}
command -v "$NODE_BIN" >/dev/null 2>&1 || { echo "pi-sandbox-run: node not found" >&2; exit 4; }

# 1. Validate policy (fail closed).
"$NODE_BIN" "$POLICY" validate "$ALLOWLIST" || exit $?

# 2. Verify pins (fail closed; empty extension lists pass trivially).
"$NODE_BIN" "$POLICY" pin-verify "$ALLOWLIST" --profile "$PROFILE" $OFFLINE || exit $?

# 3. Compose the strict argv.
if [ -n "$TASK_FILE" ]; then
  COMPOSED=$("$NODE_BIN" "$POLICY" compose "$ALLOWLIST" --profile "$PROFILE" --task-file "$TASK_FILE") || exit $?
else
  COMPOSED=$("$NODE_BIN" "$POLICY" compose "$ALLOWLIST" --profile "$PROFILE" --task "$TASK") || exit $?
fi

ARGV=$(printf '%s' "$COMPOSED" | "$NODE_BIN" -e '
  let input = "";
  process.stdin.on("data", (c) => (input += c));
  process.stdin.on("end", () => {
    const composed = JSON.parse(input);
    const argv = composed.argv.map((a) => JSON.stringify(a)).join(" ");
    console.log("ARGV " + argv);
    console.log("AGENTDIR_ENV " + composed.env.PI_CODING_AGENT_DIR);
  });
') || exit 2

argv_str=$(printf '%s\n' "$ARGV" | sed -n 's/^ARGV //p')
agent_dir_template=$(printf '%s\n' "$ARGV" | sed -n 's/^AGENTDIR_ENV //p')

# 4. Fresh per-run agent dir: user/project extensions, skills, and settings
#    cannot load even if the workspace contains a .pi/ directory.
sandbox_agent_dir=$(mktemp -d "${TMPDIR:-/tmp}/pi-sandbox-agent.XXXXXX") || exit 2
agent_dir=$(echo "$agent_dir_template" | sed "s|\$SANDBOX_AGENT_DIR|$sandbox_agent_dir|g")

if [ -n "$DRY_RUN" ]; then
  echo "profile: $PROFILE"
  echo "workspace: $WORKSPACE"
  echo "allowlist: $ALLOWLIST"
  echo "agent-dir: $agent_dir (fresh per-run)"
  echo "argv: $argv_str"
  echo "dry-run: not launching"
  exit 0
fi

command -v "$PI_BIN" >/dev/null 2>&1 || { echo "pi-sandbox-run: pi binary not found: $PI_BIN" >&2; rm -rf "$sandbox_agent_dir"; exit 2; }

# 5. Launch. cd into the workspace, fresh agent dir, strict argv. The sandbox
#    agent dir is cleaned up on exit.
cleanup() { rm -rf "$sandbox_agent_dir"; }
trap cleanup EXIT INT TERM

cd "$WORKSPACE" || exit 2
# shellcheck disable=SC2086 # argv is composed of quoted tokens
eval "PI_CODING_AGENT_DIR=\"$agent_dir\" \"$PI_BIN\" $argv_str"
exit $?