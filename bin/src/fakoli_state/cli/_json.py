"""Machine-readable ``--json`` output envelope for the fakoli-state CLI.

Backlog T006/B10 (P0): make the engine drivable by non-Claude hosts
(Codex/Cursor/CI/scripts) by emitting one consistent, pipeable JSON envelope.

Envelope shape
--------------
Success::

    {"ok": true, "command": "<name>", "data": {...}}

Failure::

    {"ok": false, "command": "<name>", "error": {"code": "<code>", "message": "<msg>"}}

Contract
--------
* When ``--json`` is set a command emits EXACTLY ONE line of valid JSON to
  **stdout** and nothing else — no Rich tables, no human-readable lines, no
  warnings. The output is therefore safe to pipe into ``jq`` / ``json.load``.
* On failure the error envelope is printed to stdout (so a consumer that
  captures stdout always gets parseable JSON) and the process exits non-zero.
* ``data`` is always a JSON object (dict). List-shaped results are wrapped
  (e.g. ``{"tasks": [...], "count": N}``) so the envelope is uniform and
  forward-compatible — new top-level keys never break existing consumers.
* Domain objects are serialized via Pydantic ``model_dump(mode="json")`` so the
  JSON never drifts from the models (datetimes → ISO-8601, enums → values).

This module owns the envelope and the success/error emit helpers. Command
modules import ``emit_success`` / ``fail`` and branch on their local
``json_output`` flag. The single ``--json`` Typer option is declared here as
``JSON_OPTION`` so every command shares one definition (consistent flag name,
help text, default).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, NoReturn

import typer

if TYPE_CHECKING:
    from pydantic import BaseModel

__all__ = [
    "JSON_OPTION",
    "dump_model",
    "dump_models",
    "emit_success",
    "fail",
    "fail_with",
]


# A single shared definition so every command's ``--json`` flag is identical.
# Declared as a module-level Typer Option; commands reference it as the default
# of their ``json_output`` parameter.
JSON_OPTION: bool = typer.Option(
    False,
    "--json",
    help=(
        "Emit a single machine-readable JSON envelope to stdout instead of "
        "human-readable output. Suppresses all tables, colour, and "
        "progress text so the result is safe to pipe into jq / json.load. "
        "On error the envelope is {\"ok\": false, ...} with a non-zero exit."
    ),
)


def dump_model(model: BaseModel) -> dict[str, Any]:
    """Serialize a single Pydantic model to a JSON-safe dict.

    Uses ``model_dump(mode="json")`` so datetimes become ISO-8601 strings and
    enums become their ``.value`` — the canonical wire form already used by the
    event payloads throughout the backend. Reusing the model's own serializer
    is what keeps the ``--json`` surface from drifting from the domain models.
    """
    return model.model_dump(mode="json")


def dump_models(models: object) -> list[dict[str, Any]]:
    """Serialize an iterable of Pydantic models to a list of JSON-safe dicts."""
    return [m.model_dump(mode="json") for m in models]  # type: ignore[attr-defined]


def emit_success(command: str, data: dict[str, Any]) -> None:
    """Print the success envelope as one compact JSON line to stdout.

    ``data`` MUST be a JSON-serializable dict. Domain objects should already be
    passed through :func:`dump_model` / :func:`dump_models`; any remaining
    non-JSON value is coerced via ``default=str`` so emission never crashes a
    command that already did its real work.
    """
    envelope = {"ok": True, "command": command, "data": data}
    typer.echo(json.dumps(envelope, default=str))


def fail(
    command: str,
    message: str,
    *,
    code: str = "error",
    exit_code: int = 1,
) -> NoReturn:
    """Print the error envelope to stdout and raise ``typer.Exit(exit_code)``.

    Printed to **stdout** (not stderr) on purpose: a consumer that captures
    stdout to ``json.load`` always receives a parseable envelope, success or
    failure. ``code`` is a short stable token (``"not_found"``,
    ``"not_initialized"``, ``"bad_request"``, …) for programmatic branching;
    ``message`` is the human-readable detail. ``exit_code`` is non-zero so shell
    callers and CI see the failure via ``$?``.
    """
    envelope = {
        "ok": False,
        "command": command,
        "error": {"code": code, "message": message},
    }
    typer.echo(json.dumps(envelope, default=str))
    raise typer.Exit(code=exit_code)


def fail_with(
    command: str,
    message: str,
    *,
    code: str = "error",
    exit_code: int = 1,
    extra: dict[str, Any] | None = None,
) -> NoReturn:
    """Like :func:`fail`, but merges *extra* keys into the ``error`` object.

    Same envelope shape and stdout/exit contract as :func:`fail` — the only
    difference is that ``extra`` adds structured detail alongside ``code`` /
    ``message`` (e.g. ``{"missing": ["test output", "PR link"]}`` for the
    T025/B25 ``evidence_incomplete`` rejection). ``code`` / ``message`` always
    win over any same-named key in ``extra`` so the canonical fields cannot be
    clobbered.
    """
    error: dict[str, Any] = dict(extra or {})
    error["code"] = code
    error["message"] = message
    envelope = {"ok": False, "command": command, "error": error}
    typer.echo(json.dumps(envelope, default=str))
    raise typer.Exit(code=exit_code)
