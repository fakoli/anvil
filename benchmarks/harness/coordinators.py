"""The two coordination arms. This is the ONLY thing that differs between runs.

Both arms drive the identical actor loop over the identical task set and the identical
"work" function. Any difference in collisions / duplicates / evidence is therefore
attributable solely to the coordination layer — the control discipline that makes the
result an argument rather than a demo.

Arm A (MarkdownCoordinator): naive shared-TODO coordination. Pick an unchecked task,
do it, tick the box. Non-atomic read-modify-write => real races.

Arm B (AnvilCoordinator): the real anvil engine. `next` then atomic
`claim` (SQLite BEGIN IMMEDIATE) => exclusive ownership; file-overlap blocks at claim
time; completion carries a structured, gate-checked evidence record.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import engine
from .engine import Project, TaskSpec

# ponytail: instrumentation log is locked so OUR measurement can't be the thing that
# races; the system-under-test's own artifacts (workspace files, TODO.md) are left
# unlocked on purpose — that's what we're measuring.
_LOG_LOCK = threading.Lock()
_MAX_DIAGNOSTIC_BYTES = 4096
_MAX_DIAGNOSTIC_FIELD_BYTES = 1024
_CLAIM_ID_RE = re.compile(r"C[0-9A-F]{8}")


def _bounded_single_line(value: object, *, limit: int) -> str:
    """Return deterministic, single-line text that fits within ``limit`` bytes."""
    text = " ".join(str(value).split()) or "-"
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    suffix = "..."
    prefix = encoded[: limit - len(suffix)].decode("utf-8", errors="ignore")
    return prefix + suffix


@dataclass(frozen=True)
class CompletionFailure:
    """A bounded command failure safe to preserve in metrics and reports."""

    phase: str
    task: str
    actor: str
    exit_code: int

    @property
    def diagnostic(self) -> str:
        fields = {
            "phase": self.phase,
            "task": self.task,
            "actor": self.actor,
        }
        rendered = "completion_failure " + " ".join(
            f"{key}={_bounded_single_line(value, limit=_MAX_DIAGNOSTIC_FIELD_BYTES)}"
            for key, value in fields.items()
        )
        rendered += f" exit_code={self.exit_code}"
        # The per-field limits keep this comfortably below the contract ceiling. Keep
        # a final guard so future fields cannot accidentally make diagnostics unbounded.
        return _bounded_single_line(rendered, limit=_MAX_DIAGNOSTIC_BYTES)


@dataclass(frozen=True)
class CompletionOutcome:
    """Structured result of one coordinator completion attempt."""

    completed: bool
    evidence_valid: bool | None
    failure: CompletionFailure | None = None


@dataclass(frozen=True)
class AcquiredTask:
    """One task reservation, including the exact claim that authorizes completion."""

    task_id: str
    claim_id: str | None


class CoordinationInfrastructureError(RuntimeError):
    """The benchmark could not determine coordination state reliably."""


def _remaining_or_fail(deadline: float, phase: str) -> float:
    """Return positive active-work time or refuse to launch another operation."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CoordinationInfrastructureError(
            "benchmark coordination infrastructure failure: "
            f"phase={phase} deadline=exceeded"
        )
    return remaining


