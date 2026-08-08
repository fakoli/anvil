"""Trusted launch gate for the SessionStart PATH version probe."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time


def _terminate_posix_target_group(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def main() -> int:
    """Launch only after the parent has placed this worker in containment."""
    request_line = sys.stdin.buffer.readline(4_097)
    if len(request_line) > 4_096 or not request_line.endswith(b"\n"):
        return 2
    if sys.stdin.buffer.read(1):
        return 2
    try:
        request = json.loads(request_line)
        executable = request["executable"]
        parent_pid = request["parent_pid"]
        if (
            not isinstance(executable, str)
            or not isinstance(parent_pid, int)
            or isinstance(parent_pid, bool)
            or parent_pid <= 0
            or set(request) != {"executable", "parent_pid"}
        ):
            return 2
    except (json.JSONDecodeError, KeyError, TypeError):
        return 2
    if os.name != "nt" and os.getppid() != parent_pid:
        return 124
    process: subprocess.Popen[bytes] | None = None
    previous_signal_mask: set[signal.Signals] | None = None
    mask_fn = getattr(signal, "pthread_sigmask", None)
    try:
        if os.name != "nt" and mask_fn is not None:
            previous_signal_mask = mask_fn(signal.SIG_BLOCK, {signal.SIGTERM})
        try:
            process = subprocess.Popen(
                [executable, "--version"],
                stdin=subprocess.DEVNULL,
                start_new_session=os.name != "nt",
            )
        except (OSError, ValueError):
            return 127
        if os.name != "nt":
            target = process

            def terminate_for_parent(_signum: int, _frame: object) -> None:
                _terminate_posix_target_group(target)
                raise SystemExit(124)

            signal.signal(signal.SIGTERM, terminate_for_parent)
            if previous_signal_mask is not None and mask_fn is not None:
                mask_fn(signal.SIG_SETMASK, previous_signal_mask)
                previous_signal_mask = None
        while process.poll() is None:
            if os.name != "nt" and os.getppid() != parent_pid:
                _terminate_posix_target_group(process)
                process.kill()
                return 124
            time.sleep(0.05)
        return int(process.returncode)
    finally:
        if process is not None:
            _terminate_posix_target_group(process)
        if previous_signal_mask is not None and mask_fn is not None:
            mask_fn(signal.SIG_SETMASK, previous_signal_mask)


if __name__ == "__main__":
    raise SystemExit(main())
