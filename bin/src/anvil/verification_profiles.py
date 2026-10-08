"""Bounded repository-owned verification contracts. Resolution executes nothing."""
from __future__ import annotations

import hashlib
import os
import stat
import tomllib
from contextlib import ExitStack
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from anvil.state.hashing import domain_separated_sha256
from anvil.state.models import (
    ProofKind,
    ProofRequirement,
    Verification,
    VerificationProfileBinding,
    VerificationProfileReference,
    _profile_commands,
    _profile_source_path,
)

MANIFEST = "anvil-verification.toml"
MAX_FILE_BYTES = 1024 * 1024
MAX_SOURCE_BYTES = 16 * MAX_FILE_BYTES
ProfileErrorCode = Literal[
    "invalid_reference", "invalid_manifest", "invalid_path", "file_unavailable",
    "unsafe_file", "file_changed", "size_limit", "source_mismatch",
    "profile_missing", "platform_unsupported", "binding_mismatch", "invalid_verification",
]


class ProfileError(ValueError):
    """Closed diagnostic: no source text, paths, commands or parser errors."""

    def __init__(self, code: ProfileErrorCode) -> None:
        self.code = code
        super().__init__(f"verification profile refused: {code}")


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _windows_open(path: Path, *, directory: bool) -> int:
    # Refuse reparse points before reading. Handles deny write/delete sharing,
    # holding every ancestor against rename until the complete read finishes.
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    # OPEN_REPARSE_POINT | BACKUP_SEMANTICS; OPEN_EXISTING; FILE_SHARE_READ.
    handle = create(str(path), 0x80000000, 1, None, 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise OSError("cannot safely open profile file")
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close(handle)
        raise
    return descriptor


def _read_file(root: Path, portable_path: str) -> tuple[bytes, tuple[int, ...]]:
    """Read once through held no-follow ancestors, with identity/byte checks."""
    try:
        _profile_source_path(portable_path)
        if not root.is_absolute() or ".." in root.parts:
            raise ValueError
    except (ValueError, UnicodeError):
        raise ProfileError("invalid_path") from None
    candidate = root.joinpath(*portable_path.split("/"))
    try:
        with ExitStack() as stack:
            current = Path(candidate.anchor)
            descriptor = None
            inspected: list[tuple[Path, os.stat_result]] = []
            for index, part in enumerate((candidate.anchor, *candidate.parts[1:])):
                if index:
                    current /= part
                directory = index < len(candidate.parts) - 1
                if os.name == "nt":
                    descriptor = _windows_open(current, directory=directory)
                else:
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                    flags |= getattr(os, "O_CLOEXEC", 0)
                    if directory:
                        flags |= os.O_DIRECTORY
                    descriptor = os.open(
                        str(current) if index == 0 else part,
                        flags, dir_fd=descriptor,
                    )
                stack.callback(os.close, descriptor)
                info = os.fstat(descriptor)
                path_info = os.lstat(current)
                if (
                    stat.S_ISLNK(path_info.st_mode)
                    or getattr(path_info, "st_file_attributes", 0) & 0x400
                    or not os.path.samestat(info, path_info)
                    or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                ):
                    raise ProfileError("unsafe_file")
                inspected.append((current, info))
            assert descriptor is not None
            before = os.fstat(descriptor)
            if before.st_size > MAX_FILE_BYTES:
                raise ProfileError("size_limit")
            chunks = []
            size = 0
            while chunk := os.read(descriptor, min(65536, MAX_FILE_BYTES + 1 - size)):
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise ProfileError("size_limit")
                chunks.append(chunk)
            data = b"".join(chunks)
            # A second descriptor read detects edits even where timestamp resolution
            # is coarse. The result never contains bytes from an unchecked handle.
            os.lseek(descriptor, 0, os.SEEK_SET)
            offset = 0
            while chunk := os.read(descriptor, min(65536, MAX_FILE_BYTES + 1 - offset)):
                if data[offset:offset + len(chunk)] != chunk:
                    raise ProfileError("file_changed")
                offset += len(chunk)
            if offset != len(data) or _identity(before) != _identity(os.fstat(descriptor)):
                raise ProfileError("file_changed")
            for path, info in inspected:
                after = os.lstat(path)
                if (
                    stat.S_ISLNK(after.st_mode)
                    or getattr(after, "st_file_attributes", 0) & 0x400
                    or not os.path.samestat(info, after)
                ):
                    raise ProfileError("file_changed")
            if _identity(before) != _identity(os.lstat(candidate)):
                raise ProfileError("file_changed")
            return data, _identity(before)
    except (OSError, ValueError) as exc:
        if isinstance(exc, ProfileError):
            raise
        raise ProfileError("file_unavailable") from None


def _manifest(data: bytes) -> dict:
    try:
        manifest = tomllib.loads(data.decode("utf-8"))
        if set(manifest) != {"schema_version", "profiles"}:
            raise ValueError
        if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
            raise ValueError
        profiles = manifest["profiles"]
        if type(profiles) is not dict or not 1 <= len(profiles) <= 64:
            raise ValueError
        for name, profile in profiles.items():
            VerificationProfileReference(name=name, platform="linux", source_sha256="0" * 64)
            if type(profile) is not dict or set(profile) != {"source_files", "platforms"}:
                raise ValueError
            sources = profile["source_files"]
            if type(sources) is not list or not 1 <= len(sources) <= 32:
                raise ValueError
            for source in sources:
                if type(source) is not str:
                    raise ValueError
                _profile_source_path(source)
            if len(set(sources)) != len(sources):
                raise ValueError
            platforms = profile["platforms"]
            if (
                type(platforms) is not dict or not platforms
                or set(platforms) - {"linux", "darwin", "windows"}
            ):
                raise ValueError
            for platform in platforms.values():
                if (
                    type(platform) is not dict or "commands" not in platform
                    or set(platform) - {"commands", "preflight_commands"}
                ):
                    raise ValueError
                for key in ("commands", "preflight_commands"):
                    commands = platform.get(key, [])
                    if (
                        type(commands) is not list or len(commands) > 16
                        or (key == "commands" and not commands)
                        or any(type(command) is not str for command in commands)
                    ):
                        raise ValueError
                    _profile_commands(tuple(commands))
        return profiles
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise ProfileError("invalid_manifest") from None


def resolve_profile(
    project_root: Path, reference: VerificationProfileReference,
) -> VerificationProfileBinding:
    """Resolve a pinned profile against an explicit absolute repository root."""
    try:
        reference = VerificationProfileReference.model_validate(reference)
    except ValidationError:
        raise ProfileError("invalid_reference") from None
    root = Path(project_root)
    manifest, manifest_identity = _read_file(root, MANIFEST)
    if hashlib.sha256(manifest).hexdigest() != reference.source_sha256:
        raise ProfileError("source_mismatch")
    profiles = _manifest(manifest)
    if reference.name not in profiles:
        raise ProfileError("profile_missing")
    profile = profiles[reference.name]
    if reference.platform not in profile["platforms"]:
        raise ProfileError("platform_unsupported")
    platform = profile["platforms"][reference.platform]
    snapshots = [(MANIFEST, manifest, manifest_identity)]
    digests = []
    size = 0
    for source in sorted(profile["source_files"]):
        data, identity = _read_file(root, source)
        size += len(data)
        if size > MAX_SOURCE_BYTES:
            raise ProfileError("size_limit")
        snapshots.append((source, data, identity))
        digests.append((source, hashlib.sha256(data).hexdigest()))
    # Check the whole input set after resolving; no silently mixed source epochs.
    for source, data, identity in snapshots:
        if _read_file(root, source) != (data, identity):
            raise ProfileError("file_changed")
    values = {
        "reference": reference.model_dump(mode="json"),
        "source_file_digests": digests,
        "commands": platform["commands"],
        "preflight_commands": platform.get("preflight_commands", []),
    }
    digest = domain_separated_sha256(b"anvil.verification-profile.v1\0", values)
    return VerificationProfileBinding(**values, contract_sha256=digest)


def materialize_verification(verification: Verification, project_root: Path) -> Verification:
    """Add literal profile commands and proofs; never replace a frozen binding."""
    try:
        checked = Verification.model_validate(verification.model_dump(mode="python"))
    except (ValidationError, ValueError):
        raise ProfileError("invalid_verification") from None
    if checked.profile is None:
        return verification
    binding = resolve_profile(project_root, checked.profile)
    if checked.profile_binding is not None and checked.profile_binding != binding:
        raise ProfileError("binding_mismatch")
    commands = list(checked.commands)
    proofs = list(checked.required_proofs)
    for command in binding.commands:
        if command not in commands:
            commands.append(command)
        if not any(
            proof.kind is ProofKind.command and proof.command == command
            and proof.passing_exit_codes == [0] for proof in proofs
        ):
            proofs.append(ProofRequirement(
                kind=ProofKind.command, command=command, passing_exit_codes=[0],
                label=f"`{command}` exits 0",
            ))
    return Verification.model_validate({
        **checked.model_dump(mode="json"), "commands": commands,
        "required_proofs": proofs, "profile_binding": binding,
    })


def require_profile_current(verification: Verification, project_root: Path) -> None:
    """Refuse unmaterialized, corrupted or stale profile contracts."""
    materialized = materialize_verification(verification, project_root)
    if materialized != verification:
        raise ProfileError("binding_mismatch")
