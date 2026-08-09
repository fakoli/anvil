"""``anvil scan`` — brownfield ingest of an existing repo (backlog T008).

Walks the existing working tree, persists a re-scannable *codebase model* in its
own SQLite db (``.anvil/scan.db``), and — on the first scan of a project
with no PRD yet — synthesises a draft ``prd.md`` plus an initial feature/task
graph by driving the same offline parse → plan → score → review pipeline that
``init --with-sample`` uses. Re-running ``scan`` reconciles against the persisted
model and reports the delta (added / removed / changed files) instead of
overwriting the seeded graph.

``init --from-repo`` is the convenience entry point: it scaffolds
``.anvil/`` (like a bare ``init``) and then immediately runs this scan.

Both surfaces honour the v1.24 conventions: ``ANVIL_ROOT`` resolution
via the shared helpers and a single ``--json`` envelope when ``--json`` is set.
"""

from __future__ import annotations

import errno
import os
import secrets
import shutil
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from anvil.cli._helpers import (
    PrdSourceIngestError,
    StateRootError,
    _open_backend,
    _resolve_project_dir,
    _resolve_state_dir,
    ingest_prd_source,
)
from anvil.cli._json import JSON_OPTION, emit_success, fail
from anvil.cli._sample import SampleSeedError

if TYPE_CHECKING:
    from anvil.cli._helpers import IngestedPrdSource
    from anvil.scan.model import CodebaseModel, ScanDelta
    from anvil.state.sqlite import SqliteBackend

__all__ = ["scan"]

_COMMAND = "scan"
_PRD_FILENAME = "prd.md"
_SOURCE_MISSING = object()
_SCAN_RECOVERY_DIRECTORY = "recovery"
_SCAN_RECOVERY_PREFIX = "scan-"
_SCAN_RETIRED_PREFIX = ".retired-scan-"
_SCAN_ARTIFACT_NAMES = ("scan.db", _PRD_FILENAME)
_SCAN_KEEP_MARKER = b"state-bound\n"
_SCAN_COMMITTED_MARKER = b"committed\n"
_SCAN_COMMITTED_NAME = "committed"
_SCAN_LOCK_FILENAME = ".scan.lock"
_SCAN_LOCK_TIMEOUT_SECONDS = 5.0
_ABSENT_MARKER = b"absent\n"
_COPY_BUFFER_SIZE = 1024 * 1024
_MAX_SCAN_RECOVERY_ENTRIES = 32
_SCAN_SESSION_LOCKS_GUARD = threading.Lock()
_SCAN_SESSION_LOCKS: dict[str, threading.RLock] = {}
_SCAN_SESSION_LOCAL = threading.local()


def _canonical_requirement_index(identifier: str) -> str | None:
    """Return the string-safe canonical numeric suffix of an ``R`` id."""
    if not identifier.startswith("R"):
        return None
    suffix = identifier[1:]
    if not suffix.isdigit():
        return None
    return suffix.lstrip("0") or "0"


def _next_generated_requirement_start(identifiers: set[str], *, block_size: int) -> int:
    """Return the first free contiguous block of generated requirement ids."""
    if block_size < 1:
        raise ValueError("block_size must be positive")

    occupied: set[str] = set()
    for identifier in identifiers:
        canonical = _canonical_requirement_index(identifier)
        if canonical is not None:
            occupied.add(canonical)

    # Search only the small ids the generator can actually reach. No authored
    # suffix is converted to int: a genuinely huge value cannot collide with a
    # small candidate, while an arbitrarily zero-padded small value still does.
    start = 1
    while True:
        for offset in range(block_size):
            if str(start + offset) in occupied:
                # Every intervening start would contain this occupied value too.
                start += offset + 1
                break
        else:
            return start


def _reserve_atomic_capture(path: Path, replacement: Path) -> Path:
    """Return the known path that will receive the displaced live source."""
    if os.name != "nt":
        return replacement
    capture_fd, capture_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.captured.",
        suffix=".tmp",
    )
    os.close(capture_fd)
    captured = Path(capture_name)
    captured.unlink()
    return captured


def _atomic_capture_replace(path: Path, replacement: Path, captured: Path) -> None:
    """Atomically publish *replacement* and retain the displaced live path.

    Unlike ``os.replace``, this operation never destroys the destination inode:
    Windows uses ``ReplaceFileW`` with a backup name, while Linux and macOS use
    their atomic path-exchange primitives. Both names remain continuously
    present at the publication boundary.
    """
    import ctypes

    if os.name == "nt":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        replace_file = kernel32.ReplaceFileW
        replace_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPVOID,
        ]
        replace_file.restype = wintypes.BOOL
        if not replace_file(str(path), str(replacement), str(captured), 1, None, None):
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error), str(path))
        return

    if captured != replacement:
        raise OSError(errno.EINVAL, "exchange capture must be the replacement path")

    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOTSUP, "atomic path exchange is unavailable")
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        if renameat2(-100, os.fsencode(path), -100, os.fsencode(replacement), 2) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(path))
        return

    if sys.platform == "darwin":
        renamex_np = getattr(libc, "renamex_np", None)
        if renamex_np is None:
            raise OSError(errno.ENOTSUP, "atomic path exchange is unavailable")
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        if renamex_np(os.fsencode(path), os.fsencode(replacement), 2) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(path))
        return

    raise OSError(errno.ENOTSUP, "atomic path exchange is unavailable")


def _atomic_move_no_replace(source: Path, destination: Path) -> None:
    """Atomically move *source* only when *destination* is still absent."""
    import ctypes

    if os.name == "nt":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move_file = kernel32.MoveFileExW
        move_file.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move_file.restype = wintypes.BOOL
        if not move_file(str(source), str(destination), 0):
            error = ctypes.get_last_error()
            if error in {80, 183}:  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
                raise FileExistsError(errno.EEXIST, ctypes.FormatError(error), str(destination))
            raise OSError(error, ctypes.FormatError(error), str(source))
        return

    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOTSUP, "atomic no-replace move is unavailable")
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        if renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
            error = ctypes.get_errno()
            if error == errno.EEXIST:
                raise FileExistsError(error, os.strerror(error), str(destination))
            raise OSError(error, os.strerror(error), str(source))
        return

    if sys.platform == "darwin":
        renamex_np = getattr(libc, "renamex_np", None)
        if renamex_np is None:
            raise OSError(errno.ENOTSUP, "atomic no-replace move is unavailable")
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        if renamex_np(os.fsencode(source), os.fsencode(destination), 4) != 0:
            error = ctypes.get_errno()
            if error == errno.EEXIST:
                raise FileExistsError(error, os.strerror(error), str(destination))
            raise OSError(error, os.strerror(error), str(source))
        return

    raise OSError(errno.ENOTSUP, "atomic no-replace move is unavailable")


