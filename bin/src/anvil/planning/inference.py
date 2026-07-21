"""Dependency, conflict-group, and (optional) sub-task inference.

Pure rule-based inference is the canonical baseline — no I/O, no LLM.  Phase 7
Wave 2 adds an optional ``expand_task`` entry point that uses an LLM provider
to propose 2-5 sub-tasks for *complex* tasks (complexity >= 4) when a provider
is supplied.  The deterministic engine on its own does not split tasks; that
responsibility lies with the author of prd.md (T001.1, T001.2 etc.).

Heuristics
----------
``infer_dependencies``:
    If Task A's ``likely_files`` is a *strict subset* of Task B's, A is added
    as a dependency of B (the broader change goes first; A specialises B).
    Conservative: only strict-subset edges are added — never speculative ones.

``infer_conflict_groups``:
    For each pair of tasks with *any* ``likely_files`` overlap that are NOT in a
    strict subset/superset relationship, they are grouped into a named
    ConflictGroup.  Group IDs follow the pattern ``CG-<sorted-task-ids>``.

``expand_task`` (LLM-only):
    With ``provider=`` and a task whose ``complexity >= 4``, asks the LLM for
    a JSON array of 2-5 sub-task proposals.  Returns ``[]`` for
    low-complexity tasks, when no provider is supplied, or when the LLM call
    or JSON parse fails (a warning is printed to stderr in the latter case).
"""

from __future__ import annotations

import errno
import json
import posixpath
import re
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, NamedTuple

from anvil.state.models import ConflictGroup, Task

if TYPE_CHECKING:
    from anvil.planning.llm import LLMProvider

__all__ = [
    "InferenceResult",
    "BundlePlanReport",
    "BundlePlanningError",
    "PathIdentityError",
    "BundleProposal",
    "build_bundle_plan",
    "SubtaskProposal",
    "expand_task",
    "infer_all",
    "infer_conflict_groups",
    "infer_dependencies",
]


# ---------------------------------------------------------------------------
# Sub-task expansion (LLM-augmented; deterministic engine returns [])
# ---------------------------------------------------------------------------


class SubtaskProposal(NamedTuple):
    """A single LLM-proposed sub-task.

    Returned by :func:`expand_task` — *proposals only*, never written to the
    backend by this module.  The caller (CLI) decides what to do with them
    (typically: print for the human to paste into prd.md).
    """

    title: str
    description: str
    acceptance_criteria: list[str]
    likely_files: list[str]


_EXPAND_SYSTEM_PROMPT = (
    "You are decomposing a complex software task into 2-5 sub-tasks. "
    "Each sub-task should be independently claimable (no overlapping scope). "
    "Your entire response must be a single JSON array. Start your output "
    'with the literal character `[` and end with `]`. Each element is an '
    'object with keys: "title" (string, imperative), "description" '
    '(string), "acceptance_criteria" (array of strings, each independently '
    'verifiable), "likely_files" (array of file path strings). '
    "Do NOT wrap the array in markdown code fences. "
    "Do NOT include any prose before or after the array. "
    "Do NOT include explanatory commentary inside the array — only data."
)
_EXPAND_MAX_TOKENS = 2000
_EXPAND_COMPLEXITY_THRESHOLD = 4
_EXPAND_MIN_SUBTASKS = 2
_EXPAND_MAX_SUBTASKS = 5


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


class InferenceResult(NamedTuple):
    """Output returned by ``infer_all`` after its inputs pass validation."""

    tasks: list[Task]
    conflict_groups: list[ConflictGroup]


@dataclass(frozen=True)
class BundleProposal:
    """One stable connected component proposed as a coordinator-owned bundle."""

    id: str
    task_ids: tuple[str, ...]
    serial_depth: int
    overlap_files: tuple[str, ...]
    review_angles: tuple[str, ...]
    expected_reviews: int
    expected_checkpoints: int = 1

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class BundlePlanReport:
    """Deterministic execution-cost report emitted before bundle execution."""

    task_count: int
    serial_depth: int
    overlap_pair_count: int
    overlap_files: tuple[str, ...]
    proposed_bundles: tuple[BundleProposal, ...]
    expected_review_count: int
    high_risk_policies: tuple[str, ...]
    expected_checkpoints: int
    max_tasks: int
    max_serial_stages: int
    limit_breaches: tuple[str, ...]
    acknowledgement_required: bool

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["proposed_bundles"] = [
            proposal.to_dict() for proposal in self.proposed_bundles
        ]
        return data


class BundlePlanningError(ValueError):
    """A planning graph or portable file scope cannot be analyzed safely."""


class PathIdentityError(BundlePlanningError):
    """Host path identity cannot be established within bounded resources."""


_MAX_PORTABLE_PROJECT_PATH_BYTES = 4_096
"""Maximum UTF-8 bytes accepted for one portable project-relative path.

4,096 bytes matches the common portable ``PATH_MAX`` envelope while counting
the serialized representation consistently on every host.  Enforcing it before
normalization and native identity work bounds diagnostics, temporary strings,
Win32 calls, and the process-wide identity caches.
"""

_PATH_TYPE_ERROR = "bundle planning requires likely-file paths to be strings"
_PATH_UNICODE_ERROR = "bundle planning requires valid UTF-8 likely-file paths"
_PATH_SIZE_ERROR = (
    "bundle planning requires likely-file paths no longer than "
    f"{_MAX_PORTABLE_PROJECT_PATH_BYTES} UTF-8 bytes"
)
_PATH_PORTABILITY_ERROR = (
    "bundle planning requires a valid portable project-relative file path"
)
_PATH_RELATIVE_ERROR = "bundle planning requires a project-relative file path"
_PATH_ESCAPE_ERROR = "bundle planning file path escapes the project"


_WINDOWS_ILLEGAL_COMPONENT_CHARACTERS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_DEVICE_BASENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
    | {f"COM{index}" for index in "¹²³"}
    | {f"LPT{index}" for index in "¹²³"}
)
_WINDOWS_RESERVED_CONSOLE_COMPONENTS = frozenset({"CONIN$", "CONOUT$"})


