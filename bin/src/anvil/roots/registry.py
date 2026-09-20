# ruff: noqa: E501
"""Durable, owner-global repository enrollment and reservation journal.

The journal deliberately lives beside Anvil workspaces rather than inside one
project's State database.  It serializes repositories enrolled by this owner
across separate State workspaces while leaving legacy projects untouched until
an explicit enrollment activates it.  State never reads this file while it
holds its SQLite append lock; callers acquire this lock first.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

try:  # pragma: no cover - Windows has no fcntl; callers fail closed there.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

_SCHEMA = "anvil.root-set-registry/v1"
_ACTIVATION_SCHEMA = "anvil.root-set-activation/v1"
_MAX_ROOTS = 16
_MAX_REQUEST_BYTES = 65_536
_MAX_REGISTRY_BYTES = 1_000_000
_MAX_LOCK_SECONDS = 5.0
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HEX = re.compile(r"^[0-9a-f]{64}$")
_LIVE_APPEND_AUTH: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "anvil_root_set_live_append_auth", default=None
)
_ORDINARY_GLOBAL_HELD: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "anvil_root_set_ordinary_global_held", default=False
)
_LIVE_ROOT_USE_AUTH: contextvars.ContextVar[bytes | None] = contextvars.ContextVar(
    "anvil_root_set_live_use_auth", default=None
)


class RootSetError(RuntimeError):
    """A bounded, machine-readable root-set refusal."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _error(code: str, message: str) -> RootSetError:
    return RootSetError(code, message)


class _RootSetClaimAuthorization:
    """Opaque capability issued only from the locked owner journal."""

    __slots__ = ("binding_bytes",)

    def __init__(self, binding: object) -> None:
        self.binding_bytes = _canonical_json(_binding_data(binding))


def _binding_data(binding: object) -> object:
    if hasattr(binding, "model_dump"):
        return binding.model_dump(mode="json")
    if isinstance(binding, dict):
        return binding
    return None


def authorize_root_set_claim(binding: object, reservation: dict[str, Any]) -> _RootSetClaimAuthorization:
    """Validate immutable binding facts before the caller takes the State lock."""
    if (
        getattr(binding, "request_id", None) != reservation.get("request_id")
        or getattr(binding, "request_digest", None) != reservation.get("digest")
        or getattr(binding, "reservation_id", None) != reservation.get("reservation_id")
        or reservation.get("state") != "pending"
    ):
        raise _error("root_set_reconciliation_required", "root-set binding does not match the pending owner reservation.")
    return _RootSetClaimAuthorization(binding)


def authorize_bound_root_set_claim(binding: object, reservation: dict[str, Any]) -> _RootSetClaimAuthorization:
    """Issue a full-binding capability while the matching reservation is bound."""
    if (
        getattr(binding, "request_id", None) != reservation.get("request_id")
        or getattr(binding, "request_digest", None) != reservation.get("digest")
        or getattr(binding, "reservation_id", None) != reservation.get("reservation_id")
        or reservation.get("state") != "bound"
    ):
        raise _error("root_set_reconciliation_required", "root-set binding does not match the bound owner reservation.")
    results = reservation.get("root_results")
    facts = getattr(binding, "root_facts", ())
    if not isinstance(results, list) or len(results) != len(facts) or not results:
        raise _error("root_set_reconciliation_required", "root-set binding lacks prepared owner facts.")
    try:
        journal_facts = [
            {key: item[key] for key in ("root_id", "repository_id", "baseline_sha", "canonical_root", "claim_worktree", "branch", "verification_commands")}
            for item in results
        ]
        binding_facts = [fact.model_dump(mode="json") for fact in facts]
        digest = request_digest({"primary_root_id": reservation.get("primary_root_id"), "roots": journal_facts})
    except (KeyError, TypeError, AttributeError):
        raise _error("root_set_reconciliation_required", "root-set owner facts are invalid.") from None
    if (
        any(not isinstance(item, dict) or item.get("state") != "prepared" for item in results)
        or len({(item.get("root_id"), item.get("repository_id")) for item in results}) != len(results)
        or binding_facts != journal_facts
        or getattr(binding, "primary_root_id", None) != reservation.get("primary_root_id")
        or getattr(binding, "root_set_digest", None) != digest
    ):
        raise _error("root_set_reconciliation_required", "root-set binding does not match immutable owner facts.")
    return _RootSetClaimAuthorization(binding)