_TASK_ID_CAPTURE = r"(?P<task_id>[^'\r\n]+)"
_CLAIM_ID_PATTERN = r"C[0-9A-F]{8}"
_ACTOR_CAPTURE = r"[^'\r\n]+"
_QUOTED_LIST_ITEM = r"(?:'(?:\\.|[^'\\\r\n])*'|\"(?:\\.|[^\"\\\r\n])*\")"
_BOUNDED_LIST = rf"\[{_QUOTED_LIST_ITEM}(?:, {_QUOTED_LIST_ITEM})*\]"
_CLAIM_CONTENTION_PATTERNS = {
    # The CLI's typed pre-claim overlap refusal.
    "conflict": (
        re.compile(
            rf"task '{_TASK_ID_CAPTURE}' has file conflicts with active claims: "
            rf"claim {_CLAIM_ID_PATTERN} by '{_ACTOR_CAPTURE}' overlaps {_BOUNDED_LIST}"
            rf"(?:; claim {_CLAIM_ID_PATTERN} by '{_ACTOR_CAPTURE}' overlaps "
            rf"{_BOUNDED_LIST})*\. Pass --force to override\."
        ),
    ),
    # ClaimError is a broad engine code, so only the exact concurrency conditions
    # emitted by ClaimManager / the transactional SQLite guards are retryable.
    "claim_error": (
        re.compile(
            rf"Task '{_TASK_ID_CAPTURE}' cannot be claimed: status is "
            r"'(?:proposed|drafted|reviewed|claimed|in_progress|blocked|"
            r"needs_review|accepted|done|rejected)', expected 'ready'\."
        ),
        re.compile(
            rf"claim\.created: concurrency guard failed for task "
            rf"'{_TASK_ID_CAPTURE}'\. Expected status 'ready', got "
            r"'(?:proposed|drafted|reviewed|in_progress|blocked|"
            r"needs_review|accepted|done|rejected)'\. Another claim may have "
            r"already acquired this task\."
        ),
        re.compile(
            rf"Task '{_TASK_ID_CAPTURE}' conflicts with active claims: "
            rf"claim {_CLAIM_ID_PATTERN} by {_ACTOR_CAPTURE} \(files: {_BOUNDED_LIST}\)"
            rf"(?:; claim {_CLAIM_ID_PATTERN} by {_ACTOR_CAPTURE} "
            rf"\(files: {_BOUNDED_LIST}\))*\. Use force=True to override\."
        ),
        re.compile(
            rf"Task '{_TASK_ID_CAPTURE}' shares a conflict_group with "
            rf"already-claimed tasks: task [^\s\r\n]+ claimed by {_ACTOR_CAPTURE}"
            rf"(?:; task [^\s\r\n]+ claimed by {_ACTOR_CAPTURE})*\. "
            r"Use force=True to override\."
        ),
        re.compile(
            rf"claim\.created: concurrency guard failed for task '{_TASK_ID_CAPTURE}'\. "
            rf"Active claim '{_CLAIM_ID_PATTERN}' by '{_ACTOR_CAPTURE}' already holds "
            r"this task\. Another claim acquired it first\."
        ),
        re.compile(
            rf"claim\.created: concurrency guard failed for task '{_TASK_ID_CAPTURE}'\. "
            rf"expected_files overlap active claim '{_CLAIM_ID_PATTERN}' by "
            rf"'{_ACTOR_CAPTURE}' \(files: {_BOUNDED_LIST}\)\. Another claim acquired "
            r"these files first; re-pick a task or use --force to override\."
        ),
        re.compile(
            rf"claim\.created: concurrency guard failed for task '{_TASK_ID_CAPTURE}'\. "
            rf"conflict_group overlap \(groups: {_BOUNDED_LIST}\) with active claim "
            rf"'{_CLAIM_ID_PATTERN}' on task '[^'\r\n]+' by '{_ACTOR_CAPTURE}'\. "
            r"Another claim in this group is active; re-pick a task or use --force "
            r"to override\."
        ),
    ),
}


def _result_envelope(result: engine.RunResult) -> dict | None:
    try:
        envelope = json.loads(result.out)
    except (json.JSONDecodeError, TypeError):
        return None
    return envelope if isinstance(envelope, dict) else None


def _infrastructure_failure(phase: str, result: engine.RunResult) -> None:
    """Raise a bounded error without copying raw CLI output into diagnostics."""
    raise CoordinationInfrastructureError(
        f"benchmark coordination infrastructure failure: "
        f"phase={phase} exit_code={result.code}"
    )


def _claim_failure_is_contention(
    result: engine.RunResult,
    *,
    expected_task_id: str,
) -> bool:
    """Recognize expected lost races while refusing all unknown claim failures."""
    if result.code != 1:
        return False
    envelope = _result_envelope(result)
    if (
        envelope is None
        or envelope.get("ok") is not False
        or envelope.get("command") != "claim"
    ):
        return False
    error = envelope.get("error")
    if not isinstance(error, dict):
        return False
    code = error.get("code")
    message = error.get("message")
    if code not in {"conflict", "claim_error"} or not isinstance(message, str):
        return False
    patterns = _CLAIM_CONTENTION_PATTERNS.get(code, ())
    for pattern in patterns:
        match = pattern.fullmatch(message)
        if match is not None and match.group("task_id") == expected_task_id:
            return True
    return False