def _retain_displaced_source(path: Path, captured: Path) -> Path:
    """Move a displaced live inode to an explicit recovery path.

    There is no portable way to prove that an uncooperative writer has closed
    every handle to an inode. In particular, a writer may retain a writable,
    share-delete handle across the atomic exchange and write after our final
    byte check. Unlinking the displaced inode would silently discard that late
    write. Keep it named as an intentional recovery artifact instead; transient
    staging and ownership paths can then be removed without losing user data.
    """
    recovery_fd, recovery_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.recovery.",
        suffix=".bak",
    )
    os.close(recovery_fd)
    recovery = Path(recovery_name)
    try:
        os.replace(captured, recovery)
    except BaseException:
        # A wrapper can report cancellation after the atomic replace landed.
        # In that case ``captured`` is gone and ``recovery`` is the only name
        # preserving the displaced inode, so it must not be cleaned up.
        if captured.exists():
            try:
                recovery.unlink()
            except OSError:
                pass
        raise
    return recovery


def _path_matches_claim(path: Path, claim: Path, expected: bytes) -> bool:
    """Return whether *path* still names *claim* with the expected bytes.

    Check identity on both sides of the byte read. A pathname replacement can
    otherwise land between ``samefile`` and ``read_bytes`` and make a stale
    identity check authorize bytes read from an unrelated inode.
    """
    source = _read_claimed_prd_source(path, claim)
    return source is not None and source.source_bytes == expected


def _path_still_has_claimed_bytes(path: Path, expected: bytes) -> bool:
    """Return whether a cleanup target is still the unchanged claimed source.

    This deliberately avoids PRD parsing: cleanup must remove a failed staging
    file even when the existing source is syntactically incomplete. The bounded
    byte comparison is not used as a State authorization boundary.
    """
    try:
        with path.open("rb") as handle:
            current = handle.read(len(expected) + 1)
        return current == expected
    except OSError:
        return False


def _read_claimed_prd_source(path: Path, claim: Path) -> IngestedPrdSource | None:
    """Return one bounded snapshot only while *path* still names *claim*.

    The final identity check defines the portable publication handoff. A
    non-cooperating process can still mutate a file after this function
    returns; no portable pathname primitive can prevent that. Callers must use
    the returned snapshot, never older caller memory, for the state mutation
    authorized by this handoff.
    """
    try:
        if not path.samefile(claim):
            return None
        source = ingest_prd_source(
            path,
            containment_root=path.parent,
            required_parent=path.parent,
        )
        if not path.samefile(claim):
            return None
        return source
    except (OSError, PrdSourceIngestError):
        return None


def _fsync_prd_staging(descriptor: int) -> None:
    """Flush one atomic PRD staging file through an injectable boundary."""
    os.fsync(descriptor)


def _atomic_replace_prd(
    path: Path,
    content: bytes,
    *,
    operation: str,
    expected: bytes | object | None = None,
) -> IngestedPrdSource | None:
    """Publish *content* atomically if the destination still matches *expected*.

    A read-then-``os.replace`` check is not a compare-and-swap: another writer
    can land after the read and be overwritten by the replace.  For an existing
    source, hard-link its inode to a private same-directory ownership file while
    leaving the live pathname in place, verify both the claimed bytes and inode,
    then atomically exchange the staged and live paths. The displaced live inode
    remains named until it is verified against the ownership link. After that
    verification it is retained under an explicit recovery name: no portable
    primitive can prove that an uncooperative writer has closed every writable
    handle, so unlinking it would still risk silently discarding a late write.
    A hard process interruption leaves the canonical path continuously present,
    and both sides of every completed or interrupted publication recoverable.
    """
    from anvil.cli._sample import SampleSeedError

    fd: int | None = None
    temp_path: Path | None = None
    ownership_fd: int | None = None
    ownership_path: Path | None = None
    publication_fd: int | None = None
    publication_path: Path | None = None
    captured_path: Path | None = None
    displaced_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temp_path = Path(temp_name)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(content)
            handle.flush()
            _fsync_prd_staging(handle.fileno())

        if expected is _SOURCE_MISSING:
            try:
                os.link(temp_path, path)
            except FileExistsError:
                return None
            published = _read_claimed_prd_source(path, temp_path)
            if published is None or published.source_bytes != content:
                return None
            return published

        if expected is not None:
            ownership_fd, ownership_name = tempfile.mkstemp(
                dir=path.parent,
                prefix=f".{path.name}.owned.",
                suffix=".tmp",
            )
            ownership_path = Path(ownership_name)
            os.close(ownership_fd)
            ownership_fd = None
            ownership_path.unlink()
            try:
                os.link(path, ownership_path)
            except FileNotFoundError:
                return None

            owned_source = _read_claimed_prd_source(ownership_path, ownership_path)
            if owned_source is None or owned_source.source_bytes != expected:
                return None
            live_source = _read_claimed_prd_source(path, ownership_path)
            if live_source is None or live_source.source_bytes != expected:
                return None

            # Keep an independent name for the generated staging inode. The
            # atomic exchange publishes that inode at ``path``, but an
            # uncooperative writer may replace the canonical pathname before
            # validation finishes. Without this claim, validating only the
            # displaced authored inode could report success while ``path``
            # already names unrelated writer bytes.
            publication_fd, publication_name = tempfile.mkstemp(
                dir=path.parent,
                prefix=f".{path.name}.published.",
                suffix=".tmp",
            )
            publication_path = Path(publication_name)
            os.close(publication_fd)
            publication_fd = None
            publication_path.unlink()
            os.link(temp_path, publication_path)

            captured_path = _reserve_atomic_capture(path, temp_path)
            _atomic_capture_replace(path, temp_path, captured_path)
            temp_path = None
            captured_matches = _path_matches_claim(
                captured_path,
                ownership_path,
                expected,
            )
            published_matches = _path_matches_claim(
                path,
                publication_path,
                content,
            )
            if not published_matches:
                # A writer replaced the generated canonical path after the
                # exchange. Never exchange over that writer while rolling
                # back. Preserve the displaced prior inode for recovery and
                # fail closed so state is not seeded from stale memory.
                _retain_displaced_source(path, captured_path)
                captured_path = None
                return None
            if not captured_matches:
                displaced_path = _reserve_atomic_capture(path, captured_path)
                _atomic_capture_replace(path, captured_path, displaced_path)
                captured_path = None
                return None
            _retain_displaced_source(path, captured_path)
            captured_path = None
            # This bounded read is deliberately the last operation before the
            # snapshot is handed to state seeding. It catches writes through a
            # handle opened on the newly published inode during displaced-source
            # retention. Mutation after this return is the unavoidable external
            # writer boundary; the caller seeds only from this exact snapshot.
            published = _read_claimed_prd_source(path, publication_path)
            if published is None or published.source_bytes != content:
                return None
            ownership_path.unlink()
            ownership_path = None
            return published

        os.replace(temp_path, path)
        published = ingest_prd_source(
            path,
            containment_root=path.parent,
            required_parent=path.parent,
        )
        return published if published.source_bytes == content else None
    except OSError as exc:
        reason = exc.strerror or exc.__class__.__name__
        raise SampleSeedError(f"cannot {operation} PRD source: {reason}") from None
    finally:
        # Cancellation is intentionally allowed to propagate. If publication
        # landed before verification completed, atomically put the captured live
        # inode back. A hard kill instead leaves both versions named for recovery.
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if ownership_fd is not None:
            try:
                os.close(ownership_fd)
            except OSError:
                pass
        if publication_fd is not None:
            try:
                os.close(publication_fd)
            except OSError:
                pass
        if captured_path is not None:
            # Cancellation recovery must not perform an unbounded pathname
            # read while the seed lock is held. Verify both the generated
            # inode claim and bounded source bytes before exchanging it back.
            published_ours = publication_path is not None and _path_matches_claim(
                path,
                publication_path,
                content,
            )
            if published_ours:
                try:
                    displaced_path = _reserve_atomic_capture(path, captured_path)
                    _atomic_capture_replace(path, captured_path, displaced_path)
                    captured_path = None
                except BaseException:
                    # Both sources remain named. The caller's state check fails
                    # closed if restoration is ambiguous.
                    pass
        if ownership_path is not None:
            try:
                ownership_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        if publication_path is not None:
            try:
                publication_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        if displaced_path is not None:
            try:
                displaced_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        # On POSIX an exchange uses the staged temporary pathname as the
        # capture name. A BaseException can land after the exchange but before
        # the normal ``temp_path = None`` handoff below it. Retain that alias
        # only when the original ownership claim no longer names ``path``;
        # otherwise the exchange itself failed and this is ordinary staging
        # debris that must be removed.
        original_still_live = (
            ownership_path is not None
            and isinstance(expected, bytes)
            and _path_still_has_claimed_bytes(path, expected)
        )
        preserve_capture_alias = (
            temp_path is not None
            and temp_path == captured_path
            and not original_still_live
        )
        if temp_path is not None and not preserve_capture_alias:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _atomic_remove_prd(path: Path, *, expected: bytes, operation: str) -> bool:
    """Remove *path* only when an atomic ownership claim contains *expected*."""
    from anvil.cli._sample import SampleSeedError

    ownership_fd: int | None = None
    ownership_path: Path | None = None
    try:
        ownership_fd, ownership_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.owned.",
            suffix=".tmp",
        )
        ownership_path = Path(ownership_name)
        os.close(ownership_fd)
        ownership_fd = None
        try:
            os.replace(path, ownership_path)
        except FileNotFoundError:
            return False
        owned_source = _read_claimed_prd_source(ownership_path, ownership_path)
        if owned_source is None or owned_source.source_bytes != expected:
            try:
                _atomic_move_no_replace(ownership_path, path)
                ownership_path = None
            except FileExistsError:
                assert ownership_path is not None
                _retain_displaced_source(path, ownership_path)
                ownership_path = None
            return False
        # The claim itself removed the expected pathname.  A writer arriving
        # afterward creates a new pathname which cleanup never touches. Keep
        # the displaced inode under an explicit recovery name: a writer can
        # retain a share-delete handle and modify it after this byte check, so
        # unlinking it would silently discard those late bytes.
        _retain_displaced_source(path, ownership_path)
        ownership_path = None
        return True
    except OSError as exc:
        reason = exc.strerror or exc.__class__.__name__
        raise SampleSeedError(f"cannot {operation} PRD source: {reason}") from None
    finally:
        if ownership_fd is not None:
            try:
                os.close(ownership_fd)
            except OSError:
                pass
        if ownership_path is not None:
            try:
                _atomic_move_no_replace(ownership_path, path)
                ownership_path = None
            except FileExistsError:
                # A new writer owns the canonical path. Preserve the displaced
                # inode separately rather than replacing or discarding either.
                try:
                    assert ownership_path is not None
                    _retain_displaced_source(path, ownership_path)
                    ownership_path = None
                except OSError:
                    pass
            except OSError:
                try:
                    assert ownership_path is not None
                    _retain_displaced_source(path, ownership_path)
                    ownership_path = None
                except OSError:
                    pass


