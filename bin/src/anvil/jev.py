"""Optional TypeSafe Jev annotations; never authoritative project-state mutations.

Wire contract: https://docs.typesafe.ai/api. Confidence is provider-reported,
not a locally calibrated probability of correctness.
"""

from __future__ import annotations

import hashlib
import json
import math
import multiprocessing
import os
import re
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import httpx

CAPABILITIES = (
    "prd_review", "evidence_triage", "proof_contracts", "skill_suggestion",
    "context_ranking", "incident_triage", "voice_intent", "browser_element_resolution",
)
API_URL = "https://api.typesafe.ai/v1/systemone"
MAX_INPUT_BYTES = 32 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_QUESTIONS = 32
MAX_CREDENTIAL_FILE_BYTES = 64 * 1024


@dataclass(frozen=True)
class JevConfig:
    enabled: bool = False
    capabilities: tuple[str, ...] = ()
    model: str = "jev-1.13.0"
    timeout_seconds: float = 5.0
    api_key_env: str = "TYPESAFE_API_KEY"

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("jev.enabled must be a boolean")
        if (
            not isinstance(self.capabilities, tuple)
            or any(not isinstance(c, str) or c not in CAPABILITIES for c in self.capabilities)
            or len(set(self.capabilities)) != len(self.capabilities)
        ):
            raise ValueError("jev.capabilities must contain distinct supported capabilities")
        if not isinstance(self.model, str) or not re.fullmatch(r"jev-\d+\.\d+\.\d+", self.model):
            raise ValueError("jev.model must pin an explicit Jev version")
        if (
            type(self.timeout_seconds) not in (int, float)
            or not 0 < self.timeout_seconds <= 60
        ):
            raise ValueError("jev.timeout_seconds must be greater than zero and at most 60")
        if not isinstance(self.api_key_env, str) or not re.fullmatch(
            r"[A-Z_][A-Z0-9_]{0,127}", self.api_key_env,
        ):
            raise ValueError("jev.api_key_env must be an environment variable name")

    @classmethod
    def from_mapping(cls, value: object) -> JevConfig:
        if not isinstance(value, dict) or value.keys() - cls.__dataclass_fields__.keys():
            raise ValueError("jev must be a mapping with supported configuration keys")
        values = dict(value)
        capabilities = values.get("capabilities", ())
        if not isinstance(capabilities, (list, tuple)):
            raise ValueError("jev.capabilities must be a list")
        values["capabilities"] = tuple(capabilities)
        return cls(**values)


def _dotenv_key(path: Path) -> str | None:
    """Read one literal TYPESAFE_API_KEY assignment without shell evaluation."""
    if path.is_symlink():
        raise ValueError("credential_source_invalid")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("credential_source_invalid")
        raw = source.read(MAX_CREDENTIAL_FILE_BYTES + 1)
    if len(raw) > MAX_CREDENTIAL_FILE_BYTES:
        raise ValueError("credential_source_invalid")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise ValueError("credential_source_invalid") from None

    matches: list[str] = []
    assignment = re.compile(r"^[ \t]*(?:export[ \t]+)?TYPESAFE_API_KEY[ \t]*=[ \t]*(.*)$")
    malformed = re.compile(
        r"^[ \t]*(?:export[ \t]+)?TYPESAFE_API_KEY(?![A-Za-z0-9_])(?:[ \t]*$|[ \t]*[^=])"
    )
    for line in lines:
        match = assignment.match(line)
        if match is None:
            if malformed.match(line):
                raise ValueError("credential_source_invalid")
            continue
        value = match.group(1).strip()
        if value[:1] in {"'", '"'}:
            quote = value[0]
            if len(value) < 2 or quote not in value[1:]:
                raise ValueError("credential_source_invalid")
            end = value.find(quote, 1)
            if value[end + 1:].strip() and not value[end + 1:].lstrip().startswith("#"):
                raise ValueError("credential_source_invalid")
            value = value[1:end]
        else:
            value = re.split(r"[ \t]+#", value, maxsplit=1)[0].rstrip()
        if not value:
            raise ValueError("credential_source_invalid")
        matches.append(value)
    if len(matches) > 1:
        raise ValueError("credential_source_invalid")
    return matches[0] if matches else None


def resolve_api_key(config: JevConfig, project_root: Path | None = None) -> str | None:
    """Resolve the default Jev key from env, selected project, then home."""
    key = os.environ.get(config.api_key_env)
    if key:
        return key
    if config.api_key_env != "TYPESAFE_API_KEY":
        return None
    candidates = ([] if project_root is None else [project_root / ".env"]) + [Path.home() / ".env"]
    for path in candidates:
        try:
            key = _dotenv_key(path)
        except FileNotFoundError:
            continue
        if key is not None:
            return key
    return None


