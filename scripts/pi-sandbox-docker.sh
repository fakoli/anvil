#!/bin/sh
# pi-sandbox-docker.sh — host-side wrapper: run a sandbox profile inside the
# anvil-pi-sandbox image.
#
# Usage:
#   scripts/pi-sandbox-docker.sh [--build] [--config FILE] <profile> <task-file> <workspace>
#
# Run config (optional): knobs that were previously hardcoded — image, network
# posture, capability preset, max concurrent containers. Resolved and validated
# fail-closed by scripts/pi-sandbox-config.mjs:
#   --config FILE or ~/.config/anvil/sandbox.config.json  — trusted scope, all fields
#   <workspace>/.pi/sandbox.config.json                   — max_containers only
# Precedence: --config > user config > defaults; env ANVIL_SANDBOX_IMAGE still
# wins over any configured image (explicit-intent escape hatch, existing behavior).
#
# Enforcement (defense in depth on top of the baked allowlist):
#   --network none   : no egress at all; loopback providers (mock) still work
#   --read-only      : rootfs read-only; /tmp + agent dirs are tmpfs
#   workspace mount  : the ONLY writable path (bind, rprivate)
#   task mount       : read-only
#   --pids-limit 256 : process-spread ceiling
#   no docker socket, no extra caps, no host network/IPC/PID namespaces
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
IMAGE=${ANVIL_SANDBOX_IMAGE:-}
BUILD=0
CONFIG_FLAG=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --build) BUILD=1; shift ;;
    --config)
      [ "$#" -ge 2 ] || { echo "pi-sandbox-docker: --config requires a file argument" >&2; exit 2; }
      CONFIG_FLAG=$2
      shift 2
      ;;
    --*) echo "pi-sandbox-docker: unknown option $1" >&2; exit 2 ;;
    *) break ;;
  esac
done
[ "$#" -eq 3 ] || {
  echo "usage: pi-sandbox-docker.sh [--build] [--config FILE] <profile> <task-file> <workspace>" >&2
  exit 2
}
PROFILE="$1"; TASK_FILE="$2"; WORKSPACE="$3"

