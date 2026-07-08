"""``anvil doctor`` — one-shot health diagnosis (backlog T010, F002).

A read-only triage command that answers "is this project's state healthy?"
in a single pass. It surfaces, as a list of severity-tagged FINDINGS:

* **state.db reachability + schema version** — can the database be opened, and
  does the on-disk ``PRAGMA user_version`` match the version this build of the
  engine targets? An un-migratable / unknown schema is the canonical "your
  install needs migration" signal and is reported as an ERROR.
* **config parse status + effective lease/heartbeat** — does
  ``config.yaml`` parse, and what lease/heartbeat values would a claim use?
  A broken config is a WARNING (the CLI falls back to defaults), not a hard
  error.
* **active / stale claim counts** — how many claims are active right now, and
  how many of those have an expired lease (and would be reaped on the next
  mutating command)? A stale claim is an ERROR — work is silently wedged.
* **replay integrity** — rebuild canonical state from ``events.jsonl`` into a
  scratch database and byte-compare it against the live ``state.db`` via the
  same :func:`serialize_state` snapshot the SL-1 replay-equivalence test uses.
  A mismatch means the event log no longer reproduces the projection — an
  ERROR (the audit log is the source of truth).
* **reconciliation drift summary** — the local INTENT/STATE/FS/GIT drift the
  ``drift`` command reports (orphan branches/worktrees/packets, stale claims,
  missing expected files). Surfaced here as a summary; a non-empty drift set is
  a WARNING (a report, not a gate) — except stale claims, which are already
  counted as an ERROR above.

Design contract
---------------
* **Read-only.** doctor never mutates state.db / events.jsonl. Stale-claim
  detection goes through :meth:`ReconciliationEngine.scan` (which does NOT
  reap, unlike the mutating ``_reap_stale_claims`` helper) and replay builds a
  scratch database under a temporary directory.
* **Total but degrade-gracefully.** Every check is wrapped so one failed probe
  (e.g. a corrupt config) produces a finding rather than a traceback, and the
  rest of the report still runs. A :class:`SchemaMismatch` is the one
  structural failure that blocks the backend-dependent checks; those are then
  reported as ``info`` "skipped" findings (not duplicate errors).
* **Exit code is a gate.** doctor exits non-zero when ANY finding is
  ERROR-level (a real health gate, unlike ``drift``'s always-0 report), so CI
  and shell callers see ``$?`` reflect project health.

Honors the v1.24 ``--json`` envelope and the ``ANVIL_ROOT`` /
``--cwd`` resolution precedence shared by every other read command.
"""

from __future__ import annotations

import datetime
import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from anvil.cli._helpers import (
    PRD_OPTION,
    StateRootError,
    _resolve_project_root,
    _resolve_state_dir,
    prd_source_path,
)
from anvil.cli._json import JSON_OPTION, emit_success, fail

if TYPE_CHECKING:
    from anvil.state.sqlite import SqliteBackend

__all__ = ["doctor"]

_COMMAND = "doctor"

# Severity ladder — string tokens reused verbatim in the JSON envelope.
_OK = "ok"
_INFO = "info"
_WARNING = "warning"
_ERROR = "error"

# ClaimManager's own fallback when no config.yaml supplies them (mirrors
# Config.default_lease_minutes / default_heartbeat_minutes defaults).
_DEFAULT_LEASE_MINUTES = 60.0
_DEFAULT_HEARTBEAT_MINUTES = 5.0


