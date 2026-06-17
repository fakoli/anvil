"""Reconciliation engine for fakoli-state (Phase 8, Task 5).

Cross-checks the three sources of truth that ``fakoli-state`` keeps in
loose coordination:

1. **SQLite** state (``tasks``, ``claims``, ``sync_mappings`` …).
2. **Filesystem** packets under ``.fakoli-state/packets/`` plus the
   project's git branches and worktrees.
3. **External sync targets** (GitHub Issues today; pluggable in future).

Each check produces a :class:`Discrepancy`; the collection is rolled up
into a :class:`ReconciliationReport`. :meth:`ReconciliationEngine.fix`
applies suggested remediations and returns a list of
:class:`FixAction` describing what was done. ``dry_run=True`` returns
the actions without executing.

Safety
------
``fix()`` is intentionally a no-guard executor: every CLI surface that
calls it MUST gate the call on ``--yes``. The two layers are split so
unit tests can exercise the remediation without re-implementing the
CLI safety prompt.
"""

from __future__ import annotations

import datetime
import re
import shutil
import subprocess
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from fakoli_state.clock import Clock
    from fakoli_state.state.backend import Backend


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# git operations are subprocess wrappers — keep them bounded so a hung
# git binary cannot freeze reconciliation.
_GIT_TIMEOUT_SECONDS = 10

# Default drift threshold for ``drift_sync_state`` (7 days, per spec).
_DEFAULT_DRIFT_THRESHOLD_DAYS = 7

# Regex matching ``agent/<task_id>-...`` branches created by ``fakoli-state claim``.
# Branch names look like ``agent/t001-add-feature`` or
# ``agent/t001-add-feature-2`` (collision suffix). Capture group 1 is the
# uppercased task id ("T001").
_AGENT_BRANCH_RE = re.compile(r"^agent/(t\d+)(?:-.*)?$")

# Name of the per-project state directory that lives at the project root.
_STATE_DIR_NAME = ".fakoli-state"


def _derive_project_root(state_dir: Path) -> Path:
    """Return the PROJECT ROOT given the engine's ``state_dir``.

    The engine's ``state_dir`` is overloaded by its two caller conventions:

    * The CLI (``drift`` / ``sync``) passes the resolved ``.fakoli-state/``
      directory itself (``_resolve_state_dir`` returns ``<root>/.fakoli-state``).
    * The unit-test / library callers pass the project root directly.

    Filesystem checks that are PROJECT-ROOT-relative (``likely_files``) or that
    need ``<root>/.fakoli-state/packets`` (orphan packets) must work under both
    conventions. Strip a trailing ``.fakoli-state`` segment when present so both
    callers resolve to the same project root.
    """
    if state_dir.name == _STATE_DIR_NAME:
        return state_dir.parent
    return state_dir


def _resolve_expected_file(rel: str, project_root: Path) -> Path:
    """Resolve a plan-declared ``likely_files`` entry against *project_root*.

    Normalisation rules (MUST-FIX 1 — ``str.lstrip("./")`` corrupted
    dot-rooted paths because it strips the CHARACTER SET ``{'.', '/'}``, so
    ``.env`` -> ``env`` and ``.github/workflows/ci.yml`` -> ``github/...``):

    * Strip only a single leading ``"./"`` PREFIX (``removeprefix``), never
      leading dots — dotfiles (``.env``, ``.gitignore``) and dot-dirs
      (``.github/workflows/ci.yml``) keep their leading dot.
    * Absolute paths are honoured as-is.
    * Relative paths resolve under ``project_root``. ``..`` segments are NOT
      mangled, but the result is clamped so a planner path cannot escape the
      project root (defence in depth — ``likely_files`` should never contain
      ``..``, but a report that silently checks ``/etc/passwd`` would be worse
      than one that reports an in-tree miss).
    """
    cleaned = rel.removeprefix("./")
    candidate = Path(cleaned)
    if candidate.is_absolute():
        return candidate
    resolved = (project_root / candidate).resolve()
    root_resolved = project_root.resolve()
    # Clamp ``..`` escapes: if the resolved path is not within the project
    # root, pin it back under the root using only the path's own parts (drop
    # any leading ``..`` / anchor) so the check stays in-tree and never probes
    # arbitrary filesystem locations.
    if resolved != root_resolved and root_resolved not in resolved.parents:
        safe_parts = [p for p in candidate.parts if p not in ("..", "/", "\\")]
        return root_resolved.joinpath(*safe_parts) if safe_parts else root_resolved
    return resolved