def root_set_claim_authorized(binding: object, authorization: object) -> bool:
    return (
        isinstance(authorization, _RootSetClaimAuthorization)
        and _canonical_json(_binding_data(binding)) == authorization.binding_bytes
    )


@contextlib.contextmanager
def live_claim_append_authorized(
    *, binding: object | None = None, authorization: object | None = None
) -> Iterator[None]:
    """Mark one supported live claim append; replay never enters this context."""
    if binding is not None and not root_set_claim_authorized(binding, authorization):
        raise _error("root_set_authorization_required", "root-set append lacks owner authorization.")
    token = _LIVE_APPEND_AUTH.set((binding, authorization) if binding is not None else True)
    try:
        yield
    finally:
        _LIVE_APPEND_AUTH.reset(token)


def validate_live_claim_append(action: str, payload: object) -> None:
    """Deny public direct claim/bundle writes when an active registry exists."""
    if action not in {"claim.created", "bundle.claimed"}:
        return
    context = _LIVE_APPEND_AUTH.get()
    if context is True:
        return
    if isinstance(context, tuple) and action == "claim.created":
        binding, authorization = context
        raw_binding = payload.get("root_set") if isinstance(payload, dict) else None
        if raw_binding is not None and root_set_claim_authorized(raw_binding, authorization):
            return
    assert_unscoped_claim_allowed()


def require_root_set_lifecycle_authorization(binding: object) -> None:
    """Require the full immutable binding capability for a lease extension."""
    context = _LIVE_APPEND_AUTH.get()
    if not (
        isinstance(context, tuple)
        and root_set_claim_authorized(binding, context[1])
    ):
        raise _error(
            "root_set_authorization_required",
            "root-set lifecycle extension requires the bound owner coordinator.",
        )


def _bound_use_matches(binding: object, reservation: dict[str, Any], backend: object) -> bool:
    """Local full-fact check for a nonterminal owner use."""
    try:
        authorize_bound_root_set_claim(binding, reservation)
    except RootSetError:
        return False
    try:
        state_identity = str(Path(backend._db_path).parent.resolve())  # noqa: SLF001
        claim = backend.get_claim(reservation.get("claim_id"))
    except (AttributeError, OSError, TypeError):
        return False
    if (
        reservation.get("state_identity") != state_identity
        or claim is None
        or getattr(claim, "root_set", None) != binding
        or reservation.get("claim_id") != getattr(claim, "id", None)
    ):
        return False
    if getattr(getattr(claim, "status", None), "value", None) != "active":
        return False
    expires = getattr(claim, "lease_expires_at", None)
    if expires is None or expires.timestamp() <= time.time():
        return False
    return True


@contextlib.contextmanager
def root_set_use_authorized(binding: object, *, backend: object) -> Iterator[None]:
    """Authorize one nonterminal root-set use under the owner journal lock."""
    with RootSetRegistry().locked() as registry:
        reservation = registry["reservations"].get(getattr(binding, "request_id", None))
        if not isinstance(reservation, dict):
            raise _error("root_set_reconciliation_required", "root-set reservation is unavailable.")
        if not _bound_use_matches(binding, reservation, backend):
            raise _error("root_set_reconciliation_required", "root-set use does not match live canonical facts.")
        authorization = authorize_bound_root_set_claim(binding, reservation)
        token = _LIVE_ROOT_USE_AUTH.set(authorization.binding_bytes)
        try:
            yield
        finally:
            _LIVE_ROOT_USE_AUTH.reset(token)