@contextmanager
def _exclusive_seed_session(backend: SqliteBackend) -> Iterator[None]:
    """Hold the canonical state lock across source publication and seeding.

    ``SqliteBackend.append`` normally takes ``_append_lock`` once per event. A
    scan seed spans several events plus one external source file, so releasing
    that lock between those steps leaves rollback vulnerable to another state
    or scan writer. Temporarily make this backend instance's lock re-entrant for
    the owning thread, then hold the real threading + cross-process file lock
    for the complete operation. Other threads still call the original lock and
    block normally; other processes contend on the same ``events.jsonl`` lock.
    """
    dynamic_backend: Any = backend
    original_lock = dynamic_backend._append_lock  # noqa: SLF001
    ownership = threading.local()

    @contextmanager
    def reentrant_lock() -> Iterator[None]:
        if getattr(ownership, "held", False):
            yield
            return
        with original_lock():
            ownership.held = True
            try:
                yield
            finally:
                ownership.held = False

    dynamic_backend._append_lock = reentrant_lock  # noqa: SLF001
    try:
        with reentrant_lock():
            yield
    finally:
        dynamic_backend._append_lock = original_lock  # noqa: SLF001


def _restore_prd_source_if_unchanged(
    path: Path,
    *,
    generated: bytes,
    prior: bytes | None,
) -> None:
    """Undo a pre-state-change publication without clobbering another writer."""
    from anvil.cli._sample import SampleSeedError

    try:
        current = ingest_prd_source(
            path,
            containment_root=path.parent,
            required_parent=path.parent,
        )
    except PrdSourceIngestError as exc:
        if exc.code == "source_not_found":
            return
        raise SampleSeedError(
            "cannot inspect PRD source after seed failure: "
            f"{exc.format_message()}"
        ) from None

    # A concurrent writer replaced or edited the published draft. It is not
    # ours to restore or remove.
    if current.source_bytes != generated:
        return
    if prior is not None:
        _atomic_replace_prd(
            path,
            prior,
            operation="restore prior",
            expected=generated,
        )
        return
    _atomic_remove_prd(
        path,
        expected=generated,
        operation="remove generated after seed failure",
    )


def _write_generated_prd(_path: Path, _prd_text: str) -> None:
    """Injectable boundary immediately after atomic PRD publication."""


class ScanArtifactError(SampleSeedError):
    """A scan artifact update failed after rollback completed."""

    def __init__(self) -> None:
        super().__init__(
            "scan artifact update failed; prior artifacts were restored",
            code="scan_artifact_error",
        )


class ScanRecoveryError(SampleSeedError):
    """Scan artifact rollback is incomplete and durable backups remain."""

    def __init__(self, token: str) -> None:
        self.token = token
        super().__init__(
            "scan artifact recovery incomplete; backups retained at "
            f"recovery/{token}",
            code="scan_recovery_incomplete",
        )


class ScanLockedError(SampleSeedError):
    """Another process or thread already owns this project's scan session."""

    def __init__(self) -> None:
        super().__init__(
            "another scan is already running; retry after it finishes",
            code="scan_locked",
        )