class _Finding:
    """One health probe result.

    ``check`` is a short stable token for programmatic branching
    (``"state_db"``, ``"config"``, ``"claims"``, ``"replay"``,
    ``"reconciliation"``); ``severity`` is one of ok/info/warning/error;
    ``detail`` adds structured, check-specific context.
    """

    __slots__ = ("check", "severity", "message", "detail")

    def __init__(
        self,
        check: str,
        severity: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.check = check
        self.severity = severity
        self.message = message
        self.detail = detail or {}

    def to_json(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity,
            "message": self.message,
            "detail": self.detail,
        }


def doctor(
    preflight: bool = typer.Option(  # noqa: B008
        False,
        "--preflight",
        help=(
            "retro-opps T013: GO/NO-GO gate before a long workflow — adds "
            "PRD-parse, unresolved-decision, and (T014) tree-state probes to "
            "the standard health checks, prints a final PREFLIGHT: GO/NO-GO "
            "line, and keeps doctor's exit contract (1 on any ERROR)."
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
    """Diagnose state, config, lease, replay, and reconciliation health.

    Read-only. Emits a list of severity-tagged findings and exits non-zero
    when ANY finding is ERROR-level (e.g. an un-migratable schema, a stale
    claim, or a replay mismatch). With ``--json`` emits the standard
    ``{"ok": true, "command": "doctor", "data": {...}}`` envelope carrying the
    findings, the worst severity, and an overall ``healthy`` flag.
    """
    # Mirror status/drift: a StateRootError under --json must reach stdout as a
    # parseable error envelope, not a raw `Error:` line, so consumers doing
    # json.load(stdout) never choke. Without --json the ClickException
    # propagates unchanged (clean human `Error:` on stderr, exit 1).
    try:
        state_dir = _resolve_state_dir(cwd)
    except StateRootError as exc:
        if json_output:
            fail(_COMMAND, str(exc), code="state_root_invalid")
        raise

    if not state_dir.exists():
        # Not initialized is a hard, unambiguous failure for a health command —
        # there is nothing to diagnose. Use the canonical not_initialized code.
        if json_output:
            fail(
                _COMMAND,
                "anvil not initialized in this project. "
                "Run `anvil init` first.",
                code="not_initialized",
            )
        typer.echo(
            "Error: anvil not initialized in this project. "
            "Run `anvil init` first.",
            err=True,
        )
        raise typer.Exit(code=1)

    findings = _diagnose(state_dir, _resolve_project_root(cwd))
    if preflight:
        # Strictly additive: plain `doctor` (no flag) never runs these, so
        # its output and exit behavior stay byte-compatible.
        findings.extend(_preflight_findings(state_dir, prd))

    worst = _worst_severity(findings)
    healthy = worst != _ERROR

    if json_output:
        data: dict[str, Any] = {
            "healthy": healthy,
            "worst_severity": worst,
            "findings": [f.to_json() for f in findings],
        }
        if preflight:
            data["preflight"] = True
            data["go"] = healthy
        emit_success(_COMMAND, data)
        if not healthy:
            raise typer.Exit(code=1)
        return

    _print_human(findings, healthy=healthy, worst=worst)
    if preflight:
        typer.echo(
            "PREFLIGHT: GO"
            if healthy
            else "PREFLIGHT: NO-GO — fix the ERROR finding(s) above."
        )
    if not healthy:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Preflight probes (retro-opps T013)
# ---------------------------------------------------------------------------


def _preflight_findings(state_dir: Path, prd: str | None) -> list[_Finding]:
    """PRD-parse + unresolved-decision probes for ``doctor --preflight``.

    Total like every doctor probe: each failure becomes a finding, never a
    traceback. The retro pattern this closes: simple PRD format issues
    surfacing deep inside long workflows instead of before they start.
    """
    findings: list[_Finding] = []

    # Resolve which partition to check. The shared sentinel rule: an explicit
    # --prd / $ANVIL_PRD wins; otherwise the default partition. resolve_prd_id
    # needs an open backend; fall back to the raw value on any hiccup so the
    # probe still runs against SOME file rather than dying on resolution.
    prd_id = prd or "default"
    if prd:
        try:
            from anvil.cli._helpers import _open_backend, resolve_prd_id

            backend = _open_backend(state_dir)
            try:
                prd_id = resolve_prd_id(backend, prd)
            finally:
                backend.close()
        except Exception:  # noqa: BLE001 — resolution failure must not kill the probe
            prd_id = prd

    prd_path = prd_source_path(state_dir, prd_id)

    # Probe 1 — the PRD parses cleanly.
    if not prd_path.exists():
        findings.append(
            _Finding(
                "prd_parse",
                _ERROR,
                f"PRD source not found at {prd_path} — author it, then run "
                "`anvil prd parse`.",
                {"path": str(prd_path), "prd_id": prd_id},
            )
        )
        return findings  # nothing further to probe without a file

    try:
        markdown = prd_path.read_text(encoding="utf-8")
    except OSError as exc:
        findings.append(
            _Finding(
                "prd_parse",
                _ERROR,
                f"PRD source unreadable at {prd_path}: {exc}",
                {"path": str(prd_path), "prd_id": prd_id},
            )
        )
        return findings

    from anvil.planning.template import parse_prd

    try:
        parsed = parse_prd(markdown, prd_id=prd_id)
    except Exception as exc:  # noqa: BLE001 — a parser crash IS the finding
        findings.append(
            _Finding(
                "prd_parse",
                _ERROR,
                f"PRD at {prd_path} failed to parse: "
                f"{type(exc).__name__}: {exc}",
                {"path": str(prd_path), "prd_id": prd_id},
            )
        )
        return findings

    if parsed.errors:
        findings.append(
            _Finding(
                "prd_parse",
                _ERROR,
                f"PRD at {prd_path} has {len(parsed.errors)} parse error(s); "
                "fix them before starting a long workflow.",
                {
                    "path": str(prd_path),
                    "prd_id": prd_id,
                    "errors": [
                        f"[{e.section}:{e.line}] {e.message}"
                        for e in parsed.errors
                    ],
                },
            )
        )
    else:
        findings.append(
            _Finding(
                "prd_parse",
                _OK,
                f"PRD at {prd_path} parses cleanly "
                f"({len(parsed.requirements)} requirements, "
                f"{len(parsed.features)} features, {len(parsed.tasks)} tasks).",
                {"path": str(prd_path), "prd_id": prd_id},
            )
        )

    # Probe 2 — unresolved decisions. needs_decision markers and tasks
    # missing acceptance/verification are exactly the deep-workflow failures
    # the retros recorded → ERROR; open questions are informational by the
    # template's own convention → WARNING.
    from anvil.planning.decisions import DecisionKind, find_unresolved_decisions

    try:
        unresolved = find_unresolved_decisions(
            markdown, prd=parsed.prd, tasks=parsed.tasks
        )
    except Exception as exc:  # noqa: BLE001
        findings.append(
            _Finding(
                "prd_decisions",
                _WARNING,
                f"decision scan failed: {type(exc).__name__}: {exc}",
                {"path": str(prd_path)},
            )
        )
        return findings

    blocking = [
        d
        for d in unresolved
        if d.kind in (DecisionKind.needs_decision, DecisionKind.missing_field)
    ]
    open_questions = [
        d for d in unresolved if d.kind is DecisionKind.open_question
    ]
    if blocking:
        findings.append(
            _Finding(
                "prd_decisions",
                _ERROR,
                f"{len(blocking)} unresolved decision item(s) "
                "(needs-decision markers / missing acceptance-verification "
                "fields) — run `anvil prd find-decisions` and resolve before "
                "a long workflow.",
                {"blocking": [d.location for d in blocking]},
            )
        )
    if open_questions:
        findings.append(
            _Finding(
                "prd_decisions",
                _WARNING,
                f"{len(open_questions)} open question(s) in the PRD "
                "(informational).",
                {"open_questions": [d.location for d in open_questions]},
            )
        )
    if not blocking and not open_questions:
        findings.append(
            _Finding(
                "prd_decisions",
                _OK,
                "no unresolved decision items.",
                {},
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _diagnose(state_dir: Path, project_root: Path | None = None) -> list[_Finding]:
    """Run every probe in order and return the finding list.

    ``project_root`` is the git checkout that plan-declared ``likely_files``
    resolve against (distinct from ``state_dir`` under the HOME-workspace
    layout); it flows to the reconciliation probe.

    The schema/state.db probe runs first because its outcome decides whether
    the backend-dependent probes (claims, replay, reconciliation) can run. When
    the backend cannot be opened (SchemaMismatch or a low-level DB error) those
    probes are reported as ``info`` "skipped" findings rather than duplicate
    errors — the single ERROR is the schema/db finding itself.
    """
    findings: list[_Finding] = []

    state_finding, backend = _check_state_db(state_dir)
    findings.append(state_finding)

    findings.append(_check_config(state_dir))

    if backend is None:
        # The backend could not open (schema mismatch / corrupt db). Replay and
        # reconciliation genuinely cannot run — they need the full projection —
        # so they are recorded as skipped (info), NOT as duplicate errors of the
        # already-ERROR state.db finding.
        #
        # The stale-claim check is the exception: the claims table shape
        # (id / task_id / status / lease_expires_at) is stable across every
        # schema version v1-v4, so we can still surface stale claims with a
        # narrow direct read. This is what makes a project with an injected
        # stale claim PLUS a schema mismatch list BOTH findings (T010 AC), not
        # just the schema one.
        findings.append(_check_claims_direct(state_dir))
        for check in ("replay", "reconciliation"):
            findings.append(
                _Finding(
                    check,
                    _INFO,
                    f"{check} check skipped: state.db could not be opened.",
                )
            )
        return findings

    try:
        findings.append(_check_claims(backend))
        findings.append(_check_max_claim_age(backend, state_dir))
        findings.append(_check_replay(backend, state_dir))
        findings.append(_check_reconciliation(backend, state_dir, project_root))
        # Verification-command paths are CHECKOUT-relative, same as likely_files:
        # under the HOME-workspace layout state_dir.parent is the workspace base,
        # not the checkout, so resolve against the threaded project_root.
        findings.append(
            _check_verification_paths(backend, project_root or state_dir.parent)
        )
    finally:
        backend.close()

    return findings


# A path-shaped token: contains a slash and ends in a known source/text
# extension, no glob metacharacters (those are intentionally skipped — a glob
# isn't a concrete path we can resolve).
_VERIFY_PATH_RE = re.compile(
    r"[\w./-]+/[\w./-]*\.(?:py|sh|txt|md|json|ya?ml|toml|cfg|ini)\b"
)

# A `cd <dir>` segment mutates the cwd for everything after it in the same shell.
# Verification commands routinely start `cd bin && uv run pytest …`, so path
# tokens must resolve against the cd'd directory, not the project root.
_CD_RE = re.compile(r"^cd\s+(\S+)$")


def _command_paths(command: str, project_root: Path) -> list[tuple[str, Path]]:
    """Return (path_token, effective_cwd) for each path-shaped token in ``command``.

    Tracks a leading ``cd <dir>`` so a token in ``cd bin && uv run pytest
    ../tests/x.py`` resolves against ``<root>/bin`` (mirroring how the command
    runs), not the project root, while a bin-relative ``tests/x.py`` (missing
    from bin/) is still flagged. ``cd`` persists across ``&&`` within a
    statement; cwd resets to the project root at each ``;`` boundary, matching
    the common multi-statement re-cd pattern ``cd bin && A; cd bin && B`` (each
    statement starts fresh from the repo root). With no ``cd`` the cwd stays the
    project root (unchanged behaviour).

    Best-effort heuristic, not a shell: it does NOT model ``cd`` inside a
    subshell ``(cd bin && ...)``, a quoted/space-containing target, ``cd``
    chained with ``||`` / pipes / env-prefixes, or ``&&``/``;`` embedded in a
    quoted argument; such tokens resolve against the current cwd. The check is
    advisory, so an occasional mis-resolve on those rare shapes is tolerable.
    """
    out: list[tuple[str, Path]] = []
    for statement in command.split(";"):
        cwd = project_root
        for segment in statement.split("&&"):
            seg = segment.strip()
            cd_match = _CD_RE.match(seg)
            if cd_match:
                cwd = cwd / cd_match.group(1)
                continue
            out.extend((token, cwd) for token in _extract_verification_paths(seg))
    return out


def _extract_verification_paths(command: str) -> list[str]:
    """Return concrete path-shaped tokens from a verification command string.

    Only tokens containing a slash are considered (a bare ``foo.py`` may be
    cwd-relative and is not the footgun this check targets). A pytest node id
    (``tests/x.py::test_y``) is matched up to the extension.
    """
    return _VERIFY_PATH_RE.findall(command)


def _check_verification_paths(backend: SqliteBackend, project_root: Path) -> _Finding:
    """Flag ready tasks whose verification command paths don't resolve (B30).

    A command like ``pytest tests/foo.py`` run from a project root where tests
    actually live at ``../tests`` resolves to a non-existent path and would be
    silently skipped by CI — a "passes by hand, never runs in CI" footgun.
    """
    offenders: list[dict[str, str]] = []
    for task in backend.list_tasks(status="ready"):
        for command in task.verification.commands:
            for token, base in _command_paths(command, project_root):
                # Normalise so a `..` token (cd bin && ... ../tests/x.py) collapses
                # lexically — the target resolves even when the cd'd dir is absent
                # on disk (e.g. a decoupled state workspace). base honours cd <dir>.
                candidate = Path(os.path.normpath(base / token))
                # Accept if the file resolves, or its parent dir exists (covers
                # a not-yet-created output file under a real directory). ``base``
                # honours a leading ``cd <dir>`` so ``cd bin && … ../tests/x.py``
                # resolves against bin/, matching how the command runs.
                if candidate.exists() or (
                    candidate.parent != base and candidate.parent.exists()
                ):
                    continue
                offenders.append({"task": task.id, "command": command, "path": token})

    if not offenders:
        return _Finding(
            "verification_paths",
            _OK,
            "All ready-task verification command paths resolve from the project root.",
        )
    summary = "; ".join(f"{o['task']}: '{o['path']}'" for o in offenders[:5])
    more = "" if len(offenders) <= 5 else f" (+{len(offenders) - 5} more)"
    return _Finding(
        "verification_paths",
        _WARNING,
        f"{len(offenders)} verification command path(s) do not resolve from the "
        f"project root and may be silently skipped by CI: {summary}{more}.",
        detail={"offenders": offenders, "project_root": str(project_root)},
    )


# ---------------------------------------------------------------------------
# Probe 1 — state.db reachability + schema version
# ---------------------------------------------------------------------------


def _check_state_db(state_dir: Path) -> tuple[_Finding, SqliteBackend | None]:
    """Report db reachability + schema version; return an open backend or None.

    Reads the TRUE on-disk ``user_version`` BEFORE opening (open() migrates
    v0-v3 up and re-stamps it, which would mask real drift), then attempts the
    normal open. A :class:`SchemaMismatch` (e.g. an unknown user_version=99) is
    an ERROR finding with ``backend=None`` so the caller skips backend-dependent
    probes. A migrated-on-open db (db_version < code_version) is healthy but
    reported with an ``info`` note so the user knows a migration ran.
    """
    from anvil.cli._helpers import _open_backend
    from anvil.state.backend import SchemaMismatch
    from anvil.state.schema import get_schema_version
    from anvil.state.sqlite import read_db_schema_version

    code_version = get_schema_version()
    db_path = state_dir / "state.db"

    if not db_path.exists():
        return (
            _Finding(
                "state_db",
                _ERROR,
                f"state.db not found at {db_path}.",
                {"db_path": str(db_path), "schema_version": code_version},
            ),
            None,
        )

    try:
        db_version = read_db_schema_version(str(db_path))
    except Exception as exc:  # noqa: BLE001 — any read failure is unreachable db
        return (
            _Finding(
                "state_db",
                _ERROR,
                f"state.db is unreachable or unreadable: {exc}",
                {"db_path": str(db_path)},
            ),
            None,
        )

    try:
        backend = _open_backend(state_dir)
    except SchemaMismatch as exc:
        return (
            _Finding(
                "state_db",
                _ERROR,
                f"schema mismatch: {exc}",
                {
                    "db_path": str(db_path),
                    "db_schema_version": db_version,
                    "code_schema_version": code_version,
                },
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001 — corrupt db, disk error, etc.
        return (
            _Finding(
                "state_db",
                _ERROR,
                f"state.db could not be opened: {exc}",
                {"db_path": str(db_path)},
            ),
            None,
        )

    detail = {
        "db_path": str(db_path),
        "db_schema_version": db_version,
        "code_schema_version": code_version,
    }
    if db_version != code_version:
        # open() just migrated v0-v3 → code_version; report it but it is not a
        # failure (the db is now at the right version).
        return (
            _Finding(
                "state_db",
                _INFO,
                f"state.db reachable; schema migrated v{db_version} → "
                f"v{code_version} on open.",
                detail,
            ),
            backend,
        )
    return (
        _Finding(
            "state_db",
            _OK,
            f"state.db reachable; schema version {code_version}.",
            detail,
        ),
        backend,
    )


# ---------------------------------------------------------------------------
# Probe 2 — config parse status + effective lease/heartbeat
# ---------------------------------------------------------------------------


def _check_config(state_dir: Path) -> _Finding:
    """Report config parse status and the effective lease/heartbeat values.

    Uses the shared soft-load (``_load_config_optional``): a missing or broken
    config never blocks a command — the CLI falls back to ClaimManager's
    60/5-minute defaults. doctor mirrors that: a missing config is ``info``, a
    broken one is a ``warning`` (the CLI will keep working on defaults), and a
    parsed config is ``ok``.
    """
    config_path = state_dir / "config.yaml"
    if not config_path.exists():
        return _Finding(
            "config",
            _INFO,
            "config.yaml not found; using built-in defaults.",
            {
                "config_path": str(config_path),
                "effective_lease_minutes": _DEFAULT_LEASE_MINUTES,
                "effective_heartbeat_minutes": _DEFAULT_HEARTBEAT_MINUTES,
            },
        )

    # _load_config_optional emits a stderr warning on a bad config and returns
    # None; we re-check parseability quietly here so the finding's severity is
    # accurate without depending on that side-channel warning.
    parsed = _try_load_config(config_path)
    if parsed is None:
        return _Finding(
            "config",
            _WARNING,
            "config.yaml failed to parse; commands will fall back to "
            "built-in defaults. Fix config.yaml and re-run.",
            {
                "config_path": str(config_path),
                "effective_lease_minutes": _DEFAULT_LEASE_MINUTES,
                "effective_heartbeat_minutes": _DEFAULT_HEARTBEAT_MINUTES,
            },
        )

    return _Finding(
        "config",
        _OK,
        "config.yaml parsed.",
        {
            "config_path": str(config_path),
            "effective_lease_minutes": parsed.default_lease_minutes,
            "effective_heartbeat_minutes": parsed.default_heartbeat_minutes,
        },
    )


def _try_load_config(config_path: Path) -> Any:
    """Return a parsed Config or None — quiet variant of the soft-load.

    ``_load_config_optional`` is the canonical soft-load but prints a stderr
    warning on failure, which would violate doctor's "single JSON line" --json
    contract. We reuse ``load_config`` directly and swallow the same error set
    so the parse VERDICT drives the finding instead of a stray stderr line.
    """
    import yaml

    try:
        from anvil.config import load_config

        return load_config(config_path)
    except (FileNotFoundError, OSError, ValueError, yaml.YAMLError):
        return None


# ---------------------------------------------------------------------------
# Probe 3 — active / stale claim counts
# ---------------------------------------------------------------------------


def _check_claims(backend: SqliteBackend) -> _Finding:
    """Count active claims and how many have an expired (stale) lease.

    Read-only: it reads ``list_active_claims`` and compares each lease against
    the current clock WITHOUT reaping (doctor must not mutate). A stale claim
    is an ERROR — work is silently wedged until a mutating command reaps it.
    """
    from anvil.clock import SystemClock

    now = SystemClock().now()
    active = backend.list_active_claims()
    stale = [c for c in active if c.lease_expires_at < now]

    detail = {
        "active": len(active),
        "stale": len(stale),
        "stale_claim_ids": [c.id for c in stale],
    }
    if stale:
        return _Finding(
            "claims",
            _ERROR,
            f"{len(stale)} stale claim(s) with expired leases "
            f"(of {len(active)} active). Release them with "
            "`anvil release <task> --force`.",
            detail,
        )
    return _Finding(
        "claims",
        _OK,
        f"{len(active)} active claim(s), none stale.",
        detail,
    )


def _check_max_claim_age(
    backend: SqliteBackend,
    state_dir: Path,
    now: datetime.datetime | None = None,
) -> _Finding:
    """Surface active claims older than the configured max-claim-age (B46).

    Read-only. An over-age claim will have its next ``renew()`` refused, after
    which its lease expires and the reaper takes it — so it is bounded, not
    silently wedged (hence WARNING, not ERROR). Surfacing it lets an operator
    release it sooner instead of waiting out the lease.

    ``now`` is injectable for deterministic tests; it defaults to the real clock.
    """
    from anvil.cli._helpers import _load_config_optional
    from anvil.clock import SystemClock

    if now is None:
        now = SystemClock().now()
    cfg = _load_config_optional(state_dir)
    lease = cfg.default_lease_minutes if cfg is not None else 60.0
    multiplier = cfg.max_claim_age_multiplier if cfg is not None else 4.0
    max_age = lease * multiplier

    active = backend.list_active_claims()
    over_age = [
        {
            "claim_id": c.id,
            "task_id": c.task_id,
            "age_minutes": round((now - c.created_at).total_seconds() / 60.0, 1),
            "max_allowed_minutes": max_age,
        }
        for c in active
        if (now - c.created_at).total_seconds() / 60.0 >= max_age
    ]
    detail = {
        "max_claim_age_minutes": max_age,
        "over_age": over_age,
        "total_over_age": len(over_age),
    }
    if over_age:
        return _Finding(
            "max_claim_age",
            _WARNING,
            f"{len(over_age)} active claim(s) past max-claim-age ({max_age:g} min); "
            "renewal is refused for these. Release with `anvil release <task> "
            "--force` to free the task (and its conflict group) now.",
            detail,
        )
    return _Finding(
        "max_claim_age",
        _OK,
        f"No active claim is past max-claim-age ({max_age:g} min).",
        detail,
    )


def _check_claims_direct(state_dir: Path) -> _Finding:
    """Stale-claim probe that bypasses the schema-version gate.

    Used only when ``_open_backend`` failed (schema mismatch / un-migratable
    db) — the normal :func:`_check_claims` cannot run because the backend
    refuses to open. The ``claims`` table columns this reads
    (``id``, ``task_id``, ``status``, ``lease_expires_at``) are present and
    unchanged in every schema version v1-v4, so a narrow read-only SELECT still
    surfaces stale claims. Any failure (table truly absent, db unreadable)
    degrades to an ``info`` finding rather than a traceback.
    """
    import sqlite3
    from datetime import datetime

    from anvil.clock import SystemClock

    db_path = state_dir / "state.db"
    now = SystemClock().now()
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT id, task_id, lease_expires_at FROM claims "
                "WHERE status = 'active'"
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — degrade to info, never crash
        return _Finding(
            "claims",
            _INFO,
            f"claims check could not read state.db directly: {exc}",
            {},
        )

    active = len(rows)
    stale_ids: list[str] = []
    for claim_id, _task_id, lease_raw in rows:
        try:
            lease = datetime.fromisoformat(str(lease_raw))
        except (TypeError, ValueError):
            continue
        if lease < now:
            stale_ids.append(str(claim_id))

    detail = {"active": active, "stale": len(stale_ids), "stale_claim_ids": stale_ids}
    if stale_ids:
        return _Finding(
            "claims",
            _ERROR,
            f"{len(stale_ids)} stale claim(s) with expired leases "
            f"(of {active} active). Release them with "
            "`anvil release <task> --force`.",
            detail,
        )
    return _Finding(
        "claims",
        _OK,
        f"{active} active claim(s), none stale.",
        detail,
    )


# ---------------------------------------------------------------------------
# Probe 4 — replay integrity
# ---------------------------------------------------------------------------


def _check_replay(backend: SqliteBackend, state_dir: Path) -> _Finding:
    """Rebuild state from events.jsonl and byte-compare against the live db.

    Reuses ``replay_from_empty`` (the same engine ``anvil replay`` uses)
    into a SCRATCH database under a temp dir, then compares the live and
    replayed projections via :func:`serialize_state` — the exact snapshot the
    SL-1 replay-equivalence test compares. A mismatch means the event log no
    longer reproduces the projection (an ERROR: the log is the source of truth).
    """
    import json

    from anvil.clock import SystemClock
    from anvil.config import read_events_storage
    from anvil.state.snapshot import serialize_state
    from anvil.state.sqlite import SqliteBackend as _SqliteBackend

    events_path = state_dir / "events.jsonl"
    if not events_path.exists():
        return _Finding(
            "replay",
            _WARNING,
            "events.jsonl not found; cannot verify replay integrity.",
            {"events_path": str(events_path)},
        )

    live_snapshot = serialize_state(backend)

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            scratch_db = str(Path(tmpdir) / "scratch.db")
            scratch_events = str(Path(tmpdir) / "scratch_events.jsonl")
            scratch = _SqliteBackend(
                db_path=scratch_db,
                events_path=scratch_events,
                clock=SystemClock(),
                events_storage=read_events_storage(state_dir / "config.yaml"),
            )
            scratch.initialize()
            try:
                scratch.replay_from_empty(str(events_path))
                replayed_snapshot = serialize_state(scratch)
            finally:
                scratch.close()
    except Exception as exc:  # noqa: BLE001 — corruption surfaces as a finding
        return _Finding(
            "replay",
            _ERROR,
            f"replay from events.jsonl failed: {exc}",
            {"events_path": str(events_path)},
        )

    live_json = json.dumps(live_snapshot, sort_keys=True)
    replayed_json = json.dumps(replayed_snapshot, sort_keys=True)
    if live_json != replayed_json:
        return _Finding(
            "replay",
            _ERROR,
            "replay integrity FAILED: rebuilding state from events.jsonl does "
            "not reproduce the live state.db. The event log and projection "
            "have diverged.",
            {"events_path": str(events_path)},
        )
    return _Finding(
        "replay",
        _OK,
        "replay integrity verified: events.jsonl reproduces the live state.db.",
        {"events_path": str(events_path)},
    )


# ---------------------------------------------------------------------------
# Probe 5 — reconciliation drift summary
# ---------------------------------------------------------------------------


def _check_reconciliation(
    backend: SqliteBackend, state_dir: Path, project_root: Path | None = None
) -> _Finding:
    """Summarize local git/fs/db reconciliation drift (the ``drift`` view).

    Reuses ``ReconciliationEngine.scan()`` with no providers and filters to
    :data:`LOCAL_DRIFT_KINDS` — exactly what ``anvil drift`` reports.
    Drift here is a WARNING (a report, not a gate); stale claims are already
    surfaced as an ERROR by the claims probe, so they are NOT re-escalated.
    """
    from anvil.clock import SystemClock
    from anvil.sync.reconciliation import (
        LOCAL_DRIFT_KINDS,
        ReconciliationEngine,
    )

    engine = ReconciliationEngine(
        backend,
        state_dir=state_dir,
        clock=SystemClock(),
        configured_providers=[],
        project_root=project_root,
    )
    report = engine.scan()
    local = [d for d in report.discrepancies if d.kind in LOCAL_DRIFT_KINDS]

    by_kind: dict[str, int] = {}
    for d in local:
        by_kind[str(d.kind)] = by_kind.get(str(d.kind), 0) + 1

    detail = {"total": len(local), "by_kind": by_kind}
    if local:
        return _Finding(
            "reconciliation",
            _WARNING,
            f"{len(local)} reconciliation drift item(s) "
            "(run `anvil drift` for detail).",
            detail,
        )
    return _Finding(
        "reconciliation",
        _OK,
        "no reconciliation drift between intent, state, and filesystem/git.",
        detail,
    )


# ---------------------------------------------------------------------------
# Severity aggregation + human rendering
# ---------------------------------------------------------------------------

_SEVERITY_RANK = {_OK: 0, _INFO: 1, _WARNING: 2, _ERROR: 3}


def _worst_severity(findings: list[_Finding]) -> str:
    """Return the highest-ranked severity across all findings (ok if empty)."""
    worst = _OK
    for f in findings:
        if _SEVERITY_RANK.get(f.severity, 0) > _SEVERITY_RANK.get(worst, 0):
            worst = f.severity
    return worst


def _print_human(findings: list[_Finding], *, healthy: bool, worst: str) -> None:
    """Render a readable per-check report plus a one-line verdict."""
    typer.echo("anvil doctor")
    typer.echo("")
    for f in findings:
        typer.echo(f"  [{f.severity.upper()}] {f.check}: {f.message}")
    typer.echo("")
    if healthy:
        typer.echo("Overall: healthy (no ERROR-level findings).")
    else:
        typer.echo(
            f"Overall: UNHEALTHY (worst severity: {worst}). "
            "Fix the ERROR finding(s) above."
        )
