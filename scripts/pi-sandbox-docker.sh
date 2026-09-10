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
BUILD=0
if [ "${1:-}" = "--build" ]; then BUILD=1; shift; fi
[ "$#" -eq 3 ] || { echo "usage: pi-sandbox-docker.sh [--build] <profile> <task-file> <workspace>" >&2; exit 2; }
PROFILE="$1"; TASK_FILE="$2"; WORKSPACE="$3"

command -v docker >/dev/null 2>&1 || { echo "pi-sandbox-docker: docker not found" >&2; exit 4; }
[ -f "$TASK_FILE" ] || { echo "pi-sandbox-docker: task file not found: $TASK_FILE" >&2; exit 2; }
[ -d "$WORKSPACE" ] || { echo "pi-sandbox-docker: workspace not found: $WORKSPACE" >&2; exit 2; }
case "$(cd -- "$WORKSPACE" && pwd -P)" in /|/home|/etc|/usr|/var|/bin|/sbin|/lib*|/boot|/dev|/proc|/sys)
  echo "pi-sandbox-docker: refusing dangerous workspace" >&2; exit 2;;
esac

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
  --ulimit nofile=256:256 \
  --security-opt no-new-privileges \
  --cap-drop ALL \
  --volume "$WORK_ABS:/work:rw" \
  --volume "$TASK_ABS:/task/task.txt:ro" \
  "$IMAGE" \
  "$PROFILE" \
  /task/task.txt