def require_claim_success(
    result: engine.RunResult,
    *,
    expected_task_id: str,
    expected_actor: str,
    phase: str = "claim",
) -> dict:
    """Return one exact active claim or fail closed at the CLI boundary."""
    envelope = _result_envelope(result)
    if (
        result.code != 0
        or envelope is None
        or envelope.get("ok") is not True
        or envelope.get("command") != "claim"
    ):
        _infrastructure_failure(phase, result)
    data = envelope.get("data")
    claim = data.get("claim") if isinstance(data, dict) else None
    if (
        not isinstance(claim, dict)
        or not isinstance(claim.get("id"), str)
        or _CLAIM_ID_RE.fullmatch(claim["id"]) is None
        or claim.get("task_id") != expected_task_id
        or claim.get("claimed_by") != expected_actor
        or claim.get("status") != "active"
    ):
        _infrastructure_failure(phase, result)
    return claim


def _claim_buffer_file(proj: Project, claim_id: str) -> Path:
    """Resolve one canonical claim buffer path without permitting path escape."""
    if _CLAIM_ID_RE.fullmatch(claim_id) is None:
        raise CoordinationInfrastructureError(
            "benchmark coordination infrastructure failure: "
            "phase=verification_record claim_id=invalid"
        )
    state_dir = (proj.root / ".anvil").resolve()
    buffer_dir = state_dir / ".evidence-buffer"
    try:
        buffer_dir.mkdir(parents=True, exist_ok=True)
        resolved_buffer = buffer_dir.resolve()
        buffer_file = (resolved_buffer / f"{claim_id}.json").resolve()
    except OSError:
        raise CoordinationInfrastructureError(
            "benchmark coordination infrastructure failure: "
            "phase=verification_record write=failed"
        ) from None
    if resolved_buffer.parent != state_dir or buffer_file.parent != resolved_buffer:
        raise CoordinationInfrastructureError(
            "benchmark coordination infrastructure failure: "
            "phase=verification_record path=refused"
        )
    return buffer_file


def _split_windows_command(command: str) -> list[str]:
    """Parse one command with the same quoting rules used by Windows processes."""
    import ctypes
    from ctypes import wintypes

    argc = ctypes.c_int()
    command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
    command_line_to_argv.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
    command_line_to_argv.restype = ctypes.POINTER(wintypes.LPWSTR)
    local_free = ctypes.windll.kernel32.LocalFree
    local_free.argtypes = (wintypes.HLOCAL,)
    local_free.restype = wintypes.HLOCAL

    argv = command_line_to_argv(command, ctypes.byref(argc))
    if not argv:
        raise ValueError("Windows command-line parsing failed")
    try:
        return [argv[index] for index in range(argc.value)]
    finally:
        local_free(argv)