def _validate_portable_project_path(candidate: str) -> None:
    """Reject spellings that cannot name a portable repository path.

    Git repositories routinely cross POSIX and Windows hosts.  In addition to
    the traversal checks performed after normalization, every authored path
    component must therefore exclude ASCII control characters, Windows-illegal
    punctuation, trailing dots/spaces, Windows device basenames, and the exact
    ``CONIN$``/``CONOUT$`` console aliases.  ``.`` and ``..`` remain valid
    normalization aliases and separators are handled before this component-level
    policy runs.
    """
    if any(
        ord(character) < 0x20
        or ord(character) == 0x7F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in candidate
    ):
        raise BundlePlanningError(_PATH_PORTABILITY_ERROR)
    for component in candidate.split("/"):
        if component in {"", ".", ".."}:
            continue
        basename = component.split(".", maxsplit=1)[0].upper()
        if (
            any(
                character in _WINDOWS_ILLEGAL_COMPONENT_CHARACTERS
                for character in component
            )
            or component.endswith((".", " "))
            or basename in _WINDOWS_RESERVED_DEVICE_BASENAMES
            or component.upper() in _WINDOWS_RESERVED_CONSOLE_COMPONENTS
        ):
            raise BundlePlanningError(_PATH_PORTABILITY_ERROR)


def _canonical_project_path(path: object) -> str:
    """Return one safe project-relative spelling for inference comparisons."""
    if not isinstance(path, str):
        raise BundlePlanningError(_PATH_TYPE_ERROR)
    encoded_size = 0
    for character in path:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise BundlePlanningError(_PATH_UNICODE_ERROR)
        if codepoint <= 0x7F:
            encoded_size += 1
        elif codepoint <= 0x7FF:
            encoded_size += 2
        elif codepoint <= 0xFFFF:
            encoded_size += 3
        else:
            encoded_size += 4
        if encoded_size > _MAX_PORTABLE_PROJECT_PATH_BYTES:
            raise BundlePlanningError(_PATH_SIZE_ERROR)
    candidate = path.replace("\\", "/")
    if not candidate or candidate.startswith("/"):
        raise BundlePlanningError(_PATH_RELATIVE_ERROR)
    _validate_portable_project_path(candidate)
    normalized = posixpath.normpath(candidate)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise BundlePlanningError(_PATH_ESCAPE_ERROR)
    return normalized


_WINDOWS_COMPARISON_CACHE_SIZE = 131_072
_WINDOWS_COLLISION_BUCKET_LIMIT = 64
_LCMAP_UPPERCASE = 0x00000200
_FILE_ID_INFO_CLASS = 18
_FILE_CASE_SENSITIVE_INFO_CLASS = 23
_FILE_CS_FLAG_CASE_SENSITIVE_DIR = 0x00000001
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_SHARE_ALL = 0x00000007
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000


def _uses_windows_path_identity() -> bool:
    """Return whether the host has authoritative Win32 path case semantics."""
    return sys.platform == "win32"


class _WindowsPathApi(NamedTuple):
    map_key: Callable[[str], str]
    equivalent: Callable[[str, str], bool]


class _WindowsExistingPath(NamedTuple):
    volume_serial: str
    file_id: str
    is_directory: bool

    @property
    def identity(self) -> tuple[str, str]:
        return self.volume_serial, self.file_id


def _windows_directory_case_sensitive(directory: str) -> bool:
    """Return the authoritative per-directory Win32 case-sensitivity flag."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        get_file_information = kernel32.GetFileInformationByHandle
        get_information = kernel32.GetFileInformationByHandleEx
        close_handle = kernel32.CloseHandle
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        get_file_information.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
        ]
        get_file_information.restype = wintypes.BOOL
        get_information.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        get_information.restype = wintypes.BOOL
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        class FileCaseSensitiveInfo(ctypes.Structure):
            _fields_ = [("flags", wintypes.ULONG)]

        class ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("file_attributes", wintypes.DWORD),
                ("creation_time", wintypes.FILETIME),
                ("last_access_time", wintypes.FILETIME),
                ("last_write_time", wintypes.FILETIME),
                ("volume_serial_number", wintypes.DWORD),
                ("file_size_high", wintypes.DWORD),
                ("file_size_low", wintypes.DWORD),
                ("number_of_links", wintypes.DWORD),
                ("file_index_high", wintypes.DWORD),
                ("file_index_low", wintypes.DWORD),
            ]

        handle = create_file(
            directory,
            _FILE_READ_ATTRIBUTES,
            _FILE_SHARE_ALL,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle in {None, invalid_handle}:
            raise PathIdentityError(
                "Windows directory identity query failed"
            )
        try:
            basic_information = ByHandleFileInformation()
            if not get_file_information(
                handle,
                ctypes.byref(basic_information),
            ):
                raise PathIdentityError(
                    "Windows directory identity query failed"
                )
            if not (
                basic_information.file_attributes
                & _FILE_ATTRIBUTE_DIRECTORY
            ):
                raise PathIdentityError(
                    "Windows directory identity query failed"
                )
            information = FileCaseSensitiveInfo()
            succeeded = get_information(
                handle,
                _FILE_CASE_SENSITIVE_INFO_CLASS,
                ctypes.byref(information),
                ctypes.sizeof(information),
            )
            if not succeeded:
                raise PathIdentityError(
                    "Windows directory identity query failed"
                )
            return bool(
                information.flags & _FILE_CS_FLAG_CASE_SENSITIVE_DIR
            )
        finally:
            close_handle(handle)
    except PathIdentityError:
        raise
    except Exception:
        raise PathIdentityError(
            "Windows directory identity query failed"
        ) from None


def _directory_uses_case_sensitive_identity(directory: Path) -> bool:
    """Return one existing directory's policy, with test-host compatibility."""
    # Cross-platform tests replace ``_uses_windows_path_identity`` to exercise
    # ordinal collision handling. They cannot query a Win32 directory handle,
    # so retain the historical insensitive policy off an actual Windows host.
    if sys.platform != "win32":
        return False
    return _windows_directory_case_sensitive(str(directory))


def _extended_windows_path(path: Path) -> str:
    spelling = str(path)
    if spelling.startswith("\\\\?\\"):
        return spelling
    if spelling.startswith("\\\\"):
        return "\\\\?\\UNC\\" + spelling[2:]
    return "\\\\?\\" + spelling


