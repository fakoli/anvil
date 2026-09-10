#!/bin/sh
# pi-sandbox-run.sh — thin shim for the node launcher (scripts/pi-sandbox-launch.mjs).
# Argument passthrough only: no eval, no composition, no interpolation.
# See packaging/pi/sandbox/README.md for the enforced guarantees and exit codes.
set -u
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
NODE_BIN=${NODE_BIN:-node}
command -v "$NODE_BIN" >/dev/null 2>&1 || { echo "pi-sandbox-run: node not found" >&2; exit 4; }
# Neutralize loader/runtime injection vectors BEFORE node starts: env
# sanitization inside the launcher would be too late for NODE_OPTIONS/LD_*,
# which node and ld.so consume at process startup.
unset NODE_OPTIONS NODE_PATH LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT \
      DYLD_INSERT_LIBRARIES DYLD_LIBRARY_PATH BASH_ENV ENV \
      PYTHONSTARTUP PYTHONPATH ZDOTDIR RUBYOPT
exec "$NODE_BIN" "$script_dir/pi-sandbox-launch.mjs" "$@"