def require_root_set_use_authorization(binding: object) -> None:
    """Reject direct nonterminal writes that lack the locked owner context."""
    if _LIVE_ROOT_USE_AUTH.get() != _canonical_json(_binding_data(binding)):
        raise _error(
            "root_set_authorization_required",
            "root-set use requires the bound owner coordinator.",
        )




def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def request_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _require_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise _error("root_set_identity_mismatch", f"{label} is invalid.")
    return value


def _no_controls(value: str, label: str) -> str:
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise _error("root_set_identity_mismatch", f"{label} is invalid.")
    return value


def _no_symlink_path(value: object) -> Path:
    if not isinstance(value, str):
        raise _error("root_set_identity_mismatch", "root path is invalid.")
    path = Path(_no_controls(value, "root path"))
    if not path.is_absolute():
        raise _error("root_set_identity_mismatch", "root path must be absolute.")
    current = Path(path.anchor)
    try:
        for component in path.parts[1:]:
            current /= component
            if current.is_symlink():
                raise _error("root_set_identity_mismatch", "root path may not traverse a symlink.")
        return path.resolve(strict=True)
    except RootSetError:
        raise
    except OSError as exc:
        raise _error("root_set_not_enrolled", "root path is unavailable.") from exc


def _git(path: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _error("root_set_identity_mismatch", "Git identity could not be read.") from exc
    if result.returncode != 0:
        raise _error("root_set_identity_mismatch", "root is not an accessible Git checkout.")
    return result.stdout.strip()


def normalize_origin(value: object) -> str:
    """Normalize a credential-free declared Git origin or explicit local ID."""
    if not isinstance(value, str):
        raise _error("root_set_identity_mismatch", "origin is invalid.")
    value = _no_controls(value, "origin")
    if value.startswith("local:"):
        return "local:" + _require_id(value.removeprefix("local:"), "local origin identity")
    # Git's common scp-like SSH spelling has no URL scheme.  Normalize it to
    # one credential-free SSH URL so a clone made with either spelling remains
    # the same enrolled repository.  The user is the public SSH account, not a
    # password-bearing credential.
    scp = re.fullmatch(r"([A-Za-z0-9._-]+)@([A-Za-z0-9.-]+):([^\s:]+)", value)
    if scp is not None:
        user, host, path = scp.groups()
        return normalize_origin(f"ssh://{user}@{host}/{path}")
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "ssh"} or not parsed.hostname or parsed.password or parsed.query or parsed.fragment:
        raise _error("root_set_identity_mismatch", "origin must be a normalized credential-free ssh or https URL.")
    if parsed.scheme == "https" and parsed.username:
        raise _error("root_set_identity_mismatch", "origin must be a normalized credential-free ssh or https URL.")
    host = parsed.hostname.lower()
    try:
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError as exc:
        raise _error("root_set_identity_mismatch", "origin port is invalid.") from exc
    path = parsed.path.rstrip("/")
    if not path or "//" in path:
        raise _error("root_set_identity_mismatch", "origin path is invalid.")
    user = f"{parsed.username}@" if parsed.scheme == "ssh" and parsed.username else ""
    return urlunsplit((parsed.scheme, user + host + port, path, "", ""))


def live_repository_identity(path_value: object, *, declared_origin: str | None = None) -> dict[str, str]:
    """Resolve one exact Git root/common-dir and verify its live origin."""
    path = _no_symlink_path(path_value)
    root = Path(_git(path, "rev-parse", "--show-toplevel"))
    common_raw = Path(_git(path, "rev-parse", "--git-common-dir"))
    common_candidate = path / common_raw if not common_raw.is_absolute() else common_raw
    common = _no_symlink_path(str(common_candidate))
    root = _no_symlink_path(str(root))
    try:
        origin_raw = _git(root, "config", "--get", "remote.origin.url")
    except RootSetError:
        origin_raw = ""
    if origin_raw:
        origin = normalize_origin(origin_raw)
        if declared_origin is not None and origin != normalize_origin(declared_origin):
            raise _error("root_set_identity_mismatch", "declared origin does not match the live origin.")
    else:
        origin = normalize_origin(declared_origin) if declared_origin is not None else ""
        if not origin.startswith("local:"):
            raise _error("root_set_identity_mismatch", "a repository without origin needs an explicit local identity.")
    return {"path": str(root), "common_dir": str(common), "origin": origin}