def _validate_single_process_command(command: str, *, windows: bool) -> None:
    """Reject unquoted syntax whose meaning requires a command shell.

    Verification commands are executed directly as one argv vector. Accepting shell
    control, redirection, or expansion syntax would therefore record a different
    operation from the one the author requested. That excludes environment prefixes,
    glob/home/brace expansion, substitution, and compound commands. Quoted occurrences
    remain ordinary child-process arguments and are intentionally allowed.
    """
    if "\r" in command or "\n" in command:
        raise ValueError("verification command requires shell interpretation")

    control = ";&|<>()"
    expansion = "$`*?[]{}"
    if windows:
        control += "^"
        expansion += "%!"

    if re.match(r"\s*(?:[A-Za-z_][A-Za-z0-9_]*\+?=|!(?:\s|$))", command):
        raise ValueError("verification command requires shell interpretation")

    quoted = False
    word_start = True
    assignment_name: str | None = ""
    assignment_value = False
    tilde_position = True
    index = 0
    while index < len(command):
        char = command[index]
        if windows:
            if char == "\\":
                slash_start = index
                while index < len(command) and command[index] == "\\":
                    index += 1
                word_start = False
                assignment_name = None
                tilde_position = False
                if index < len(command) and command[index] == '"':
                    if (index - slash_start) % 2 == 0:
                        quoted = not quoted
                    index += 1
                continue
            if char == '"':
                word_start = False
                if not assignment_value:
                    assignment_name = None
                tilde_position = False
                quoted = not quoted
                index += 1
                continue
        else:
            if char == "\\":
                if not quoted and index + 1 >= len(command):
                    raise ValueError("verification command requires shell interpretation")
                if not quoted or (
                    quoted == '"'
                    and index + 1 < len(command)
                    and command[index + 1] in {'"', "\\", "$", "`", "\r", "\n"}
                ):
                    word_start = False
                    assignment_name = None
                    tilde_position = False
                    index += 2
                    continue
            if char in {"'", '"'}:
                if not quoted:
                    quoted = char
                    word_start = False
                    if not assignment_value:
                        assignment_name = None
                    tilde_position = False
                elif quoted == char:
                    quoted = False
                index += 1
                continue

        if not quoted:
            if char.isspace():
                word_start = True
                assignment_name = ""
                assignment_value = False
                tilde_position = True
                index += 1
                continue
            if (
                char in control
                or char in expansion
                or (char == "#" and word_start)
                or (char == "~" and tilde_position)
            ):
                raise ValueError("verification command requires shell interpretation")
            if (
                char == "="
                and not assignment_value
                and assignment_name is not None
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\+?", assignment_name)
            ):
                assignment_value = True
                tilde_position = True
            elif char == ":" and assignment_value:
                tilde_position = True
            else:
                tilde_position = False
            if not assignment_value and assignment_name is not None:
                assignment_name += char
            word_start = False
        index += 1

    if quoted:
        raise ValueError("verification command requires shell interpretation")


def _split_verification_command(command: str) -> list[str]:
    """Return an argv vector without invoking a platform command shell."""
    if not command.strip() or "\0" in command:
        raise ValueError("verification command is empty or contains NUL")
    _validate_single_process_command(command, windows=os.name == "nt")
    if os.name == "nt":
        argv = _split_windows_command(command)
    else:
        argv = shlex.split(command, posix=True)
    if not argv or not argv[0]:
        raise ValueError("verification command has no executable")
    return argv


def _record_verification_proofs(
    proj: Project,
    actor: str,
    claim_id: str,
    commands: list[str],
    *,
    deadline: float,
) -> None:
    """Run requested commands and persist bounded observed proofs for submit."""
    buffer_file = _claim_buffer_file(proj, claim_id)
    for command in commands:
        try:
            argv = _split_verification_command(command)
        except (OSError, ValueError):
            raise CoordinationInfrastructureError(
                "benchmark coordination infrastructure failure: "
                "phase=verification_command parse=failed"
            ) from None
        if not argv:
            raise CoordinationInfrastructureError(
                "benchmark coordination infrastructure failure: "
                "phase=verification_command empty=refused"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CoordinationInfrastructureError(
                "benchmark coordination infrastructure failure: "
                "phase=verification_command deadline=exceeded"
            )
        try:
            result = engine.run_process(argv, proj.root, timeout=remaining)
        except Exception:
            raise CoordinationInfrastructureError(
                "benchmark coordination infrastructure failure: "
                "phase=verification_command invocation=failed"
            ) from None
        if result.code in {124, 125, 126}:
            _infrastructure_failure("verification_command", result)
        record = {
            "kind": "command",
            "timestamp": datetime.now(UTC).isoformat(),
            "command": command,
            "exit_code": result.code,
            "output_sha256": hashlib.sha256(
                (result.out + result.err).encode("utf-8")
            ).hexdigest(),
            "actor": actor,
        }
        try:
            with buffer_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        except OSError:
            raise CoordinationInfrastructureError(
                "benchmark coordination infrastructure failure: "
                "phase=verification_record write=failed"
            ) from None


@dataclass
class WorkLog:
    """Append-only instrumentation. One line per file-write and per task-completion."""

    path: Path

    def write_event(self, actor: str, task: str, kind: str, target: str = "",
                    extra: str = "") -> None:
        line = f"{time.time():.6f}\t{actor}\t{task}\t{kind}\t{target}\t{extra}\n"
        with _LOG_LOCK:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)

    def rows(self) -> list[tuple[float, str, str, str, str, str]]:
        if not self.path.exists():
            return []
        out = []
        for ln in self.path.read_text(encoding="utf-8").splitlines():
            parts = ln.split("\t")
            if len(parts) == 6:
                ts, actor, task, kind, target, extra = parts
                out.append((float(ts), actor, task, kind, target, extra))
        return out


