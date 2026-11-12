"""Bounded process isolation for raw SQLite file descriptors.

Closing any raw DB descriptor can release another SQLite connection's POSIX
locks in the same process. Only the owned worker opens raw database files;
transport failure never falls back to the caller process.
"""
from __future__ import annotations

import json
import os
from typing import Any

MAX_SCHEMA_PROBE_RESPONSE_BYTES = 4_096
MAX_SQLITE_SCHEMA_VERSION = 2_147_483_647
SCHEMA_PROBE_TIMEOUT_SECONDS = 2.0
FILE_OPERATION_TIMEOUT_SECONDS = 10.0


class BoundedFileInspection:
    """Own one bounded child for finite schema, copy and durability operations."""

    def __init__(self, timeout_seconds: float | None = None) -> None:
        self._process: Any = None
        self._cleanup_pending = False
        self._transport_threads: list[Any] = []
        self._remaining_seconds = (
            FILE_OPERATION_TIMEOUT_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        )

    @property
    def closed(self) -> bool:
        """Whether this probe owns no live worker process."""
        return self._process is None or (
            not self._cleanup_pending and self._process.poll() is not None
            and not any(thread.is_alive() for thread in self._transport_threads)
        )

    def close(self) -> None:
        """Terminate and reap the probe worker, including all pipe handles."""
        import subprocess

        process = self._process
        if process is None:
            return

        first_failure: BaseException | None = None

        def remember(exc: BaseException) -> None:
            nonlocal first_failure
            if first_failure is None:
                first_failure = exc

        try:
            alive = process.poll() is None
        except BaseException as exc:
            remember(exc)
            alive = True

        if alive:
            try:
                process.terminate()
            except BaseException as exc:
                remember(exc)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except BaseException as exc:
                    remember(exc)
                try:
                    process.wait(timeout=1)
                except BaseException as exc:
                    remember(exc)
            except BaseException as exc:
                # Cancellation may arrive before wait reaps the child. Escalate
                # cleanup, then restore the cancellation after every resource
                # has received a release attempt.
                remember(exc)
                try:
                    process.kill()
                except BaseException as kill_exc:
                    remember(kill_exc)
                try:
                    process.wait(timeout=1)
                except BaseException as wait_exc:
                    remember(wait_exc)
        try:
            stopped = process.poll() is not None
        except BaseException as exc:
            remember(exc)
            stopped = False
        if not stopped:
            # Retain the exact owner. Closing buffered pipes here can itself
            # block behind a request/reader thread still waiting on this child.
            # A later call must retry cleanup, never reuse or replace it.
            self._cleanup_pending = True
            from anvil.state.backend import SchemaProbeFailed

            raise SchemaProbeFailed(
                "File inspection worker cleanup is incomplete; ownership is retained."
            ) from first_failure
        for thread in self._transport_threads:
            try:
                if thread.ident is not None:
                    thread.join(timeout=1)
            except BaseException as exc:
                remember(exc)
        if any(thread.is_alive() for thread in self._transport_threads):
            self._cleanup_pending = True
            from anvil.state.backend import SchemaProbeFailed

            raise SchemaProbeFailed(
                "File inspection transport cleanup is incomplete; ownership is retained."
            ) from first_failure
        self._transport_threads.clear()
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except BaseException as exc:
                    remember(exc)
        self._process = None
        self._cleanup_pending = False
        if first_failure is not None:
            raise first_failure

    def _close_without_masking(self) -> None:
        """Release probe resources without replacing an existing failure."""
        try:
            self.close()
        except BaseException:
            pass

    def _start_worker(self) -> Any:
        import subprocess
        import sys

        if self._cleanup_pending:
            self.close()
        if not self.closed:
            return self._process
        self.close()
        self._process = subprocess.Popen(
            [sys.executable, "-m", "anvil.state._file_inspection_worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return self._process

    def __call__(self, db_path: str | os.PathLike[str]) -> int:
        """Probe with a cleanup boundary that includes cancellation."""
        try:
            result = self._call_worker({"path": os.fspath(db_path)}, schema=True)
            assert isinstance(result, int)
            return result
        except BaseException as exc:
            self._close_without_masking()
            if self._cleanup_pending:
                exc.add_note("File inspection worker cleanup is incomplete; ownership is retained.")
            raise

    def _call_worker(self, request: dict[str, object], *, schema: bool) -> int | None:
        """Run one side-effect-free probe within the remaining wait budget."""
        import threading
        import time

        from anvil.state.backend import SchemaMismatch, SchemaProbeFailed

        if self._remaining_seconds <= 0:
            raise SchemaProbeFailed(
                "Database schema inspection exceeded the transport time bound."
            )
        self._transport_threads = [
            thread for thread in self._transport_threads if thread.is_alive()
        ]
        started = time.monotonic()
        try:
            process = self._start_worker()
        except (OSError, ValueError):
            self._close_without_masking()
            raise SchemaProbeFailed("Database schema inspection failed.") from None
        self._remaining_seconds = max(
            0.0,
            self._remaining_seconds - (time.monotonic() - started),
        )
        if self._remaining_seconds <= 0:
            self._close_without_masking()
            raise SchemaProbeFailed(
                "Database schema inspection exceeded the transport time bound."
            )
        if process.stdin is None or process.stdout is None:
            self._close_without_masking()
            raise SchemaProbeFailed("Database schema inspection failed.")

        request_errors: list[BaseException] = []

        def write_request() -> None:
            try:
                process.stdin.write(
                    json.dumps(request, ensure_ascii=True) + "\n"
                )
                process.stdin.flush()
            except BaseException as exc:  # transported to the caller thread
                request_errors.append(exc)

        writer = threading.Thread(target=write_request, name="anvil-schema-request")
        self._transport_threads.append(writer)
        started = time.monotonic()
        try:
            writer.start()
            writer.join(self._remaining_seconds)
        except BaseException:
            self._close_without_masking()
            if writer.ident is not None:
                writer.join(timeout=1)
            raise
        self._remaining_seconds = max(
            0.0,
            self._remaining_seconds - (time.monotonic() - started),
        )
        if writer.is_alive():
            self._close_without_masking()
            writer.join(timeout=1)
            raise SchemaProbeFailed(
                "Database schema inspection exceeded the transport time bound."
            ) from None
        if request_errors:
            self._close_without_masking()
            raise SchemaProbeFailed("Database schema inspection failed.") from None
        if self._remaining_seconds <= 0:
            self._close_without_masking()
            raise SchemaProbeFailed(
                "Database schema inspection exceeded the transport time bound."
            )

        response: list[str] = []
        response_errors: list[BaseException] = []

        def read_response() -> None:
            try:
                response.append(
                    process.stdout.readline(MAX_SCHEMA_PROBE_RESPONSE_BYTES + 1)
                )
            except BaseException as exc:  # transported to the caller thread
                response_errors.append(exc)

        reader = threading.Thread(target=read_response, name="anvil-schema-response")
        self._transport_threads.append(reader)
        started = time.monotonic()
        try:
            reader.start()
            reader.join(self._remaining_seconds)
        except BaseException:
            self._close_without_masking()
            if reader.ident is not None:
                reader.join(timeout=1)
            raise
        self._remaining_seconds = max(
            0.0,
            self._remaining_seconds - (time.monotonic() - started),
        )
        if reader.is_alive():
            self._close_without_masking()
            reader.join(timeout=1)
            raise SchemaProbeFailed(
                "Database schema inspection exceeded the transport time bound."
            ) from None
        if response_errors or not response or not response[0]:
            self._close_without_masking()
            raise SchemaProbeFailed("Database schema inspection failed.")
        frame = response[0]
        if (
            not frame.endswith("\n")
            or len(frame.encode("utf-8")) > MAX_SCHEMA_PROBE_RESPONSE_BYTES
        ):
            self._close_without_masking()
            raise SchemaProbeFailed(
                "Database schema inspection returned invalid data."
            )
        try:
            payload = json.loads(frame)
        except (json.JSONDecodeError, TypeError):
            self._close_without_masking()
            raise SchemaProbeFailed(
                "Database schema inspection returned invalid data."
            ) from None
        if not isinstance(payload, dict):
            self._close_without_masking()
            raise SchemaProbeFailed("Database schema inspection returned invalid data.")

        def valid_schema_version(value: object) -> bool:
            return (
                isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value <= MAX_SQLITE_SCHEMA_VERSION
            )

        keys = set(payload)
        if not schema:
            if keys == {"ok"} and payload["ok"] is True:
                return None
            self._close_without_masking()
            raise SchemaProbeFailed("Isolated database file operation failed.")
        if keys == {"ok", "version"} and payload["ok"] is True:
            version = payload["version"]
            if valid_schema_version(version):
                return int(version)
            self._close_without_masking()
            raise SchemaProbeFailed("Database schema inspection returned invalid data.")
        if (
            keys == {"ok", "code"}
            and payload["ok"] is False
            and payload["code"] == "schema_probe_failed"
        ):
            self._close_without_masking()
            raise SchemaProbeFailed("Database schema inspection failed.")
        if keys == {"ok", "code", "actual", "expected", "direction"}:
            actual = payload["actual"]
            expected = payload["expected"]
            direction = payload["direction"]
            if (
                payload["ok"] is False
                and payload["code"] == "schema_mismatch"
                and valid_schema_version(actual)
                and valid_schema_version(expected)
                and (
                    (direction == "newer" and actual > expected)
                    or (direction == "older" and actual < expected)
                )
            ):
                self._close_without_masking()
                raise SchemaMismatch(
                    actual=int(actual),
                    expected=int(expected),
                    direction=direction,
                )
        self._close_without_masking()
        raise SchemaProbeFailed("Database schema inspection failed.")

    def __enter__(self) -> BoundedFileInspection:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is None:
            self.close()
        else:
            self._close_without_masking()

    def _operation(self, operation: str, **paths: object) -> None:
        try:
            self._call_worker({"operation": operation, **paths}, schema=False)
        except BaseException as exc:
            self._close_without_masking()
            if self._cleanup_pending:
                exc.add_note("File inspection worker cleanup is incomplete; ownership is retained.")
            raise

    def copy_database(
        self, source: str | os.PathLike[str], destination: str | os.PathLike[str],
    ) -> None:
        self._operation(
            "copy_database", source=os.fspath(source), destination=os.fspath(destination),
        )

    def copy_tree(
        self, source: str | os.PathLike[str], destination: str | os.PathLike[str],
    ) -> None:
        self._operation("copy_tree", source=os.fspath(source), destination=os.fspath(destination))

    def fsync_files(self, paths: list[str | os.PathLike[str]]) -> None:
        self._operation("fsync", paths=[os.fspath(path) for path in paths])


class BoundedSchemaProbe(BoundedFileInspection):
    """Share the original cumulative two-second budget across schema probes."""

    def __init__(self, timeout_seconds: float | None = None) -> None:
        super().__init__(
            SCHEMA_PROBE_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
        )