def _windows_existing_path_identity(
    path: Path,
) -> _WindowsExistingPath | None:
    """Return authoritative host identity for an existing path.

    The handle follows symlinks/reparse points. ``FileIdInfo`` supplies the
    volume serial plus the full 128-bit file ID, so existing aliases share an
    identity while case-distinct entries on a sensitive filesystem remain
    distinct. Only authoritative not-found results fall back to prospective
    component-policy inference; every other failure stays fail-closed.
    """
    if any(
        len(component.encode("utf-16-le")) // 2 > 255
        for component in path.parts
    ):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        get_basic_information = kernel32.GetFileInformationByHandle
        get_extended_information = kernel32.GetFileInformationByHandleEx
        close_handle = kernel32.CloseHandle
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        get_basic_information.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
        ]
        get_basic_information.restype = wintypes.BOOL
        get_extended_information.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        get_extended_information.restype = wintypes.BOOL
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        class ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("file_attributes", wintypes.DWORD),
                ("creation_time", wintypes.FILETIME),
                ("last_access_time", wintypes.FILETIME),
                ("last_write_time", wintypes.FILETIME),
                ("volume_serial_number", wintypes.DWORD),
                ("file_size_high", wintypes.DWORD),
                ("file_size_low", wintypes.DWORD),
                ("number_of_links", wintypes.DWORD),
                ("file_index_high", wintypes.DWORD),
                ("file_index_low", wintypes.DWORD),
            ]

        class FileId128(ctypes.Structure):
            _fields_ = [("identifier", ctypes.c_ubyte * 16)]

        class FileIdInfo(ctypes.Structure):
            _fields_ = [
                ("volume_serial_number", ctypes.c_ulonglong),
                ("file_id", FileId128),
            ]

        handle = create_file(
            _extended_windows_path(path),
            _FILE_READ_ATTRIBUTES,
            _FILE_SHARE_ALL,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle in {None, invalid_handle}:
            if ctypes.get_last_error() in {2, 3, 206}:
                return None
            raise PathIdentityError("Windows path identity query failed")
        try:
            basic_information = ByHandleFileInformation()
            if not get_basic_information(
                handle,
                ctypes.byref(basic_information),
            ):
                raise PathIdentityError(
                    "Windows path identity query failed"
                )
            file_id_information = FileIdInfo()
            has_extended_id = get_extended_information(
                handle,
                _FILE_ID_INFO_CLASS,
                ctypes.byref(file_id_information),
                ctypes.sizeof(file_id_information),
            )
            if not has_extended_id:
                raise PathIdentityError(
                    "Windows path identity query failed"
                )
            file_id_bytes = bytes(
                file_id_information.file_id.identifier
            )
            if file_id_bytes in {bytes(16), bytes([0xFF]) * 16}:
                raise PathIdentityError(
                    "Windows path identity query failed"
                )
            volume_serial = (
                f"id128:{file_id_information.volume_serial_number:016x}"
            )
            file_id = file_id_bytes.hex()
            return _WindowsExistingPath(
                volume_serial=volume_serial,
                file_id=file_id,
                is_directory=bool(
                    basic_information.file_attributes
                    & _FILE_ATTRIBUTE_DIRECTORY
                ),
            )
        finally:
            close_handle(handle)
    except PathIdentityError:
        raise
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        if exc.errno == errno.ENOENT or getattr(exc, "winerror", None) in {
            2,
            3,
        }:
            return None
        raise PathIdentityError(
            "Windows path identity query failed"
        ) from None
    except Exception:
        raise PathIdentityError(
            "Windows path identity query failed"
        ) from None


def _windows_path_policy(
    project_root: Path,
    path: str,
    directory_policy: Callable[[Path], bool] = (
        _directory_uses_case_sensitive_identity
    ),
    existing_path: Callable[[Path], _WindowsExistingPath | None] = (
        _windows_existing_path_identity
    ),
) -> tuple[tuple[str, bool], ...]:
    """Return each component with the policy of its containing directory."""
    current = project_root
    inherited_policy = directory_policy(current)
    root_information = existing_path(current)
    if root_information is None or not root_information.is_directory:
        raise PathIdentityError("Windows path identity query failed")
    policy: list[tuple[str, bool]] = [
        ("\0".join(("anchor", *root_information.identity)), True)
    ]
    components = PurePosixPath(path).parts
    probing = True
    for index, component in enumerate(components):
        policy.append((component, inherited_policy))
        if index == len(components) - 1:
            continue
        if not probing:
            continue
        child = current / component
        child_information = existing_path(child)
        if child_information is None:
            # A prospective child inherits the current directory's policy; do
            # not probe any deeper or query an unrelated ancestor for later
            # components after the first missing directory.
            probing = False
            continue
        if not child_information.is_directory:
            raise PathIdentityError("Windows path identity query failed")
        current = child
        inherited_policy = directory_policy(current)
        policy = [
            ("\0".join(("anchor", *child_information.identity)), True)
        ]
    return tuple(policy)


def _windows_path_policy_key(
    policy: tuple[tuple[str, bool], ...],
) -> tuple[str, ...]:
    return tuple(
        f"S:{component}"
        if case_sensitive
        else f"I:{_cached_windows_path_key(component)}"
        for component, case_sensitive in policy
    )


def _windows_path_policies_equal(
    left_policy: tuple[tuple[str, bool], ...],
    right_policy: tuple[tuple[str, bool], ...],
) -> bool:
    """Verify a key collision under each containing directory's policy."""
    if len(left_policy) != len(right_policy):
        return False
    for (left_component, left_sensitive), (
        right_component,
        right_sensitive,
    ) in zip(left_policy, right_policy, strict=True):
        if left_sensitive != right_sensitive:
            return False
        if left_sensitive:
            if left_component != right_component:
                return False
        elif not _host_paths_equal(left_component, right_component):
            return False
    return True


def _windows_filesystem_path_key(project_root: Path, path: str) -> tuple[str, ...]:
    """Build a component key honoring mixed per-directory Windows policies."""
    return _windows_path_policy_key(_windows_path_policy(project_root, path))


def _windows_filesystem_paths_equal(
    project_root: Path,
    left: str,
    right: str,
) -> bool:
    return _windows_path_policies_equal(
        _windows_path_policy(project_root, left),
        _windows_path_policy(project_root, right),
    )


@lru_cache(maxsize=1)
def _load_windows_path_api() -> _WindowsPathApi:
    """Load fail-closed wrappers for the two authoritative Win32 operations."""
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except Exception:
        raise PathIdentityError(
            "Windows path API unavailable (library load failed)"
        ) from None

    try:
        compare = kernel32.CompareStringOrdinal
        map_string = kernel32.LCMapStringEx
    except Exception:
        raise PathIdentityError(
            "Windows path API unavailable (required symbol missing)"
        ) from None

    try:
        compare.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_int,
            ctypes.c_wchar_p,
            ctypes.c_int,
            ctypes.c_int,
        ]
        compare.restype = ctypes.c_int
        map_string.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint,
            ctypes.c_wchar_p,
            ctypes.c_int,
            ctypes.c_wchar_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ssize_t,
        ]
        map_string.restype = ctypes.c_int
    except Exception:
        raise PathIdentityError(
            "Windows path API unavailable (signature configuration failed)"
        ) from None

    def map_key(path: str) -> str:
        # LOCALE_NAME_INVARIANT plus LCMAP_UPPERCASE, without
        # LCMAP_LINGUISTIC_CASING, requests Windows' file-system casing rules.
        try:
            required = map_string(
                "", _LCMAP_UPPERCASE, path, -1, None, 0, None, None, 0
            )
            if required == 0:
                raise PathIdentityError("Windows path case mapping failed")
            buffer = ctypes.create_unicode_buffer(required)
            written = map_string(
                "",
                _LCMAP_UPPERCASE,
                path,
                -1,
                buffer,
                required,
                None,
                None,
                0,
            )
            if written == 0:
                raise PathIdentityError("Windows path case mapping failed")
            return buffer.value
        except PathIdentityError:
            raise
        except Exception:
            raise PathIdentityError("Windows path case mapping failed") from None

    def equivalent(left: str, right: str) -> bool:
        # Portable paths reject NUL, so null-terminated comparison is exact and
        # lets Win32 count supplementary characters in native UTF-16 units.
        try:
            result = compare(left, -1, right, -1, 1)
        except Exception:
            raise PathIdentityError("Windows path comparison failed") from None
        if result == 0:
            raise PathIdentityError("Windows path comparison failed")
        return result == 2  # CSTR_EQUAL

    return _WindowsPathApi(map_key=map_key, equivalent=equivalent)


