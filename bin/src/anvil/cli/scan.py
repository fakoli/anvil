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
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from anvil.cli._helpers import (
    PrdSourceIngestError,
    StateRootError,
    _open_backend,
    _resolve_base_dir,
    _resolve_state_dir,
    ingest_prd_source,
)
from anvil.cli._json import JSON_OPTION, emit_success, fail

if TYPE_CHECKING:
    from anvil.cli._helpers import IngestedPrdSource
    from anvil.scan.model import CodebaseModel, ScanDelta
    from anvil.state.sqlite import SqliteBackend

__all__ = ["scan"]

_COMMAND = "scan"
_PRD_FILENAME = "prd.md"
_SOURCE_MISSING = object()


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
            os.fsync(handle.fileno())

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

    project_root = _resolve_base_dir(cwd)
    from anvil.cli._sample import SampleSeedError

    try:
        result = _run_scan(state_dir, project_root, force=force)
    except SampleSeedError as exc:
        if json_output:
            fail(_COMMAND, str(exc), code=exc.code)
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
    from shutil import copy2
    from tempfile import TemporaryDirectory

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

    previous: CodebaseModel | None = load_model(scan_db)
    current: CodebaseModel = scan_working_tree(project_root)
    delta: ScanDelta = compute_delta(previous, current)

    seeded: dict[str, Any] | None = None
    seed_reason = _should_seed(state_dir, prd_path, force=force)
    if seed_reason is None:
        # Ordinary re-scans cannot enter the seed failure path and need no
        # artifact backup; persist the refreshed model directly.
        save_model(current, scan_db)
    else:
        # Preserve scan.db before its first mutation. _seed_draft owns the
        # canonical PRD source's atomic publish/rollback under the state lock;
        # duplicating that rollback here could restore stale bytes after a
        # successful state append.
        with TemporaryDirectory(
            prefix=".scan-rollback-", dir=state_dir
        ) as backup_dir:
            backup_root = Path(backup_dir)
            backup_path: Path | None = None
            if scan_db.exists():
                backup_path = backup_root / scan_db.name
                copy2(scan_db, backup_path)
            try:
                save_model(current, scan_db)
                seeded = _seed_draft(
                    state_dir,
                    project_root,
                    current,
                    revalidate_first_seed=seed_reason == "no_prd",
                )
            except SampleSeedError:
                if backup_path is None:
                    scan_db.unlink(missing_ok=True)
                else:
                    backup_path.replace(scan_db)
                raise

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
                summary = seed_pipeline_from_prd(
                    backend,
                    published.markdown,
                    actor="anvil-cli",
                    review_notes="auto-seeded by scan (brownfield)",
                    parse_error_hint=(
                        "The generated draft PRD failed to parse — this is an "
                        "anvil bug; please report it."
                    ),
                )
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
