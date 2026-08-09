"""JSON-only provider project reads."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import typer

from anvil.cli._helpers import StateRootError, _resolve_state_dir
from anvil.cli._json import JSON_OPTION, dump_model, emit_success, fail_with
from anvil.cli.describe import API_VERSION
from anvil.project_snapshot import ProjectSnapshotError, read_project_snapshot
from anvil.read_contracts import (
    PROJECT_SNAPSHOT_OPERATION_ID,
    PROJECT_SNAPSHOT_OPERATION_VERSION,
    PROJECT_SNAPSHOT_SCHEMA_ID,
    ProviderLimitRefusalV1,
    ProviderReadLimitsV1,
    ReadErrorCode,
    ReadErrorV1,
)
from anvil.state.schema import get_schema_version

project_app = typer.Typer(help="Read bounded, versioned project state.")

_COMMAND = "project snapshot"
_DIGEST_ALGORITHM = "sha256"
_MAX_LIMIT_ARGUMENT_BYTES = 128


def _emit_refusal(error: ReadErrorV1 | ProviderLimitRefusalV1) -> NoReturn:
    data = dump_model(error)
    code = str(data.pop("code"))
    message = str(
        data.pop("message", "A provider read limit was exceeded.")
    )
    fail_with(_COMMAND, message, code=code, extra=data)


def _invalid_request() -> NoReturn:
    _emit_refusal(ReadErrorV1(code=ReadErrorCode.invalid_request, field="request"))


def _parse_limits(values: list[str] | None) -> dict[str, int] | None:
    if not values:
        return None
    if len(values) > len(ProviderReadLimitsV1.model_fields):
        _invalid_request()

    requested: dict[str, int] = {}
    for value in values:
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeError:
            _invalid_request()
        if len(encoded) > _MAX_LIMIT_ARGUMENT_BYTES:
            _invalid_request()
        name, separator, raw_value = value.partition("=")
        if (
            not separator
            or name not in ProviderReadLimitsV1.model_fields
            or name in requested
            or not raw_value.isascii()
            or not raw_value.isdecimal()
        ):
            _invalid_request()
        try:
            requested[name] = int(raw_value, 10)
        except ValueError:
            _invalid_request()
    return requested


@project_app.command("snapshot")
def snapshot(
    json_output: bool = JSON_OPTION,
    limit: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--limit",
        help=(
            "Lower one provider ceiling as NAME=VALUE; repeat for additional "
            "ceilings. Values may never exceed the published version-1 limit."
        ),
    ),
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
) -> None:
    """Return one atomic allowlisted project hierarchy as JSON."""
    if not json_output:
        _invalid_request()
    requested_limits = _parse_limits(limit)
    try:
        state_dir = _resolve_state_dir(cwd)
        result = read_project_snapshot(state_dir, limits=requested_limits)
    except StateRootError:
        _emit_refusal(
            ReadErrorV1(code=ReadErrorCode.state_unavailable, field="state")
        )
    except ProjectSnapshotError as exc:
        _emit_refusal(exc.error)

    data = dump_model(result)
    data.update(
        {
            "operation_id": PROJECT_SNAPSHOT_OPERATION_ID,
            "operation_version": PROJECT_SNAPSHOT_OPERATION_VERSION,
            "output_schema_id": PROJECT_SNAPSHOT_SCHEMA_ID,
            "api_version": API_VERSION,
            "schema_version": get_schema_version(),
            "digest_algorithm": _DIGEST_ALGORITHM,
            "truncated": False,
        }
    )
    emit_success(_COMMAND, data)


__all__ = ["project_app", "snapshot"]