@lru_cache(maxsize=_WINDOWS_COMPARISON_CACHE_SIZE)
def _cached_windows_paths_equal(left: str, right: str) -> bool:
    return _load_windows_path_api().equivalent(left, right)


@lru_cache(maxsize=_WINDOWS_COMPARISON_CACHE_SIZE)
def _cached_windows_path_key(path: str) -> str:
    return _load_windows_path_api().map_key(path)


def _host_paths_equal(left: str, right: str) -> bool:
    """Compare path spellings using only authoritative host semantics.

    Windows delegates to ``CompareStringOrdinal`` with case ignored.  Other
    hosts remain case-sensitive because this project has no repository-level
    filesystem policy that can authoritatively override their exact identity.
    """
    if left == right:
        return True
    if not _uses_windows_path_identity():
        return False
    if right < left:
        left, right = right, left
    return _cached_windows_paths_equal(left, right)


class _PathIdentityRegistry:
    """Intern host path identities using a native key plus verified collisions."""

    def __init__(self, project_root: Path | None = None) -> None:
        self._windows_identity = _uses_windows_path_identity()
        root = Path.cwd() if project_root is None else project_root
        if self._windows_identity:
            try:
                self._project_root = root.resolve(strict=True)
            except OSError:
                raise PathIdentityError(
                    "Windows directory identity query failed"
                ) from None
        else:
            self._project_root = root.resolve()
        # ``Path`` equality follows the host filesystem policy. On Windows
        # that makes case-distinct siblings such as ``Foo`` and ``foo`` hash
        # equal even when their parent has per-directory case sensitivity
        # enabled. Preserve the exact, already-normalized spelling used while
        # walking the authored path; the component policy still aliases true
        # case variants beneath insensitive parents.
        self._directory_policy: dict[str, bool] = {}
        self._path_observations: dict[
            str, _WindowsExistingPath | None
        ] = {}
        self._exact: dict[str, int] = {}
        self._native_buckets: dict[
            tuple[str, ...],
            list[tuple[str, int, tuple[tuple[str, bool], ...]]],
        ] = {}
        self._next_identity = 0

    def _case_sensitive_directory(self, directory: Path) -> bool:
        exact_key = str(directory)
        policy = self._directory_policy.get(exact_key)
        if policy is None:
            policy = _directory_uses_case_sensitive_identity(directory)
            self._directory_policy[exact_key] = policy
        return policy

    def _existing_path(
        self,
        path: Path,
    ) -> _WindowsExistingPath | None:
        exact_key = str(path)
        if exact_key not in self._path_observations:
            self._path_observations[exact_key] = (
                _windows_existing_path_identity(path)
            )
        return self._path_observations[exact_key]

    def intern(self, path: str) -> int:
        existing = self._exact.get(path)
        if existing is not None:
            return existing

        bucket: list[
            tuple[str, int, tuple[tuple[str, bool], ...]]
        ] | None = None
        path_policy: tuple[tuple[str, bool], ...] | None = None
        if self._windows_identity:
            host_path = self._project_root.joinpath(*PurePosixPath(path).parts)
            existing_identity = self._existing_path(host_path)
            if existing_identity is not None:
                native_key = ("E", *existing_identity.identity)
                bucket = self._native_buckets.setdefault(native_key, [])
                if bucket:
                    identity = bucket[0][1]
                    self._exact[path] = identity
                    return identity
                path_policy = ()
            else:
                path_policy = _windows_path_policy(
                    self._project_root,
                    path,
                    self._case_sensitive_directory,
                    self._existing_path,
                )
                native_key = _windows_path_policy_key(path_policy)
                bucket = self._native_buckets.setdefault(native_key, [])
                for _representative, identity, representative_policy in bucket:
                    if _windows_path_policies_equal(
                        path_policy,
                        representative_policy,
                    ):
                        self._exact[path] = identity
                        return identity
            if len(bucket) >= _WINDOWS_COLLISION_BUCKET_LIMIT:
                raise PathIdentityError(
                    "Windows path identity collision limit exceeded"
                )

        identity = self._next_identity
        self._next_identity += 1
        self._exact[path] = identity
        if bucket is not None:
            assert path_policy is not None
            bucket.append((path, identity, path_policy))
        return identity


def _canonical_file_scopes(
    tasks: list[Task],
    *,
    project_root: Path | None = None,
) -> tuple[dict[str, frozenset[int]], dict[int, str]]:
    """Validate and intern task scopes under the authoritative host policy.

    Validation is completed before native identity work begins.  Canonical
    spellings are then interned in exact lexical order, independent of task or
    ``likely_files`` input order.  Each identity's display spelling is selected
    by lowest task ID and then exact authored spelling, preserving the
    established sorted-task display policy while removing file-order effects.
    This keeps the full persisted conflict-group record deterministic and
    retains an authored spelling rather than inventing an engine spelling.
    """
    canonical_by_task: dict[str, list[tuple[str, str]]] = {}
    for task in tasks:
        canonical_by_task[task.id] = [
            (_canonical_project_path(path), path) for path in task.likely_files
        ]

    registry = _PathIdentityRegistry(project_root)
    identity_by_path = {
        path: registry.intern(path)
        for path in sorted(
            {
                canonical
                for task_paths in canonical_by_task.values()
                for canonical, _authored in task_paths
            }
        )
    }
    scopes: dict[str, frozenset[int]] = {}
    display_candidates: dict[int, list[tuple[str, str]]] = {}
    for task_id in sorted(canonical_by_task):
        identities = {
            identity_by_path[canonical]
            for canonical, _authored in canonical_by_task[task_id]
        }
        scopes[task_id] = frozenset(identities)
        for canonical, authored in canonical_by_task[task_id]:
            display_candidates.setdefault(identity_by_path[canonical], []).append(
                (task_id, authored)
            )
    display_paths = {
        identity: min(candidates)[1]
        for identity, candidates in display_candidates.items()
    }
    return scopes, display_paths


