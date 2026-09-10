#!/bin/sh
# entrypoint.sh — in-image launcher for the pi sandbox (M4).
#
# Trust model (advisory-reviewed): the image bakes VERIFIED artifacts; the
# launcher re-verifies against the baked allowlist at every container start
# (byte equality with the baked tree), so nothing trusts build time alone.
# Policy/code run read-only; only the mounted workspace is writable.
#
# Starts the deterministic loopback mock provider, seeds the agent dir with
# models.json pointing at it, then execs the sandbox launcher.
#
# Usage: entrypoint.sh <profile> <task-file> [--model <provider/id>]
#   (docker maps the workspace at /work and the task file anywhere readable)
set -eu
SANDBOX_ROOT=/opt/anvil-sandbox
WORK=/work
SEED_DIR=/seed
MOCK_PORT=${MOCK_PORT:-8765}
MOCK_SEQUENCE_FILE=${MOCK_SEQUENCE_FILE:-$SANDBOX_ROOT/dogfood-sequence.json}
MODEL=${MOCK_MODEL:-sandbox-mock/steerer}
export MOCK_PORT MOCK_SEQUENCE_FILE

if [ "$#" -lt 2 ]; then
  echo "usage: entrypoint.sh <profile> <task-file>" >&2
  exit 2
fi
PROFILE="$1"
TASK_FILE="$2"

if [ ! -f "$TASK_FILE" ]; then
  echo "entrypoint: task file not found: $TASK_FILE" >&2
  exit 2
fi
if [ ! -d "$WORK" ]; then
  echo "entrypoint: workspace not mounted at $WORK" >&2
  exit 2
fi

# Build-time verification ran in the Dockerfile; re-verify here so drift
# between build layers (or a tampered layer) fails the start, not the child.
node "$SANDBOX_ROOT/scripts/pi-sandbox-policy.mjs" validate "$SANDBOX_ROOT/packaging/pi/sandbox/allowlist.json"

# Fresh agent-dir seed: models.json wiring the loopback mock provider. Written
# at start (not baked) so the port is a runtime choice; contents are fixed.
mkdir -p "$SEED_DIR"
cat > "$SEED_DIR/models.json" <<EOF
{
  "providers": {
    "sandbox-mock": {
      "baseUrl": "http://127.0.0.1:${MOCK_PORT}/v1",
      "api": "openai-completions",
      "apiKey": "sandbox-mock",
      "compat": { "supportsDeveloperRole": false, "supportsReasoningEffort": false },
      "models": [{ "id": "steerer", "name": "Sandbox Steerer" }]
    }
  }
}
EOF

# Loopback-only mock provider: reachable inside --network none, unreachable
# from outside the container namespace.
node "$SANDBOX_ROOT/packaging/pi/sandbox/mock-llm.mjs" &
MOCK_PID=$!
cleanup() { kill "$MOCK_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

# Wait for the mock to accept connections (bounded, no external tools).
i=0
until node -e "fetch('http://127.0.0.1:${MOCK_PORT}/v1/chat/completions',{method:'POST',headers:{'content-type':'application/json'},body:'{}'}).then(()=>process.exit(0),()=>process.exit(1))" 2>/dev/null; do
  i=$((i + 1))
  [ "$i" -gt 50 ] && { echo "entrypoint: mock provider did not come up" >&2; exit 3; }
  sleep 0.2
done

exec node "$SANDBOX_ROOT/scripts/pi-sandbox-launch.mjs" \
  --profile "$PROFILE" \
  --workspace "$WORK" \
  --task-file "$TASK_FILE" \
  --allowlist "$SANDBOX_ROOT/packaging/pi/sandbox/allowlist.json" \
  --seed-dir "$SEED_DIR" \
  --model "$MODEL"