# ---------------------------------------------------------------------------
# Public models — DiscrepancyKind / Severity / Discrepancy / Report / FixAction
# ---------------------------------------------------------------------------


class DiscrepancyKind(StrEnum):
    """Categorical kind for each :class:`Discrepancy`."""

    orphan_branch = "orphan_branch"
    orphan_packet = "orphan_packet"
    orphan_worktree = "orphan_worktree"
    stale_claim = "stale_claim"
    missing_expected_file = "missing_expected_file"
    missing_sync_mapping = "missing_sync_mapping"
    drift_sync_state = "drift_sync_state"


# Kinds that depend ONLY on local sources (SQLite state, filesystem, git) and
# therefore require no configured external sync provider. The read-only
# ``fakoli-state drift`` view is built from exactly these — see
# ``cli/drift.py``. ``missing_sync_mapping`` and ``drift_sync_state`` are
# excluded because they only fire when a provider is configured and describe
# external-target divergence, not local intent/state/fs drift.
LOCAL_DRIFT_KINDS: frozenset[DiscrepancyKind] = frozenset({
    DiscrepancyKind.orphan_branch,
    DiscrepancyKind.orphan_packet,
    DiscrepancyKind.orphan_worktree,
    DiscrepancyKind.stale_claim,
    DiscrepancyKind.missing_expected_file,
})


class Severity(StrEnum):
    """Severity ladder for discrepancies. Drives CLI rendering, not behaviour."""

    info = "info"
    warning = "warning"
    error = "error"


class Discrepancy(BaseModel):
    """One detected inconsistency between SQLite / filesystem / git / external.

    Attributes
    ----------
    kind:
        Categorical :class:`DiscrepancyKind`.
    severity:
        :class:`Severity` — drives the CLI's coloring + exit code, never
        used to suppress detection.
    target_id:
        Identifier of the offending entity (task id, branch name, packet
        filename, worktree path, …). Free-form per ``target_kind``.
    target_kind:
        One of ``"task"``, ``"claim"``, ``"branch"``, ``"packet"``,
        ``"worktree"``, ``"sync_mapping"``.
    description:
        Human-readable explanation of what is wrong. Surfaced verbatim
        in CLI output and audit events.
    suggested_fix:
        A shell command or CLI invocation that would remediate. Treated
        as advice for humans AND as the actual executable string used by
        :meth:`ReconciliationEngine.fix` (parsed into argv via the
        per-kind handler).
    payload:
        Free-form bag of extra detail for the fix handler / CLI
        renderer. Per-kind contracts documented inline below.
    """

    model_config = ConfigDict(extra="forbid")

    kind: DiscrepancyKind
    severity: Severity
    target_id: str
    target_kind: str
    description: str
    suggested_fix: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ReconciliationReport(BaseModel):
    """Output of :meth:`ReconciliationEngine.scan`.

    Attributes
    ----------
    scanned_at:
        UTC timestamp captured at the START of the scan.
    discrepancies:
        Every detected :class:`Discrepancy`, in deterministic order
        (kind ASC, then target_id ASC).
    summary:
        Map of ``{DiscrepancyKind: count}`` for at-a-glance rendering.
        Counts derived from :attr:`discrepancies` — invariant enforced
        by :meth:`ReconciliationReport.validate_summary` below.
    """

    model_config = ConfigDict(extra="forbid")

    scanned_at: datetime.datetime
    discrepancies: list[Discrepancy]
    summary: dict[str, int] = Field(default_factory=dict)

    def validate_summary(self) -> None:
        """Assert ``summary`` matches the histogram of ``discrepancies``.

        Used by callers (notably tests) that want to verify the summary
        is internally consistent without doing the count themselves.
        """
        expected: dict[str, int] = {}
        for d in self.discrepancies:
            expected[str(d.kind)] = expected.get(str(d.kind), 0) + 1
        if self.summary != expected:
            raise ValueError(
                f"ReconciliationReport.summary {self.summary!r} does not match "
                f"discrepancy histogram {expected!r}."
            )


