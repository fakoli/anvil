"""Explicit Jev advice and a bounded local consumer bridge, never proof truth."""
# ruff: noqa: B008 -- Typer declares command options as defaults.

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import typer
import yaml

from anvil.cli._helpers import _require_state_dir, _resolve_state_dir
from anvil.cli._json import JSON_OPTION, emit_success, fail
from anvil.config import Config, global_config_path, load_merged_config
from anvil.jev import CAPABILITIES, JevConfig
from anvil.jev import evaluate as call_jev

jev_app = typer.Typer(help="Optional Jev cloud advice. Never proof or action authority.")
MAX_BRIDGE_BYTES = 64 * 1024


def _selected_input(path: Path) -> tuple[object, str]:
    """Read a selected bounded regular JSON file, never an env file or FIFO."""
    if path.name.startswith(".env") or path.is_symlink():
        raise ValueError("sensitive_source")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("invalid_source")
        raw = source.read(MAX_BRIDGE_BYTES + 1)
    import io

    return _read_json(io.BytesIO(raw)), hashlib.sha256(raw).hexdigest()


def _read_json(stream: Any) -> object:
    raw = stream.read(MAX_BRIDGE_BYTES + 1)
    if len(raw) > MAX_BRIDGE_BYTES:
        raise ValueError("input_limit")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=unique)


def advise(
    config: JevConfig,
    capability: str,
    value: object,
    *,
    allow_api: bool,
    allow_export: bool,
    disabled: bool = False,
) -> dict[str, Any]:
    """Validate a product rubric only after all export guards pass."""
    if disabled or not config.enabled or capability not in config.capabilities or not allow_api:
        return call_jev(config, capability, {}, {}, allow_api=allow_api, disabled=disabled)
    if allow_export is not True:
        report = call_jev(config, capability, {}, {}, allow_api=False)
        report["reason"] = "export_permission_required"
        return report
    from anvil.jev_questions import build_questions

    try:
        state, questions = build_questions(capability, value)
    except (ValueError, TypeError, RecursionError):
        report = call_jev(config, capability, {}, {}, allow_api=False)
        report["reason"] = "invalid_or_sensitive_input"
        return report
    return call_jev(config, capability, state, questions, allow_api=allow_api)


def _show(command: str, report: dict[str, Any], json_output: bool) -> None:
    if json_output:
        emit_success(command, report)
        return
    typer.echo(f"Jev advisory — {report['status']} ({report['reason']}). Not proof or approval.")
    typer.echo(f"Model: {report['model']}; request attempted: {report['request_started']}")
    if report.get("answers"):
        typer.echo(json.dumps(report["answers"], indent=2))


def _project(cwd: Path | None, command: str, json_output: bool) -> tuple[Path, Config]:
    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command=command, json_output=json_output)
    try:
        config = load_merged_config(state_dir / "config.yaml")
    except (ValueError, OSError, yaml.YAMLError):
        fail(command, "Project configuration is unavailable or invalid.", code="invalid_config")
    return state_dir, config


def _generation(state_dir: Path) -> tuple[tuple[int, ...] | None, ...]:
    """Invalidate advice across disable/re-enable, even with identical settings."""
    stamps: list[tuple[int, ...] | None] = []
    for path in (state_dir / "config.yaml", global_config_path()):
        try:
            info = path.stat()
            stamps.append((info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns))
        except FileNotFoundError:
            stamps.append(None)
    return tuple(stamps)


def _snapshot(
    cwd: Path | None,
    command: str,
    json_output: bool,
) -> tuple[Path, Config, tuple[tuple[int, ...] | None, ...]]:
    state_dir = _resolve_state_dir(cwd)
    try:
        generation = _generation(state_dir)
        _, config = _project(cwd, command, json_output)
        if _generation(state_dir) != generation:
            raise ValueError("policy_changed")
    except (OSError, ValueError):
        fail(command, "Project configuration changed or is unavailable.", code="invalid_config")
    return state_dir, config, generation