command -v docker >/dev/null 2>&1 || { echo "pi-sandbox-docker: docker not found" >&2; exit 4; }
command -v node >/dev/null 2>&1 || { echo "pi-sandbox-docker: node not found (required for run-config resolution)" >&2; exit 4; }
[ -f "$TASK_FILE" ] || { echo "pi-sandbox-docker: task file not found: $TASK_FILE" >&2; exit 2; }
[ -d "$WORKSPACE" ] || { echo "pi-sandbox-docker: workspace not found: $WORKSPACE" >&2; exit 2; }
# Safe-workspace policy (advisory F7): reject the sensitive roots AND their
# descendants — a bind mount of /run could expose a host docker socket.
case "$(cd -- "$WORKSPACE" && pwd -P)" in \
  /|/bin|/boot|/dev|/etc|/lib|/lib32|/lib64|/libx32|/proc|/root|/run|/sbin|/sys|/usr|/var\
  |/bin/*|/boot/*|/dev/*|/etc/*|/lib/*|/lib32/*|/lib64/*|/libx32/*|/proc/*|/root/*|/run/*|/sbin/*|/sys/*|/usr/*|/var/*)
    echo "pi-sandbox-docker: refusing sensitive workspace root (mount a project dir)" >&2; exit 2;;
esac
# The stated non-root guarantee: never map the container to host root.
if [ "$(id -u)" = "0" ]; then
  echo "pi-sandbox-docker: refusing to run as root (host uid 0 maps to container root)" >&2
  exit 2
fi

# ---- run-config resolution (fail-closed; validator prints KEY<TAB>VALUE) ----
# Same early env-neutralization as pi-sandbox-run.sh: NODE_OPTIONS/LD_* are
# consumed at node process startup — sanitizing inside the script would be late.
unset NODE_OPTIONS NODE_PATH LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT \
      DYLD_INSERT_LIBRARIES DYLD_LIBRARY_PATH BASH_ENV ENV \
      PYTHONSTARTUP PYTHONPATH ZDOTDIR RUBYOPT
tab=$(printf '\t')
set -- resolve --profile "$PROFILE" --workspace "$WORKSPACE" \
  --allowlist "$script_dir/../packaging/pi/sandbox/allowlist.json"
if [ -n "$CONFIG_FLAG" ]; then
  set -- "$@" --config "$CONFIG_FLAG"
fi
CONFIG_OUT=$(node "$script_dir/pi-sandbox-config.mjs" "$@") || {
  echo "pi-sandbox-docker: run-config resolution failed (exit $?)" >&2
  exit 2
}
CONFIG_IMAGE=""; CONFIG_NETWORK=""; CONFIG_CAPS=""; CONFIG_MAX=""; CONFIG_SOURCE=""
while IFS= read -r line; do
  [ -n "$line" ] || continue
  key=${line%%"$tab"*}
  val=${line#*"$tab"}
  if [ "$key" = "$line" ] || { [ -z "$val" ] && [ "$key" != "IMAGE" ] && [ "$key" != "MAX_CONTAINERS" ]; }; then
    echo "pi-sandbox-docker: malformed config output line: $line" >&2
    exit 2
  fi
  case "$key" in
    IMAGE) CONFIG_IMAGE=$val ;;
    NETWORK) CONFIG_NETWORK=$val ;;
    CAPS) CONFIG_CAPS=$val ;;
    MAX_CONTAINERS) CONFIG_MAX=$val ;;
    CONFIG_SOURCE) CONFIG_SOURCE=$val ;;
    *) echo "pi-sandbox-docker: unexpected config key: $key" >&2; exit 2 ;;
  esac
done <<EOF
$CONFIG_OUT
EOF

# Defense-in-depth: never trust the composed values blindly.
[ "$CONFIG_NETWORK" = "none" ] || { echo "pi-sandbox-docker: only network \"none\" is supported on the docker path (got $CONFIG_NETWORK)" >&2; exit 2; }
case "$CONFIG_CAPS" in
  all-dropped|docker-default) ;;
  *) echo "pi-sandbox-docker: invalid caps preset: $CONFIG_CAPS" >&2; exit 2 ;;
esac
case "$CONFIG_MAX" in
  ""|[1-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-6]) ;;
  *) echo "pi-sandbox-docker: invalid max_containers: $CONFIG_MAX" >&2; exit 2 ;;
esac

# ---- image selection: env (explicit intent) > config (digest-pinned) > default
if [ -n "$IMAGE" ]; then
  : # env override, validated below
elif [ -n "$CONFIG_IMAGE" ]; then
  IMAGE=$CONFIG_IMAGE
else
  IMAGE=anvil-pi-sandbox
fi
# The image override is TRUSTED EXECUTABLE SELECTION (advisory F7): it controls
# the entrypoint and verifier. Validate the reference shape and require the
# caller to mean it — an option-shaped value can never smuggle docker flags.
case "$IMAGE" in
  -*|"" )
    echo "pi-sandbox-docker: image must be an image reference, not an option" >&2; exit 2;;
esac
case "$IMAGE" in
  *[a-zA-Z0-9]*) : ;;
  *) echo "pi-sandbox-docker: invalid image reference" >&2; exit 2;;
esac
case "$IMAGE" in
  *@sha256:[a-f0-9][a-f0-9]*|[a-zA-Z0-9][a-zA-Z0-9._/-]*|*[a-zA-Z0-9]:[a-zA-Z0-9_.-]*) : ;;
  *) echo "pi-sandbox-docker: image has unexpected characters: $IMAGE" >&2; exit 2;;
esac
# A digest-pinned image names fixed bytes: it cannot be a build target.
if [ "$BUILD" -eq 1 ] && case "$IMAGE" in *@sha256:*) true ;; *) false ;; esac; then
  echo "pi-sandbox-docker: --build cannot target a digest-pinned image ($IMAGE); build a tag and pin the digest in the run config" >&2
  exit 2
fi

WORK_ABS=$(cd -- "$WORKSPACE" && pwd -P)
TASK_ABS=$(cd -- "$(dirname -- "$TASK_FILE")" && pwd -P)/$(basename -- "$TASK_FILE")

if [ "$BUILD" -eq 1 ]; then
  repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
  docker build -f "$repo_root/packaging/pi/sandbox/Dockerfile" -t "$IMAGE" "$repo_root"
fi

# Match the host user so bind-mounted workspaces (host-owned) are writable by
# the child; tmpfs ownership below must follow. Keeps the non-root guarantee.
HOST_UID=$(id -u); HOST_GID=$(id -g)

# ---- max_containers guard (advisory: TOCTOU tolerated) ----------------------
if [ -n "$CONFIG_MAX" ]; then
  ps_ids=$(docker ps --filter label=anvil.sandbox=pi-sandbox --filter status=running -q) || {
    echo "pi-sandbox-docker: cannot verify running-container count (docker ps failed)" >&2
    exit 2
  }
  running=$(printf '%s' "$ps_ids" | grep -c . || true)
  if [ "$running" -ge "$CONFIG_MAX" ]; then
    echo "pi-sandbox-docker: refusing launch — $running sandbox container(s) already running, max_containers=$CONFIG_MAX" >&2
    exit 2
  fi
fi

echo "pi-sandbox-config: source=$CONFIG_SOURCE image=$IMAGE network=$CONFIG_NETWORK caps=$CONFIG_CAPS max_containers=${CONFIG_MAX:-unlimited}" >&2

set -- docker run \
  --rm \
  -i \
  --network "$CONFIG_NETWORK" \
  --read-only \
  --user "$HOST_UID:$HOST_GID" \
  --label anvil.sandbox=pi-sandbox \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,uid=$HOST_UID,gid=$HOST_GID,size=128m \
  --tmpfs /seed:rw,nosuid,nodev,uid=$HOST_UID,gid=$HOST_GID,size=8m \
  --tmpfs /home/fakoli:rw,nosuid,nodev,size=64m \
  --pids-limit 256 \
  --memory 2g \
  --ulimit nofile=256:256 \
  --security-opt no-new-privileges
if [ "$CONFIG_CAPS" = "all-dropped" ]; then
  set -- "$@" --cap-drop ALL
fi
set -- "$@" \
  --volume "$WORK_ABS:/work:rw" \
  --volume "$TASK_ABS:/task/task.txt:ro" \
  "$IMAGE" \
  "$PROFILE" \
  /task/task.txt
exec "$@"