def _scan_process_lock(state_dir: Path) -> tuple[str, threading.RLock]:
    """Return the same-process lock for one canonical state directory."""
    canonical = os.path.normcase(os.path.realpath(state_dir))
    with _SCAN_SESSION_LOCKS_GUARD:
        return canonical, _SCAN_SESSION_LOCKS.setdefault(canonical, threading.RLock())


@contextmanager
def _exclusive_scan_session(state_dir: Path) -> Iterator[None]:
    """Serialize scan recovery and mutation across threads and processes."""
    canonical, process_lock = _scan_process_lock(state_dir)
    owned: set[str] = getattr(_SCAN_SESSION_LOCAL, "owned", set())
    _SCAN_SESSION_LOCAL.owned = owned
    if canonical in owned:
        yield
        return

    with process_lock:
        lock_path = state_dir / _SCAN_LOCK_FILENAME
        descriptor: int | None = None
        handle: Any | None = None
        try:
            _require_safe_directory(state_dir)
            _require_direct_child(lock_path, state_dir)
            flags = (
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(lock_path, flags, stat.S_IRUSR | stat.S_IWUSR)
            path_metadata = lock_path.lstat()
            descriptor_metadata = os.fstat(descriptor)
            if (
                _is_reparse_or_symlink(path_metadata)
                or not stat.S_ISREG(path_metadata.st_mode)
                or not stat.S_ISREG(descriptor_metadata.st_mode)
                or (path_metadata.st_dev, path_metadata.st_ino)
                != (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
            ):
                raise OSError("unsafe scan lock")
            handle = os.fdopen(descriptor, "r+b")
            descriptor = None
        except OSError:
            if descriptor is not None:
                os.close(descriptor)
            raise ScanArtifactError() from None

        from anvil.state import sqlite as sqlite_module

        try:
            deadline = time.monotonic() + _SCAN_LOCK_TIMEOUT_SECONDS
            delays = sqlite_module._flock_backoff_delays()  # noqa: SLF001
            while True:
                try:
                    sqlite_module._append_lock_acquire_nb(handle)  # noqa: SLF001
                    break
                except OSError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ScanLockedError() from None
                    time.sleep(min(next(delays), remaining))
            owned.add(canonical)
            try:
                yield
            finally:
                owned.remove(canonical)
                sqlite_module._append_lock_release(handle)  # noqa: SLF001
        finally:
            handle.close()


def _new_scan_recovery_token() -> str:
    """Return one opaque, filesystem-safe recovery token."""
    return _SCAN_RECOVERY_PREFIX + secrets.token_hex(8)


def _is_scan_recovery_token(token: str) -> bool:
    suffix = token.removeprefix(_SCAN_RECOVERY_PREFIX)
    return (
        token.startswith(_SCAN_RECOVERY_PREFIX)
        and len(suffix) == 16
        and all(character in "0123456789abcdef" for character in suffix)
    )


def _write_durable_marker(path: Path) -> None:
    """Create and flush a fixed recovery marker before artifact mutation."""
    with path.open("xb") as marker:
        marker.write(_ABSENT_MARKER)
        marker.flush()
        os.fsync(marker.fileno())


def _fsync_file(path: Path) -> None:
    # Windows requires a writable descriptor for FlushFileBuffers/fsync.
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _mark_scan_artifact_state_bound(
    recovery_root: Path,
    artifact_name: str,
) -> None:
    """Durably exempt a state-bound artifact from rollback."""
    if artifact_name not in _SCAN_ARTIFACT_NAMES:
        raise OSError("artifact is not allowlisted")
    marker = recovery_root / f"{artifact_name}.state-bound"
    with marker.open("xb") as handle:
        handle.write(_SCAN_KEEP_MARKER)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(recovery_root)


def _fsync_directory(path: Path) -> None:
    """Flush directory entries where the host exposes directory descriptors."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _path_lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _is_reparse_or_symlink(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _require_safe_directory(path: Path) -> None:
    metadata = _path_lstat(path)
    if (
        metadata is None
        or _is_reparse_or_symlink(metadata)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise OSError("unsafe recovery directory")


def _require_safe_regular(path: Path, *, allow_missing: bool = False) -> bool:
    metadata = _path_lstat(path)
    if metadata is None:
        if allow_missing:
            return False
        raise OSError("missing recovery file")
    if _is_reparse_or_symlink(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise OSError("unsafe recovery file")
    return True


def _read_exact_marker(path: Path, expected: bytes) -> bytes:
    """Read one fixed marker without allocating attacker-controlled bytes."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if (
            _is_reparse_or_symlink(before)
            or not stat.S_ISREG(before.st_mode)
            or before.st_size != len(expected)
        ):
            raise OSError("invalid recovery marker size or type")
        chunks: list[bytes] = []
        remaining = len(expected) + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != before.st_size
        ):
            raise OSError("recovery marker changed while reading")
        material = b"".join(chunks)
        if material != expected:
            raise OSError("invalid recovery marker")
        return material
    finally:
        os.close(descriptor)


def _require_direct_child(path: Path, parent: Path) -> None:
    if Path(os.path.abspath(path.parent)) != Path(os.path.abspath(parent)):
        raise OSError("recovery path escaped containment")


def _files_match(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as left_handle, right.open("rb") as right_handle:
        while True:
            left_chunk = left_handle.read(_COPY_BUFFER_SIZE)
            right_chunk = right_handle.read(_COPY_BUFFER_SIZE)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def _copy_regular_exclusive(source: Path, destination: Path) -> None:
    """Copy one regular file to a newly-created no-follow destination."""
    binary_flag = getattr(os, "O_BINARY", 0)
    source_flags = os.O_RDONLY | binary_flag | getattr(os, "O_NOFOLLOW", 0)
    destination_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | binary_flag
        | getattr(os, "O_NOFOLLOW", 0)
    )
    source_descriptor = os.open(source, source_flags)
    try:
        source_metadata = os.fstat(source_descriptor)
        if _is_reparse_or_symlink(source_metadata) or not stat.S_ISREG(
            source_metadata.st_mode
        ):
            raise OSError("unsafe copy source")
        destination_descriptor = os.open(
            destination,
            destination_flags,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        try:
            destination_metadata = os.fstat(destination_descriptor)
            if _is_reparse_or_symlink(
                destination_metadata
            ) or not stat.S_ISREG(destination_metadata.st_mode):
                raise OSError("unsafe copy destination")
            while True:
                chunk = os.read(source_descriptor, _COPY_BUFFER_SIZE)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_descriptor, view)
                    view = view[written:]
            os.fsync(destination_descriptor)
        finally:
            os.close(destination_descriptor)
    finally:
        os.close(source_descriptor)


def _allowed_recovery_names() -> set[str]:
    return {
        f"{artifact_name}.{suffix}"
        for artifact_name in _SCAN_ARTIFACT_NAMES
        for suffix in ("backup", "absent", "restore", "state-bound")
    } | {_SCAN_COMMITTED_NAME}


