"""prd sub-app: prd parse, prd review, prd find-decisions (Phase 3 + v1.14.0)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

import typer

from anvil.cli._helpers import (
    _DEFAULT_PRD_IDS,
    PRD_OPTION,
    PrdAmbiguityError,
    PrdSourceIngestError,
    StateRootError,
    _get_project_id,
    _open_backend,
    _require_state_dir,
    _resolve_state_dir,
    canonical_prd_id,
    ingest_prd_source,
    ingest_prd_source_for_id,
    prd_source_filename,
    replace_prd_source_for_id,
    resolve_prd_id,
    selected_prd_source_path,
    validate_prd_id,
)
from anvil.cli._json import JSON_OPTION, emit_success, fail, fail_with
from anvil.state.backend import EventRejected
from anvil.state.models import EventDraft

prd_app = typer.Typer(
    name="prd",
    help="PRD lifecycle commands: parse, assess, review, approve.",
    no_args_is_help=True,
)

_ALLOWED_TERMINAL_TITLE_FORMAT_CONTROLS = {"\u200c", "\u200d"}
_MAX_DECISION_SOURCE_BYTES = 2_097_152
_MAX_DECISION_EVENT_BYTES = 16 * 1024 * 1024


def _decision_prd_id(
    backend: Any,
    requested: str | None,
    *,
    require_existing: bool = True,
) -> str:
    """Resolve one existing PRD partition for decision operations."""
    try:
        resolved = canonical_prd_id(resolve_prd_id(backend, requested))
    except PrdAmbiguityError:
        if backend.list_prds():
            raise
        # Decision discovery historically runs before the first parse. Keep
        # that source-only workflow while still making an explicit id/env the
        # authoritative future partition.
        resolved = canonical_prd_id(requested or "default")
    validated = canonical_prd_id(validate_prd_id(resolved))
    if require_existing and backend.get_prd(validated) is None:
        raise PrdSourceIngestError(
            "prd_not_found",
            f"PRD partition {validated!r} does not exist",
        )
    return validated


def _decision_source(
    state_dir: Path,
    *,
    cwd: Path | None,
    file: Path | None,
    prd_id: str,
) -> tuple[Path, str, str]:
    """Read one exact decision source while keeping content and scope separate."""
    if file is not None:
        source_path = file
        if not source_path.is_absolute():
            base = cwd.resolve() if cwd is not None else Path.cwd().resolve()
            source_path = base / source_path
        source = ingest_prd_source(source_path)
        return source_path, source.markdown, "custom"
    source_path = selected_prd_source_path(state_dir, prd_id)
    source = ingest_prd_source_for_id(state_dir, prd_id)
    return source_path, source.markdown, prd_id


def _replace_custom_decision_source(
    source_path: Path,
    *,
    expected_sha256: str,
    markdown: str,
) -> str:
    """CAS-replace one custom decision source through a same-directory temp."""
    source_bytes = markdown.encode("utf-8", errors="strict")
    if len(source_bytes) > _MAX_DECISION_SOURCE_BYTES:
        raise PrdSourceIngestError(
            "source_limit_exceeded",
            "PRD source exceeds the configured byte limit",
        )
    current = ingest_prd_source(source_path)
    if current.source_sha256 != expected_sha256:
        raise PrdSourceIngestError(
            "source_changed",
            "PRD source changed before verified replacement",
        )
    resolved = source_path.resolve(strict=True)
    opened = os.stat(resolved, follow_symlinks=False)
    if not stat.S_ISREG(opened.st_mode):
        raise PrdSourceIngestError(
            "source_unavailable",
            "custom PRD source is not a regular file",
        )

    descriptor = -1
    temp_path: Path | None = None
    try:
        descriptor, raw_temp = tempfile.mkstemp(
            prefix=f".{resolved.name}.",
            suffix=".tmp",
            dir=resolved.parent,
        )
        temp_path = Path(raw_temp)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(source_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        latest = ingest_prd_source(resolved)
        if latest.source_sha256 != expected_sha256:
            raise PrdSourceIngestError(
                "source_changed",
                "PRD source changed before verified replacement",
            )
        os.chmod(temp_path, stat.S_IMODE(opened.st_mode))
        os.replace(temp_path, resolved)
        temp_path = None
    except PrdSourceIngestError:
        raise
    except OSError as exc:
        raise PrdSourceIngestError(
            "source_unavailable",
            "cannot replace verified custom PRD source",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
    return hashlib.sha256(source_bytes).hexdigest()


def _decision_event_was_appended(
    events_path: Path,
    *,
    previous_size: int,
    draft: EventDraft,
) -> bool:
    """Return whether one complete matching event became durable after an error."""
    try:
        with events_path.open("rb") as stream:
            stream.seek(previous_size)
            appended = stream.read(_MAX_DECISION_EVENT_BYTES + 1)
    except OSError:
        return False
    if (
        not appended
        or len(appended) > _MAX_DECISION_EVENT_BYTES
        or not appended.endswith(b"\n")
        or appended.count(b"\n") != 1
    ):
        return False
    try:
        record = json.loads(appended.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(record, dict)
        and record.get("actor") == draft.actor
        and record.get("action") == draft.action
        and record.get("target_kind") == draft.target_kind
        and record.get("target_id") == draft.target_id
        and record.get("payload_json") == draft.payload_json
    )


@prd_app.command("show")
def prd_show(
    prd_id: str = typer.Argument(..., help="Exact PRD partition identifier."),
    json_output: bool = JSON_OPTION,
    section: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--section",
        help=(
            "Exact case-sensitive slash-delimited ATX heading path; repeat to "
            "select multiple non-overlapping sections in source order."
        ),
    ),
    expected_digest: str | None = typer.Option(  # noqa: B008
        None,
        "--expected-digest",
        help="Require this exact lowercase SHA-256 persisted-source digest.",
    ),
    limit: str | None = typer.Option(  # noqa: B008
        None,
        "--limit",
        "--max-bytes",
        help="Lower the immutable 2 MiB returned-content ceiling.",
    ),
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        hidden=True,
    ),
) -> None:
    """Return exact persisted PRD source bytes through the JSON-only v1 read."""
    from anvil.prd_content import (
        PrdContentRefusal,
        parse_prd_content_limit,
        read_prd_content,
        validate_prd_content_request,
    )
    from anvil.read_contracts import ReadErrorCode

    command = "prd show"
    if not json_output:
        fail(
            command,
            "The read request is invalid.",
            code=ReadErrorCode.invalid_request.value,
    )
    try:
        max_bytes = parse_prd_content_limit(limit)
        validate_prd_content_request(
            prd_id,
            sections=section,
            expected_digest=expected_digest,
            max_bytes=max_bytes,
        )
        state_dir = _resolve_state_dir(cwd)
        response = read_prd_content(
            state_dir,
            prd_id,
            sections=section,
            expected_digest=expected_digest,
            max_bytes=max_bytes,
        )
    except (StateRootError, OSError):
        fail(
            command,
            "Project state is unavailable.",
            code=ReadErrorCode.state_unavailable.value,
            exit_code=1,
        )
    except PrdContentRefusal as exc:
        error = exc.error.model_dump(mode="json", exclude={"code", "message"})
        error["truncated"] = False
        fail_with(
            command,
            exc.error.message,
            code=exc.error.code.value,
            extra=error,
        )
    emit_success(command, response.model_dump(mode="json"))


def _escape_legacy_title_for_terminal(title: str) -> str:
    """Escape terminal-active legacy title code points without changing data.

    New parses reject these characters, but old/imported event logs may already
    contain them. Human output is a trust boundary; JSON and projection state
    remain lossless and unchanged.
    """
    rendered: list[str] = []
    for char in title:
        category = unicodedata.category(char)
        unsafe = category in {"Cc", "Cs", "Zl", "Zp"} or (
            category == "Cf"
            and char not in _ALLOWED_TERMINAL_TITLE_FORMAT_CONTROLS
        )
        if not unsafe:
            rendered.append(char)
            continue
        codepoint = ord(char)
        if codepoint <= 0xFF:
            rendered.append(f"\\x{codepoint:02x}")
        elif codepoint <= 0xFFFF:
            rendered.append(f"\\u{codepoint:04x}")
        else:
            rendered.append(f"\\U{codepoint:08x}")
    return "".join(rendered)

@prd_app.command("parse")
def prd_parse(
    file: Path | None = typer.Option(  # noqa: B008
        None,
        "--file",
        help=(
            "Path to the PRD markdown file. "
            "Defaults to .anvil/prd.md in the current directory."
        ),
    ),
    prd: str | None = typer.Option(  # noqa: B008
        None,
        "--prd",
        help=(
            "Named PRD to parse (multi-PRD). Reads its portable source in the "
            "PRD collection and scopes the parse to that partition. Omit for "
            "the default PRD. Ignored when --file is given."
        ),
    ),
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Parse a PRD and store the result as a prd.parsed event.

    Reads the default source (or --file PATH, or a named portable source via
    --prd), calls the template parser, emits a prd.parsed event with the full PRD +
    requirements payload. With --prd the event carries that prd_id so the
    backend writes only that PRD's partition, leaving other PRDs untouched.

    Exits 1 if there are parse errors or the file cannot be read.
    On success, prints a summary of what was parsed.
    """
    from anvil.clock import SystemClock
    from anvil.planning.diagnostics import format_parse_error, parse_diagnostic_report
    from anvil.planning.prd_persistence import (
        PrdRevisionError,
        build_prd_persistence_plan,
    )
    from anvil.planning.template import parse_prd

    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="prd parse", json_output=json_output)

    # The parse-time prd_id controls id shape and the partition the event
    # writes into. ``--prd v0.2`` scopes to a named PRD; the default ('prd'
    # sentinel) keeps bare ids and the default partition, byte-identical to
    # the pre-multi-PRD behaviour. ``--file`` always reads the given path but
    # still honours ``--prd`` for the partition.
    source_identity: str | None = None
    try:
        parse_prd_id = validate_prd_id(prd if prd is not None else "prd")
        if file is not None:
            source_identity = "custom"
            prd_path = file
            if not prd_path.is_absolute():
                base = cwd.resolve() if cwd is not None else Path.cwd().resolve()
                prd_path = base / prd_path
            source = ingest_prd_source(prd_path)
        else:
            source_identity = canonical_prd_id(parse_prd_id)
            source = ingest_prd_source_for_id(state_dir, parse_prd_id)
    except PrdSourceIngestError as exc:
        detail = f"{exc.message.rstrip('.')}."
        if json_output:
            code = "invalid_encoding" if exc.code == "source_invalid_utf8" else exc.code
            fail("prd parse", detail, code=code)
        suffix = f" Source: {source_identity}." if source_identity is not None else ""
        typer.echo(f"Error: {detail}{suffix}", err=True)
        raise typer.Exit(code=1) from exc
    markdown = source.markdown

    result = parse_prd(markdown, prd_id=parse_prd_id)

    if result.errors:
        report = parse_diagnostic_report(result.errors)
        if json_output:
            fail_with(
                "prd parse",
                f"PRD parse failed with {len(result.errors)} error(s).",
                code="parse_error",
                extra={
                    "errors": [
                        {
                            "section": err.section,
                            "line": err.line,
                            "message": err.message,
                        }
                        for err in report.entries
                    ],
                    "error_count": report.total_count,
                    "errors_shown": report.shown_count,
                    "errors_omitted": report.omitted_count,
                    "errors_truncated": report.errors_truncated,
                    "error_messages_truncated": report.messages_truncated,
                },
            )
        for err in report.entries:
            typer.echo(
                f"  Parse error {format_parse_error(err)}",
                err=True,
            )
        if report.omitted_count:
            typer.echo(
                f"  ... showing {report.shown_count} of {report.total_count}; "
                f"{report.omitted_count} omitted.",
                err=True,
            )
        typer.echo(
            f"Error: PRD parse failed with {len(result.errors)} error(s). "
            "Fix the issues above and re-run.",
            err=True,
        )
        raise typer.Exit(code=1)

    backend = _open_backend(state_dir)
    persistence_action = "parsed"
    effective_status = result.prd.status.value
    try:
        clock = SystemClock()
        project_id = _get_project_id(backend)
        stored_prd_id = result.prd.id
        is_default_prd = parse_prd_id in _DEFAULT_PRD_IDS
        try:
            persistence = build_prd_persistence_plan(
                backend,
                result,
                source,
                project_id=project_id,
                is_default=is_default_prd,
                actor="anvil-cli",
                clock=clock,
            )
        except PrdRevisionError as exc:
            message = str(exc)
            if json_output:
                fail("prd parse", message, code="invalid_revision")
            typer.echo(f"Error: {message}", err=True)
            raise typer.Exit(code=1) from exc
        persistence_action = persistence.action
        effective_status = persistence.status
        try:
            if persistence.draft is not None:
                backend.append(persistence.draft)
        except EventRejected as exc:
            # Current create/revision events carry optimistic preconditions.
            # A concurrent first parse, re-parse, review, or approval can make
            # them stale; surface that domain rejection without a traceback.
            message = f"PRD parse rejected: {exc}"
            if json_output:
                fail("prd parse", message, code="event_rejected")
            typer.echo(f"Error: {message}", err=True)
            raise typer.Exit(code=1) from exc
        if persistence.draft is not None:
            persisted_prd = backend.get_prd(stored_prd_id)
            if persisted_prd is not None:
                effective_status = persisted_prd.status.value
    finally:
        backend.close()

    verb = {
        "parsed": "Parsed",
        "revised": "Revised",
        "unchanged": "Unchanged",
    }[persistence_action]
    if json_output:
        emit_success(
            "prd parse",
            {
                "prd_id": result.prd.id,
                "action": persistence_action,
                "prd_status": effective_status,
                "requirement_count": len(result.requirements),
                "feature_count": len(result.features),
                "task_count": len(result.tasks),
                "prd_source": source_identity,
            },
        )
        return
    typer.echo(
        f"{verb} {len(result.requirements)} requirements, "
        f"{len(result.features)} features, "
        f"{len(result.tasks)} tasks."
    )
    typer.echo(f"PRD source: {source_identity}")