class FixAction(BaseModel):
    """Result of executing one suggested fix.

    Attributes
    ----------
    kind:
        The :class:`DiscrepancyKind` this action remediated.
    target_id:
        Echo of the discrepancy's ``target_id`` for traceability.
    command:
        The actual shell-ish command that was (or would be) executed.
        For state-backend mutations (``stale_claim``) this is a
        ``fakoli-state ...`` invocation string for audit clarity even
        though the engine reaches into the backend directly.
    result:
        ``"applied"`` (run, succeeded), ``"skipped"`` (dry-run or no-op),
        or ``"failed"`` (exception bubbled).
    error:
        Failure detail; ``None`` on success or skip.
    """

    model_config = ConfigDict(extra="forbid")

    kind: DiscrepancyKind
    target_id: str
    command: str
    result: str
    error: str | None = None


# ---------------------------------------------------------------------------
# ReconciliationEngine
# ---------------------------------------------------------------------------


class ReconciliationEngine:
    """Scan + remediate fakoli-state drift across SQLite / filesystem / git.

    Construction
    ------------
    backend:
        Any object satisfying :class:`fakoli_state.state.backend.Backend`.
        The engine never opens a new connection — callers manage lifecycle.
    state_dir:
        The directory containing ``packets/`` and the git working tree
        used for branch / worktree detection. Typically the project root.
        Note: it does NOT need to be the same directory as ``state.db`` —
        the backend already knows where its files live.
    clock:
        :class:`fakoli_state.clock.Clock` for stale-claim detection +
        report timestamps. Defaults to :class:`SystemClock`.
    drift_threshold_days:
        Threshold for ``drift_sync_state``: a SyncMapping whose
        ``last_synced_at`` is older than this many days surfaces as a
        warning. Default 7 days.
    configured_providers:
        Iterable of provider ids (``"github_issues"``, …) configured for
        this project. Used by ``missing_sync_mapping``: a ``done`` task
        with no SyncMapping is only flagged when at least one provider
        is configured. Empty by default — calling code resolves config.
    """

    def __init__(
        self,
        backend: Backend,
        *,
        state_dir: Path,
        clock: Clock | None = None,
        drift_threshold_days: int = _DEFAULT_DRIFT_THRESHOLD_DAYS,
        configured_providers: list[str] | None = None,
    ) -> None:
        self._backend = backend
        self._state_dir = state_dir
        if clock is None:
            # Local import keeps the module load light.
            from fakoli_state.clock import SystemClock

            clock = SystemClock()
        self._clock = clock
        self._drift_threshold = datetime.timedelta(days=drift_threshold_days)
        self._configured_providers = list(configured_providers or [])

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self) -> ReconciliationReport:
        """Run every check and return a :class:`ReconciliationReport`."""
        scanned_at = self._clock.now()
        discrepancies: list[Discrepancy] = []
        discrepancies.extend(self._scan_orphan_branches())
        discrepancies.extend(self._scan_orphan_packets())
        discrepancies.extend(self._scan_orphan_worktrees())
        discrepancies.extend(self._scan_stale_claims())
        discrepancies.extend(self._scan_missing_expected_files())
        discrepancies.extend(self._scan_missing_sync_mappings())
        discrepancies.extend(self._scan_drift_sync_state())

        # Deterministic order: by kind ASC, then target_id ASC. Makes
        # report-equality tests + CLI rendering stable.
        discrepancies.sort(key=lambda d: (str(d.kind), d.target_id))

        summary: dict[str, int] = {}
        for d in discrepancies:
            summary[str(d.kind)] = summary.get(str(d.kind), 0) + 1

        return ReconciliationReport(
            scanned_at=scanned_at,
            discrepancies=discrepancies,
            summary=summary,
        )

    def fix(
        self,
        report: ReconciliationReport,
        *,
        dry_run: bool = False,
    ) -> list[FixAction]:
        """Execute the suggested fix for each discrepancy in ``report``.

        ``dry_run=True`` returns the :class:`FixAction` list without
        executing anything (every action has ``result='skipped'``).

        Failures in one action do NOT abort the loop — every discrepancy
        gets an action entry with ``result='failed'`` and the exception
        message in ``error``. This is the "best-effort" wrapping loop
        the critic flagged on PR #47: do NOT let one bad branch break
        the rest of the reconciliation pass.
        """
        actions: list[FixAction] = []
        for d in report.discrepancies:
            command = d.suggested_fix
            if dry_run:
                actions.append(FixAction(
                    kind=d.kind,
                    target_id=d.target_id,
                    command=command,
                    result="skipped",
                ))
                continue
            try:
                self._apply_fix(d)
                actions.append(FixAction(
                    kind=d.kind,
                    target_id=d.target_id,
                    command=command,
                    result="applied",
                ))
            except Exception as exc:  # noqa: BLE001 — best-effort loop
                actions.append(FixAction(
                    kind=d.kind,
                    target_id=d.target_id,
                    command=command,
                    result="failed",
                    error=str(exc),
                ))
        return actions

    # ------------------------------------------------------------------
    # Check 1 — orphan_branch
    # ------------------------------------------------------------------

    def _scan_orphan_branches(self) -> list[Discrepancy]:
        """``agent/t*-*`` branches whose task id is not in the SQLite store."""
        if not _is_git_repo(self._state_dir):
            return []
        branches = _git_list_branches(self._state_dir)
        known_task_ids = {t.id.lower() for t in self._backend.list_tasks()}
        out: list[Discrepancy] = []
        for branch in branches:
            m = _AGENT_BRANCH_RE.match(branch)
            if m is None:
                continue
            task_id = m.group(1).lower()
            if task_id in known_task_ids:
                continue
            out.append(Discrepancy(
                kind=DiscrepancyKind.orphan_branch,
                severity=Severity.warning,
                target_id=branch,
                target_kind="branch",
                description=(
                    f"Branch {branch!r} references task '{task_id.upper()}' "
                    "which no longer exists in the state store."
                ),
                suggested_fix=f"git branch -D {branch}",
                payload={"task_id": task_id.upper(), "branch": branch},
            ))
        return out

    # ------------------------------------------------------------------
    # Check 2 — orphan_packet
    # ------------------------------------------------------------------

    def _scan_orphan_packets(self) -> list[Discrepancy]:
        """Packet files under ``.fakoli-state/packets/`` for missing tasks.

        Packet naming convention is ``<TASK_ID>.md`` (e.g. ``T001.md``);
        anything that isn't a ``.md`` file is ignored.

        MUST-FIX 2: ``state_dir`` is overloaded — the CLI (``drift`` / ``sync``)
        passes the ``.fakoli-state/`` directory itself, while library/test
        callers pass the project root. The old hardcoded
        ``self._state_dir / ".fakoli-state" / "packets"`` only resolved
        correctly for the latter, so ``orphan_packet`` NEVER fired through the
        CLI (it looked at ``<root>/.fakoli-state/.fakoli-state/packets``).
        Derive the project root first so packets resolve to
        ``<root>/.fakoli-state/packets`` under both conventions.
        """
        project_root = _derive_project_root(self._state_dir)
        packets_dir = project_root / _STATE_DIR_NAME / "packets"
        if not packets_dir.exists():
            return []
        known_task_ids = {t.id for t in self._backend.list_tasks()}
        out: list[Discrepancy] = []
        for entry in sorted(packets_dir.iterdir()):
            if not entry.is_file() or entry.suffix != ".md":
                continue
            task_id = entry.stem
            if task_id in known_task_ids:
                continue
            out.append(Discrepancy(
                kind=DiscrepancyKind.orphan_packet,
                severity=Severity.info,
                target_id=str(entry),
                target_kind="packet",
                description=(
                    f"Packet file {entry.name!r} references task "
                    f"'{task_id}' which is not in the state store."
                ),
                suggested_fix=f"rm {entry}",
                payload={"task_id": task_id, "path": str(entry)},
            ))
        return out

    # ------------------------------------------------------------------
    # Check 3 — orphan_worktree
    # ------------------------------------------------------------------

    def _scan_orphan_worktrees(self) -> list[Discrepancy]:
        """Worktrees pointing at ``agent/t*-*`` branches whose task is gone."""
        if not _is_git_repo(self._state_dir):
            return []
        worktrees = _git_list_worktrees(self._state_dir)
        known_task_ids = {t.id.lower() for t in self._backend.list_tasks()}
        active_claims_by_task = {
            c.task_id.lower() for c in self._backend.list_active_claims()
        }
        out: list[Discrepancy] = []
        for wt in worktrees:
            branch = wt.get("branch")
            if branch is None:
                continue
            m = _AGENT_BRANCH_RE.match(branch)
            if m is None:
                continue
            task_id = m.group(1).lower()
            # Orphan = task gone OR no active claim AND task not present.
            task_known = task_id in known_task_ids
            claim_active = task_id in active_claims_by_task
            if task_known and claim_active:
                continue
            # If the task is known but the claim is gone we still flag —
            # the worktree was created for an active claim and the claim
            # has been released; the directory is now leftover state.
            wt_path = wt["path"]
            out.append(Discrepancy(
                kind=DiscrepancyKind.orphan_worktree,
                severity=Severity.warning,
                target_id=wt_path,
                target_kind="worktree",
                description=(
                    f"Worktree {wt_path!r} (branch {branch!r}) references "
                    f"task '{task_id.upper()}'; "
                    + (
                        "task no longer exists."
                        if not task_known
                        else "no active claim holds this worktree."
                    )
                ),
                suggested_fix=f"git worktree remove --force {wt_path}",
                payload={
                    "task_id": task_id.upper(),
                    "branch": branch,
                    "path": wt_path,
                },
            ))
        return out

    # ------------------------------------------------------------------
    # Check 4 — stale_claim
    # ------------------------------------------------------------------

    def _scan_stale_claims(self) -> list[Discrepancy]:
        """Active claims whose ``lease_expires_at`` is in the past."""
        now = self._clock.now()
        out: list[Discrepancy] = []
        for claim in self._backend.list_active_claims():
            if claim.lease_expires_at >= now:
                continue
            out.append(Discrepancy(
                kind=DiscrepancyKind.stale_claim,
                severity=Severity.error,
                target_id=claim.id,
                target_kind="claim",
                description=(
                    f"Claim '{claim.id}' on task '{claim.task_id}' has "
                    f"status='active' but lease expired at "
                    f"{claim.lease_expires_at.isoformat()} "
                    f"(now={now.isoformat()})."
                ),
                suggested_fix=(
                    f'fakoli-state release {claim.task_id} --force '
                    f'--reason "stale lease"'
                ),
                payload={
                    "claim_id": claim.id,
                    "task_id": claim.task_id,
                    "lease_expires_at": claim.lease_expires_at.isoformat(),
                },
            ))
        return out

    # ------------------------------------------------------------------
    # Check 5 — missing_expected_file
    # ------------------------------------------------------------------

    def _scan_missing_expected_files(self) -> list[Discrepancy]:
        """Terminal tasks whose declared ``likely_files`` are absent on disk.

        This is the INTENT-vs-FILESYSTEM check — "is the code still what the
        spec/plan said?". A task that the planner expected to touch a set of
        files (``Task.likely_files``) and that has reached a terminal status
        (``done`` / ``accepted``) but whose files do not exist on disk has
        drifted: the work was marked complete but the artefact the plan
        promised is missing (deleted, moved, or never written).

        Only terminal tasks are scanned: a ``ready`` or ``in_progress`` task
        whose files don't exist yet is the *normal* mid-flight state, not
        drift. Paths resolve relative to ``state_dir`` (the project root) and
        absolute paths are honoured as-is. The check is read-only — no
        :meth:`fix` handler exists for it (it is intentionally NOT in
        ``_apply_fix``), because "the spec says a file should exist but it
        doesn't" has no safe automatic remediation: only a human can decide
        whether to rewrite the file, amend the plan, or reopen the task.
        """
        _terminal = {"done", "accepted"}
        # ``likely_files`` are PROJECT-ROOT-relative (e.g. ``src/widget.py``),
        # not ``.fakoli-state/``-relative. The CLI passes ``state_dir`` as the
        # ``.fakoli-state/`` directory while the test/engine callers pass the
        # project root directly, so derive the root under both conventions.
        project_root = _derive_project_root(self._state_dir)
        out: list[Discrepancy] = []
        for task in self._backend.list_tasks():
            if str(task.status) not in _terminal:
                continue
            for rel in task.likely_files:
                # Normalise WITHOUT corrupting dotfiles: strip only a leading
                # ``./`` prefix, never leading dots (``.env`` stays ``.env``),
                # and resolve safely under the project root (no ``..`` escape).
                candidate = _resolve_expected_file(rel, project_root)
                if candidate.exists():
                    continue
                out.append(Discrepancy(
                    kind=DiscrepancyKind.missing_expected_file,
                    severity=Severity.warning,
                    target_id=f"{task.id}:{rel}",
                    target_kind="task",
                    description=(
                        f"Task '{task.id}' is status={task.status} but its "
                        f"expected file {rel!r} does not exist on disk "
                        f"(looked at {candidate}). The plan promised this "
                        "artefact; the code no longer matches the intent."
                    ),
                    suggested_fix=(
                        f"fakoli-state show {task.id}  "
                        "# verify the plan, restore the file, or reopen the task"
                    ),
                    payload={
                        "task_id": task.id,
                        "expected_file": rel,
                        "resolved_path": str(candidate),
                        "status": str(task.status),
                    },
                ))
        return out

    # ------------------------------------------------------------------
    # Check 6 — missing_sync_mapping
    # ------------------------------------------------------------------

    def _scan_missing_sync_mappings(self) -> list[Discrepancy]:
        """Done tasks without a SyncMapping for EACH configured provider.

        P2-2 fix: when a project configures multiple providers
        (``github_issues`` AND ``linear``) we must emit a discrepancy
        per provider that lacks a mapping for the task. The old code
        called ``get_sync_mapping(task.id)`` once (no ``external_system``
        kwarg) which returned the alphabetical-first mapping — so a task
        mapped to ``github_issues`` but missing from ``linear`` was
        treated as "fully mapped" and the ``linear`` gap was never
        flagged.

        Each discrepancy carries ``payload['missing_provider']`` so the
        operator can see exactly which provider is unmapped, and the
        suggested-fix points at that specific provider id.
        """
        if not self._configured_providers:
            return []
        out: list[Discrepancy] = []
        for task in self._backend.list_tasks(status="done"):
            for provider_id in self._configured_providers:
                # Scoped lookup: pass ``external_system=`` so we get THIS
                # provider's mapping (or None), not the ASC-first.
                mapping = self._backend.get_sync_mapping(
                    task.id, external_system=provider_id,
                )
                if mapping is not None:
                    continue
                out.append(Discrepancy(
                    kind=DiscrepancyKind.missing_sync_mapping,
                    severity=Severity.warning,
                    target_id=task.id,
                    target_kind="task",
                    description=(
                        f"Task '{task.id}' is status=done but has no "
                        f"SyncMapping for provider {provider_id!r}; "
                        "configured providers: "
                        f"{', '.join(self._configured_providers)}."
                    ),
                    suggested_fix=(
                        f"fakoli-state sync provider {provider_id} "
                        f"--push --task {task.id}"
                    ),
                    payload={
                        "task_id": task.id,
                        "missing_provider": provider_id,
                        "configured_providers": list(self._configured_providers),
                    },
                ))
        return out

    # ------------------------------------------------------------------
    # Check 7 — drift_sync_state
    # ------------------------------------------------------------------

    def _scan_drift_sync_state(self) -> list[Discrepancy]:
        """SyncMappings in conflict, externally deleted, or whose
        last_synced_at is too old.

        SF-5: ``external_deleted`` is surfaced as its own discrepancy
        reason so reconciliation can list tombstoned mappings. Before
        this fix, the tombstone path left the mapping at ``in_sync``
        and the drift scan was blind to the fact that the remote was
        gone — operators had to grep stderr to discover dangling
        references.
        """
        now = self._clock.now()
        out: list[Discrepancy] = []
        for mapping in self._backend.list_sync_mappings():
            state_str = str(mapping.sync_state)
            in_conflict = state_str == "conflict"
            externally_deleted = state_str == "external_deleted"
            stale = (now - mapping.last_synced_at) > self._drift_threshold
            if not in_conflict and not externally_deleted and not stale:
                continue
            if in_conflict:
                reason = "conflict"
                fix = (
                    f"fakoli-state sync provider {mapping.external_system} "
                    f"--pull --task {mapping.task_id}"
                )
                description = (
                    f"SyncMapping for task '{mapping.task_id}' "
                    f"({mapping.external_system}) is in conflict; "
                    "resolve via pull or edit the manual-merge file."
                )
            elif externally_deleted:
                reason = "external_deleted"
                # The remote is gone; the operator must decide whether
                # to delete the local task or unlink the mapping. The
                # suggested fix points at the latter because deleting
                # the task is a heavier mutation that we don't want to
                # auto-suggest.
                fix = (
                    f"fakoli-state sync provider {mapping.external_system} "
                    f"--task {mapping.task_id} "
                    "# remote deleted: unlink mapping or recreate remote"
                )
                description = (
                    f"SyncMapping for task '{mapping.task_id}' "
                    f"({mapping.external_system}) references external_id "
                    f"'{mapping.external_id}' which no longer exists on the "
                    "remote (tombstoned)."
                )
            else:
                reason = "stale"
                fix = (
                    f"fakoli-state sync provider {mapping.external_system} "
                    f"--pull --task {mapping.task_id}"
                )
                description = (
                    f"SyncMapping for task '{mapping.task_id}' "
                    f"({mapping.external_system}) has not synced since "
                    f"{mapping.last_synced_at.isoformat()} "
                    f"(threshold: {self._drift_threshold.days}d)."
                )
            out.append(Discrepancy(
                kind=DiscrepancyKind.drift_sync_state,
                severity=Severity.warning,
                target_id=mapping.task_id,
                target_kind="sync_mapping",
                description=description,
                suggested_fix=fix,
                payload={
                    "task_id": mapping.task_id,
                    "external_system": str(mapping.external_system),
                    "sync_state": str(mapping.sync_state),
                    "last_synced_at": mapping.last_synced_at.isoformat(),
                    "reason": reason,
                },
            ))
        return out

    # ------------------------------------------------------------------
    # Fix dispatch
    # ------------------------------------------------------------------

    def _apply_fix(self, d: Discrepancy) -> None:
        """Execute one discrepancy's remediation.

        Branches by :class:`DiscrepancyKind`. State mutations go through
        the backend directly (race-free, audit-recorded); shell-outs go
        via subprocess with a timeout. Some kinds — namely
        ``missing_sync_mapping`` and ``drift_sync_state`` — require a
        real provider push/pull and are intentionally NOT auto-fixed
        here; the CLI surface in Wave 3 owns that flow because the
        provider client / network access lives there.
        """
        kind = d.kind
        if kind == DiscrepancyKind.orphan_branch:
            self._fix_orphan_branch(d)
        elif kind == DiscrepancyKind.orphan_packet:
            self._fix_orphan_packet(d)
        elif kind == DiscrepancyKind.orphan_worktree:
            self._fix_orphan_worktree(d)
        elif kind == DiscrepancyKind.stale_claim:
            self._fix_stale_claim(d)
        elif kind == DiscrepancyKind.missing_expected_file:
            # No safe automatic remediation: "the plan promised a file that
            # is not on disk" requires a human decision (rewrite the file,
            # amend the plan, or reopen the task). Surface the discrepancy
            # via scan() / `fakoli-state drift`; do not auto-fix.
            raise NotImplementedError(
                f"reconciliation cannot auto-fix {kind.value!r}; "
                f"inspect the task: {d.suggested_fix}"
            )
        elif kind in (
            DiscrepancyKind.missing_sync_mapping,
            DiscrepancyKind.drift_sync_state,
        ):
            # Auto-fix requires a real provider push/pull (network access,
            # provider credentials, conflict resolution) which lives on the
            # CLI sync surface, not in the reconciliation engine. Point the
            # operator at the suggested command rather than the wave number.
            raise NotImplementedError(
                f"reconciliation cannot auto-fix {kind.value!r}; "
                f"run the suggested command: {d.suggested_fix}"
            )
        else:  # pragma: no cover — defensive, StrEnum exhaustive above
            raise ValueError(f"Unknown DiscrepancyKind: {kind!r}")

    def _fix_orphan_branch(self, d: Discrepancy) -> None:
        branch = d.payload.get("branch") or d.target_id
        _git_run(
            ["git", "branch", "-D", branch],
            cwd=self._state_dir,
        )

    def _fix_orphan_packet(self, d: Discrepancy) -> None:
        path = Path(d.payload.get("path") or d.target_id)
        if path.exists():
            path.unlink()

    def _fix_orphan_worktree(self, d: Discrepancy) -> None:
        wt_path = d.payload.get("path") or d.target_id
        _git_run(
            ["git", "worktree", "remove", "--force", wt_path],
            cwd=self._state_dir,
        )

    def _fix_stale_claim(self, d: Discrepancy) -> None:
        """Force-release the stale claim via the existing event handler.

        This emits a ``claim.released`` event with ``force=True`` so the
        backend's idempotent path handles the case where the claim was
        already terminal between scan and fix.
        """
        from fakoli_state.state.models import EventDraft

        claim_id = d.payload.get("claim_id") or d.target_id
        draft = EventDraft(
            timestamp=self._clock.now(),
            actor="reconciliation",
            action="claim.released",
            target_kind="claim",
            target_id=claim_id,
            payload_json={
                "claim_id": claim_id,
                "released_by": "reconciliation",
                "release_reason": "stale lease",
                "force": True,
            },
        )
        # append() may return None for an idempotent no-op (already-released
        # claim) — treat as success; the claim state is already correct.
        self._backend.append(draft)