def _serial_depth(tasks: list[Task]) -> int:
    by_id = {task.id: task for task in tasks}
    visiting: list[str] = []
    memo: dict[str, int] = {}

    def visit(task_id: str) -> int:
        if task_id in memo:
            return memo[task_id]
        if task_id in visiting:
            start = visiting.index(task_id)
            cycle = visiting[start:] + [task_id]
            raise BundlePlanningError(
                "bundle planning found a dependency cycle: " + " -> ".join(cycle)
            )
        visiting.append(task_id)
        depth = 1 + max(
            (visit(dep) for dep in by_id[task_id].dependencies if dep in by_id),
            default=0,
        )
        visiting.pop()
        memo[task_id] = depth
        return depth

    return max((visit(task_id) for task_id in sorted(by_id)), default=0)


def _risk_angles(tasks: list[Task]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    angles = {"correctness", "security", "integration"}
    policies: set[str] = set()
    joined_files = " ".join(path.lower() for task in tasks for path in task.likely_files)
    classifications = {
        "security": ("security", "auth", "crypto", "secret"),
        "privacy": ("privacy", "pii", "personal_data"),
        "topology": ("schema", "migration", "state/", "state\\"),
        "transport": ("http", "network", "sync/", "sync\\", "mcp"),
        "release": ("release", "packaging", "changelog"),
        "public-api": ("cli/", "cli\\", "api", "__init__.py"),
    }
    for angle, markers in classifications.items():
        if any(marker in joined_files for marker in markers):
            angles.add(angle)
            policies.add(angle)
    if any(
        (task.scores.blast_radius or 0) >= 4
        or (task.scores.review_risk or 0) >= 4
        for task in tasks
    ):
        angles.add("blast-radius")
        policies.add("high-score")
    return tuple(sorted(angles)), tuple(sorted(policies))


def build_bundle_plan(
    tasks: list[Task],
    *,
    max_tasks: int = 12,
    max_serial_stages: int = 6,
    project_root: Path | None = None,
) -> BundlePlanReport:
    """Propose stable graph/file components and quantify execution overhead."""
    for name, value in (
        ("max_tasks", max_tasks),
        ("max_serial_stages", max_serial_stages),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 500:
            raise BundlePlanningError(f"{name} must be an integer in the range 1-500")
    ids = [task.id for task in tasks]
    duplicates = sorted({task_id for task_id in ids if ids.count(task_id) > 1})
    if duplicates:
        raise BundlePlanningError(f"bundle planning found duplicate task ids: {duplicates}")
    ordered_input = sorted(tasks, key=lambda task: task.id)
    canonical_files, display_path = _canonical_file_scopes(
        ordered_input,
        project_root=project_root,
    )
    inferred_dependencies = {
        task.id: set(task.dependencies) for task in ordered_input
    }
    for left in ordered_input:
        for right in ordered_input:
            if (
                left.id != right.id
                and canonical_files[left.id]
                and canonical_files[left.id] < canonical_files[right.id]
                and left.id not in inferred_dependencies[right.id]
            ):
                inferred_dependencies[left.id].add(right.id)
    ordered = [
        task.model_copy(
            update={"dependencies": sorted(inferred_dependencies[task.id])}
        )
        for task in ordered_input
    ]
    task_ids = {task.id for task in ordered}
    missing_dependencies = sorted(
        {
            dependency
            for task in ordered
            for dependency in task.dependencies
            if dependency not in task_ids
        }
    )
    if missing_dependencies:
        raise BundlePlanningError(
            "bundle planning found missing dependency nodes: "
            f"{missing_dependencies}"
        )
    adjacency: dict[str, set[str]] = {task.id: set() for task in ordered}
    overlap_files: set[str] = set()
    overlap_pair_count = 0
    for task in ordered:
        for dependency in task.dependencies:
            if dependency in task_ids:
                adjacency[task.id].add(dependency)
                adjacency[dependency].add(task.id)
    for index, left in enumerate(ordered):
        left_files = set(canonical_files[left.id])
        for right in ordered[index + 1 :]:
            overlap = left_files & set(canonical_files[right.id])
            if not overlap:
                continue
            overlap_pair_count += 1
            overlap_files.update(overlap)
            adjacency[left.id].add(right.id)
            adjacency[right.id].add(left.id)

    components: list[tuple[str, ...]] = []
    remaining = set(task_ids)
    while remaining:
        seed = min(remaining)
        stack = [seed]
        component: set[str] = set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            stack.extend(sorted(adjacency[current] - component, reverse=True))
        remaining -= component
        components.append(tuple(sorted(component)))
    components.sort()

    by_id = {task.id: task for task in ordered}
    proposals: list[BundleProposal] = []
    high_risk: set[str] = set()
    for index, component in enumerate(components, start=1):
        members = [by_id[task_id] for task_id in component]
        member_files = [set(canonical_files[task.id]) for task in members]
        component_overlap = {
            path
            for left_index, left in enumerate(member_files)
            for right in member_files[left_index + 1 :]
            for path in left & right
        }
        risk_members = [
            task.model_copy(
                update={
                    "likely_files": sorted(
                        display_path[path] for path in canonical_files[task.id]
                    )
                }
            )
            for task in members
        ]
        angles, policies = _risk_angles(risk_members)
        high_risk.update(policies)
        proposals.append(
            BundleProposal(
                id=f"BP{index:03d}",
                task_ids=component,
                serial_depth=_serial_depth(members),
                overlap_files=tuple(
                    sorted(display_path[path] for path in component_overlap)
                ),
                review_angles=angles,
                expected_reviews=max(3, len(angles)),
            )
        )

    serial_depth = _serial_depth(ordered)
    breaches: list[str] = []
    if len(ordered) > max_tasks:
        breaches.append(f"task_count {len(ordered)} exceeds limit {max_tasks}")
    if serial_depth > max_serial_stages:
        breaches.append(
            f"serial_depth {serial_depth} exceeds limit {max_serial_stages}"
        )
    return BundlePlanReport(
        task_count=len(ordered),
        serial_depth=serial_depth,
        overlap_pair_count=overlap_pair_count,
        overlap_files=tuple(sorted(display_path[path] for path in overlap_files)),
        proposed_bundles=tuple(proposals),
        expected_review_count=sum(item.expected_reviews for item in proposals),
        high_risk_policies=tuple(sorted(high_risk)),
        expected_checkpoints=len(proposals),
        max_tasks=max_tasks,
        max_serial_stages=max_serial_stages,
        limit_breaches=tuple(breaches),
        acknowledgement_required=bool(breaches),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _DependencyReachability:
    """Maintain dependency reachability while inferred edges are accepted.

    Reachability rows are Python integer bitsets.  The initial explicit graph
    is closed once with bitset Floyd-Warshall; each candidate is then checked
    with one bit lookup.  When a safe edge adds new reachability, only newly
    reachable pairs are propagated.  Across one inference run each pair can
    become reachable at most once, keeping dense nested plans bounded instead
    of traversing the graph once per candidate.

    The counters are intentionally structural diagnostics for regression
    tests.  They count incremental closure work, not wall-clock time.
    """

    def __init__(self, dependencies: dict[str, set[str]]) -> None:
        task_ids = sorted(dependencies)
        self._index = {task_id: index for index, task_id in enumerate(task_ids)}
        self._reachable = [0] * len(task_ids)

        for task_id, dependency_ids in dependencies.items():
            source = self._index[task_id]
            for dependency_id in dependency_ids:
                target = self._index.get(dependency_id)
                if target is not None:
                    self._reachable[source] |= 1 << target

        # Bitset Floyd-Warshall closes the explicit graph once.  Mutating rows
        # in place is valid because every newly exposed path also uses the
        # current intermediate and therefore belongs in the same closure step.
        for intermediate in range(len(task_ids)):
            intermediate_bit = 1 << intermediate
            targets = self._reachable[intermediate]
            for source, reachable in enumerate(self._reachable):
                if reachable & intermediate_bit:
                    self._reachable[source] |= targets

        self._reaching = [0] * len(task_ids)
        for source, reachable in enumerate(self._reachable):
            source_bit = 1 << source
            while reachable:
                target_bit = reachable & -reachable
                target = target_bit.bit_length() - 1
                self._reaching[target] |= source_bit
                reachable ^= target_bit

        self.cycle_checks = 0
        self.closure_row_updates = 0
        self.closure_pair_updates = 0

    def add_if_acyclic(self, task_id: str, dependency_id: str) -> bool:
        """Add reachability for a safe edge and return whether it was accepted."""
        self.cycle_checks += 1
        source = self._index[task_id]
        target = self._index[dependency_id]
        source_bit = 1 << source
        target_bit = 1 << target

        # task -> dependency cycles exactly when dependency already reaches task.
        if self._reachable[target] & source_bit:
            return False

        # A direct edge that is already represented transitively changes no
        # reachability, though the caller still records the inferred direct edge.
        if self._reachable[source] & target_bit:
            return True

        target_reach = self._reachable[target] | target_bit
        predecessors = (self._reaching[source] | source_bit) & ~self._reaching[target]

        while predecessors:
            predecessor_bit = predecessors & -predecessors
            predecessor = predecessor_bit.bit_length() - 1
            newly_reachable = target_reach & ~self._reachable[predecessor]
            self._reachable[predecessor] |= target_reach
            self.closure_row_updates += 1
            self.closure_pair_updates += newly_reachable.bit_count()

            while newly_reachable:
                reached_bit = newly_reachable & -newly_reachable
                reached = reached_bit.bit_length() - 1
                self._reaching[reached] |= predecessor_bit
                newly_reachable ^= reached_bit

            predecessors ^= predecessor_bit

        return True


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def infer_dependencies(
    tasks: list[Task],
    *,
    project_root: Path | None = None,
) -> list[Task]:
    """Return a new Task list with ``.dependencies`` populated by subset heuristics.

    For each pair (A, B): if A.likely_files is a *strict* subset of B.likely_files,
    A is added to B.dependencies.  "B is a broader change; A specialises B, so B
    should be authored first."

    Pure — takes a Task list, returns a Task list.  Input tasks are never mutated;
    output tasks are produced via ``model_copy``.

    Args:
        tasks: List of Task models (likely_files populated from PRD parse).

    Returns:
        New list of Task instances with dependencies set from subset edges.
        Tasks with no inferred dependencies are returned unchanged.

    Raises:
        BundlePlanningError: A likely-file path is absolute, escaping, or not
            portable.  Validation fails before any result is returned and never
            mutates the input tasks.
    """
    if not tasks:
        return []
    file_sets, _ = _canonical_file_scopes(tasks, project_root=project_root)
    return _infer_dependencies_with_scopes(tasks, file_sets)


def _infer_dependencies_with_scopes(
    tasks: list[Task],
    file_sets: dict[str, frozenset[int]],
) -> list[Task]:
    """Infer dependencies using caller-validated, reusable file scopes."""
    # Build a map from task ID to its file set, then find all strict-subset edges.
    # An edge A → B means "A.files ⊂ B.files (strict)", so B depends on A.
    # Wait — task spec says: "if Task A's likely_files is a strict subset of
    # Task B's, A depends on B (because B is a broader change that A specialises;
    # the broader work usually goes first)."
    # So: A_files ⊂ B_files (strict) → A.dependencies.append(B.id)

    # Collect dependency edges: new_deps[task_id] = set of dependency IDs.
    new_deps: dict[str, set[str]] = {t.id: set(t.dependencies) for t in tasks}
    reachability = _DependencyReachability(new_deps)

    # Stable ordering makes the chosen safe subset deterministic even when the
    # caller supplies the same tasks in a different order.
    task_ids = sorted(file_sets)
    for id_a in task_ids:
        set_a = file_sets[id_a]
        if not set_a:
            # A task with no likely_files cannot be a subset of anything.
            continue
        for id_b in task_ids:
            if id_a == id_b:
                continue
            set_b = file_sets[id_b]
            # Strict subset: A ⊂ B means A ⊆ B and A ≠ B.
            if set_a < set_b:
                # A specialises B → A depends on B.
                if id_b in new_deps[id_a]:
                    continue
                if reachability.add_if_acyclic(id_a, id_b):
                    new_deps[id_a].add(id_b)

    # Build the output list, replacing only tasks whose dependency set changed.
    updated: list[Task] = []
    for task in tasks:
        merged = sorted(new_deps[task.id])
        if merged != task.dependencies:
            updated.append(task.model_copy(update={"dependencies": merged}))
        else:
            updated.append(task)

    return updated


def infer_conflict_groups(
    tasks: list[Task],
    *,
    project_root: Path | None = None,
) -> tuple[list[Task], list[ConflictGroup]]:
    """Return (tasks-with-conflict_groups-populated, ConflictGroup list).

    For each pair of tasks with ANY ``likely_files`` overlap that are NOT in a
    strict subset/superset relationship, group them together.  Groups are named
    ``CG-<sorted-task-ids>`` where the IDs are separated by ``-``.

    A task may appear in multiple conflict groups (one per pair that it is part
    of).  The ``Task.conflict_groups`` field records the IDs of all groups the
    task belongs to.

    Pure — takes a Task list, returns a new Task list and ConflictGroup list.

    Args:
        tasks: List of Task models (dependency inference should already be applied).

    Returns:
        Tuple of (updated Task list, list of ConflictGroup instances).

    Raises:
        BundlePlanningError: A likely-file path is absolute, escaping, or not
            portable.  Validation fails before any result is returned and never
            mutates the input tasks.
    """
    if not tasks:
        return [], []
    file_sets, display_path = _canonical_file_scopes(
        tasks,
        project_root=project_root,
    )
    return _infer_conflict_groups_with_scopes(tasks, file_sets, display_path)


def _infer_conflict_groups_with_scopes(
    tasks: list[Task],
    file_sets: dict[str, frozenset[int]],
    display_path: dict[int, str],
) -> tuple[list[Task], list[ConflictGroup]]:
    """Infer conflicts using caller-validated, reusable file scopes."""

    # Map task ID → set of conflict-group IDs it belongs to.
    task_conflict_groups: dict[str, set[str]] = {t.id: set() for t in tasks}
    conflict_groups: list[ConflictGroup] = []

    task_ids = sorted(t.id for t in tasks)
    seen_pairs: set[frozenset[str]] = set()

    for idx_a in range(len(task_ids)):
        id_a = task_ids[idx_a]
        set_a = file_sets[id_a]
        if not set_a:
            continue
        for idx_b in range(idx_a + 1, len(task_ids)):
            id_b = task_ids[idx_b]
            pair = frozenset({id_a, id_b})
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            set_b = file_sets[id_b]
            if not set_b:
                continue

            overlap = set_a & set_b
            if not overlap:
                continue

            # If one is a strict subset of the other, skip — that's a dependency,
            # not a conflict.
            if set_a < set_b or set_b < set_a:
                continue

            # Partial overlap and neither is a subset: this is a conflict group.
            sorted_ids = [id_a, id_b]
            cg_id = "CG-" + "-".join(sorted_ids)
            cg = ConflictGroup(
                id=cg_id,
                name=cg_id,
                task_ids=sorted_ids,
                reason=(
                    f"Tasks {sorted_ids[0]} and {sorted_ids[1]} share overlapping files: "
                    + ", ".join(sorted(display_path[path] for path in overlap))
                ),
            )
            conflict_groups.append(cg)
            task_conflict_groups[id_a].add(cg_id)
            task_conflict_groups[id_b].add(cg_id)

    # Build updated task list.
    updated_tasks: list[Task] = []
    for task in tasks:
        new_cgs = sorted(task_conflict_groups[task.id])
        existing_cgs = sorted(task.conflict_groups)
        if new_cgs != existing_cgs:
            updated_tasks.append(
                task.model_copy(update={"conflict_groups": new_cgs})
            )
        else:
            updated_tasks.append(task)

    return updated_tasks, conflict_groups


def infer_all(
    tasks: list[Task],
    *,
    project_root: Path | None = None,
) -> InferenceResult:
    """Compose dependency and conflict inference into a single result.

    Apply in order: dependencies first, then conflict groups.  This ordering
    matters because ``infer_conflict_groups`` skips strict-subset pairs which
    are correctly classified as dependencies by ``infer_dependencies``.

    Pure — takes a Task list, returns an InferenceResult.  No I/O.

    Args:
        tasks: List of Task models to annotate.

    Returns:
        InferenceResult with the fully-annotated Task list and conflict groups.

    Raises:
        BundlePlanningError: A likely-file path is absolute, escaping, or not
            portable.  Validation fails before an ``InferenceResult`` is returned
            and never mutates the input tasks.
    """
    if not tasks:
        return InferenceResult(tasks=[], conflict_groups=[])
    file_sets, display_path = _canonical_file_scopes(
        tasks,
        project_root=project_root,
    )
    tasks_with_deps = _infer_dependencies_with_scopes(tasks, file_sets)
    tasks_with_all, conflict_groups = _infer_conflict_groups_with_scopes(
        tasks_with_deps, file_sets, display_path
    )
    return InferenceResult(tasks=tasks_with_all, conflict_groups=conflict_groups)


# ---------------------------------------------------------------------------
# expand_task — LLM-augmented sub-task proposal (additive)
# ---------------------------------------------------------------------------


def expand_task(
    task: Task,
    *,
    provider: LLMProvider | None = None,
    threshold: int = _EXPAND_COMPLEXITY_THRESHOLD,
) -> list[SubtaskProposal]:
    """Propose 2-5 sub-tasks for a complex Task using an LLM.

    Deterministic baseline (provider=None or complexity < *threshold*):
    returns ``[]``.  The deterministic engine never proposes sub-tasks — that
    responsibility lies with the PRD author (manual subtask entries in prd.md).

    With ``provider=`` and ``task.scores.complexity >= threshold`` the
    provider is asked to return a JSON array of {title, description,
    acceptance_criteria, likely_files}.  On any failure (provider error, JSON
    parse error, schema mismatch) a warning is printed to stderr and ``[]``
    is returned — failures NEVER raise.

    Args:
        task: The Task to expand.  Must already be scored.
        provider: Optional LLM provider.
        threshold: Inclusive complexity cut-off below which the task is
            deemed simple enough to ship as-is.  Defaults to 4; callers with
            a loaded config pass ``Config.auto_expand_threshold`` (v1.21.0).

    Returns:
        A list of :class:`SubtaskProposal` (possibly empty).  Never raises.
    """
    if provider is None:
        return []

    complexity = task.scores.complexity
    if complexity is None or complexity < threshold:
        return []

    # Local import — keeps the optional LLM dep out of the main import graph.
    from anvil.planning.llm import LLMProviderError

    user_payload = json.dumps(
        {
            "task_id": task.id,
            "title": task.title,
            "description": task.description,
            "likely_files": task.likely_files,
            "acceptance_criteria": task.acceptance_criteria,
            "scores": {
                "complexity": task.scores.complexity,
                "parallelizability": task.scores.parallelizability,
                "context_load": task.scores.context_load,
                "blast_radius": task.scores.blast_radius,
                "review_risk": task.scores.review_risk,
                "agent_suitability": task.scores.agent_suitability,
            },
        },
        sort_keys=True,
    )

    try:
        response = provider.generate(
            system=_EXPAND_SYSTEM_PROMPT,
            user=user_payload,
            max_tokens=_EXPAND_MAX_TOKENS,
        )
    except LLMProviderError as exc:
        print(
            f"warning: LLM expansion of {task.id} failed ({exc}); "
            "no sub-task proposals produced.",
            file=sys.stderr,
        )
        return []
    except Exception as exc:  # noqa: BLE001 — Phase 7 contract: LLM never aborts
        # Non-conforming custom provider; preserve deterministic-empty result.
        print(
            f"warning: LLM expansion of {task.id} raised non-conforming "
            f"{type(exc).__name__}: {exc}; no sub-task proposals produced.",
            file=sys.stderr,
        )
        return []

    proposals = _parse_subtask_response(task.id, response.text)
    return proposals


_FENCE_RE = re.compile(
    # Matches the OPENING fence: ```json | ```jsonl | ``` plus any trailing
    # whitespace and a newline. Captures nothing — we just need the span to
    # drop it.
    r"^```(?:json|jsonl|JSON)?\s*\n",
    re.MULTILINE,
)


def _strip_markdown_fences(text: str) -> str:
    """Drop opening ```json fence + closing ``` fence if present.

    Tolerates both fenced (` ```json [...] ``` `) and unfenced output. The
    fence patterns are anchored: opening fence must start the (stripped)
    text; closing fence must end it. Mid-response fences are intentionally
    left alone — they could be part of nested code blocks inside a
    description field.
    """
    stripped = text.strip()
    m = _FENCE_RE.match(stripped)
    if m:
        stripped = stripped[m.end():]
        # Drop a trailing ``` (optionally surrounded by whitespace) if
        # present at end-of-string.
        if stripped.rstrip().endswith("```"):
            tail_idx = stripped.rstrip().rfind("```")
            stripped = stripped[:tail_idx].rstrip()
    return stripped


def _extract_first_json_array(text: str) -> str | None:
    """Return the substring spanning the first balanced JSON array, or None.

    Handles the case where the LLM prepends prose ("Here are 3 sub-tasks:
    [{...}, ...]") despite the prompt. Bracket-matching is simple but
    string-aware — quoted strings can contain ``[`` / ``]`` chars that
    should not affect depth. The matcher is NOT a full JSON parser; it
    returns the substring for ``json.loads`` to validate.
    """
    in_string = False
    escape = False
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "[":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "]":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    return text[start:i + 1]
    return None


def _parse_subtask_response(task_id: str, raw: str) -> list[SubtaskProposal]:
    """Parse the LLM's JSON-list response into ``SubtaskProposal``s.

    Tolerant of common LLM output quirks:
    - leading/trailing whitespace
    - markdown code fences (```json ... ``` or ``` ... ```)
    - leading/trailing prose around the JSON array (regex-extracts the first
      bracketed array as fallback)

    Strict on schema once a JSON array is in hand. On any parse failure
    surfaces a warning that INCLUDES a sample of the raw response (first
    300 chars) so the user can see what the LLM actually wrote without
    re-running with extra verbosity.
    """
    text = raw.strip()
    if not text:
        print(
            f"warning: LLM expansion of {task_id} returned an empty "
            "response; ignoring.",
            file=sys.stderr,
        )
        return []

    # Strip markdown code fences. Modern Claude models often wrap JSON in
    # ```json ... ``` despite the prompt saying not to — silently handle
    # the common case rather than make the user debug a non-JSON warning.
    text = _strip_markdown_fences(text)

    decoded: object | None = None
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        # Fallback: regex-extract the first balanced JSON array. Tolerates
        # prose preambles like "Here are 3 sub-tasks: [...]" without
        # forcing the user to debug a prompt-tuning issue.
        extracted = _extract_first_json_array(text)
        if extracted is not None:
            try:
                decoded = json.loads(extracted)
            except json.JSONDecodeError:
                decoded = None

    if decoded is None:
        sample = raw.strip()[:300]
        if len(raw.strip()) > 300:
            sample += "…"
        print(
            f"warning: LLM expansion of {task_id} returned non-JSON; "
            f"ignoring. First 300 chars of response: {sample!r}",
            file=sys.stderr,
        )
        return []

    if not isinstance(decoded, list):
        print(
            f"warning: LLM expansion of {task_id} returned non-list JSON; ignoring.",
            file=sys.stderr,
        )
        return []

    proposals: list[SubtaskProposal] = []
    for idx, item in enumerate(decoded):
        if not isinstance(item, dict):
            print(
                f"warning: LLM expansion of {task_id}: item {idx} is not an "
                "object; skipping.",
                file=sys.stderr,
            )
            continue
        title = item.get("title")
        description = item.get("description", "")
        acceptance_criteria = item.get("acceptance_criteria", []) or []
        likely_files = item.get("likely_files", []) or []
        if not isinstance(title, str) or not title.strip():
            print(
                f"warning: LLM expansion of {task_id}: item {idx} missing "
                "title; skipping.",
                file=sys.stderr,
            )
            continue
        if not isinstance(acceptance_criteria, list) or not isinstance(likely_files, list):
            print(
                f"warning: LLM expansion of {task_id}: item {idx} has invalid "
                "list fields; skipping.",
                file=sys.stderr,
            )
            continue
        proposals.append(
            SubtaskProposal(
                title=title.strip(),
                description=str(description).strip(),
                acceptance_criteria=[str(c).strip() for c in acceptance_criteria if str(c).strip()],
                likely_files=[str(f).strip() for f in likely_files if str(f).strip()],
            )
        )

    # The prompt requests 2-5; tolerate edge cases but cap upper bound so a
    # runaway LLM cannot flood the output.
    if len(proposals) > _EXPAND_MAX_SUBTASKS:
        proposals = proposals[:_EXPAND_MAX_SUBTASKS]
    if len(proposals) < _EXPAND_MIN_SUBTASKS:
        # Spec says 2-5; fewer than 2 is not a useful split — warn but still
        # return what we got so the caller can decide.
        print(
            f"warning: LLM expansion of {task_id} returned only "
            f"{len(proposals)} sub-task(s); spec asks for "
            f"{_EXPAND_MIN_SUBTASKS}-{_EXPAND_MAX_SUBTASKS}.",
            file=sys.stderr,
        )

    return proposals