def _current(
    state_dir: Path,
    config: Config,
    generation: tuple[tuple[int, ...] | None, ...],
) -> bool:
    try:
        fresh = load_merged_config(state_dir / "config.yaml")
        return (
            _generation(state_dir) == generation
            and fresh.jev == config.jev
            and fresh.llm_allow_api == config.llm_allow_api
        )
    except (OSError, ValueError, yaml.YAMLError):
        return False


@jev_app.command("status")
def status(
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),
) -> None:
    """Show non-secret effective settings; never probe TypeSafe or read a key."""
    _settings("jev status", cwd, json_output)


def _settings(command: str, cwd: Path | None, json_output: bool) -> None:
    _, config = _project(cwd, command, json_output)
    data = {
        **asdict(config.jev),
        "allow_api": config.llm_allow_api,
        "provider": "typesafe",
        "advisory": True,
        "export": "explicit per operation",
        "supported_capabilities": CAPABILITIES,
    }
    if json_output:
        emit_success(command, data)
    else:
        typer.echo(json.dumps(data, indent=2))


def _save_settings(
    path: Path,
    config: JevConfig,
    *,
    grant_api: bool = False,
    expected: Config | None = None,
) -> None:
    """Preserve other settings; never write a secret or initialize project state."""
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("unsupported_config_file")
    # Reuse the existing capture-and-verify publication primitive: a plain
    # read/check/replace would silently discard a concurrently applied disable.
    from anvil.cli._sample import SampleSeedError
    from anvil.cli.scan import _atomic_replace_prd

    original = path.read_bytes()
    if expected is not None and load_merged_config(path) != expected:
        raise ValueError("config_changed")
    data = yaml.safe_load(original)
    if not isinstance(data, dict):
        raise ValueError("invalid_config")
    data["jev"] = {**asdict(config), "capabilities": list(config.capabilities)}
    if grant_api:
        data["llm_allow_api"] = True
    try:
        published = _atomic_replace_prd(
            path,
            yaml.safe_dump(data, sort_keys=False).encode(),
            operation="update Jev configuration",
            expected=original,
        )
        if published is None:
            raise ValueError("config_changed")
    except SampleSeedError:
        raise ValueError("config_write_failed") from None


@jev_app.command("enable")
def enable(
    capability: str,
    allow_api: bool = typer.Option(
        False, "--allow-api", help="Explicitly permit API calls; does not switch providers."
    ),
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),
) -> None:
    """Enable one capability. Selected text still requires --allow-export at use."""
    state_dir, config = _project(cwd, "jev enable", json_output)
    if capability not in CAPABILITIES:
        fail("jev enable", "Unsupported Jev capability.", code="invalid_capability")
    if not config.llm_allow_api and not allow_api:
        fail(
            "jev enable",
            "API permission is off; use --allow-api to grant it explicitly.",
            code="api_permission_required",
        )
    selected = replace(
        config.jev,
        enabled=True,
        capabilities=tuple(dict.fromkeys((*config.jev.capabilities, capability))),
    )
    try:
        _save_settings(state_dir / "config.yaml", selected, grant_api=allow_api, expected=config)
    except (OSError, ValueError, yaml.YAMLError):
        fail("jev enable", "Could not safely update Jev configuration.", code="config_write_failed")
    _settings("jev enable", cwd, json_output)


@jev_app.command("disable")
def disable(
    capability: str | None = typer.Argument(None),
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),
) -> None:
    """Disable all Jev calls, or remove one capability. Does not alter API providers."""
    state_dir, config = _project(cwd, "jev disable", json_output)
    if capability is not None and capability not in CAPABILITIES:
        fail("jev disable", "Unsupported Jev capability.", code="invalid_capability")
    selected = (
        replace(config.jev, enabled=False)
        if capability is None
        else replace(
            config.jev,
            capabilities=tuple(c for c in config.jev.capabilities if c != capability),
        )
    )
    try:
        _save_settings(state_dir / "config.yaml", selected, expected=config)
    except (OSError, ValueError, yaml.YAMLError):
        fail(
            "jev disable", "Could not safely update Jev configuration.", code="config_write_failed"
        )
    _settings("jev disable", cwd, json_output)