def do_work(
    proj: Project,
    log: WorkLog,
    actor: str,
    task: TaskSpec,
    jitter: float,
    *,
    stop_event: threading.Event | None = None,
    deadline: float | None = None,
) -> bool:
    """The actual 'work': append this actor's mark to each target file.

    Unlocked on purpose. If two actors run the same task (or two tasks sharing a file)
    concurrently, both append => the file shows >1 distinct actor == a collision.
    The jitter widens the window so races are observable, seeded for reproducibility.
    """
    for rel in task.files:
        fpath = proj.root / rel
        fpath.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        existing = fpath.read_text(encoding="utf-8") if fpath.exists() else ""
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            return False
        wait_for = jitter if remaining is None else min(jitter, remaining)
        if stop_event is None:
            time.sleep(wait_for)
        elif stop_event.wait(wait_for):
            return False
        if deadline is not None and time.monotonic() >= deadline:
            return False
        fpath.write_text(existing + f"{task.id} by {actor}\n", encoding="utf-8")
        # record the [start, end] interval so the oracle can detect *concurrent*
        # writes (a real race) vs. sequential writes by different actors (correct).
        log.write_event(actor, task.id, "write", rel, f"{t0:.6f}:{time.time():.6f}")
    log.write_event(actor, task.id, "done")
    return True


class Coordinator(ABC):
    name: str

    @abstractmethod
    def acquire(
        self, actor: str, rng, timeout: float | None = None
    ) -> AcquiredTask | None:
        """Try to reserve a task, including any claim needed for completion."""

    @abstractmethod
    def complete(
        self,
        actor: str,
        task: TaskSpec,
        gamed: bool,
        timeout: float | None = None,
        *,
        claim_id: str | None = None,
    ) -> CompletionOutcome:
        """Attempt completion and return its explicit success/evidence outcome."""

    @abstractmethod
    def finished(self, timeout: float | None = None) -> bool:
        """True when no more work can ever be acquired."""

    def task(self, task_id: str) -> TaskSpec:
        return self._by_id[task_id]


# --- Arm A: naive markdown coordination -------------------------------------

class MarkdownCoordinator(Coordinator):
    name = "markdown"

    def __init__(self, proj: Project, race_window: float = 0.01):
        self.proj = proj
        self._by_id = {t.id: t for t in proj.tasks}
        self.race_window = race_window
        self.todo = proj.root / "TODO.md"
        self.todo.write_text(
            "\n".join(f"- [ ] {t.id} {t.title}" for t in proj.tasks) + "\n",
            encoding="utf-8",
        )

    def _unchecked(self) -> list[str]:
        ids = []
        for ln in self.todo.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln.startswith("- [ ] "):
                ids.append(ln.split()[3])
        return ids

    def acquire(
        self, actor: str, rng, timeout: float | None = None
    ) -> AcquiredTask | None:
        # No atomic reservation: read, (race window), return a pick. Another actor
        # reading concurrently sees the same task unchecked and picks it too.
        ids = self._unchecked()
        if not ids:
            return None
        pick = ids[rng.randrange(len(ids))]
        time.sleep(self.race_window)   # window during which a peer can pick `pick` too
        return AcquiredTask(task_id=pick, claim_id=None)

    def complete(
        self,
        actor: str,
        task: TaskSpec,
        gamed: bool,
        timeout: float | None = None,
        *,
        claim_id: str | None = None,
    ) -> CompletionOutcome:
        # Non-atomic read-modify-write of TODO.md: concurrent writers drop each other's
        # checkmarks (lost updates). No evidence is recorded at all.
        text = self.todo.read_text(encoding="utf-8")
        time.sleep(self.race_window)
        text = text.replace(f"- [ ] {task.id} ", f"- [x] {task.id} ")
        self.todo.write_text(text, encoding="utf-8")
        # Markdown has no evidence concept, but checking the box itself completed.
        return CompletionOutcome(completed=True, evidence_valid=None)

    def finished(self, timeout: float | None = None) -> bool:
        return not self._unchecked()