def _json_bytes(value: object) -> bytes:
    """Canonical encoding, bounded before retaining the complete request."""
    result = bytearray()
    encoder = json.JSONEncoder(
        ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    )
    for part in encoder.iterencode(value):
        encoded = part.encode("utf-8")
        if len(result) + len(encoded) > MAX_INPUT_BYTES:
            raise ValueError("input_limit")
        result.extend(encoded)
    return bytes(result)


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _questions(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or not 1 <= len(value) <= MAX_QUESTIONS:
        raise ValueError("invalid_questions")
    for name, question in value.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", name):
            raise ValueError("invalid_question_id")
        if (
            not isinstance(question, dict)
            or question.keys() - {"type", "instructions", "criteria"}
            or not _text(question.get("instructions"))
        ):
            raise ValueError("invalid_question")
        kind, criteria = question.get("type"), question.get("criteria")
        if kind == "choice":
            valid = (
                isinstance(criteria, dict) and 2 <= len(criteria) <= 255
                and all(_text(k) and _text(v) for k, v in criteria.items())
            )
        elif kind == "score":
            valid = isinstance(criteria, list) and 2 <= len(criteria) <= 10 and all(
                _text(level) for level in criteria
            )
        else:
            valid = kind == "noul" and "criteria" not in question
        if not valid:
            raise ValueError("invalid_question")
    return value


def _number(value: object, maximum: int = 1) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= maximum:
        raise ValueError("invalid_number")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _answers(
    payload: object, questions: dict[str, Any], model: str,
) -> tuple[dict[str, Any], dict[str, int]]:
    if not isinstance(payload, dict) or payload.get("model") != model:
        raise ValueError("model_mismatch")
    raw = payload.get("answers")
    if not isinstance(raw, dict) or raw.keys() != questions.keys():
        raise ValueError("answer_keys_mismatch")
    answers = {}
    for name, question in questions.items():
        answer, kind = raw[name], question["type"]
        if not isinstance(answer, dict) or answer.get("type") != kind:
            raise ValueError("answer_type_mismatch")
        clean: dict[str, Any] = {"type": kind}
        if kind == "noul":
            clean["noul"] = _number(answer.get("noul"))
        else:
            criteria = question["criteria"]
            options = set(criteria) if kind == "choice" else {str(i) for i in range(len(criteria))}
            probabilities = answer.get("probabilities")
            if not isinstance(probabilities, dict) or probabilities.keys() != options:
                raise ValueError("probability_keys_mismatch")
            clean["probabilities"] = {k: _number(v) for k, v in probabilities.items()}
            if not math.isclose(sum(clean["probabilities"].values()), 1.0, abs_tol=0.02):
                raise ValueError("probability_sum")
            clean["confidence"] = _number(answer.get("confidence"))
            if kind == "choice":
                choice = answer.get("choice")
                if not isinstance(choice, str) or choice not in options:
                    raise ValueError("invalid_choice")
                clean["choice"] = choice
            else:
                clean["score"] = _number(answer.get("score"), len(criteria) - 1)
        answers[name] = clean
    usage = payload.get("usage")
    if not isinstance(usage, dict) or any(
        type(usage.get(key)) is not int or usage[key] < 0
        for key in ("input_tokens", "output_tokens")
    ):
        raise ValueError("invalid_usage")
    return answers, {key: usage[key] for key in ("input_tokens", "output_tokens")}


def _request(
    config: JevConfig, key: str, body: bytes, transport: httpx.BaseTransport | None,
    on_started: Callable[[], None],
) -> tuple[str, str, bytes]:
    """Perform only bounded HTTP I/O; the owning process enforces its lifetime."""
    try:
        with httpx.Client(
            timeout=config.timeout_seconds, verify=True, follow_redirects=False,
            trust_env=False, transport=transport,
        ) as client:
            on_started()
            with client.stream(
                "POST", API_URL, content=body,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                         "Accept": "application/json", "Accept-Encoding": "identity"},
            ) as response:
                if response.status_code in (401, 403):
                    return "unavailable", "authentication_failed", b""
                if response.is_redirect:
                    return "unavailable", "redirect_rejected", b""
                if not response.is_success:
                    return "unavailable", "http_error", b""
                if response.headers.get("content-encoding", "identity") != "identity":
                    return "invalid_response", "unexpected_encoding", b""
                content = bytearray()
                for chunk in response.iter_bytes(chunk_size=8192):
                    if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                        return "invalid_response", "response_limit", b""
                    content.extend(chunk)
        return "completed", "received", bytes(content)
    except httpx.TimeoutException:
        return "unavailable", "timeout", b""
    except Exception:
        return "unavailable", "transport_error", b""