@prd_app.command("source-name")
def prd_source_name(
    prd: str | None = typer.Option(  # noqa: B008
        None,
        "--prd",
        help="Named PRD identity. Omit for the default PRD.",
    ),
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Print the portable relative source name for authoring workflows."""
    command = "prd source-name"
    try:
        validated_id = validate_prd_id(prd if prd is not None else "prd")
    except PrdSourceIngestError as exc:
        if json_output:
            fail(command, exc.message, code=exc.code)
        typer.echo(f"Error: {exc.message}", err=True)
        raise typer.Exit(code=1) from exc
    source_identity = canonical_prd_id(validated_id)
    try:
        state_dir = _resolve_state_dir(cwd)
    except StateRootError as exc:
        message = "cannot resolve Anvil state directory"
        if json_output:
            fail(command, message, code="state_root_error")
        typer.echo(f"Error: {message}", err=True)
        raise typer.Exit(code=1) from exc
    try:
        selected_path = selected_prd_source_path(state_dir, validated_id)
        relative_name = selected_path.relative_to(state_dir).as_posix()
    except PrdSourceIngestError as exc:
        if exc.code == "legacy_source_migration_required":
            destination = f"prds/{prd_source_filename(validated_id)}"
            message = f"{exc.message}; move it to {destination}"
        else:
            message = exc.message
        if json_output:
            fail(command, message, code=exc.code)
        typer.echo(f"Error: {message}", err=True)
        raise typer.Exit(code=1) from exc
    if json_output:
        emit_success(
            command,
            {
                "prd_source": source_identity,
                "relative_name": relative_name,
            },
        )
        return
    typer.echo(relative_name)


@prd_app.command("assess")
def prd_assess(
    file: Path | None = typer.Option(  # noqa: B008
        None,
        "--file",
        help="Path to PRD markdown. Defaults to the selected PRD source.",
    ),
    prd: str | None = typer.Option(  # noqa: B008
        None,
        "--prd",
        help="Named PRD to assess. Omit for the default PRD.",
    ),
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Report deterministic, advisory behaviour-readiness findings.

    This command only reads and parses the PRD. It never emits an event or
    changes approval, planning, claim, or autonomous-execution behaviour.
    """
    from anvil.planning.behavioral_readiness import (
        assess_behavioral_readiness,
        findings_as_dicts,
    )
    from anvil.planning.diagnostics import (
        format_parse_error,
        format_parse_error_summary,
        parse_diagnostic_report,
    )
    from anvil.planning.template import parse_prd

    command = "prd assess"
    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command=command, json_output=json_output)
    source_identity: str | None = None
    try:
        parse_prd_id = validate_prd_id(prd if prd is not None else "prd")
        if file is not None:
            source_identity = "custom"
            prd_path = file
            if not prd_path.is_absolute():
                base = cwd.resolve() if cwd is not None else Path.cwd().resolve()
                prd_path = base / prd_path
            source = ingest_prd_source(prd_path)
        else:
            source_identity = canonical_prd_id(parse_prd_id)
            source = ingest_prd_source_for_id(state_dir, parse_prd_id)
    except PrdSourceIngestError as exc:
        message = f"{exc.message}: {source_identity}" if source_identity else exc.message
        if json_output:
            fail(command, message, code=exc.code)
        typer.echo(f"Error: {message}", err=True)
        raise typer.Exit(code=1) from exc
    markdown = source.markdown

    result = parse_prd(markdown, prd_id=parse_prd_id)
    if result.errors:
        report = parse_diagnostic_report(result.errors)
        message = (
            f"PRD parse failed with {len(result.errors)} error(s): "
            f"{format_parse_error_summary(result.errors)}"
        )
        if json_output:
            fail(command, message, code="parse_error")
        for error in report.entries:
            typer.echo(
                f"  Parse error {format_parse_error(error)}",
                err=True,
            )
        if report.omitted_count:
            typer.echo(
                f"  ... showing {report.shown_count} of {report.total_count}; "
                f"{report.omitted_count} omitted.",
                err=True,
            )
        typer.echo(f"Error: {message}", err=True)
        raise typer.Exit(code=1)

    findings = assess_behavioral_readiness(result)
    if json_output:
        emit_success(
            command,
            {
                "prd_source": source_identity,
                "findings": findings_as_dicts(findings),
                "count": len(findings),
                "advisory": True,
            },
        )
        return

    typer.echo(f"PRD source: {source_identity}")
    if not findings:
        typer.echo("No behavioural-readiness findings. This remains an advisory check.")
        return
    typer.echo(f"{len(findings)} advisory behavioural-readiness finding(s):")
    for finding in findings:
        typer.echo("")
        typer.echo(f"  [{finding.id}] {finding.severity} - {finding.category}")
        typer.echo(f"    location:  {finding.location}")
        typer.echo(f"    finding:  {finding.message}")
        typer.echo(f"    challenge: {finding.challenge_question}")