def _entry_aliases(entry: dict[str, Any]) -> list[dict[str, str]]:
    aliases = entry.get("aliases", [])
    return aliases if isinstance(aliases, list) else []


def _entry_has_live_identity(entry: dict[str, Any], live: dict[str, str]) -> bool:
    return any(
        all(alias.get(key) == live[key] for key in ("path", "common_dir", "origin"))
        for alias in _entry_aliases(entry)
    )


class RootSetRegistry:
    """A locked, strict JSON projection for enrolled repositories and reservations."""

    def __init__(self, home: Path | None = None) -> None:
        self.base = (home or Path.home()) / ".anvil" / "root-sets"
        self.activation_path = self.base / "activation.json"
        self.path = self.base / "registry.json"
        self.lock_path = self.base / "registry.lock"

    @contextlib.contextmanager
    def locked(self, *, require_active: bool = True) -> Iterator[dict[str, Any]]:
        # Legacy projects must not require a writable home or platform locking
        # before an owner has explicitly activated this feature.
        if not self.activation_path.exists() and not self.path.exists() and not self.base.exists():
            if require_active:
                raise _error("root_set_registry_unavailable", "root registry is not activated.")
            yield {"schema": _SCHEMA, "repositories": {}, "reservations": {}}
            return
        if fcntl is None:
            if not self.activation_path.exists() and not self.path.exists():
                if require_active:
                    raise _error("root_set_registry_unavailable", "root registry is not activated.")
                yield {"schema": _SCHEMA, "repositories": {}, "reservations": {}}
                return
            raise _error("root_set_unsupported", "this platform cannot lock the owner root registry.")
        self.base.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            if self.base.is_symlink() or not self.base.is_dir():
                raise _error("root_set_registry_unavailable", "owner root registry directory is unsafe.")
        except OSError as exc:
            raise _error("root_set_registry_unavailable", "owner root registry directory is unavailable.") from exc
        try:
            os.chmod(self.base, 0o700)
        except OSError:
            pass
        try:
            lock_fd = os.open(
                self.lock_path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise _error("root_set_registry_unavailable", "owner root registry lock is unsafe.")
        except (OSError, RootSetError) as exc:
            try:
                os.close(locals().get("lock_fd", -1))
            except OSError:
                pass
            raise _error("root_set_registry_unavailable", "owner root registry lock is unavailable.") from exc
        with os.fdopen(lock_fd, "a+b") as handle:
            deadline = time.monotonic() + _MAX_LOCK_SECONDS
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise _error("root_set_registry_unavailable", "owner root registry lock timed out.") from exc
                    time.sleep(0.02)
            try:
                data = self._load(require_active=require_active)
                before = _canonical_json(data)
                yield data
                if _canonical_json(data) != before:
                    self._write(data)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _load(self, *, require_active: bool) -> dict[str, Any]:
        activated = self.activation_path.exists()
        exists = self.path.exists()
        if not activated and not exists:
            if require_active:
                raise _error("root_set_registry_unavailable", "root registry is not activated.")
            return {"schema": _SCHEMA, "repositories": {}, "reservations": {}}
        if not activated or not exists:
            raise _error("root_set_registry_unavailable", "activated root registry is incomplete.")
        try:
            activation = self._read_json_regular(self.activation_path, limit=4096)
            data = self._read_json_regular(self.path, limit=_MAX_REGISTRY_BYTES)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise _error("root_set_registry_unavailable", "activated root registry is unreadable.") from exc
        if activation != {"schema": _ACTIVATION_SCHEMA} or not isinstance(data, dict) or set(data) != {"schema", "repositories", "reservations"} or data.get("schema") != _SCHEMA or not isinstance(data["repositories"], dict) or not isinstance(data["reservations"], dict):
            raise _error("root_set_registry_unavailable", "activated root registry is corrupt.")
        try:
            for repository_id, entry in data["repositories"].items():
                _require_id(repository_id, "repository id")
                if not isinstance(entry, dict) or set(entry) != {
                    "path", "common_dir", "origin", "verification_commands", "aliases"
                }:
                    raise ValueError
                _no_symlink_path(entry["path"])
                _no_symlink_path(entry["common_dir"])
                normalize_origin(entry["origin"])
                _validate_commands(entry["verification_commands"])
                aliases = _entry_aliases(entry)
                if not aliases or len(aliases) > _MAX_ROOTS:
                    raise ValueError
                for alias in aliases:
                    if not isinstance(alias, dict) or set(alias) != {
                        "path", "common_dir", "origin"
                    }:
                        raise ValueError
                    _no_symlink_path(alias["path"])
                    _no_symlink_path(alias["common_dir"])
                    if normalize_origin(alias["origin"]) != entry["origin"]:
                        raise ValueError
            for request_id, reservation in data["reservations"].items():
                _require_id(request_id, "request id")
                if not isinstance(reservation, dict) or not {
                    "reservation_id", "request_id", "digest", "actor", "state_identity",
                    "roots", "repository_ids", "state", "claim_id", "created_at",
                }.issubset(reservation) or set(reservation) - {
                    "reservation_id", "request_id", "digest", "actor", "state_identity",
                    "roots", "repository_ids", "state", "claim_id", "created_at", "renewed_at",
                    "root_results", "primary_root_id",
                    "state_append_attempted",
                }:
                    raise ValueError
                if reservation["request_id"] != request_id:
                    raise ValueError
                _require_id(reservation["reservation_id"], "reservation id")
                if not isinstance(reservation["actor"], str) or not reservation["actor"]:
                    raise ValueError
                if not isinstance(reservation["state_identity"], str) or not reservation["state_identity"]:
                    raise ValueError
                if not isinstance(reservation["digest"], str) or not _HEX.fullmatch(reservation["digest"]):
                    raise ValueError
                if reservation["state"] not in {"pending", "bound", "release_pending", "released"}:
                    raise ValueError
                if "state_append_attempted" in reservation and not isinstance(reservation["state_append_attempted"], bool):
                    raise ValueError
                if not isinstance(reservation["repository_ids"], list) or not reservation["repository_ids"]:
                    raise ValueError
                if (
                    not isinstance(reservation["roots"], list)
                    or not reservation["roots"]
                    or len(reservation["roots"]) > _MAX_ROOTS
                ):
                    raise ValueError
                root_ids: set[str] = set()
                repo_ids: set[str] = set()
                for root in reservation["roots"]:
                    if not isinstance(root, dict) or set(root) != {
                        "root_id", "repository_id", "path", "expected_files", "verification_commands"
                    }:
                        raise ValueError
                    root_id = _require_id(root["root_id"], "root id")
                    repo_id = _require_id(root["repository_id"], "repository id")
                    if root_id in root_ids or repo_id in repo_ids:
                        raise ValueError
                    root_ids.add(root_id)
                    repo_ids.add(repo_id)
                    _no_symlink_path(root["path"])
                    if not isinstance(root["expected_files"], list) or len(root["expected_files"]) > 256:
                        raise ValueError
                    for expected in root["expected_files"]:
                        if (
                            not isinstance(expected, str) or not expected
                            or expected.startswith("/") or ".." in Path(expected).parts
                            or any(ord(char) < 32 or ord(char) == 127 for char in expected)
                        ):
                            raise ValueError
                    _validate_commands(root["verification_commands"])
                if sorted(repo_ids) != reservation["repository_ids"]:
                    raise ValueError
                if "root_results" in reservation:
                    results = reservation["root_results"]
                    if not isinstance(results, list) or len(results) != len(reservation["roots"]):
                        raise ValueError
                    result_pairs: set[tuple[str, str]] = set()
                    for result in results:
                        if not isinstance(result, dict) or set(result) != {
                            "root_id", "repository_id", "baseline_sha", "canonical_root",
                            "claim_worktree", "branch", "verification_commands", "state",
                        }:
                            raise ValueError
                        if any(
                            not isinstance(result[key], str) or not result[key]
                            for key in (
                            "root_id", "repository_id", "baseline_sha", "canonical_root",
                            "claim_worktree", "branch", "state",
                            )
                        ):
                            raise ValueError
                        if result["state"] not in {"intended", "prepared"}:
                            raise ValueError
                        _validate_commands(result["verification_commands"])
                        if result["root_id"] not in root_ids or result["repository_id"] not in repo_ids:
                            raise ValueError
                        pair = (result["root_id"], result["repository_id"])
                        if pair in result_pairs:
                            raise ValueError
                        result_pairs.add(pair)
                    if result_pairs != {(root["root_id"], root["repository_id"]) for root in reservation["roots"]}:
                        raise ValueError
                if "primary_root_id" in reservation:
                    _require_id(reservation["primary_root_id"], "primary root id")
                if not isinstance(reservation["created_at"], (int, float)):
                    raise ValueError
                if "renewed_at" in reservation and not isinstance(reservation["renewed_at"], (int, float)):
                    raise ValueError
        except (RootSetError, TypeError, ValueError):
            raise _error("root_set_registry_unavailable", "activated root registry is corrupt.") from None
        return data

    @staticmethod
    def _read_json_regular(path: Path, *, limit: int) -> object:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size < 0 or info.st_size > limit:
                raise ValueError("unsafe registry file")
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining:
                chunk = os.read(fd, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > limit:
                raise ValueError("oversized registry file")
            return json.loads(raw.decode("utf-8"))
        finally:
            os.close(fd)

    def _write(self, data: dict[str, Any]) -> None:
        payload = _canonical_json(data)
        if len(payload) > _MAX_REGISTRY_BYTES:
            raise _error("root_set_registry_unavailable", "root registry exceeds its bounded size.")
        fd, name = tempfile.mkstemp(prefix="registry.", dir=self.base)
        try:
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            fd = -1
            os.chmod(name, 0o600)
            os.replace(name, self.path)
            directory_fd = os.open(self.base, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            Path(name).unlink(missing_ok=True)

    def _write_activation(self) -> None:
        fd, name = tempfile.mkstemp(prefix="activation.", dir=self.base)
        try:
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(_canonical_json({"schema": _ACTIVATION_SCHEMA}))
                handle.flush()
                os.fsync(handle.fileno())
            fd = -1
            os.chmod(name, 0o600)
            os.replace(name, self.activation_path)
            directory_fd = os.open(self.base, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            Path(name).unlink(missing_ok=True)

    def checkpoint(self, data: dict[str, Any]) -> None:
        """Durably commit a journal transition while the global lock is held."""
        self._write(data)

    def enroll(self, *, repository_id: str, path: object, origin: object, verification_commands: list[str] | None = None) -> dict[str, str]:
        repository_id = _require_id(repository_id, "repository id")
        live = live_repository_identity(path, declared_origin=str(origin))
        commands = _validate_commands(verification_commands or [])
        # Enrollment is the explicit activation action, so it may create the
        # owner directory after legacy callers deliberately avoided doing so.
        self.base.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.locked(require_active=False) as data:
            known = data["repositories"].get(repository_id)
            entry = {
                **live,
                "verification_commands": commands,
                "aliases": [live],
            }
            if known is not None:
                if (
                    known.get("origin") != live["origin"]
                    or known.get("verification_commands") != commands
                ):
                    raise _error("root_set_identity_mismatch", "repository id already has a different enrollment.")
                aliases = _entry_aliases(known)
                if not _entry_has_live_identity(known, live):
                    if len(aliases) >= _MAX_ROOTS:
                        raise _error("root_set_identity_mismatch", "repository alias limit exceeded.")
                    aliases.append(live)
                    known["aliases"] = aliases
                entry = known
            for other_id, other in data["repositories"].items():
                if other_id != repository_id and (
                    other.get("origin") == live["origin"]
                    or any(alias.get("common_dir") == live["common_dir"] for alias in _entry_aliases(other))
                ):
                    raise _error("root_set_identity_mismatch", "repository identity is already enrolled under another id.")
            data["repositories"][repository_id] = entry
            if not self.activation_path.exists():
                self._write_activation()
        return {"repository_id": repository_id, **live}

    def reserve(self, data: dict[str, Any], *, request_id: str, digest: str, actor: str, state_identity: str, roots: list[dict[str, Any]]) -> dict[str, Any]:
        request_id = _require_id(request_id, "request id")
        if not _HEX.fullmatch(digest):
            raise _error("root_set_request_collision", "request digest is invalid.")
        existing = data["reservations"].get(request_id)
        if existing is not None:
            if existing.get("digest") != digest or existing.get("actor") != actor or existing.get("state_identity") != state_identity:
                raise _error("root_set_request_collision", "request id is already bound to another immutable request.")
            if existing.get("state") != "bound":
                raise _error(
                    "root_set_reconciliation_required",
                    "this request is not safely reusable; reconcile the original request.",
                )
            return existing
        enrolled = data["repositories"]
        seen: set[str] = set()
        declared_paths: list[Path] = []
        normalized_roots: list[dict[str, Any]] = []
        for root in roots:
            if not isinstance(root, dict):
                raise _error("root_set_identity_mismatch", "root declaration is invalid.")
            repository_id = _require_id(root.get("repository_id"), "repository id")
            root_id = _require_id(root.get("root_id"), "root id")
            if repository_id in seen:
                raise _error("root_set_identity_mismatch", "a root set cannot repeat a repository.")
            seen.add(repository_id)
            entry = enrolled.get(repository_id)
            if entry is None:
                raise _error("root_set_not_enrolled", "every root must be enrolled before it can be claimed.")
            live = live_repository_identity(
                root["path"], declared_origin=entry["origin"]
            )
            if not _entry_has_live_identity(entry, live):
                raise _error("root_set_identity_mismatch", "root path does not match its enrollment.")
            root_path = Path(live["path"])
            if any(root_path == prior or root_path in prior.parents or prior in root_path.parents for prior in declared_paths):
                raise _error("root_set_identity_mismatch", "root-set paths may not overlap.")
            declared_paths.append(root_path)
            expected_files = root.get("expected_files", [])
            if not isinstance(expected_files, list) or len(expected_files) > 256:
                raise _error("root_set_identity_mismatch", "root expected files are invalid.")
            normalized_roots.append(
                {
                    "root_id": root_id,
                    "repository_id": repository_id,
                    "path": live["path"],
                    "expected_files": expected_files,
                    "verification_commands": _validate_commands(
                        root.get("verification_commands", entry["verification_commands"])
                    ),
                }
            )
        for reservation in data["reservations"].values():
            if reservation.get("state") in {"pending", "bound", "release_pending"} and seen.intersection(reservation.get("repository_ids", [])):
                raise _error("root_set_conflict", "an enrolled repository is already reserved by another request.")
        reservation = {"reservation_id": "R" + uuid.uuid4().hex, "request_id": request_id, "digest": digest, "actor": actor, "state_identity": state_identity, "roots": normalized_roots, "repository_ids": sorted(seen), "state": "pending", "claim_id": None, "created_at": time.time(), "state_append_attempted": False}
        data["reservations"][request_id] = reservation
        return reservation

    @staticmethod
    def bind(data: dict[str, Any], reservation: dict[str, Any], claim_id: str) -> None:
        reservation["claim_id"] = claim_id
        reservation["state"] = "bound"

    @staticmethod
    def release_after_terminal(data: dict[str, Any], reservation: dict[str, Any]) -> None:
        """Retain a terminal overhold until runner-stop reconciliation proves safe."""
        reservation["state"] = "release_pending"

    @staticmethod
    def extend_before_canonical_renewal(
        data: dict[str, Any], reservation: dict[str, Any], *, now: float
    ) -> None:
        """Record the durable global extension before the State renewal event.

        Reservations intentionally have no automatic expiry: an owner must
        reconcile a failed State write rather than allow a second repository
        claim while the first is uncertain.  ``renewed_at`` is therefore the
        owner-global lease-extension record, not an advisory timestamp.
        """
        if reservation.get("state") != "bound":
            raise _error("root_set_reconciliation_required", "root-set reservation is not bound.")
        reservation["renewed_at"] = now

    def _ordinary_claim_allowed_locked(self, data: dict[str, Any], path: Path) -> None:
        if not self.activation_path.exists() and not self.path.exists():
            return
        repositories = data["repositories"]
        root = _no_symlink_path(str(path))
        common_raw = Path(_git(root, "rev-parse", "--git-common-dir"))
        common_candidate = root / common_raw if not common_raw.is_absolute() else common_raw
        common = _no_symlink_path(str(common_candidate))
        matching = [
            (repository_id, item)
            for repository_id, item in repositories.items()
            if any(
                alias.get("path") == str(root) or alias.get("common_dir") == str(common)
                for alias in _entry_aliases(item)
            )
        ]
        if not matching:
            # A separate clone has its own common dir, so recognize it by its
            # declared origin before an enrolled repository can bypass owner
            # coordination through a new checkout.
            try:
                origin = normalize_origin(_git(root, "config", "--get", "remote.origin.url"))
            except RootSetError:
                origin = ""
            matching = [(repository_id, item) for repository_id, item in repositories.items() if origin and item.get("origin") == origin]
        if not matching:
            return
        if len(matching) != 1:
            raise _error("root_set_registry_unavailable", "owner root registry has ambiguous repository identity.")
        raise _error("root_set_registered", "this repository is enrolled; use `anvil roots claim` for coordinated work.")

    @contextlib.contextmanager
    def ordinary_claim_coordinator(self, path: Path) -> Iterator[None]:
        """Hold the owner lock across a legacy State claim append (global -> State)."""
        with self.locked(require_active=False) as data:
            self._ordinary_claim_allowed_locked(data, path)
            token = _ORDINARY_GLOBAL_HELD.set(True)
            try:
                yield
            finally:
                _ORDINARY_GLOBAL_HELD.reset(token)

    def ordinary_claim_allowed(self, path: Path) -> None:
        with self.locked(require_active=False) as data:
            self._ordinary_claim_allowed_locked(data, path)

    def unscoped_claim_allowed(self) -> None:
        """Refuse callers that cannot identify a checkout once roots are active."""
        with self.locked(require_active=False):
            if self.activation_path.exists() or self.path.exists():
                raise _error(
                    "root_set_identity_mismatch",
                    "an activated root registry requires a canonical project root for claim creation.",
                )


def _validate_commands(commands: object) -> list[str]:
    if not isinstance(commands, list) or len(commands) > 16:
        raise _error("root_set_identity_mismatch", "verification policy is invalid.")
    output: list[str] = []
    for command in commands:
        if not isinstance(command, str) or not command or len(command.encode("utf-8")) > 1024 or any(ord(char) < 32 for char in command):
            raise _error("root_set_identity_mismatch", "verification policy is invalid.")
        output.append(command)
    return output


def assert_ordinary_claim_allowed(project_root: Path) -> None:
    """Fail closed only after the owner registry has been explicitly activated."""
    RootSetRegistry().ordinary_claim_allowed(project_root)


def assert_unscoped_claim_allowed() -> None:
    """Keep direct creation paths from bypassing an activated registry."""
    RootSetRegistry().unscoped_claim_allowed()


def ordinary_claim_coordinator_held() -> bool:
    """Whether a supported caller holds the global registry before State."""
    return _ORDINARY_GLOBAL_HELD.get()