@jev_app.command("evaluate")
def evaluate(
    capability: str,
    input_file: Path = typer.Option(
        ..., "--input", help="Selected JSON input, never an automatically discovered source."
    ),
    allow_export: bool = typer.Option(
        False, "--allow-export", help="Permit this selected input to be sent to TypeSafe."
    ),
    no_jev: bool = typer.Option(
        False, "--no-jev", help="Disable Jev for this operation, before reading the input."
    ),
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),
) -> None:
    """Request explicitly attributed advice without changing any project state."""
    state_dir, config, generation = _snapshot(cwd, "jev evaluate", json_output)
    value: object = None
    source_digest: str | None = None
    if (
        not no_jev
        and config.jev.enabled
        and capability in config.jev.capabilities
        and config.llm_allow_api
        and allow_export
    ):
        try:
            value, source_digest = _selected_input(input_file)
        except (OSError, ValueError, RecursionError):
            fail(
                "jev evaluate",
                "Input must be bounded, valid, selected JSON; env files and symlinks are refused.",
                code="invalid_input",
            )
    report = advise(
        config.jev,
        capability,
        value,
        allow_api=config.llm_allow_api,
        allow_export=allow_export,
        disabled=no_jev or not _current(state_dir, config, generation),
    )
    # Recheck a concurrent off switch before exposing a late recommendation.
    if not _current(state_dir, config, generation):
        report.update(status="unavailable", reason="policy_changed", used=False, answers={})
    if source_digest is not None:
        try:
            unchanged = _selected_input(input_file)[1] == source_digest
        except (OSError, ValueError, RecursionError):
            unchanged = False
        if not unchanged:
            report.update(status="unavailable", reason="source_changed", used=False, answers={})
    _show("jev evaluate", report, json_output)


@jev_app.command("audit")
def audit(
    input_file: Path = typer.Option(
        ..., "--input", help="JSON list of at most 16 selected advisory items."
    ),
    allow_export: bool = typer.Option(
        False, "--allow-export", help="Permit these selected inputs to be sent to TypeSafe."
    ),
    no_jev: bool = typer.Option(False, "--no-jev"),
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),
) -> None:
    """Audit selected claims/decisions in order; never approve or execute them."""
    state_dir, config, generation = _snapshot(cwd, "jev audit", json_output)
    if (
        no_jev
        or not config.jev.enabled
        or not config.jev.capabilities
        or not config.llm_allow_api
        or not allow_export
    ):
        report = advise(
            config.jev,
            "evidence_triage",
            None,
            allow_api=config.llm_allow_api,
            allow_export=allow_export,
            disabled=no_jev,
        )
        _show("jev audit", report, json_output)
        return
    try:
        items, source_digest = _selected_input(input_file)
        if not isinstance(items, list) or not 1 <= len(items) <= 16:
            raise ValueError("invalid_batch")
        ids = set()
        for item in items:
            if not isinstance(item, dict) or set(item) != {"id", "capability", "input"}:
                raise ValueError("invalid_item")
            identifier = item["id"]
            if (
                not isinstance(identifier, str)
                or not identifier.isascii()
                or not identifier.replace("-", "").replace("_", "").isalnum()
                or not 1 <= len(identifier) <= 64
                or identifier in ids
            ):
                raise ValueError("invalid_id")
            ids.add(identifier)
            if item["capability"] not in CAPABILITIES:
                raise ValueError("invalid_capability")
    except (OSError, TypeError, ValueError, RecursionError):
        fail(
            "jev audit",
            "Audit input must contain 1–16 unique, bounded selected items.",
            code="invalid_input",
        )
    reports = []
    for item in items:
        if not _current(state_dir, config, generation):
            break
        report = advise(
            config.jev,
            item["capability"],
            item["input"],
            allow_api=config.llm_allow_api,
            allow_export=allow_export,
        )
        reports.append({"id": item["id"], "annotation": report})
    try:
        unchanged = _selected_input(input_file)[1] == source_digest
    except (OSError, ValueError, RecursionError):
        unchanged = False
    stale = not unchanged or not _current(state_dir, config, generation)
    if stale:
        for row in reports:
            row["annotation"].update(
                status="unavailable", reason="source_or_policy_changed", used=False, answers={}
            )
    data = {
        "schema": "anvil.jev.audit.v1",
        "advisory": True,
        "provider": "typesafe",
        "source_digest": source_digest,
        "items": reports,
        "complete": len(reports) == len(items) and not stale,
        "input_count": len(items),
        "request_count": sum(r["annotation"]["request_started"] for r in reports),
    }
    if json_output:
        emit_success("jev audit", data)
    else:
        typer.echo(
            "Jev advisory audit — not proof or approval. One bounded request per enabled item."
        )
        for row in reports:
            annotation = row["annotation"]
            detail = (
                json.dumps(annotation["answers"]) if annotation["answers"] else annotation["status"]
            )
            typer.echo(f"{row['id']}: {detail}")
        typer.echo(f"Complete: {data['complete']}; requests attempted: {data['request_count']}")