def _validate_recovery_root(
    recovery_root: Path,
    *,
    require_complete: bool,
) -> None:
    _require_safe_directory(recovery_root)
    allowed_names = _allowed_recovery_names()
    for entry in recovery_root.iterdir():
        _require_direct_child(entry, recovery_root)
        if entry.name not in allowed_names:
            raise OSError("unexpected recovery entry")
        _require_safe_regular(entry)

    committed = recovery_root / _SCAN_COMMITTED_NAME
    if _require_safe_regular(committed, allow_missing=True):
        _read_exact_marker(committed, _SCAN_COMMITTED_MARKER)

    if not require_complete:
        return
    for artifact_name in _SCAN_ARTIFACT_NAMES:
        backup = recovery_root / f"{artifact_name}.backup"
        absent = recovery_root / f"{artifact_name}.absent"
        state_bound = recovery_root / f"{artifact_name}.state-bound"
        has_backup = _require_safe_regular(backup, allow_missing=True)
        has_absent = _require_safe_regular(absent, allow_missing=True)
        if has_backup == has_absent:
            raise OSError("invalid recovery record")
        if has_absent:
            _read_exact_marker(absent, _ABSENT_MARKER)
        if _require_safe_regular(state_bound, allow_missing=True):
            _read_exact_marker(state_bound, _SCAN_KEEP_MARKER)


def _mark_scan_recovery_committed(recovery_root: Path) -> None:
    """Durably record that every live scan artifact is now authoritative."""
    _validate_recovery_root(recovery_root, require_complete=True)
    marker = recovery_root / _SCAN_COMMITTED_NAME
    _require_direct_child(marker, recovery_root)
    if _require_safe_regular(marker, allow_missing=True):
        _read_exact_marker(marker, _SCAN_COMMITTED_MARKER)
        return
    with marker.open("xb") as handle:
        handle.write(_SCAN_COMMITTED_MARKER)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(recovery_root)
    _validate_recovery_root(recovery_root, require_complete=True)


def _preflight_scan_recovery(state_dir: Path, recovery_root: Path) -> None:
    _require_safe_directory(state_dir)
    _require_direct_child(recovery_root, state_dir / _SCAN_RECOVERY_DIRECTORY)
    _validate_recovery_root(recovery_root, require_complete=True)
    for artifact_name in _SCAN_ARTIFACT_NAMES:
        artifact = state_dir / artifact_name
        _require_direct_child(artifact, state_dir)
        _require_safe_regular(artifact, allow_missing=True)


def _verify_restored_scan_artifacts(
    state_dir: Path,
    recovery_root: Path,
) -> bool:
    try:
        _preflight_scan_recovery(state_dir, recovery_root)
        for artifact_name in _SCAN_ARTIFACT_NAMES:
            artifact = state_dir / artifact_name
            backup = recovery_root / f"{artifact_name}.backup"
            absent = recovery_root / f"{artifact_name}.absent"
            state_bound = recovery_root / f"{artifact_name}.state-bound"
            if _path_lstat(state_bound) is not None:
                _require_safe_regular(artifact, allow_missing=True)
            elif _path_lstat(absent) is not None:
                if _path_lstat(artifact) is not None:
                    return False
            elif not _files_match(artifact, backup):
                return False
    except (OSError, ValueError):
        return False
    return True


def _create_scan_recovery(state_dir: Path) -> tuple[str, Path]:
    """Create one recovery record while owning the project scan session."""
    with _exclusive_scan_session(state_dir):
        return _create_scan_recovery_unlocked(state_dir)


def _create_scan_recovery_unlocked(state_dir: Path) -> tuple[str, Path]:
    """Persist pre-mutation backups and return their opaque recovery token."""
    recovery_parent = state_dir / _SCAN_RECOVERY_DIRECTORY
    try:
        _require_safe_directory(state_dir)
        _require_direct_child(recovery_parent, state_dir)
        for artifact_name in _SCAN_ARTIFACT_NAMES:
            artifact = state_dir / artifact_name
            _require_direct_child(artifact, state_dir)
            _require_safe_regular(artifact, allow_missing=True)

        if _path_lstat(recovery_parent) is None:
            recovery_parent.mkdir()
            _fsync_directory(state_dir)
        _require_safe_directory(recovery_parent)
        entry_count = 0
        for entry in recovery_parent.iterdir():
            entry_count += 1
            if entry_count > _MAX_SCAN_RECOVERY_ENTRIES:
                raise OSError("too many recovery entries")
            _require_direct_child(entry, recovery_parent)
            if entry.name.startswith(_SCAN_RECOVERY_PREFIX):
                if not _is_scan_recovery_token(entry.name):
                    raise OSError("invalid active recovery token")
                _preflight_scan_recovery(state_dir, entry)
                raise ScanRecoveryError(entry.name)
            if entry.name.startswith(_SCAN_RETIRED_PREFIX):
                suffix = entry.name.removeprefix(_SCAN_RETIRED_PREFIX)
                if not _is_scan_recovery_token(
                    f"{_SCAN_RECOVERY_PREFIX}{suffix}"
                ):
                    raise OSError("invalid retired recovery token")
                continue
            raise OSError("unexpected recovery entry")
    except ScanRecoveryError:
        raise
    except Exception:
        raise ScanArtifactError() from None

    for _attempt in range(16):
        token = _new_scan_recovery_token()
        recovery_root = recovery_parent / token
        _require_direct_child(recovery_root, recovery_parent)
        try:
            recovery_root.mkdir()
            _fsync_directory(recovery_parent)
        except FileExistsError:
            continue
        except Exception:
            raise ScanArtifactError() from None
        try:
            for artifact_name in _SCAN_ARTIFACT_NAMES:
                artifact = state_dir / artifact_name
                if _require_safe_regular(artifact, allow_missing=True):
                    backup = recovery_root / f"{artifact_name}.backup"
                    _require_direct_child(backup, recovery_root)
                    _copy_regular_exclusive(artifact, backup)
                    _require_safe_regular(backup)
                    if not _files_match(artifact, backup):
                        raise OSError("backup verification failed")
                else:
                    marker = recovery_root / f"{artifact_name}.absent"
                    _require_direct_child(marker, recovery_root)
                    _write_durable_marker(marker)
                    _require_safe_regular(marker)
            _fsync_directory(recovery_root)
            _validate_recovery_root(recovery_root, require_complete=True)
            return token, recovery_root
        except Exception:
            if not _retire_scan_recovery(
                recovery_root,
                require_complete=False,
            ):
                raise ScanRecoveryError(token) from None
            raise ScanArtifactError() from None
    raise ScanArtifactError()


