#!/bin/sh
# pi-sandbox-docker.sh — host-side wrapper: run a sandbox profile inside the
# anvil-pi-sandbox image.
#
# Usage: scripts/pi-sandbox-docker.sh [--build] <profile> <task-file> <workspace>
#
# Enforcement (defense in depth on top of the baked allowlist):
#   --network none   : no egress at all; loopback providers (mock) still work
#   --read-only      : rootfs read-only; /tmp + agent dirs are tmpfs
#   workspace mount  : the ONLY writable path (bind, rprivate)
#   task mount       : read-only
#   --pids-limit 256 : process-spread ceiling
#   no docker socket, no extra caps, no host network/IPC/PID namespaces
set -eu
IMAGE=${ANVIL_SANDBOX_IMAGE:-anvil-pi-sandbox}
# The image override is TRUSTED EXECUTABLE SELECTION (advisory F7): it controls
# the entrypoint and verifier. Validate the reference shape and require the
# caller to mean it — an option-shaped value can never smuggle docker flags.
case "$IMAGE" in
  -*|"" )
    echo "pi-sandbox-docker: ANVIL_SANDBOX_IMAGE must be an image reference, not an option" >&2; exit 2;;
esac
case "$IMAGE" in
  *[a-zA-Z0-9]*) : ;;
  *) echo "pi-sandbox-docker: invalid ANVIL_SANDBOX_IMAGE" >&2; exit 2;;
esac
case "$IMAGE" in
  *@sha256:[a-f0-9][a-f0-9]*|[a-zA-Z0-9][a-zA-Z0-9._/-]*|*[a-zA-Z0-9]:[a-zA-Z0-9_.-]*) : ;;
  *) echo "pi-sandbox-docker: ANVIL_SANDBOX_IMAGE has unexpected characters: $IMAGE" >&2; exit 2;;
esac
BUILD=0
if [ "${1:-}" = "--build" ]; then BUILD=1; shift; fi
[ "$#" -eq 3 ] || { echo "usage: pi-sandbox-docker.sh [--build] <profile> <task-file> <workspace>" >&2; exit 2; }
PROFILE="$1"; TASK_FILE="$2"; WORKSPACE="$3"

command -v docker >/dev/null 2>&1 || { echo "pi-sandbox-docker: docker not found" >&2; exit 4; }
[ -f "$TASK_FILE" ] || { echo "pi-sandbox-docker: task file not found: $TASK_FILE" >&2; exit 2; }
[ -d "$WORKSPACE" ] || { echo "pi-sandbox-docker: workspace not found: $WORKSPACE" >&2; exit 2; }
# Safe-workspace policy (advisory F7): reject the sensitive roots AND their
# descendants — a bind mount of /run could expose a host docker socket.
case "$(cd -- "$WORKSPACE" && pwd -P)" in \
  /|/bin|/boot|/dev|/etc|/home|/lib|/lib32|/lib64|/libx32|/opt|/proc|/root|/run|/sbin|/srv|/sys|/usr|/var\
  |/bin/*|/boot/*|/dev/*|/etc/*|/lib/*|/lib32/*|/lib64/*|/libx32/*|/proc/*|/root/*|/run/*|/sbin/*|/sys/*|/usr/*|/var/*)
    echo "pi-sandbox-docker: refusing sensitive workspace root (mount a project dir)" >&2; exit 2;;
esac
# The stated non-root guarantee: never map the container to host root.
if [ "$(id -u)" = "0" ]; then
  echo "pi-sandbox-docker: refusing to run as root (host uid 0 maps to container root)" >&2
  exit 2
fi

if [ "$BUILD" -eq 1 ]; then
  repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
  docker build -f "$repo_root/packaging/pi/sandbox/Dockerfile" -t "$IMAGE" "$repo_root"
fi

WORK_ABS=$(cd -- "$WORKSPACE" && pwd -P)
TASK_ABS=$(cd -- "$(dirname -- "$TASK_FILE")" && pwd -P)/$(basename -- "$TASK_FILE")

# Match the host user so bind-mounted workspaces (host-owned) are writable by
# the child; tmpfs ownership below must follow. Keeps the non-root guarantee.
HOST_UID=$(id -u); HOST_GID=$(id -g)

exec docker run \
  --rm \
  -i \
  --network none \
  --read-only \
  --user "$HOST_UID:$HOST_GID" \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,uid=$HOST_UID,gid=$HOST_GID,size=128m \
  --tmpfs /seed:rw,nosuid,nodev,uid=$HOST_UID,gid=$HOST_GID,size=8m \
  --tmpfs /home/fakoli:rw,nosuid,nodev,size=64m \
  --pids-limit 256 \
  --memory 2g \
  --ulimit nofile=256:256 \
  --security-opt no-new-privileges \
  --cap-drop ALL \
  --volume "$WORK_ABS:/work:rw" \
  --volume "$TASK_ABS:/task/task.txt:ro" \
  "$IMAGE" \
  "$PROFILE" \
  /task/task.txt