# ---------------------------------------------------------------------------
# git helpers (private)
# ---------------------------------------------------------------------------


def _is_git_repo(cwd: Path) -> bool:
    """True if *cwd* is inside a git repository.

    Mirrors :func:`fakoli_state.git_ops.branch.is_git_repo` but is duplicated
    here so the reconciliation module does not pull a dependency on the
    claim-flow helpers. Stderr is suppressed; timeouts and missing-git
    both return False.
    """
    if shutil.which("git") is None:
        return False
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(cwd),
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0


def _git_list_branches(cwd: Path) -> list[str]:
    """Return all local branch names under *cwd*.

    Empty list on any git failure — reconciliation is best-effort.
    """
    try:
        r = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if r.returncode != 0:
        return []
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def _git_list_worktrees(cwd: Path) -> list[dict[str, str]]:
    """Parse ``git worktree list --porcelain`` into a list of dicts.

    Each dict has keys ``path``, ``branch`` (without the ``refs/heads/``
    prefix), and possibly ``HEAD``. Returns the main worktree too — the
    caller filters by ``agent/`` branch pattern, so the main worktree
    (typically on ``main``) is harmlessly ignored.
    """
    try:
        r = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if r.returncode != 0:
        return []
    out: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in r.stdout.splitlines():
        line = line.rstrip()
        if not line:
            if current:
                out.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current["path"] = line[len("worktree "):]
        elif line.startswith("HEAD "):
            current["HEAD"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            ref = line[len("branch "):]
            # Strip refs/heads/ prefix.
            if ref.startswith("refs/heads/"):
                ref = ref[len("refs/heads/"):]
            current["branch"] = ref
        elif line == "detached":
            current["detached"] = "true"
    if current:
        out.append(current)
    return out


def _git_run(argv: list[str], *, cwd: Path) -> None:
    """Run a git command; raise RuntimeError on non-zero or timeout."""
    try:
        r = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"git command timed out after {_GIT_TIMEOUT_SECONDS}s: {' '.join(argv)}"
        ) from exc
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "unknown git error").strip()
        raise RuntimeError(f"git failed ({' '.join(argv)}): {msg}")


__all__ = [
    "LOCAL_DRIFT_KINDS",
    "DiscrepancyKind",
    "Severity",
    "Discrepancy",
    "ReconciliationReport",
    "FixAction",
    "ReconciliationEngine",
]