def _restore_scan_artifact(
    state_dir: Path,
    recovery_root: Path,
    artifact_name: str,
) -> None:
    """Restore one artifact while retaining its durable source backup."""
    if artifact_name not in _SCAN_ARTIFACT_NAMES:
        raise OSError("artifact is not allowlisted")
    artifact = state_dir / artifact_name
    backup = recovery_root / f"{artifact_name}.backup"
    absent = recovery_root / f"{artifact_name}.absent"
    state_bound = recovery_root / f"{artifact_name}.state-bound"
    _require_direct_child(artifact, state_dir)
    _require_direct_child(backup, recovery_root)
    _require_direct_child(absent, recovery_root)
    _require_direct_child(state_bound, recovery_root)
    has_backup = _require_safe_regular(backup, allow_missing=True)
    has_absent = _require_safe_regular(absent, allow_missing=True)
    has_state_bound = _require_safe_regular(state_bound, allow_missing=True)
    if has_state_bound:
        _read_exact_marker(state_bound, _SCAN_KEEP_MARKER)
        _require_safe_regular(artifact, allow_missing=True)
        return
    if has_backup == has_absent:
        raise OSError("invalid recovery record")
    _require_safe_regular(artifact, allow_missing=True)
    if has_absent:
        _read_exact_marker(absent, _ABSENT_MARKER)
        artifact.unlink(missing_ok=True)
        _fsync_directory(state_dir)
        if _path_lstat(artifact) is not None:
            raise OSError("absence restoration failed")
        return
    staging = recovery_root / f"{artifact_name}.restore"
    _require_direct_child(staging, recovery_root)
    if _require_safe_regular(staging, allow_missing=True):
        staging.unlink()
    _copy_regular_exclusive(backup, staging)
    _require_safe_regular(staging)
    if not _files_match(backup, staging):
        raise OSError("staging verification failed")
    staging.replace(artifact)
    _fsync_directory(state_dir)
    _require_safe_regular(artifact)
    if not _files_match(backup, artifact):
        raise OSError("artifact verification failed")


def _retire_scan_recovery(
    recovery_root: Path,
    *,
    require_complete: bool = True,
) -> bool:
    """Atomically make a completed recovery inactive, then remove it."""
    try:
        _require_direct_child(recovery_root, recovery_root.parent)
        _validate_recovery_root(
            recovery_root,
            require_complete=require_complete,
        )
        token = recovery_root.name
        if not _is_scan_recovery_token(token):
            raise OSError("invalid recovery token")
        completed = recovery_root.with_name(
            f"{_SCAN_RETIRED_PREFIX}{token.removeprefix(_SCAN_RECOVERY_PREFIX)}"
        )
        _require_direct_child(completed, recovery_root.parent)
        if _path_lstat(completed) is not None:
            raise OSError("retirement target exists")
        recovery_root.replace(completed)
        _fsync_directory(recovery_root.parent)
        _validate_recovery_root(
            completed,
            require_complete=require_complete,
        )
    except Exception:
        return False
    try:
        shutil.rmtree(completed)
        _fsync_directory(completed.parent)
    except Exception:
        return False
    return _path_lstat(completed) is None


def _resume_retired_scan_recovery(recovery_root: Path) -> bool:
    """Finish safe deletion of an already retired recovery record."""
    try:
        _require_direct_child(recovery_root, recovery_root.parent)
        _validate_recovery_root(recovery_root, require_complete=False)
        shutil.rmtree(recovery_root)
        _fsync_directory(recovery_root.parent)
    except Exception:
        return False
    return _path_lstat(recovery_root) is None


def _restore_scan_recovery(state_dir: Path, recovery_root: Path) -> bool:
    """Best-effort every artifact; return true only after full restoration."""
    try:
        _preflight_scan_recovery(state_dir, recovery_root)
    except (OSError, ValueError):
        return False
    committed = recovery_root / _SCAN_COMMITTED_NAME
    if _require_safe_regular(committed, allow_missing=True):
        try:
            _read_exact_marker(committed, _SCAN_COMMITTED_MARKER)
        except OSError:
            return False
        return _retire_scan_recovery(recovery_root)
    failures = False
    for artifact_name in _SCAN_ARTIFACT_NAMES:
        try:
            _restore_scan_artifact(state_dir, recovery_root, artifact_name)
        except BaseException:
            failures = True
    if failures:
        return False
    if not _verify_restored_scan_artifacts(state_dir, recovery_root):
        return False
    return _retire_scan_recovery(recovery_root)


def _resume_scan_recovery(state_dir: Path) -> None:
    """Resume durable recovery while owning the project scan session."""
    with _exclusive_scan_session(state_dir):
        _resume_scan_recovery_unlocked(state_dir)


def _resume_scan_recovery_unlocked(state_dir: Path) -> None:
    """Finish any durable rollback before reading or mutating scan artifacts."""
    recovery_parent = state_dir / _SCAN_RECOVERY_DIRECTORY
    if _path_lstat(recovery_parent) is None:
        return
    try:
        _require_safe_directory(state_dir)
        _require_direct_child(recovery_parent, state_dir)
        _require_safe_directory(recovery_parent)
        entries: list[Path] = []
        for entry in recovery_parent.iterdir():
            if len(entries) >= _MAX_SCAN_RECOVERY_ENTRIES:
                raise OSError("too many recovery entries")
            _require_direct_child(entry, recovery_parent)
            name = entry.name
            if name.startswith(_SCAN_RECOVERY_PREFIX):
                if not _is_scan_recovery_token(name):
                    raise OSError("invalid active recovery token")
            elif name.startswith(_SCAN_RETIRED_PREFIX):
                suffix = name.removeprefix(_SCAN_RETIRED_PREFIX)
                if not _is_scan_recovery_token(
                    f"{_SCAN_RECOVERY_PREFIX}{suffix}"
                ):
                    raise OSError("invalid retired recovery token")
            else:
                raise OSError("unexpected recovery entry")
            entries.append(entry)
        entries.sort(key=lambda path: path.name)
    except Exception:
        raise ScanArtifactError() from None
    active_records: list[Path] = []
    retired_records: list[Path] = []
    for recovery_root in entries:
        name = recovery_root.name
        if name.startswith(_SCAN_RECOVERY_PREFIX):
            if not _is_scan_recovery_token(name):
                raise ScanArtifactError()
            try:
                _preflight_scan_recovery(state_dir, recovery_root)
            except (OSError, ValueError):
                raise ScanRecoveryError(name) from None
            active_records.append(recovery_root)
            continue
        if name.startswith(_SCAN_RETIRED_PREFIX):
            suffix = name.removeprefix(_SCAN_RETIRED_PREFIX)
            token = f"{_SCAN_RECOVERY_PREFIX}{suffix}"
            if not _is_scan_recovery_token(token):
                raise ScanArtifactError()
            try:
                _validate_recovery_root(
                    recovery_root,
                    require_complete=False,
                )
            except (OSError, ValueError):
                raise ScanRecoveryError(token) from None
            retired_records.append(recovery_root)
    if len(active_records) > 1:
        raise ScanArtifactError()
    for retired_root in retired_records:
        token = (
            f"{_SCAN_RECOVERY_PREFIX}"
            f"{retired_root.name.removeprefix(_SCAN_RETIRED_PREFIX)}"
        )
        if not _resume_retired_scan_recovery(retired_root):
            raise ScanRecoveryError(token)
    if active_records:
        recovery_root = active_records[0]
        if not _restore_scan_recovery(state_dir, recovery_root):
            raise ScanRecoveryError(recovery_root.name)