def _request_worker(
    connection: Connection, config: JevConfig, key: str, body: bytes,
    transport: httpx.BaseTransport | None,
) -> None:
    with connection:
        result = _request(config, key, body, transport, lambda: connection.send(("started", None)))
        connection.send(("result", result))


def _bounded_request(
    config: JevConfig, key: str, body: bytes, transport: httpx.BaseTransport | None,
    deadline: float,
) -> tuple[bool, str, str, bytes]:
    """Own and reap one worker, including stalled DNS, TLS, headers, and body.

    Production uses spawn to avoid inheriting locks from threaded callers.
    The injected-transport test seam uses fork when available so local mock
    handlers need not be pickleable. Neither path leaves a background worker.
    """
    if deadline <= time.monotonic():
        return False, "unavailable", "timeout", b""
    context = (
        multiprocessing.get_context("fork")
        if transport is not None and "fork" in multiprocessing.get_all_start_methods()
        else multiprocessing.get_context("spawn")
    )
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(target=_request_worker, args=(sender, config, key, body, transport))
    worker.daemon = True
    attempted = False
    result = ("unavailable", "timeout", b"")
    try:
        worker.start()
        sender.close()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not receiver.poll(remaining):
                break
            kind, value = receiver.recv()
            if kind == "started":
                attempted = True
            else:
                result = value
                break
    except Exception:
        result = ("unavailable", "transport_error", b"")
    finally:
        sender.close()
        if worker.is_alive():
            worker.kill()
        if worker.pid is not None:
            worker.join()
        worker.close()
        # A start notice can race the deadline. Drain only after the writer is
        # reaped so the report never incorrectly denies already attempted egress.
        try:
            while receiver.poll():
                kind, _ = receiver.recv()
                attempted = attempted or kind == "started"
        except (EOFError, OSError):
            pass
        receiver.close()
    return attempted, *result


def evaluate(
    config: JevConfig,
    capability: str,
    state: object,
    questions: dict[str, Any],
    *,
    allow_api: bool = False,
    disabled: bool = False,
    transport: httpx.BaseTransport | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Return a sanitized, advisory report. No retries, fallback, or state writes.

    Disabled and permission guards run before reading credentials or encoding
    input. ``requested`` records opt-in; ``request_started`` records attempted
    HTTP; ``used`` is true only for a fully validated annotation.
    """
    started = time.monotonic()
    report: dict[str, Any] = {
        "schema": "anvil.jev.annotation.v1", "provider": "typesafe", "model": config.model,
        "capability": capability if capability in CAPABILITIES else None,
        "status": "disabled", "reason": "disabled", "requested": False, "used": False,
        "request_started": False, "elapsed_ms": 0, "rubric_digest": None,
        "input_digest": None, "answers": {}, "usage": {},
    }

    def finish(status: str, reason: str) -> dict[str, Any]:
        report.update(
            status=status, reason=reason, elapsed_ms=round((time.monotonic() - started) * 1000),
        )
        return report

    if disabled or not config.enabled:
        return finish("disabled", "disabled")
    if capability not in CAPABILITIES:
        return finish("blocked", "unsupported_capability")
    if capability not in config.capabilities:
        return finish("disabled", "capability_disabled")
    report["requested"] = True
    if allow_api is not True:
        return finish("blocked", "api_permission_required")
    try:
        questions = _questions(questions)
        if not isinstance(state, (str, dict, list)):
            raise ValueError("invalid_state")
        body = _json_bytes({"model": config.model, "state": state, "questions": questions})
        snapshot = json.loads(body)
        questions = snapshot["questions"]
        report["rubric_digest"] = hashlib.sha256(_json_bytes(questions)).hexdigest()
        report["input_digest"] = hashlib.sha256(_json_bytes(snapshot["state"])).hexdigest()
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return finish("blocked", "invalid_input")
    try:
        key = resolve_api_key(config, project_root)
    except (OSError, ValueError):
        return finish("unavailable", "credential_source_invalid")
    if not key:
        return finish("unavailable", "missing_credentials")
    if len(key) > 4096 or any(not 33 <= ord(c) <= 126 for c in key):
        return finish("unavailable", "invalid_credentials")
    attempted, status, reason, content = _bounded_request(
        config, key, body, transport, started + config.timeout_seconds,
    )
    report["request_started"] = attempted
    if status != "completed":
        return finish(status, reason)
    try:
        payload = json.loads(content, object_pairs_hook=_unique_object)
        answers, usage = _answers(payload, questions, config.model)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return finish("invalid_response", "invalid_response")
    except Exception:
        # Optional annotations must not expose provider exceptions to callers.
        return finish("unavailable", "transport_error")
    if time.monotonic() - started > config.timeout_seconds:
        return finish("unavailable", "timeout")
    report.update(used=True, answers=answers, usage=usage)
    return finish("completed", "validated")