@prd_app.command("review")
def prd_review(
    approve: bool = typer.Option(  # noqa: B008
        False,
        "--approve",
        help="Approve the PRD (reviewed → approved). Without this flag: draft → reviewed.",
    ),
    reviewer: str = typer.Option(  # noqa: B008
        "human",
        "--reviewer",
        help="Identity of the reviewer.",
    ),
    notes: str | None = typer.Option(  # noqa: B008
        None,
        "--notes",
        help="Optional review notes.",
    ),
    prd: str | None = PRD_OPTION,
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Transition the PRD through the review lifecycle.

    Without --approve: draft → reviewed (emits prd.reviewed event).
    With --approve:    reviewed → approved (emits prd.approved event).

    ``--prd`` (T019) names which PRD partition to review on a multi-PRD project:
    the status check reads that PRD via ``get_prd`` and the emitted event carries
    its ``prd_id`` so the handler mutates only that PRD's row. Omitting it on a
    single-PRD project keeps the pre-T019 default-PRD behaviour unchanged.
    """
    from anvil.clock import SystemClock

    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir)

    backend = _open_backend(state_dir)
    try:
        clock = SystemClock()
        now = clock.now()
        project_id = _get_project_id(backend)

        # T019: resolve which PRD this review targets. With no --prd/$ANVIL_PRD
        # the resolver returns the single/default PRD's id, so single-PRD
        # projects keep working unchanged; an explicit value scopes the lookup
        # and the emitted event to that partition. Collapse the default sentinel
        # ('prd') to the stored id ('default') so `--prd prd` finds the default
        # PRD row instead of looking up a nonexistent id='prd'.
        resolved_prd_id = canonical_prd_id(resolve_prd_id(backend, prd))

        prd_model = backend.get_prd(resolved_prd_id)
        if prd_model is None:
            typer.echo(
                "Error: no PRD found in state. Run `anvil prd parse` first.",
                err=True,
            )
            raise typer.Exit(code=1)

        try:
            canonical_source = ingest_prd_source_for_id(state_dir, resolved_prd_id)
        except PrdSourceIngestError as exc:
            typer.echo(f"Error: cannot verify canonical PRD source: {exc.message}", err=True)
            raise typer.Exit(code=1) from exc
        if (
            not prd_model.content_available
            or prd_model.source_bytes != canonical_source.source_bytes
            or prd_model.source_sha256 != canonical_source.source_sha256
            or prd_model.material_sha256 is None
            or prd_model.content_event_id is None
        ):
            typer.echo(
                "Error: canonical PRD source is not the exact parsed revision. "
                "Copy/reparse any custom source into the managed PRD source, then retry.",
                err=True,
            )
            raise typer.Exit(code=1)

        # Stamp prd_id into the event payload ONLY for a named (non-default)
        # PRD. The default PRD omits the key so the payload stays byte-identical
        # to the pre-multi-PRD event (the payload defaults prd_id='default').
        def _scope(payload: dict[str, object]) -> dict[str, object]:
            if prd_model.id not in _DEFAULT_PRD_IDS:
                payload["prd_id"] = prd_model.id
            return payload

        if approve:
            if prd_model.status.value != "reviewed":
                typer.echo(
                    f"Error: PRD must be in 'reviewed' status to approve, "
                    f"got '{prd_model.status.value}'. "
                    "Run `anvil prd review` first.",
                    err=True,
                )
                raise typer.Exit(code=1)

            draft = EventDraft(
                timestamp=now,
                actor="anvil-cli",
                action="prd.approved",
                target_kind="prd",
                target_id=project_id,
                payload_json=_scope(
                    {
                        "project_id": project_id,
                        "expected_revision": prd_model.revision,
                        "expected_status": prd_model.status.value,
                        "binding_version": 1,
                        "source_sha256": prd_model.source_sha256,
                        "material_sha256": prd_model.material_sha256,
                        "content_event_id": prd_model.content_event_id,
                        "review_event_id": prd_model.review_event_id,
                        "approver": reviewer,
                    }
                ),
            )
            try:
                backend.append(draft)
            except EventRejected as exc:
                typer.echo(f"Error: PRD approval rejected: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            typer.echo(f"PRD approved by '{reviewer}'.")
        else:
            if prd_model.status.value != "draft":
                typer.echo(
                    f"Error: PRD must be in 'draft' status to review, "
                    f"got '{prd_model.status.value}'. "
                    "Pass --approve to move from reviewed → approved.",
                    err=True,
                )
                raise typer.Exit(code=1)

            draft = EventDraft(
                timestamp=now,
                actor="anvil-cli",
                action="prd.reviewed",
                target_kind="prd",
                target_id=project_id,
                payload_json=_scope(
                    {
                        "project_id": project_id,
                        "expected_revision": prd_model.revision,
                        "expected_status": prd_model.status.value,
                        "binding_version": 1,
                        "source_sha256": prd_model.source_sha256,
                        "material_sha256": prd_model.material_sha256,
                        "content_event_id": prd_model.content_event_id,
                        "reviewer": reviewer,
                        "notes": notes,
                    }
                ),
            )
            try:
                backend.append(draft)
            except EventRejected as exc:
                typer.echo(f"Error: PRD review rejected: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            typer.echo(f"PRD reviewed by '{reviewer}'.")
            typer.echo("Run `anvil prd review --approve` to approve.")
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# prd find-decisions (v1.14.0)
# ---------------------------------------------------------------------------


_CONTEXT_TRUNCATE = 120


def _truncate(text: str, limit: int = _CONTEXT_TRUNCATE) -> str:
    """Trim a context paragraph for terminal display."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


@prd_app.command("list")
def prd_list(
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """List every PRD in the project — the multi-PRD entry point.

    Shows each release-scoped PRD (id, status, revision, target version/tag) so you
    can pick which one to work on; the default PRD is marked ``*``. A single-PRD
    project lists just the default PRD. Read-only.
    """
    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="prd list", json_output=json_output)

    backend = _open_backend(state_dir)
    try:
        # Default PRD first, then by id, so the canonical partition leads.
        prds = sorted(backend.list_prds(), key=lambda p: (not p.is_default, p.id))
    finally:
        backend.close()

    if json_output:
        emit_success(
            "prd list",
            {
                "prds": [
                    {
                        "id": p.id,
                        "status": p.status.value,
                        "revision": p.revision,
                        "is_default": p.is_default,
                        "title": p.title,
                        "target_version": p.target_version,
                        "target_tag": p.target_tag,
                    }
                    for p in prds
                ]
            },
        )
        return

    if not prds:
        typer.echo("No PRDs yet. Run `anvil prd parse` to create one.")
        return

    for p in prds:
        marker = "*" if p.is_default else " "
        target = p.target_version or p.target_tag
        suffix = f" -> {target}" if target else ""
        safe_title = _escape_legacy_title_for_terminal(p.title)
        title = f"  {safe_title}" if safe_title else ""
        typer.echo(f"{marker} {p.id}  [{p.status.value} r{p.revision}]{suffix}{title}")


@prd_app.command("find-decisions")
def prd_find_decisions(
    file: Path | None = typer.Option(  # noqa: B008
        None,
        "--file",
        help=(
            "Path to the PRD markdown file. "
            "Defaults to .anvil/prd.md in the current directory."
        ),
    ),
    prd: str | None = PRD_OPTION,
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Scan the PRD for items needing a human decision and print them.

    Read-only inspection: walks `[NEEDS DECISION]` markers in the raw
    markdown, items under `## Open Questions`, and tasks with empty
    `acceptance_criteria` / `verification.commands`. Output is grouped by
    kind (needs_decision, open_question, missing_field) with a summary
    line at the bottom.

    Exits 0 whether or not decisions are found — this is a probe, not a
    gate. Parse errors still exit 1 (matching `prd parse`) so the user
    fixes structural problems before they're hidden by missing data.
    """
    from anvil.planning.decisions import (
        DecisionKind,
        UnresolvedDecision,
        find_unresolved_decisions,
    )
    from anvil.planning.diagnostics import (
        format_parse_error,
        format_parse_error_summary,
        parse_diagnostic_report,
    )
    from anvil.planning.template import parse_prd

    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="prd find-decisions", json_output=json_output)

    backend = _open_backend(state_dir)
    try:
        effective_prd_id = _decision_prd_id(backend, prd, require_existing=False)
        prd_path, markdown, source_identity = _decision_source(
            state_dir,
            cwd=cwd,
            file=file,
            prd_id=effective_prd_id,
        )
        backend_tasks = backend.list_tasks(prd_id=effective_prd_id)
        tasks_or_none = backend_tasks or None
    except (PrdAmbiguityError, PrdSourceIngestError) as exc:
        code = exc.code if isinstance(exc, PrdSourceIngestError) else "prd_ambiguous"
        if json_output:
            fail(
                "prd find-decisions",
                str(exc),
                code=code,
            )
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        backend.close()

    result = parse_prd(markdown, prd_id=effective_prd_id)

    if result.errors:
        if json_output:
            fail(
                "prd find-decisions",
                f"PRD parse failed with {len(result.errors)} error(s): "
                + format_parse_error_summary(result.errors),
                code="parse_error",
            )
        report = parse_diagnostic_report(result.errors)
        for err in report.entries:
            typer.echo(
                f"  Parse error {format_parse_error(err)}",
                err=True,
            )
        if report.omitted_count:
            typer.echo(
                f"  ... showing {report.shown_count} of {report.total_count}; "
                f"{report.omitted_count} omitted.",
                err=True,
            )
        typer.echo(
            f"Error: PRD parse failed with {len(result.errors)} error(s). "
            "Fix the issues above and re-run.",
            err=True,
        )
        raise typer.Exit(code=1)

    decisions = find_unresolved_decisions(
        markdown,
        prd=result.prd,
        tasks=tasks_or_none,
    )

    if json_output:
        import dataclasses

        decisions_data: list[dict[str, Any]] = []
        for d in decisions:
            item = dataclasses.asdict(d)
            # ``kind`` is a DecisionKind (StrEnum) — coerce to its value so the
            # envelope carries the plain string, not the enum repr.
            item["kind"] = d.kind.value
            decisions_data.append(item)
        counts = {
            "needs_decision": sum(
                1 for d in decisions if d.kind.value == "needs_decision"
            ),
            "open_question": sum(
                1 for d in decisions if d.kind.value == "open_question"
            ),
            "missing_field": sum(
                1 for d in decisions if d.kind.value == "missing_field"
            ),
        }
        emit_success(
            "prd find-decisions",
            {
                "prd_id": effective_prd_id,
                "prd_source": source_identity,
                "decisions": decisions_data,
                "count": len(decisions),
                "counts_by_kind": counts,
            },
        )
        return

    # Group by kind, preserving the canonical order needs_decision →
    # open_question → missing_field. The detector already returns items in
    # that order so we can partition cheaply.
    by_kind: dict[DecisionKind, list[UnresolvedDecision]] = {
        DecisionKind.needs_decision: [],
        DecisionKind.open_question: [],
        DecisionKind.missing_field: [],
    }
    for d in decisions:
        by_kind[d.kind].append(d)

    _KIND_HEADERS = {
        DecisionKind.needs_decision: "NEEDS DECISION markers",
        DecisionKind.open_question: "Open Questions",
        DecisionKind.missing_field: "Missing fields",
    }

    typer.echo(f"PRD: {effective_prd_id}")
    typer.echo(f"PRD source: {source_identity}")

    for kind in (
        DecisionKind.needs_decision,
        DecisionKind.open_question,
        DecisionKind.missing_field,
    ):
        items = by_kind[kind]
        if not items:
            continue
        typer.echo("")
        typer.echo(f"== {_KIND_HEADERS[kind]} ({len(items)}) ==")
        for d in items:
            typer.echo("")
            typer.echo(f"  [{d.id}] {d.kind.value}")
            typer.echo(f"    location: {d.location}")
            typer.echo(f"    text:     {d.text}")
            if d.context_paragraph:
                typer.echo(f"    context:  {_truncate(d.context_paragraph)}")
            typer.echo(f"    resolve:  {d.suggested_resolution_field}")
            file_arg = " --file <same-file>" if file is not None else ""
            typer.echo(
                "    command:  anvil prd resolve-decision "
                f"{d.id} --prd {effective_prd_id}{file_arg} "
                "--resolution <answer>"
            )

    typer.echo("")
    typer.echo(
        f"{len(decisions)} total: "
        f"{len(by_kind[DecisionKind.needs_decision])} NEEDS_DECISION, "
        f"{len(by_kind[DecisionKind.open_question])} open questions, "
        f"{len(by_kind[DecisionKind.missing_field])} missing fields."
    )


# ---------------------------------------------------------------------------
# prd resolve-decision (T018 — decision back-propagation)
# ---------------------------------------------------------------------------


@prd_app.command("resolve-decision")
def prd_resolve_decision(
    decision_id: str = typer.Argument(  # noqa: B008
        ...,
        metavar="DECISION_ID",
        help=(
            "The decision to resolve, as reported by `prd find-decisions` "
            "(e.g. ND-001, OQ001, MF-T012-AC)."
        ),
    ),
    resolution: str = typer.Option(  # noqa: B008
        ...,
        "--resolution",
        "-r",
        help="The answer to write back into the referenced PRD span.",
    ),
    resolved_by: str = typer.Option(  # noqa: B008
        "human",
        "--by",
        help="Identity recorded as the resolver in the event log.",
    ),
    file: Path | None = typer.Option(  # noqa: B008
        None,
        "--file",
        help=(
            "Path to the PRD markdown file. "
            "Defaults to .anvil/prd.md in the current directory."
        ),
    ),
    prd: str | None = PRD_OPTION,
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Back-propagate a resolved decision into the PRD and record it (T018).

    Locates DECISION_ID via the same detector `prd find-decisions` uses,
    writes ``--resolution`` into the referenced PRD span *without overwriting
    unrelated content*, saves ``prd.md``, and appends an additive
    ``prd.decision_resolved`` event to the log. Resolving a ``[NEEDS DECISION]``
    marker rewrites the linked requirement inline; an open question moves to a
    ``## Decisions`` section; a missing field is added under its task block.

    The PRD source is edited on disk — re-run ``prd parse`` afterwards to
    refresh state.db. The event is the immutable audit fact that the decision
    was answered.
    """
    from anvil.clock import SystemClock
    from anvil.planning.decisions import (
        ResolutionError,
        apply_decision_to_markdown,
        find_unresolved_decisions,
    )
    from anvil.planning.template import parse_prd
    from anvil.state.transitions import (
        TransitionError,
        prd_decision_resolved,
    )

    cmd = "prd resolve-decision"
    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command=cmd, json_output=json_output)

    backend = _open_backend(state_dir)
    try:
        effective_prd_id = _decision_prd_id(backend, prd)
        prd_path, markdown, source_identity = _decision_source(
            state_dir,
            cwd=cwd,
            file=file,
            prd_id=effective_prd_id,
        )
        source_sha256 = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    except (PrdAmbiguityError, PrdSourceIngestError) as exc:
        backend.close()
        code = exc.code if isinstance(exc, PrdSourceIngestError) else "prd_ambiguous"
        if json_output:
            fail(cmd, str(exc), code=code)
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    result = parse_prd(markdown, prd_id=effective_prd_id)
    if result.errors:
        backend.close()
        msg = f"PRD parse failed with {len(result.errors)} error(s)."
        if json_output:
            fail(cmd, msg, code="parse_error")
        typer.echo(f"Error: {msg} Fix prd.md and re-run.", err=True)
        raise typer.Exit(code=1)

    # Pull tasks from the backend so MF-* decisions can be located.
    try:
        backend_tasks = backend.list_tasks(prd_id=effective_prd_id)
        tasks_or_none = backend_tasks or None
        decisions = find_unresolved_decisions(
            markdown,
            prd=result.prd,
            tasks=tasks_or_none,
        )

        target = next((d for d in decisions if d.id == decision_id), None)
        if target is None:
            available = ", ".join(d.id for d in decisions) or "(none)"
            msg = (
                f"decision {decision_id!r} not found. "
                f"Run `anvil prd find-decisions`. Available: {available}"
            )
            if json_output:
                fail(cmd, msg, code="not_found")
            typer.echo(f"Error: {msg}", err=True)
            raise typer.Exit(code=1)

        # Validate the recorded transition BEFORE touching the file, so a bad
        # PRD status or empty input fails without a partial write.
        clock = SystemClock()
        now = clock.now()
        project_id = _get_project_id(backend)
        prd_model = backend.get_prd(effective_prd_id) or result.prd

        try:
            transition_payload = prd_decision_resolved(
                prd_model,
                decision_id=target.id,
                prd_ref=target.prd_ref,
                resolution=resolution,
                resolved_by=resolved_by,
                now=now,
            )
        except TransitionError as exc:
            if json_output:
                fail(cmd, exc.message, code=exc.code)
            typer.echo(f"Error: {exc.message}", err=True)
            raise typer.Exit(code=1) from exc

        # Back-propagate into the markdown (surgical, non-destructive).
        try:
            resolution_result = apply_decision_to_markdown(
                markdown, decision=target, resolution=resolution
            )
        except ResolutionError as exc:
            if json_output:
                fail(cmd, str(exc), code="resolution_failed")
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        payload: dict[str, object] = {
            "project_id": project_id,
            "prd_id": effective_prd_id,
            "decision_id": target.id,
            "decision_kind": target.kind.value,
            "prd_ref": transition_payload["prd_ref"],
            "resolution": transition_payload["resolution"],
            "resolved_by": resolved_by,
            "section": resolution_result.section,
            "before": resolution_result.before,
            "after": resolution_result.after,
        }
        draft = EventDraft(
            timestamp=now,
            actor="anvil-cli",
            action="prd.decision_resolved",
            target_kind="prd",
            target_id=effective_prd_id,
            payload_json=payload,
        )

        def replace_source(expected_sha256: str, replacement: str) -> str:
            if file is None:
                return replace_prd_source_for_id(
                    state_dir,
                    effective_prd_id,
                    expected_sha256=expected_sha256,
                    markdown=replacement,
                ).source_sha256
            return _replace_custom_decision_source(
                prd_path,
                expected_sha256=expected_sha256,
                markdown=replacement,
            )

        def current_source_sha256() -> str:
            current = (
                ingest_prd_source_for_id(state_dir, effective_prd_id)
                if file is None
                else ingest_prd_source(prd_path)
            )
            return current.source_sha256

        def ensure_source(expected_sha256: str, replacement: str) -> None:
            observed = current_source_sha256()
            if observed != expected_sha256:
                replace_source(observed, replacement)
            if current_source_sha256() != expected_sha256:
                raise RuntimeError(
                    "decision source changed during the locked event commit"
                )

        try:
            with backend.claim_operation_lock():
                events_path = state_dir / "events.jsonl"
                log_size_before = events_path.stat().st_size
                updated_sha256 = replace_source(
                    source_sha256, resolution_result.markdown
                )
                try:
                    event = backend.append(draft)
                except BaseException:
                    if _decision_event_was_appended(
                        events_path,
                        previous_size=log_size_before,
                        draft=draft,
                    ):
                        ensure_source(
                            updated_sha256, resolution_result.markdown
                        )
                    else:
                        try:
                            replace_source(updated_sha256, markdown)
                        except Exception as rollback_exc:
                            raise RuntimeError(
                                "decision source rollback failed after event refusal"
                            ) from rollback_exc
                    raise
                ensure_source(updated_sha256, resolution_result.markdown)
        except PrdSourceIngestError as exc:
            if json_output:
                fail(cmd, str(exc), code=exc.code)
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    finally:
        backend.close()

    if json_output:
        emit_success(
            cmd,
            {
                "prd_id": effective_prd_id,
                "prd_source": source_identity,
                "decision_id": target.id,
                "decision_kind": target.kind.value,
                "prd_ref": target.prd_ref,
                "section": resolution_result.section,
                "before": resolution_result.before,
                "after": resolution_result.after,
                "event_id": event.id if event is not None else None,
                "continuation": {
                    "parse_argv": [
                        "anvil",
                        "prd",
                        "parse",
                        *(["--file", str(file)] if file is not None else []),
                        "--prd",
                        effective_prd_id,
                    ]
                },
            },
        )
        return

    typer.echo(
        f"Resolved {target.id} ({target.kind.value}) in PRD "
        f"{effective_prd_id} ({source_identity})."
    )
    typer.echo(f"  section:  {resolution_result.section}")
    typer.echo(f"  before:   {_truncate(resolution_result.before)}")
    typer.echo(f"  after:    {_truncate(resolution_result.after)}")
    if event is not None:
        typer.echo(f"  recorded: {event.id} (prd.decision_resolved)")
    file_arg = " --file <same-file>" if file is not None else ""
    typer.echo(
        "Run `anvil prd parse"
        f"{file_arg} --prd {effective_prd_id}` to refresh state.db."
    )