def scan(
    json_output: bool = JSON_OPTION,
    force: bool = typer.Option(  # noqa: B008
        False,
        "--force",
        help=(
            "Re-seed the draft PRD and task graph even when a PRD already "
            "exists. Without this, a re-scan only updates the codebase model "
            "and reports the file delta — it never clobbers an authored PRD."
        ),
    ),
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Scan the working tree, persist a codebase model, and seed a draft graph.

    First run (no PRD yet): writes ``.anvil/prd.md`` from the discovered
    structure and seeds features/tasks so ``anvil next`` returns a ready
    task. Subsequent runs: refresh the persisted codebase model and report the
    added / removed / changed file delta. Pass ``--force`` to re-seed the PRD.
    """
    try:
        state_dir = _resolve_state_dir(cwd)
    except StateRootError as exc:
        if json_output:
            fail(_COMMAND, str(exc), code="state_root_invalid")
        raise

    if not state_dir.exists():
        msg = (
            "anvil not initialized in this project. Run "
            "`anvil init` (or `anvil init --from-repo`) first."
        )
        if json_output:
            fail(_COMMAND, msg, code="not_initialized")
        typer.echo(f"Error: {msg}", err=True)
        raise typer.Exit(code=1)

    project_root = _resolve_project_dir(cwd)
    try:
        result = _run_scan(state_dir, project_root, force=force)
    except SampleSeedError as exc:
        if json_output:
            code = exc.code if exc.code != "sample_seed_error" else "seed_rejected"
            fail(_COMMAND, str(exc), code=code)
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if json_output:
        emit_success(_COMMAND, result)
        return

    _print_human(result, state_dir)


# ---------------------------------------------------------------------------
# Core scan logic (shared by `scan` and `init --from-repo`)
# ---------------------------------------------------------------------------


def run_scan_and_report(
    state_dir: Path,
    project_root: Path,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Public entry point used by both ``scan`` and ``init --from-repo``.

    Returns the same ``data`` dict the ``--json`` envelope carries.
    """
    return _run_scan(state_dir, project_root, force=force)


def _run_scan(
    state_dir: Path,
    project_root: Path,
    *,
    force: bool,
) -> dict[str, Any]:
    with _exclusive_scan_session(state_dir):
        return _run_scan_locked(state_dir, project_root, force=force)


def _run_scan_locked(
    state_dir: Path,
    project_root: Path,
    *,
    force: bool,
) -> dict[str, Any]:
    from anvil.cli._sample import SampleSeedError
    from anvil.scan.model import (
        SCAN_DB_NAME,
        compute_delta,
        load_model,
        save_model,
        scan_working_tree,
    )

    scan_db = state_dir / SCAN_DB_NAME
    prd_path = state_dir / _PRD_FILENAME

    _resume_scan_recovery(state_dir)
    previous: CodebaseModel | None = load_model(scan_db)
    current: CodebaseModel = scan_working_tree(project_root)
    delta: ScanDelta = compute_delta(previous, current)

    seeded: dict[str, Any] | None = None
    seed_reason = _should_seed(state_dir, prd_path, force=force)
    # Every scan rewrites scan.db. Establish a durable two-artifact recovery
    # record before the first mutation so an interrupted restore can resume.
    token, recovery_root = _create_scan_recovery(state_dir)
    try:
        save_model(current, scan_db)
        if seed_reason is not None:
            seeded = _seed_draft(
                state_dir,
                project_root,
                current,
                revalidate_first_seed=seed_reason == "no_prd",
                recovery_root=recovery_root,
            )
        _mark_scan_recovery_committed(recovery_root)
    except BaseException as exc:
        if not _restore_scan_recovery(state_dir, recovery_root):
            raise ScanRecoveryError(token) from None
        if isinstance(exc, SampleSeedError):
            raise
        if not isinstance(exc, Exception):
            raise
        raise ScanArtifactError() from None
    if not _retire_scan_recovery(recovery_root):
        raise ScanRecoveryError(token)

    return {
        "project_root": str(project_root),
        "scan_db": str(scan_db),
        "files_scanned": current.file_count,
        "components": {name: len(files) for name, files in current.components().items()},
        "languages": current.language_counts(),
        "first_scan": previous is None,
        "delta": {
            "added": delta.added,
            "removed": delta.removed,
            "changed": delta.changed,
            "unchanged_count": len(delta.unchanged),
        },
        "seeded": seeded,
        "prd_path": str(prd_path) if seeded is not None else None,
    }


def _should_seed(state_dir: Path, prd_path: Path, *, force: bool) -> str | None:
    """Return a reason string when the draft graph should be (re)seeded, else None.

    Seeds when the project has no PRD yet (the common brownfield first run), or
    when ``--force`` is given. Never overwrites an authored/approved PRD without
    ``--force`` — a re-scan of an active project just refreshes the model.
    """
    if force:
        return "force"

    # If ANY PRD already exists in state, do not re-seed (idempotent re-scan).
    #
    # This is an existence probe for the brownfield first-run seed — "has this
    # project been given a PRD yet?". Use list_prds(), NOT bare get_prd(): a
    # project that holds only NON-default PRDs (e.g. v0.1/v0.2, no is_default
    # row) is already populated, and bare get_prd() would return None and
    # re-seed a fresh default draft graph on top of the existing multi-PRD
    # state. Any PRD's presence must suppress re-seeding.
    backend = _open_backend(state_dir)
    try:
        has_prd = bool(backend.list_prds())
    finally:
        backend.close()
    if has_prd:
        return None
    if prd_path.exists():
        # A prd.md exists but was never parsed into state — leave it for the
        # user to `prd parse` rather than clobbering their draft.
        return None
    return "no_prd"


def _seed_draft(
    state_dir: Path,
    project_root: Path,
    model: CodebaseModel,
    *,
    revalidate_first_seed: bool = False,
    recovery_root: Path | None = None,
) -> dict[str, Any] | None:
    """Write the draft prd.md and drive the offline seed pipeline.

    Reuses ``cli._sample.seed_pipeline_from_prd`` (the same engine path
    ``init --with-sample`` uses) so the brownfield seed cannot drift from the
    hand-run command sequence.
    """
    from anvil.cli._sample import seed_pipeline_from_prd
    from anvil.scan.prd_draft import draft_prd_from_model

    prd_path = state_dir / _PRD_FILENAME
    backend = _open_backend(state_dir)
    try:
        with _exclusive_seed_session(backend):
            if revalidate_first_seed and (backend.list_prds() or prd_path.exists()):
                # The optimistic eligibility check happens before this backend
                # and its cross-process mutation lock are acquired. Another
                # first scan may have seeded while we waited; re-check both
                # canonical representations inside the lock so only one wins.
                return None
            project_name = _project_name(state_dir, project_root)
            prd_text = draft_prd_from_model(model, project_name=project_name)
            live_ids = {item.id for item in backend.list_requirements()}
            all_ids = {item.id for item in backend.list_requirements(include_superseded=True)}
            retired_ids = all_ids - live_ids
            generated_ids = {
                line.split(":", 1)[0][2:]
                for line in prd_text.splitlines()
                if line.startswith("- R") and ":" in line
            }
            ids_by_canonical_index: dict[str, set[str]] = {}
            for requirement_id in all_ids:
                canonical = _canonical_requirement_index(requirement_id)
                if canonical is not None:
                    ids_by_canonical_index.setdefault(canonical, set()).add(requirement_id)
            generated_id_conflict = False
            for requirement_id in generated_ids:
                canonical = _canonical_requirement_index(requirement_id)
                aliases = (
                    ids_by_canonical_index.get(canonical, set()) if canonical is not None else set()
                )
                if requirement_id in retired_ids or any(
                    existing_id != requirement_id for existing_id in aliases
                ):
                    generated_id_conflict = True
                    break
            if generated_id_conflict:
                prd_text = draft_prd_from_model(
                    model,
                    project_name=project_name,
                    requirement_start=_next_generated_requirement_start(
                        all_ids,
                        block_size=len(generated_ids),
                    ),
                )
            generated_bytes = prd_text.encode("utf-8")
            try:
                prior_source = ingest_prd_source(
                    prd_path,
                    containment_root=state_dir,
                    required_parent=state_dir,
                )
                prior_bytes = prior_source.source_bytes
            except PrdSourceIngestError as exc:
                if exc.code == "source_not_found":
                    prior_bytes = None
                else:
                    from anvil.cli._sample import SampleSeedError

                    raise SampleSeedError(
                        f"cannot read existing PRD source: {exc.format_message()}"
                    ) from None
            except OSError as exc:
                from anvil.cli._sample import SampleSeedError

                reason = exc.strerror or exc.__class__.__name__
                raise SampleSeedError(f"cannot read existing PRD source: {reason}") from None

            prior_prd = backend.get_prd()
            events_path = state_dir / "events.jsonl"
            try:
                prior_events_size = events_path.stat().st_size
            except OSError as exc:
                from anvil.cli._sample import SampleSeedError

                reason = exc.strerror or exc.__class__.__name__
                raise SampleSeedError(
                    f"cannot inspect event log before PRD seed: {reason}"
                ) from None
            publication_hook_started = False
            publication_hook_completed = False
            try:
                published = _atomic_replace_prd(
                    prd_path,
                    generated_bytes,
                    operation="publish generated",
                    expected=prior_bytes if prior_bytes is not None else _SOURCE_MISSING,
                )
                if published is None:
                    from anvil.cli._sample import SampleSeedError

                    raise SampleSeedError(
                        "cannot publish generated PRD source: source changed concurrently"
                    )
                publication_hook_started = True
                _write_generated_prd(prd_path, prd_text)
                publication_hook_completed = True
                summary = seed_pipeline_from_prd(
                    backend,
                    published.markdown,
                    actor="anvil-cli",
                    review_notes="auto-seeded by scan (brownfield)",
                    project_root=project_root,
                    parse_error_hint=(
                        "The generated draft PRD failed to parse — this is an "
                        "anvil bug; please report it."
                    ),
                )
                if recovery_root is not None:
                    _mark_scan_recovery_committed(recovery_root)
            except BaseException as failure:
                # The same state lock that covers every append remains held for
                # this comparison and file restoration. A concurrent state
                # writer therefore cannot advance between the two, and another
                # scan cannot replace the source inside its byte-ownership check.
                try:
                    state_advanced = backend.get_prd() != prior_prd
                    if not state_advanced:
                        state_advanced = events_path.stat().st_size != prior_events_size
                except BaseException as state_error:
                    # Ambiguous state is fail-closed: keep the generated source,
                    # which is the only representation that may match an event
                    # committed immediately before the read failed.
                    failure.add_note(
                        "PRD source rollback skipped because state could not be "
                        f"checked safely: {state_error.__class__.__name__}"
                    )
                    state_advanced = True
                if not state_advanced:
                    try:
                        _restore_prd_source_if_unchanged(
                            prd_path,
                            generated=generated_bytes,
                            prior=prior_bytes,
                        )
                    except BaseException as restore_error:
                        failure.add_note(
                            f"PRD source rollback failed safely: {restore_error.__class__.__name__}"
                        )
                # The inner CAS boundary owns PRD rollback and concurrent-writer
                # preservation. Record that durable decision so the outer
                # artifact rollback (and a later retry) cannot clobber it. The
                # injectable post-publication write failure is the sole case
                # where the outer recovery owns the partially-mutated PRD.
                preserve_current = not (
                    publication_hook_started and not publication_hook_completed
                )
                if preserve_current and recovery_root is not None:
                    try:
                        _mark_scan_artifact_state_bound(
                            recovery_root,
                            _PRD_FILENAME,
                        )
                    except BaseException as marker_error:
                        failure.add_note(
                            "PRD recovery state could not be recorded safely: "
                            f"{marker_error.__class__.__name__}"
                        )
                        raise ScanRecoveryError(recovery_root.name) from failure
                raise
    finally:
        backend.close()
    return summary


def _project_name(state_dir: Path, project_root: Path) -> str:
    """Resolve a human-readable project name from config, else the dir name."""
    config_path = state_dir / "config.yaml"
    if config_path.exists():
        try:
            from anvil.config import load_config

            return load_config(config_path).project_name
        except Exception:  # noqa: BLE001
            pass
    return project_root.name


# ---------------------------------------------------------------------------
# Human-readable rendering
# ---------------------------------------------------------------------------


def _print_human(result: dict[str, Any], state_dir: Path) -> None:
    typer.echo(f"Scanned {result['files_scanned']} file(s) from {result['project_root']}")
    components = result["components"]
    if components:
        typer.echo("")
        typer.echo("Components:")
        for name, count in components.items():
            typer.echo(f"  {name}: {count} file(s)")
    languages = result["languages"]
    if languages:
        typer.echo("")
        typer.echo("Languages:")
        for lang, count in languages.items():
            typer.echo(f"  {lang}: {count}")

    delta = result["delta"]
    typer.echo("")
    if result["first_scan"]:
        typer.echo("First scan - persisted a new codebase model.")
    else:
        typer.echo(
            "Re-scan delta: "
            f"{len(delta['added'])} added, "
            f"{len(delta['removed'])} removed, "
            f"{len(delta['changed'])} changed, "
            f"{delta['unchanged_count']} unchanged."
        )
        for path in delta["added"]:
            typer.echo(f"  + {path}")
        for path in delta["removed"]:
            typer.echo(f"  - {path}")
        for path in delta["changed"]:
            typer.echo(f"  ~ {path}")

    seeded = result["seeded"]
    if seeded is not None:
        typer.echo("")
        typer.echo(
            "Seeded draft project: "
            f"{seeded['features']} feature(s), "
            f"{seeded['tasks']} task(s), "
            f"{seeded['ready']} ready."
        )
        typer.echo(f"  {result['prd_path']}")
        typer.echo("")
        typer.echo(
            "Draft PRD is a SEED — edit it to capture real intent, then run "
            "`anvil next` to see your first ready task."
        )
    else:
        typer.echo("")
        typer.echo("Codebase model refreshed (PRD left unchanged).")