@jev_app.command("bridge")
def bridge(json_output: bool = JSON_OPTION) -> None:
    """Stateless local bridge for a trusted consumer's policy and selected input.

    Reads one bounded JSON envelope from stdin; never loads or initializes an
    Anvil project. The calling owner must authorize its principal and sources.
    """
    try:
        value = _read_json(sys.stdin.buffer)
        if not isinstance(value, dict) or set(value) != {
            "jev",
            "allow_api",
            "allow_export",
            "capability",
            "input",
        }:
            raise ValueError("invalid_envelope")
        if type(value["allow_api"]) is not bool or type(value["allow_export"]) is not bool:
            raise ValueError("invalid_permission")
        if not isinstance(value["capability"], str) or value["capability"] not in CAPABILITIES:
            raise ValueError("invalid_capability")
        config = JevConfig.from_mapping(value["jev"])
    except (OSError, ValueError, TypeError, RecursionError):
        fail("jev bridge", "Invalid or oversized Jev bridge envelope.", code="invalid_input")
    report = advise(
        config,
        value["capability"],
        value["input"],
        allow_api=value["allow_api"],
        allow_export=value["allow_export"],
    )
    _show("jev bridge", report, json_output)


@jev_app.command("assess")
def assess(
    file: Path = typer.Option(..., "--file", help="Selected PRD draft to assess; never rewritten."),
    allow_export: bool = typer.Option(
        False, "--allow-export", help="Permit selected acceptance criteria to be sent to TypeSafe."
    ),
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),
) -> None:
    """Show local PRD findings plus separately enabled semantic advice."""
    from anvil.cli._helpers import PrdSourceIngestError, ingest_prd_source
    from anvil.planning.behavioral_readiness import assess_behavioral_readiness, findings_as_dicts
    from anvil.planning.template import parse_prd

    state_dir, config, generation = _snapshot(cwd, "jev assess", json_output)
    try:
        source = ingest_prd_source(file)
        parsed = parse_prd(source.markdown)
        if parsed.errors:
            raise ValueError("invalid_prd")
    except (OSError, ValueError, PrdSourceIngestError):
        fail("jev assess", "The selected source must be a valid PRD.", code="invalid_prd")
    criteria = [
        {"id": f"criterion{i + 1}", "text": text}
        for i, text in enumerate(parsed.prd.acceptance_criteria)
    ]
    report = advise(
        config.jev,
        "prd_review",
        {"criteria": criteria},
        allow_api=config.llm_allow_api,
        allow_export=allow_export,
        disabled=not _current(state_dir, config, generation),
    )
    if not _current(state_dir, config, generation):
        report.update(status="unavailable", reason="policy_changed", used=False, answers={})
    try:
        unchanged = ingest_prd_source(file).source_sha256 == source.source_sha256
    except (OSError, ValueError, PrdSourceIngestError):
        unchanged = False
    if not unchanged:
        report.update(status="unavailable", reason="source_changed", used=False, answers={})
    findings = findings_as_dicts(assess_behavioral_readiness(parsed))
    data = {
        "advisory": True,
        "source_digest": source.source_sha256,
        "findings": findings,
        "jev": report,
    }
    if json_output:
        emit_success("jev assess", data)
    else:
        typer.echo(f"Local behavioural findings: {len(findings)}")
        _show("jev assess", report, False)