# --- Arm B: the real anvil engine ------------------------------------

class AnvilCoordinator(Coordinator):
    name = "anvil"

    # A non-test command: the durable evidence record will visibly lack real
    # verification, which is what makes gamed work auditable after the fact.
    GAMED_COMMAND = "git status --short"

    def __init__(self, proj: Project):
        self.proj = proj
        self._by_id = {t.id: t for t in proj.tasks}

    def acquire(
        self, actor: str, rng, timeout: float | None = None
    ) -> AcquiredTask | None:
        # `next` suggests a claimable task (respects deps + skips claimed/overlapping).
        invocation_deadline = time.monotonic() + (
            60.0 if timeout is None else timeout
        )
        try:
            r = engine.run(
                ["next", "--json"], self.proj.root, actor=actor,
                timeout=_remaining_or_fail(invocation_deadline, "next"),
            )
        except CoordinationInfrastructureError:
            raise
        except Exception:
            raise CoordinationInfrastructureError(
                "benchmark coordination infrastructure failure: "
                "phase=next invocation=failed"
            ) from None
        envelope = _result_envelope(r)
        if (
            r.code not in {0, 3}
            or envelope is None
            or envelope.get("ok") is not True
            or envelope.get("command") != "next"
        ):
            _infrastructure_failure("next", r)
        data = envelope.get("data")
        if not isinstance(data, dict):
            _infrastructure_failure("next", r)
        task_data = data.get("task")
        if task_data is None:
            return None
        if r.code != 0 or not isinstance(task_data, dict):
            _infrastructure_failure("next", r)
        task_id = task_data.get("id")
        if not isinstance(task_id, str) or not task_id:
            _infrastructure_failure("next", r)
        if task_id not in self._by_id:
            _infrastructure_failure("next", r)
        # Atomic claim. No --force, so a file-overlap or a lost race ERRORS, and we
        # return None to back off — exactly the safety the benchmark is measuring.
        try:
            c = engine.run(
                ["claim", task_id, "--json"], self.proj.root, actor=actor,
                timeout=_remaining_or_fail(invocation_deadline, "claim"),
            )
        except CoordinationInfrastructureError:
            raise
        except Exception:
            raise CoordinationInfrastructureError(
                "benchmark coordination infrastructure failure: "
                "phase=claim invocation=failed"
            ) from None
        if not c.ok:
            if _claim_failure_is_contention(c, expected_task_id=task_id):
                return None
            _infrastructure_failure("claim", c)
        claim = require_claim_success(
            c,
            expected_task_id=task_id,
            expected_actor=actor,
        )
        return AcquiredTask(task_id=task_id, claim_id=claim["id"])

    def complete(
        self,
        actor: str,
        task: TaskSpec,
        gamed: bool,
        timeout: float | None = None,
        *,
        claim_id: str | None = None,
    ) -> CompletionOutcome:
        # files non-empty either way (an empty --files-changed is rejected by submit);
        # the gamed tell is the absence of a real verification command in the record.
        if not isinstance(claim_id, str) or not claim_id:
            raise CoordinationInfrastructureError(
                "benchmark coordination infrastructure failure: "
                "phase=completion_claim binding=missing"
            )
        invocation_deadline = time.monotonic() + (
            60.0 if timeout is None else timeout
        )
        requested_commands = (
            [self.GAMED_COMMAND] if gamed else list(task.verification)
        )
        requested_files = list(task.files)
        _record_verification_proofs(
            self.proj,
            actor,
            claim_id,
            requested_commands,
            deadline=invocation_deadline,
        )
        submit_args = ["submit", task.id]
        for command in requested_commands:
            submit_args.extend(("--commands", command))
        for file_path in requested_files:
            submit_args.extend(("--files-changed", file_path))
        submit_args.append("--json")
        try:
            submitted = engine.run(
                submit_args,
                self.proj.root,
                actor=actor,
                timeout=_remaining_or_fail(invocation_deadline, "submit"),
            )
        except CoordinationInfrastructureError:
            raise
        except Exception:
            raise CoordinationInfrastructureError(
                "benchmark coordination infrastructure failure: "
                "phase=submit invocation=failed"
            ) from None
        if submitted.code in {124, 125, 126}:
            _infrastructure_failure("submit", submitted)
        submit_envelope = _result_envelope(submitted)
        submit_data = (
            submit_envelope.get("data")
            if isinstance(submit_envelope, dict)
            else None
        )
        if (
            not submitted.ok
            or not isinstance(submit_envelope, dict)
            or submit_envelope.get("ok") is not True
            or submit_envelope.get("command") != "submit"
            or not isinstance(submit_data, dict)
            or submit_data.get("submitted_by") != actor
            or not isinstance(submit_data.get("evidence_id"), str)
            or not submit_data.get("evidence_id")
            or submit_data.get("claim_id") != claim_id
            or not isinstance(submit_data.get("commands_run"), list)
            or not isinstance(submit_data.get("files_changed"), list)
            or not isinstance(submit_data.get("evidence_gate"), dict)
            or not isinstance(submit_data["evidence_gate"].get("passed"), bool)
            or not isinstance(submit_data.get("task"), dict)
            or submit_data["task"].get("id") != task.id
            or submit_data["task"].get("status") != "needs_review"
        ):
            return CompletionOutcome(
                completed=False,
                evidence_valid=None,
                failure=CompletionFailure(
                    phase="submit",
                    task=task.id,
                    actor=actor,
                    exit_code=submitted.code,
                ),
            )
        submitted_evidence_valid = (
            submit_data["commands_run"] == requested_commands
            and submit_data["files_changed"] == requested_files
            and submit_data["evidence_gate"]["passed"] is True
        )
        # Auto-approve so the task reaches `done`; a real reviewer would weigh the gate.
        try:
            applied = engine.run(
                ["apply", task.id, "--approve", "--reviewer", "bench", "--json"],
                self.proj.root,
                timeout=_remaining_or_fail(invocation_deadline, "apply"),
            )
        except CoordinationInfrastructureError:
            raise
        except Exception:
            raise CoordinationInfrastructureError(
                "benchmark coordination infrastructure failure: "
                "phase=apply invocation=failed"
            ) from None
        if applied.code in {124, 125, 126}:
            _infrastructure_failure("apply", applied)
        apply_envelope = _result_envelope(applied)
        apply_data = (
            apply_envelope.get("data")
            if isinstance(apply_envelope, dict)
            else None
        )
        if (
            not applied.ok
            or not isinstance(apply_envelope, dict)
            or apply_envelope.get("ok") is not True
            or apply_envelope.get("command") != "apply"
            or not isinstance(apply_data, dict)
            or apply_data.get("task_id") != task.id
            or apply_data.get("decision") != "accepted"
            or apply_data.get("reviewer") != "bench"
            or apply_data.get("status") != "done"
            or apply_data.get("has_evidence") is not True
            or not isinstance(apply_data.get("evidence_gate"), dict)
            or not isinstance(apply_data["evidence_gate"].get("passed"), bool)
            or not isinstance(apply_data.get("task"), dict)
            or apply_data["task"].get("id") != task.id
            or apply_data["task"].get("status") != "done"
        ):
            return CompletionOutcome(
                completed=False,
                evidence_valid=submitted_evidence_valid,
                failure=CompletionFailure(
                    phase="apply",
                    task=task.id,
                    actor=actor,
                    exit_code=applied.code,
                ),
            )
        # A durable evidence record now exists for this task. It is "valid" only if it
        # carries real verification; a gamed record exists but visibly lacks it.
        return CompletionOutcome(
            completed=True,
            evidence_valid=(
                submitted_evidence_valid
                and apply_data["evidence_gate"]["passed"] is True
            ),
        )

    def finished(self, timeout: float | None = None) -> bool:
        try:
            status = engine.task_status(
                self.proj,
                timeout=5.0 if timeout is None else timeout,
            )
        except Exception:
            raise CoordinationInfrastructureError(
                "benchmark coordination infrastructure failure: "
                "phase=status invocation=failed"
            ) from None
        terminal = {"done", "accepted", "rejected"}
        # Finished when nothing is still workable. A still-claimed-but-crashed task is
        # NOT finished: its lease must expire and be reclaimed first.
        return all(s in terminal for s in status.values())
