"""Git-backed events Phase A tests (v1.22.0).

Covers the four pillars of docs/specs/2026-06-10-git-backed-events.md Phase A:

1. **Hash-chained ids** — git-mode appends produce
   ``"E-" + sha256(parent ‖ canonical_json(payload) ‖ actor ‖ ts)[:12]`` ids,
   chain through ``parent_event_id``, and carry a monotonically increasing
   ``lamport``; local mode keeps ``E{N:06d}`` and the pre-1.22.0 line bytes.
2. **Order-tolerant replay** — dedupe by event id (a line duplicated by a
   ``merge=union`` union applies once), order by ``(lamport, ts, event_id)``,
   torn trailing line tolerated exactly like the strict local replay.
3. **Divergent-merge simulation** — two logs sharing a common prefix with
   independent suffixes, concatenated in BOTH orders (as merge=union would),
   replay to byte-identical state; two competing ``claim.created`` events on
   one task surface deterministically (earliest ``(lamport, ts, id)`` wins
   the task transition — ``claim.superseded`` materialization is Phase B).
4. **Migration round-trip** — ``migrate-events --to git`` rewrites the
   committed replay fixture preserving order; replaying the migrated log
   reproduces the pre-migration state modulo ids (via id_mapping.json).

All scratch state lives under tmp_path. FrozenClock keeps every path
deterministic; where ordering must be observable, drafts carry explicit
distinct timestamps instead.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from itertools import permutations
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from anvil.cli import app
from anvil.clock import FrozenClock
from anvil.state.backend import EventRejected, TransactionAborted
from anvil.state.hashing import canonical_payload_json, hash_event_id
from anvil.state.models import Event, EventDraft
from anvil.state.snapshot import serialize_state
from anvil.state.sqlite import SqliteBackend

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 5, 24, 18, 0, 0, tzinfo=UTC)
_HASH_ID_RE = re.compile(r"^E-[0-9a-f]{12}$")
_FIXTURE_EVENTS = (
    Path(__file__).parent / "fixtures" / "replay" / "sample-project" / "events.jsonl"
)

runner = CliRunner()


def _make_backend(
    state_dir: Path,
    *,
    storage: str = "git",
    clock: FrozenClock | None = None,
) -> SqliteBackend:
    """A fresh, initialized backend rooted under *state_dir*."""
    if clock is None:
        clock = FrozenClock(_T0)
    events_path = state_dir / "events.jsonl"
    events_path.touch()
    b = SqliteBackend(
        db_path=str(state_dir / "state.db"),
        events_path=str(events_path),
        clock=clock,
        events_storage=storage,
    )
    b.initialize()
    return b


def _draft(
    action: str,
    payload: dict[str, Any],
    *,
    target_kind: str = "project",
    target_id: str = "proj-1",
    ts: datetime = _T0,
    actor: str = "test",
) -> EventDraft:
    return EventDraft(
        timestamp=ts,
        actor=actor,
        action=action,
        target_kind=target_kind,
        target_id=target_id,
        payload_json=payload,
    )


def _task_payload(task_id: str = "T001") -> dict[str, Any]:
    return {
        "id": task_id,
        "feature_id": "F001",
        "title": f"Task {task_id}",
        "description": "desc",
        "status": "proposed",
        "priority": "medium",
        "dependencies": [],
        "conflict_groups": [],
        "scores": {},
        "acceptance_criteria": [],
        "implementation_notes": [],
        "verification": {},
        "likely_files": [],
        "parent_task_id": None,
        "created_at": _T0.isoformat(),
        "updated_at": _T0.isoformat(),
    }


def _prd_parsed_payload(
    *,
    prd_id: str = "default",
    title: str = "Original PRD",
    expected_absent: bool | None = True,
    requirements: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "project_id": "proj-1",
        "prd_id": prd_id,
        "title": title,
        "is_default": prd_id == "default",
        "status": "draft",
        "summary": "Git replay PRD.",
        "goals": [],
        "non_goals": [],
        "requirements": requirements or [],
        "acceptance_criteria": [],
        "risks": [],
        "open_questions": [],
        "assumptions": [],
    }
    if expected_absent is not None:
        payload["expected_absent"] = expected_absent
    return payload


def _prd_revised_payload(
    *,
    prd_id: str = "default",
    revision: int = 2,
    title: str = "Renamed PRD",
    expected_status: str | None = "draft",
    superseded: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "project_id": "proj-1",
        "prd_id": prd_id,
        "revision": revision,
        "is_default": prd_id == "default",
        "title": title,
        "status": "draft",
        "summary": "Git replay PRD.",
        "goals": [],
        "non_goals": [],
        "acceptance_criteria": [],
        "risks": [],
        "open_questions": [],
        "assumptions": [],
        "requirements_added": [],
        "requirements_superseded": superseded or [],
        "requirements_unchanged": [],
    }
    if expected_status is not None:
        payload["expected_status"] = expected_status
    return payload


def _seed_prd(b: SqliteBackend, *, prd_id: str = "default") -> None:
    b.append(
        _draft(
            "project.created",
            {
                "id": "proj-1",
                "name": "Git PRD",
                "description": "",
                "created_at": _T0.isoformat(),
                "updated_at": _T0.isoformat(),
            },
        )
    )
    b.append(_draft("state.initialized", {}))
    b.append(
        _draft(
            "prd.parsed",
            _prd_parsed_payload(prd_id=prd_id),
            target_kind="prd",
            target_id=prd_id,
        )
    )


def _handcrafted_git_event(
    *,
    event_id: str,
    parent_event_id: str,
    lamport: int,
    action: str,
    payload: dict[str, Any],
    prd_id: str = "default",
    timestamp_offset: int = 1,
) -> str:
    return json.dumps({
        "timestamp": (_T0 + timedelta(seconds=timestamp_offset)).isoformat(),
        "actor": "git-branch",
        "action": action,
        "target_kind": "prd",
        "target_id": prd_id,
        "payload_json": payload,
        "id": event_id,
        "parent_event_id": parent_event_id,
        "lamport": lamport,
    })


def _claim_payload(
    claim_id: str,
    *,
    task_id: str = "T001",
    claimed_by: str = "agent-a",
    ts: datetime = _T0,
) -> dict[str, Any]:
    return {
        "id": claim_id,
        "task_id": task_id,
        "claimed_by": claimed_by,
        "claim_type": "task",
        "status": "active",
        "branch": None,
        "worktree_path": None,
        "expected_files": [],
        "created_at": ts.isoformat(),
        "lease_expires_at": (ts + timedelta(hours=1)).isoformat(),
        "last_heartbeat_at": ts.isoformat(),
        "released_at": None,
        "release_reason": None,
    }


def _seed_ready_task(b: SqliteBackend) -> None:
    """Project + feature + T001 promoted proposed→drafted→reviewed→ready.

    Seven events total — the shared history every divergence test forks from.
    """
    b.append(
        _draft(
            "project.created",
            {
                "id": "proj-1",
                "name": "Git Events",
                "description": "",
                "created_at": _T0.isoformat(),
                "updated_at": _T0.isoformat(),
            },
        )
    )
    b.append(_draft("state.initialized", {}))
    b.append(
        _draft(
            "feature.created",
            {
                "id": "F001",
                "title": "Feature F001",
                "description": "the feature",
                "status": "proposed",
                "requirements": [],
                "tasks": [],
            },
            target_kind="feature",
            target_id="F001",
        )
    )
    b.append(
        _draft("task.created", _task_payload(), target_kind="task", target_id="T001")
    )
    for from_status, to_status in (
        ("proposed", "drafted"),
        ("drafted", "reviewed"),
        ("reviewed", "ready"),
    ):
        b.append(
            _draft(
                "task.status_changed",
                {"task_id": "T001", "from": from_status, "to": to_status},
                target_kind="task",
                target_id="T001",
            )
        )


def _log_lines(state_dir: Path) -> list[dict[str, Any]]:
    """Parse every line of the dir's events.jsonl."""
    out: list[dict[str, Any]] = []
    with (state_dir / "events.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                out.append(json.loads(line))
    return out


def _append_external_initialized(events_path: Path, event_id: str) -> None:
    """Append one valid Git event as a non-cooperating external writer."""
    lines = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    tail = lines[-1]
    event = {
        "timestamp": (_T0 + timedelta(seconds=100)).isoformat(),
        "actor": "external-writer",
        "action": "state.initialized",
        "target_kind": "project",
        "target_id": "proj-1",
        "payload_json": {},
        "id": event_id,
        "parent_event_id": tail["id"],
        "lamport": max(int(line.get("lamport") or 0) for line in lines) + 1,
    }
    with events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, separators=(",", ":")) + "\n")


def _events_table(state_dir: Path) -> list[tuple[str, int | None]]:
    """Return (id, seq) rows from the projection, ordered by seq."""
    conn = sqlite3.connect(str(state_dir / "state.db"))
    try:
        return list(
            conn.execute("SELECT id, seq FROM events ORDER BY seq ASC").fetchall()
        )
    finally:
        conn.close()


def _snap(b: SqliteBackend) -> str:
    return json.dumps(serialize_state(b), sort_keys=True)


# ---------------------------------------------------------------------------
# 1. Hash id generation + chain linkage
# ---------------------------------------------------------------------------


class TestHashChainedIds:
    def test_ids_are_hash_format_and_chained(self, tmp_path: Path) -> None:
        """Every git-mode id matches E-<12 hex>; parents form a linear chain."""
        b = _make_backend(tmp_path)
        try:
            _seed_ready_task(b)
        finally:
            b.close()

        lines = _log_lines(tmp_path)
        assert len(lines) == 7
        for line in lines:
            assert _HASH_ID_RE.fullmatch(line["id"]), line["id"]
        # Chain root has an explicit null parent; every other event links to
        # its file predecessor. (strict=False: pairwise over offset slices is
        # intentionally one element short.)
        assert lines[0]["parent_event_id"] is None
        for prev, cur in zip(lines, lines[1:], strict=False):
            assert cur["parent_event_id"] == prev["id"]

    def test_lamport_increments_from_one(self, tmp_path: Path) -> None:
        b = _make_backend(tmp_path)
        try:
            _seed_ready_task(b)
        finally:
            b.close()
        assert [line["lamport"] for line in _log_lines(tmp_path)] == list(range(1, 8))

    def test_ids_match_the_spec_formula(self, tmp_path: Path) -> None:
        """The writer's ids are recomputable from the spec inputs.

        Locks the hash material to the full event identity and payload — if
        the writer ever drifts from state/hashing.hash_event_id, already-
        committed logs would stop being verifiable.
        """
        b = _make_backend(tmp_path)
        try:
            _seed_ready_task(b)
        finally:
            b.close()

        parent: str | None = None
        for line in _log_lines(tmp_path):
            event = Event.model_validate(line)
            expected = hash_event_id(
                parent_event_id=parent,
                action=event.action,
                target_kind=event.target_kind,
                target_id=event.target_id,
                payload=event.payload_json,
                actor=event.actor,
                ts=event.timestamp.isoformat(),
            )
            assert event.id == expected
            parent = event.id

    def test_frozen_clock_identical_drafts_get_distinct_ids(
        self, tmp_path: Path
    ) -> None:
        """Same payload/actor/ts twice → distinct ids, because the parent differs.

        This is the chain property doing real work: without the parent in the
        hash input, FrozenClock (tests) or rapid agents (production) would
        collide successive ids.
        """
        identical_payload = {
            "task_id": "T001",
            "actor": "test",
            "notes": "same note",
            "noted_at": _T0.isoformat(),
        }
        b = _make_backend(tmp_path)
        try:
            _seed_ready_task(b)
            e1 = b.append(
                _draft(
                    "progress.noted",
                    identical_payload,
                    target_kind="task",
                    target_id="T001",
                )
            )
            e2 = b.append(
                _draft(
                    "progress.noted",
                    identical_payload,
                    target_kind="task",
                    target_id="T001",
                )
            )
        finally:
            b.close()
        assert e1 is not None and e2 is not None
        assert e1.id != e2.id
        assert e2.parent_event_id == e1.id

    def test_canonical_json_is_key_order_independent(self) -> None:
        assert canonical_payload_json({"b": 1, "a": 2}) == canonical_payload_json(
            {"a": 2, "b": 1}
        )

    def test_same_payload_different_action_gets_distinct_ids_on_replay(
        self, tmp_path: Path
    ) -> None:
        """Dedup must not collapse distinct branch events that share a payload.

        Two branches can append different audit-only actions from the same
        parent with the same actor, timestamp, and payload. The event id must
        include the action/target identity; otherwise git replay dedupes by id
        and silently drops one fact.
        """
        project_event = {
            "timestamp": _T0.isoformat(),
            "actor": "test",
            "action": "project.created",
            "target_kind": "project",
            "target_id": "proj-1",
            "payload_json": {
                "id": "proj-1",
                "name": "Hash Identity",
                "description": "",
                "created_at": _T0.isoformat(),
                "updated_at": _T0.isoformat(),
            },
            "parent_event_id": None,
            "lamport": 1,
        }
        project_event["id"] = hash_event_id(
            parent_event_id=None,
            action=project_event["action"],
            target_kind=project_event["target_kind"],
            target_id=project_event["target_id"],
            payload=project_event["payload_json"],
            actor=project_event["actor"],
            ts=project_event["timestamp"],
        )

        suffixes = []
        for action, target_kind, target_id in (
            ("state.initialized", "project", "proj-1"),
            ("file_changed", "file", "README.md"),
        ):
            line = {
                "timestamp": _T0.isoformat(),
                "actor": "test",
                "action": action,
                "target_kind": target_kind,
                "target_id": target_id,
                "payload_json": {},
                "parent_event_id": project_event["id"],
                "lamport": 2,
            }
            line["id"] = hash_event_id(
                parent_event_id=project_event["id"],
                action=action,
                target_kind=target_kind,
                target_id=target_id,
                payload={},
                actor="test",
                ts=_T0.isoformat(),
            )
            suffixes.append(line)

        assert suffixes[0]["id"] != suffixes[1]["id"]

        (tmp_path / "events.jsonl").write_text(
            "".join(
                json.dumps(line) + "\n" for line in [project_event, *suffixes]
            ),
            encoding="utf-8",
        )

        b = _make_backend(tmp_path)
        try:
            assert len(_events_table(tmp_path)) == 3
        finally:
            b.close()

    def test_live_append_assigns_display_seq(self, tmp_path: Path) -> None:
        """Git-mode appends number the projection's seq column 1..N."""
        b = _make_backend(tmp_path)
        try:
            _seed_ready_task(b)
        finally:
            b.close()
        rows = _events_table(tmp_path)
        assert [seq for _id, seq in rows] == list(range(1, 8))


class TestLocalModeUntouched:
    def test_local_lines_keep_pre_1_22_shape(self, tmp_path: Path) -> None:
        """Local mode emits neither parent_event_id nor lamport keys.

        The replay byte-equality guarantee covers the log line bytes — a new
        always-null key would churn every fixture and golden downstream.
        """
        b = _make_backend(tmp_path, storage="local")
        try:
            _seed_ready_task(b)
        finally:
            b.close()
        lines = _log_lines(tmp_path)
        assert [line["id"] for line in lines][:2] == ["E000001", "E000002"]
        for line in lines:
            assert set(line.keys()) == {
                "timestamp",
                "actor",
                "action",
                "target_kind",
                "target_id",
                "payload_json",
                "id",
            }

    def test_local_mode_leaves_seq_null(self, tmp_path: Path) -> None:
        """Local mode derives order from the monotonic id; seq stays NULL."""
        b = _make_backend(tmp_path, storage="local")
        try:
            _seed_ready_task(b)
        finally:
            b.close()
        conn = sqlite3.connect(str(tmp_path / "state.db"))
        try:
            rows = conn.execute("SELECT seq FROM events").fetchall()
        finally:
            conn.close()
        assert rows and all(row[0] is None for row in rows)


# ---------------------------------------------------------------------------
# 2. Order-tolerant replay: dedupe, ordering, torn lines
# ---------------------------------------------------------------------------


class TestGitReplayDedupe:
    def test_union_duplicated_lines_apply_once(self, tmp_path: Path) -> None:
        """Duplicating interior + trailing lines (as merge=union can) is a no-op."""
        src = tmp_path / "src"
        src.mkdir()
        b = _make_backend(src)
        try:
            _seed_ready_task(b)
            clean_state = _snap(b)
        finally:
            b.close()

        lines = (src / "events.jsonl").read_text(encoding="utf-8").splitlines()
        duplicated = (
            lines[:3] + [lines[2]] + lines[3:] + [lines[-1]]
        )  # dup line 3 (interior) and the last line
        dup_dir = tmp_path / "dup"
        dup_dir.mkdir()
        (dup_dir / "events.jsonl").write_text(
            "".join(line + "\n" for line in duplicated), encoding="utf-8"
        )

        b2 = _make_backend(dup_dir)  # initialize() converges via git replay
        try:
            assert _snap(b2) == clean_state
        finally:
            b2.close()
        # The projection holds each event exactly once, seq still 1..7.
        rows = _events_table(dup_dir)
        assert len(rows) == 7
        assert [seq for _id, seq in rows] == list(range(1, 8))

    @pytest.mark.parametrize("reverse", [False, True])
    def test_conflicting_duplicate_id_fails_closed_in_every_file_order(
        self, tmp_path: Path, reverse: bool
    ) -> None:
        """One id with unequal normalized event material is corruption.

        Trusting the first physical occurrence would select a different title
        when merge=union line order flips. Replay must instead reject both
        permutations with the same deterministic error.
        """
        base = tmp_path / f"duplicate-conflict-base-{reverse}"
        base.mkdir()
        backend = _make_backend(base)
        try:
            _seed_prd(backend)
        finally:
            backend.close()
        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parent = json.loads(prefix[-1])["id"]
        first = _handcrafted_git_event(
            event_id="E-aaaaaaaaaaaa",
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=_prd_revised_payload(title="Duplicate A"),
        )
        second = _handcrafted_git_event(
            event_id="E-aaaaaaaaaaaa",
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=_prd_revised_payload(title="Duplicate B"),
        )
        suffix = [second, first] if reverse else [first, second]
        merged = tmp_path / f"duplicate-conflict-merged-{reverse}"
        merged.mkdir()
        (merged / "events.jsonl").write_text(
            "\n".join(prefix + suffix) + "\n", encoding="utf-8"
        )

        with pytest.raises(
            ValueError,
            match="git replay: conflicting duplicate event id 'E-aaaaaaaaaaaa'",
        ):
            _make_backend(merged)

        bounded_dir = tmp_path / f"duplicate-conflict-bounded-{reverse}"
        bounded_dir.mkdir()
        bounded = _make_backend(bounded_dir)
        try:
            with pytest.raises(
                ValueError,
                match=(
                    "git bounded replay: conflicting duplicate event id "
                    "'E-aaaaaaaaaaaa'"
                ),
            ):
                bounded.replay_to_event_id(
                    str(merged / "events.jsonl"), "E-aaaaaaaaaaaa"
                )
        finally:
            bounded.close()

    @pytest.mark.parametrize("reverse", [False, True])
    def test_conflicting_duplicate_id_fails_closed_with_converged_projection(
        self, tmp_path: Path, reverse: bool
    ) -> None:
        """Set-equal SQLite/log ids must not bypass duplicate-material checks."""
        state_dir = tmp_path / f"duplicate-converged-{reverse}"
        state_dir.mkdir()
        backend = _make_backend(state_dir)
        try:
            _seed_prd(backend)
        finally:
            backend.close()

        events_path = state_dir / "events.jsonl"
        lines = events_path.read_text(encoding="utf-8").splitlines()
        parsed_index = next(
            index
            for index, line in enumerate(lines)
            if json.loads(line)["action"] == "prd.parsed"
        )
        conflicting = json.loads(lines[parsed_index])
        conflicting["payload_json"]["title"] = "Conflicting duplicate"
        conflicting_line = json.dumps(conflicting, separators=(",", ":"))
        if reverse:
            merged = (
                lines[:parsed_index]
                + [conflicting_line, lines[parsed_index]]
                + lines[parsed_index + 1 :]
            )
        else:
            merged = lines + [conflicting_line]
        events_path.write_text("\n".join(merged) + "\n", encoding="utf-8")

        # The duplicate does not change the event-id set, so initialize takes
        # the converged-projection path rather than rebuilding state.db.
        with pytest.raises(
            ValueError,
            match=(
                "git append parent scan: conflicting duplicate event id "
                f"{conflicting['id']!r}"
            ),
        ):
            _make_backend(state_dir)

    @pytest.mark.parametrize("reverse", [False, True])
    def test_identical_duplicate_remains_idempotent_with_converged_projection(
        self, tmp_path: Path, reverse: bool
    ) -> None:
        state_dir = tmp_path / f"duplicate-identical-converged-{reverse}"
        state_dir.mkdir()
        backend = _make_backend(state_dir)
        try:
            _seed_prd(backend)
            expected = _snap(backend)
        finally:
            backend.close()

        events_path = state_dir / "events.jsonl"
        lines = events_path.read_text(encoding="utf-8").splitlines()
        duplicate_index = next(
            index
            for index, line in enumerate(lines)
            if json.loads(line)["action"] == "prd.parsed"
        )
        if reverse:
            merged = (
                lines[:duplicate_index]
                + [lines[duplicate_index], lines[duplicate_index]]
                + lines[duplicate_index + 1 :]
            )
        else:
            merged = lines + [lines[duplicate_index]]
        events_path.write_text("\n".join(merged) + "\n", encoding="utf-8")

        reopened = _make_backend(state_dir)
        try:
            assert _snap(reopened) == expected
        finally:
            reopened.close()

    def test_append_catch_up_rejects_conflicting_duplicate_id(
        self, tmp_path: Path
    ) -> None:
        """An open writer must fail closed when external log drift forces a scan."""
        backend = _make_backend(tmp_path)
        try:
            _seed_prd(backend)
            expected = _snap(backend)
            events_path = tmp_path / "events.jsonl"
            lines = events_path.read_text(encoding="utf-8").splitlines()
            parsed = next(
                json.loads(line)
                for line in lines
                if json.loads(line)["action"] == "prd.parsed"
            )
            parsed["payload_json"]["title"] = "Conflicting duplicate"
            parsed["lamport"] = backend._max_lamport + 1  # noqa: SLF001
            lines.append(json.dumps(parsed, separators=(",", ":")))
            events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with pytest.raises(
                ValueError,
                match=(
                    "git append parent scan: conflicting duplicate event id "
                    f"{parsed['id']!r}"
                ),
            ):
                backend.append(
                    _draft(
                        "prd.revised",
                        _prd_revised_payload(title="Live revision"),
                        target_kind="prd",
                        target_id="default",
                    )
                )
            assert _snap(backend) == expected
        finally:
            backend.close()

    def test_non_prd_append_rejects_external_conflicting_duplicate_id(
        self, tmp_path: Path
    ) -> None:
        """Every Git mutation validates external drift, not only PRD drafts."""
        backend = _make_backend(tmp_path)
        try:
            _seed_ready_task(backend)
            expected = _snap(backend)
            events_path = tmp_path / "events.jsonl"
            lines = events_path.read_text(encoding="utf-8").splitlines()
            project = json.loads(lines[0])
            project["payload_json"]["name"] = "Conflicting project"
            project["lamport"] = backend._max_lamport + 1  # noqa: SLF001
            lines.append(json.dumps(project, separators=(",", ":")))
            events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            claim_id = "C-CONFLICT-GUARD"
            with pytest.raises(
                ValueError,
                match=(
                    "git append parent scan: conflicting duplicate event id "
                    f"{project['id']!r}"
                ),
            ):
                backend.append(
                    _draft(
                        "claim.created",
                        _claim_payload(claim_id),
                        target_kind="claim",
                        target_id=claim_id,
                    )
                )
            assert _snap(backend) == expected
            assert len(events_path.read_text(encoding="utf-8").splitlines()) == (
                len(lines)
            )
        finally:
            backend.close()

    @pytest.mark.parametrize("rewrite_kind", ["payload", "parent"])
    def test_reopen_rejects_single_same_id_material_rewrite(
        self, tmp_path: Path, rewrite_kind: str
    ) -> None:
        """Set equality cannot bless a sole rewritten occurrence on reopen."""
        backend = _make_backend(tmp_path)
        try:
            _seed_prd(backend)
        finally:
            backend.close()

        events_path = tmp_path / "events.jsonl"
        lines = events_path.read_text(encoding="utf-8").splitlines()
        rewritten = json.loads(lines[0])
        if rewrite_kind == "payload":
            rewritten["payload_json"]["name"] = "Rewritten project"
        else:
            rewritten["parent_event_id"] = "E-deadbeefdead"
        lines[0] = json.dumps(rewritten, separators=(",", ":"))
        events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with pytest.raises(
            TransactionAborted,
            match=(
                "git projection convergence: event .* has different material "
                "than the projected Git envelope"
            ),
        ):
            _make_backend(tmp_path)

    def test_non_prd_append_rejects_single_same_id_material_rewrite(
        self, tmp_path: Path
    ) -> None:
        """A long-lived writer must not bless a sole rewritten log line."""
        backend = _make_backend(tmp_path)
        try:
            _seed_ready_task(backend)
            events_path = tmp_path / "events.jsonl"
            lines = events_path.read_text(encoding="utf-8").splitlines()
            rewritten = json.loads(lines[0])
            rewritten["payload_json"]["name"] = "Rewritten project"
            lines[0] = json.dumps(rewritten, separators=(",", ":"))
            events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            claim_id = "C-REWRITE-GUARD"
            with pytest.raises(
                TransactionAborted,
                match=(
                    "git append integrity: event .* has different material "
                    "than the projected Git envelope"
                ),
            ):
                backend.append(
                    _draft(
                        "claim.created",
                        _claim_payload(claim_id),
                        target_kind="claim",
                        target_id=claim_id,
                    )
                )
            assert len(events_path.read_text(encoding="utf-8").splitlines()) == len(
                lines
            )
        finally:
            backend.close()

    def test_append_detects_same_length_rewrite_with_restored_mtime(
        self, tmp_path: Path
    ) -> None:
        """The unchanged-log fast path is content-authenticated, not metadata-only."""
        backend = _make_backend(tmp_path)
        try:
            _seed_ready_task(backend)
            events_path = tmp_path / "events.jsonl"
            stat = events_path.stat()
            original = events_path.read_bytes()
            rewritten = original.replace(
                b'"name":"Git Events"', b'"name":"Bad Events"', 1
            )
            assert rewritten != original
            assert len(rewritten) == len(original)
            events_path.write_bytes(rewritten)
            os.utime(events_path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            after = events_path.stat()
            assert after.st_size == stat.st_size
            assert after.st_mtime_ns == stat.st_mtime_ns

            claim_id = "C-METADATA-BYPASS"
            with pytest.raises(
                TransactionAborted,
                match="git append integrity: event .* has different material",
            ):
                backend.append(
                    _draft(
                        "claim.created",
                        _claim_payload(claim_id),
                        target_kind="claim",
                        target_id=claim_id,
                    )
                )
            assert events_path.read_bytes() == rewritten
        finally:
            backend.close()

    def test_append_rejects_log_rewrite_after_material_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A post-read rewrite cannot be cached as though it produced the snapshot."""
        backend = _make_backend(tmp_path)
        try:
            _seed_ready_task(backend)
            events_path = tmp_path / "events.jsonl"
            # Benign external drift forces the validator off its already-known
            # signature without changing the event set or material.
            with events_path.open("ab") as fh:
                fh.write(b"\n")
            validated_before = backend._git_validated_log_signature  # noqa: SLF001
            original_reader = backend._read_git_events_ordered  # noqa: SLF001

            def read_then_rewrite() -> list[Event]:
                ordered = original_reader()
                before = events_path.read_bytes()
                after = before.replace(
                    b'"name":"Git Events"', b'"name":"Bad Events"', 1
                )
                assert after != before
                events_path.write_bytes(after)
                return ordered

            monkeypatch.setattr(
                backend,
                "_read_git_events_ordered",
                read_then_rewrite,
            )
            claim_id = "C-POST-READ-REWRITE"
            with pytest.raises(
                TransactionAborted,
                match="events.jsonl changed while its material was being validated",
            ):
                backend.append(
                    _draft(
                        "claim.created",
                        _claim_payload(claim_id),
                        target_kind="claim",
                        target_id=claim_id,
                    )
                )
            assert backend._git_validated_log_signature == validated_before  # noqa: SLF001
            assert claim_id not in events_path.read_text(encoding="utf-8")

            # The corrupt post-read bytes were not blessed into the cache: a
            # stable second attempt reaches the persisted-material mismatch.
            monkeypatch.setattr(
                backend,
                "_read_git_events_ordered",
                original_reader,
            )
            with pytest.raises(
                TransactionAborted,
                match="git append integrity: event .* has different material",
            ):
                backend.append(
                    _draft(
                        "claim.created",
                        _claim_payload(claim_id),
                        target_kind="claim",
                        target_id=claim_id,
                    )
                )
        finally:
            backend.close()

    @pytest.mark.parametrize("new_event_first", [False, True])
    def test_reopen_catch_up_rejects_same_id_rewrite_before_rebuild(
        self, tmp_path: Path, new_event_first: bool
    ) -> None:
        """Log-ahead convergence validates old material before rebuilding."""
        backend = _make_backend(tmp_path)
        try:
            _seed_prd(backend)
        finally:
            backend.close()

        events_path = tmp_path / "events.jsonl"
        lines = events_path.read_text(encoding="utf-8").splitlines()
        rewritten = json.loads(lines[0])
        rewritten["payload_json"]["name"] = "Rewritten before catch-up"
        lines[0] = json.dumps(rewritten, separators=(",", ":"))
        tail = json.loads(lines[-1])
        merged_event = json.dumps(
            {
                "timestamp": (_T0 + timedelta(seconds=10)).isoformat(),
                "actor": "merged-writer",
                "action": "state.initialized",
                "target_kind": "project",
                "target_id": "proj-1",
                "payload_json": {},
                "id": "E-feedfacefeed",
                "parent_event_id": tail["id"],
                "lamport": int(tail["lamport"]) + 1,
            },
            separators=(",", ":"),
        )
        merged = [merged_event, *lines] if new_event_first else [*lines, merged_event]
        events_path.write_text("\n".join(merged) + "\n", encoding="utf-8")

        with pytest.raises(
            TransactionAborted,
            match=(
                "git projection convergence: event .* has different material "
                "than the projected Git envelope"
            ),
        ):
            _make_backend(tmp_path)

    def test_legacy_fingerprint_bootstrap_is_atomic_and_retryable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An interrupted first bootstrap leaves no partial permanent ledger."""
        backend = _make_backend(tmp_path)
        try:
            _seed_prd(backend)
        finally:
            backend.close()

        with sqlite3.connect(str(tmp_path / "state.db")) as conn:
            conn.execute("DROP TABLE git_event_material_state")
            conn.execute("DROP TABLE git_event_material")

        interrupted = SqliteBackend(
            db_path=str(tmp_path / "state.db"),
            events_path=str(tmp_path / "events.jsonl"),
            clock=FrozenClock(_T0),
            events_storage="git",
        )

        def fail_after_first_insert(
            conn: sqlite3.Connection, rows: list[tuple[str, str]]
        ) -> None:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO git_event_material (event_id, fingerprint) "
                    "VALUES (?, ?)",
                    rows[0],
                )
                raise RuntimeError("injected bootstrap interruption")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

        monkeypatch.setattr(
            interrupted,
            "_git_bootstrap_material_fingerprints",
            fail_after_first_insert,
        )
        with pytest.raises(RuntimeError, match="injected bootstrap interruption"):
            interrupted.initialize()
        interrupted.close()

        with sqlite3.connect(str(tmp_path / "state.db")) as conn:
            assert conn.execute("SELECT COUNT(*) FROM git_event_material").fetchone() == (
                0,
            )
            assert conn.execute(
                "SELECT COUNT(*) FROM git_event_material_state"
            ).fetchone() == (0,)

        retry = _make_backend(tmp_path)
        try:
            event_count = retry._require_conn().execute(  # noqa: SLF001
                "SELECT COUNT(*) FROM events"
            ).fetchone()[0]
            assert tuple(
                retry._require_conn().execute(  # noqa: SLF001
                    "SELECT COUNT(*) FROM git_event_material"
                ).fetchone()
            ) == (event_count,)
            assert tuple(
                retry._require_conn().execute(  # noqa: SLF001
                    "SELECT initialized FROM git_event_material_state "
                    "WHERE singleton = 1"
                ).fetchone()
            ) == (1,)
        finally:
            retry.close()

    def test_torn_trailing_line_tolerated_interior_raises(
        self, tmp_path: Path
    ) -> None:
        """Same tolerance contract as the strict local replay."""
        src = tmp_path / "src"
        src.mkdir()
        b = _make_backend(src)
        try:
            _seed_ready_task(b)
            clean_state = _snap(b)
        finally:
            b.close()
        log = (src / "events.jsonl").read_text(encoding="utf-8")

        # Torn trailing line: tolerated silently.
        torn_dir = tmp_path / "torn"
        torn_dir.mkdir()
        (torn_dir / "events.jsonl").write_text(
            log + '{"id": "E-truncat', encoding="utf-8"
        )
        b2 = _make_backend(torn_dir)
        try:
            assert _snap(b2) == clean_state
        finally:
            b2.close()

        # Interior malformed line: corruption — replay must raise.
        lines = log.splitlines()
        corrupt = lines[:2] + ["{not json"] + lines[2:]
        corrupt_dir = tmp_path / "corrupt"
        corrupt_dir.mkdir()
        (corrupt_dir / "events.jsonl").write_text(
            "".join(line + "\n" for line in corrupt), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="interior line"):
            _make_backend(corrupt_dir)


class TestGitReplayOrdering:
    def test_lamport_then_ts_then_id_orders_replay(self, tmp_path: Path) -> None:
        """Two events tied on (lamport, ts) are ordered by id — deterministically.

        Handcrafted suffix: two status transitions out of 'ready' with equal
        lamport and equal ts. The first in id order applies (its WHERE
        status='ready' guard matches); the second no-ops. Final task status
        therefore proves which one replay put first.
        """
        src = tmp_path / "src"
        src.mkdir()
        b = _make_backend(src)
        try:
            _seed_ready_task(b)
        finally:
            b.close()

        lines = (src / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parent = json.loads(lines[-1])["id"]
        tie_a = {
            "timestamp": _T0.isoformat(),
            "actor": "agent-a",
            "action": "task.status_changed",
            "target_kind": "task",
            "target_id": "T001",
            "payload_json": {"task_id": "T001", "from": "ready", "to": "claimed"},
            "id": "E-aaaaaaaaaaaa",  # fabricated, valid-shape: replay trusts ids
            "parent_event_id": parent,
            "lamport": 8,
        }
        tie_b = {
            **tie_a,
            "actor": "agent-b",
            "payload_json": {"task_id": "T001", "from": "ready", "to": "blocked"},
            "id": "E-bbbbbbbbbbbb",
        }

        # File order deliberately REVERSED relative to id order.
        merged_dir = tmp_path / "merged"
        merged_dir.mkdir()
        (merged_dir / "events.jsonl").write_text(
            "".join(line + "\n" for line in lines)
            + json.dumps(tie_b)
            + "\n"
            + json.dumps(tie_a)
            + "\n",
            encoding="utf-8",
        )

        b2 = _make_backend(merged_dir)
        try:
            task = b2.get_task("T001")
            assert task is not None
            # E-aaaa… < E-bbbb… so the 'claimed' transition applied first and
            # won the ready-guard; 'blocked' no-opped.
            assert task.status == "claimed"
        finally:
            b2.close()
        # And the projection's display order reflects the id tiebreak, not
        # the file order.
        rows = _events_table(merged_dir)
        assert [row[0] for row in rows[-2:]] == ["E-aaaaaaaaaaaa", "E-bbbbbbbbbbbb"]

    def test_missing_lamport_sorts_first_not_crash(self, tmp_path: Path) -> None:
        """A hand-edited line without lamport sorts as 0 instead of raising."""
        state_dir = tmp_path / "p"
        state_dir.mkdir()
        no_lamport = {
            "timestamp": _T0.isoformat(),
            "actor": "test",
            "action": "project.created",
            "target_kind": "project",
            "target_id": "proj-1",
            "payload_json": {
                "id": "proj-1",
                "name": "X",
                "description": "",
                "created_at": _T0.isoformat(),
                "updated_at": _T0.isoformat(),
            },
            "id": "E-cccccccccccc",
        }
        with_lamport = {
            "timestamp": _T0.isoformat(),
            "actor": "test",
            "action": "state.initialized",
            "target_kind": "project",
            "target_id": "proj-1",
            "payload_json": {},
            "id": "E-dddddddddddd",
            "parent_event_id": "E-cccccccccccc",
            "lamport": 1,
        }
        # File order reversed: the lamport-less line must still apply FIRST.
        (state_dir / "events.jsonl").write_text(
            json.dumps(with_lamport) + "\n" + json.dumps(no_lamport) + "\n",
            encoding="utf-8",
        )
        b = _make_backend(state_dir)
        try:
            project = b.get_project()
            assert project is not None and project.id == "proj-1"
        finally:
            b.close()
        assert [row[0] for row in _events_table(state_dir)] == [
            "E-cccccccccccc",
            "E-dddddddddddd",
        ]


class TestGitPrdLifecycleReplay:
    @pytest.mark.parametrize("reverse_physical_order", [False, True])
    def test_current_orphan_revision_lineage_is_ignored_in_full_and_bounded_replay(
        self, tmp_path: Path, reverse_physical_order: bool
    ) -> None:
        """Marked content/lifecycle facts need an existing causal content parent."""
        base = tmp_path / f"orphan-current-base-{reverse_physical_order}"
        base.mkdir()
        backend = _make_backend(base)
        try:
            _seed_prd(backend)
        finally:
            backend.close()
        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()

        orphan_revision = _handcrafted_git_event(
            event_id="E-aaaaaaaaaaaa",
            parent_event_id="E-deadbeefdead",
            lamport=4,
            action="prd.revised",
            payload=_prd_revised_payload(revision=2, title="Orphan graft"),
        )
        orphan_review = _handcrafted_git_event(
            event_id="E-bbbbbbbbbbbb",
            parent_event_id="E-aaaaaaaaaaaa",
            lamport=5,
            action="prd.reviewed",
            payload={
                "project_id": "proj-1",
                "expected_revision": 2,
                "reviewer": "orphan-reviewer",
            },
        )
        orphan_approval = _handcrafted_git_event(
            event_id="E-cccccccccccc",
            parent_event_id="E-bbbbbbbbbbbb",
            lamport=6,
            action="prd.approved",
            payload={
                "project_id": "proj-1",
                "expected_revision": 2,
                "approver": "orphan-approver",
            },
        )
        suffix = [orphan_revision, orphan_review, orphan_approval]
        physical = list(reversed(suffix)) if reverse_physical_order else suffix
        merged = tmp_path / f"orphan-current-merged-{reverse_physical_order}"
        merged.mkdir()
        events_path = merged / "events.jsonl"
        events_path.write_text(
            "\n".join(prefix + physical) + "\n",
            encoding="utf-8",
        )

        replayed = _make_backend(merged)
        try:
            prd = replayed.get_prd("default")
            assert prd is not None
            assert prd.title == "Original PRD"
            assert prd.revision == 1
            assert prd.status.value == "draft"
        finally:
            replayed.close()

        bounded_dir = tmp_path / f"orphan-current-bounded-{reverse_physical_order}"
        bounded_dir.mkdir()
        bounded = _make_backend(bounded_dir)
        try:
            bounded.replay_to_event_id(str(events_path), "E-cccccccccccc")
            prd = bounded.get_prd("default")
            assert prd is not None
            assert prd.title == "Original PRD"
            assert prd.revision == 1
            assert prd.status.value == "draft"
        finally:
            bounded.close()

    def test_transparent_ancestor_lifecycle_survives_material_replay(
        self, tmp_path: Path
    ) -> None:
        """A causal title ancestor is history, not a losing fork sibling."""
        backend = _make_backend(tmp_path)
        try:
            _seed_prd(backend)
            backend.append(
                _draft(
                    "prd.revised",
                    _prd_revised_payload(revision=2, title="Transparent r2"),
                    target_kind="prd",
                    target_id="default",
                    ts=_T0 + timedelta(seconds=2),
                )
            )
            backend.append(
                _draft(
                    "prd.reviewed",
                    {
                        "project_id": "proj-1",
                        "expected_revision": 2,
                        "expected_status": "draft",
                        "reviewer": "r2-reviewer",
                    },
                    target_kind="prd",
                    target_id="default",
                    ts=_T0 + timedelta(seconds=3),
                )
            )
            backend.append(
                _draft(
                    "prd.approved",
                    {
                        "project_id": "proj-1",
                        "expected_revision": 2,
                        "expected_status": "reviewed",
                        "approver": "r2-approver",
                    },
                    target_kind="prd",
                    target_id="default",
                    ts=_T0 + timedelta(seconds=4),
                )
            )
            material_payload = _prd_revised_payload(
                revision=3,
                # A newer causal material revision may intentionally restore
                # the semantic base's title; the older r2 overlay must not win.
                title="Original PRD",
                expected_status="approved",
            )
            material_payload["status"] = "approved"
            material_payload["assumptions"] = [
                {
                    "id": "A001",
                    "statement": "Material revision.",
                    "rationale": "Exercises lifecycle-preserving replay.",
                    "requirement_ids": [],
                }
            ]
            backend.append(
                _draft(
                    "prd.revised",
                    material_payload,
                    target_kind="prd",
                    target_id="default",
                    ts=_T0 + timedelta(seconds=5),
                )
            )

            expected = _snap(backend)
            expected_reviews = [
                tuple(row)
                for row in backend._require_conn().execute(  # noqa: SLF001
                    "SELECT reviewed_by FROM reviews ORDER BY reviewed_by"
                )
            ]
            assert expected_reviews == [("r2-approver",)]

            events_path = tmp_path / "events.jsonl"
            backend.replay_from_empty(str(events_path))

            assert _snap(backend) == expected
            assert [
                tuple(row)
                for row in backend._require_conn().execute(  # noqa: SLF001
                    "SELECT reviewed_by FROM reviews ORDER BY reviewed_by"
                )
            ] == expected_reviews
        finally:
            backend.close()

    def test_material_revision_preserves_observed_base_approval_across_replay(
        self, tmp_path: Path
    ) -> None:
        """A material CAS against approved r1 preserves its audited approval."""
        backend = _make_backend(tmp_path)
        _seed_prd(backend)
        backend.append(
            _draft(
                "prd.reviewed",
                {
                    "project_id": "proj-1",
                    "expected_revision": 1,
                    "expected_status": "draft",
                    "reviewer": "r1-reviewer",
                },
                target_kind="prd",
                target_id="default",
                ts=_T0 + timedelta(seconds=2),
            )
        )
        backend.append(
            _draft(
                "prd.approved",
                {
                    "project_id": "proj-1",
                    "expected_revision": 1,
                    "expected_status": "reviewed",
                    "approver": "r1-approver",
                },
                target_kind="prd",
                target_id="default",
                ts=_T0 + timedelta(seconds=3),
            )
        )
        material_payload = _prd_revised_payload(
            revision=2,
            title="Approved material r2",
            expected_status="approved",
        )
        material_payload["status"] = "approved"
        material_payload["assumptions"] = [
            {
                "id": "A001",
                "statement": "Material r2.",
                "rationale": "Proves the observed approval survives replay.",
                "requirement_ids": [],
            }
        ]
        material = backend.append(
            _draft(
                "prd.revised",
                material_payload,
                target_kind="prd",
                target_id="default",
                ts=_T0 + timedelta(seconds=4),
            )
        )
        assert material is not None
        expected = _snap(backend)
        events_path = tmp_path / "events.jsonl"
        lines = events_path.read_text(encoding="utf-8").splitlines()
        prefix, suffix = lines[:-3], lines[-3:]
        backend.close()

        reopened = _make_backend(tmp_path)
        try:
            assert _snap(reopened) == expected
            assert [review.reviewed_by for review in reopened.list_reviews()] == [
                "r1-approver"
            ]
            reopened.replay_from_empty(str(events_path))
            assert _snap(reopened) == expected
        finally:
            reopened.close()

        for index, physical_order in enumerate(permutations(suffix)):
            merged = tmp_path / f"observed-approval-{index}"
            merged.mkdir()
            merged_events = merged / "events.jsonl"
            merged_events.write_text(
                "\n".join(prefix + list(physical_order)) + "\n",
                encoding="utf-8",
            )
            replayed = _make_backend(merged)
            try:
                assert _snap(replayed) == expected
                assert [review.reviewed_by for review in replayed.list_reviews()] == [
                    "r1-approver"
                ]
            finally:
                replayed.close()

            bounded_dir = tmp_path / f"observed-approval-bounded-{index}"
            bounded_dir.mkdir()
            bounded = _make_backend(bounded_dir)
            try:
                bounded.replay_to_event_id(str(merged_events), material.id)
                assert _snap(bounded) == expected
            finally:
                bounded.close()

    @pytest.mark.parametrize("prd_id", ["default", "v0.2"])
    @pytest.mark.parametrize("revision_sorts_after_approval", [False, True])
    def test_current_non_material_revision_never_regresses_approval(
        self,
        tmp_path: Path,
        prd_id: str,
        revision_sorts_after_approval: bool,
    ) -> None:
        """Equal-Lamport branch facts converge for default and named PRDs.

        The event-id tiebreak is exercised in both directions, and the JSONL
        union itself is replayed in both file orders.
        """
        base = tmp_path / "base"
        base.mkdir()
        backend = _make_backend(base)
        try:
            _seed_prd(backend, prd_id=prd_id)
        finally:
            backend.close()
        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parent = json.loads(prefix[-1])["id"]
        if revision_sorts_after_approval:
            approval_id, revision_id = "E-aaaaaaaaaaaa", "E-bbbbbbbbbbbb"
        else:
            revision_id, approval_id = "E-aaaaaaaaaaaa", "E-bbbbbbbbbbbb"
        revision = _handcrafted_git_event(
            event_id=revision_id,
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=_prd_revised_payload(prd_id=prd_id),
            prd_id=prd_id,
        )
        approval = _handcrafted_git_event(
            event_id=approval_id,
            parent_event_id=parent,
            lamport=4,
            action="prd.approved",
            payload={
                "project_id": "proj-1",
                "prd_id": prd_id,
                "approver": "parallel-human",
            },
            prd_id=prd_id,
        )

        snapshots: list[str] = []
        for index, suffix in enumerate(([revision, approval], [approval, revision])):
            merged = tmp_path / f"merged-{index}"
            merged.mkdir()
            (merged / "events.jsonl").write_text(
                "\n".join(prefix + suffix) + "\n", encoding="utf-8"
            )
            replayed = _make_backend(merged)
            try:
                prd = replayed.get_prd(prd_id)
                assert prd is not None
                assert prd.title == "Renamed PRD"
                assert prd.revision == 2
                assert prd.status.value == "approved"
                snapshots.append(_snap(replayed))
            finally:
                replayed.close()
        assert snapshots[0] == snapshots[1]

    def test_legacy_revision_keeps_historical_payload_authority(
        self, tmp_path: Path
    ) -> None:
        base = tmp_path / "base"
        base.mkdir()
        backend = _make_backend(base)
        try:
            _seed_prd(backend)
        finally:
            backend.close()
        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parent = json.loads(prefix[-1])["id"]
        approval = _handcrafted_git_event(
            event_id="E-aaaaaaaaaaaa",
            parent_event_id=parent,
            lamport=4,
            action="prd.approved",
            payload={"project_id": "proj-1", "approver": "parallel-human"},
        )
        legacy_revision = _handcrafted_git_event(
            event_id="E-bbbbbbbbbbbb",
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=_prd_revised_payload(expected_status=None, title="Legacy Rename"),
        )
        merged = tmp_path / "merged"
        merged.mkdir()
        (merged / "events.jsonl").write_text(
            "\n".join(prefix + [legacy_revision, approval]) + "\n",
            encoding="utf-8",
        )
        replayed = _make_backend(merged)
        try:
            prd = replayed.get_prd("default")
            assert prd is not None
            assert prd.title == "Legacy Rename"
            assert prd.status.value == "draft"
        finally:
            replayed.close()

    def test_material_revision_still_demotes_when_sorted_after_approval(
        self, tmp_path: Path
    ) -> None:
        base = tmp_path / "base"
        base.mkdir()
        backend = _make_backend(base)
        requirement = {
            "id": "R001",
            "prd_section": "requirements",
            "text": "Original requirement.",
            "source_paragraph": None,
            "derived": False,
        }
        try:
            backend.append(
                _draft(
                    "project.created",
                    {
                        "id": "proj-1",
                        "name": "Git PRD",
                        "description": "",
                        "created_at": _T0.isoformat(),
                        "updated_at": _T0.isoformat(),
                    },
                )
            )
            backend.append(_draft("state.initialized", {}))
            backend.append(
                _draft(
                    "prd.parsed",
                    _prd_parsed_payload(requirements=[requirement]),
                    target_kind="prd",
                    target_id="default",
                )
            )
        finally:
            backend.close()
        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parent = json.loads(prefix[-1])["id"]
        approval = _handcrafted_git_event(
            event_id="E-aaaaaaaaaaaa",
            parent_event_id=parent,
            lamport=4,
            action="prd.approved",
            payload={"project_id": "proj-1", "approver": "parallel-human"},
        )
        material_revision = _handcrafted_git_event(
            event_id="E-bbbbbbbbbbbb",
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=_prd_revised_payload(superseded=[requirement]),
        )
        merged = tmp_path / "merged"
        merged.mkdir()
        (merged / "events.jsonl").write_text(
            "\n".join(prefix + [material_revision, approval]) + "\n",
            encoding="utf-8",
        )
        replayed = _make_backend(merged)
        try:
            prd = replayed.get_prd("default")
            assert prd is not None
            assert prd.status.value == "draft"
            assert prd.revision == 2
            assert replayed.list_requirements(prd_id="default") == []
        finally:
            replayed.close()

    def test_current_first_parse_is_first_writer_wins_during_union_replay(
        self, tmp_path: Path
    ) -> None:
        base = tmp_path / "base"
        base.mkdir()
        backend = _make_backend(base)
        try:
            backend.append(
                _draft(
                    "project.created",
                    {
                        "id": "proj-1",
                        "name": "Git PRD",
                        "description": "",
                        "created_at": _T0.isoformat(),
                        "updated_at": _T0.isoformat(),
                    },
                )
            )
            backend.append(_draft("state.initialized", {}))
        finally:
            backend.close()
        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parent = json.loads(prefix[-1])["id"]
        winner = _handcrafted_git_event(
            event_id="E-aaaaaaaaaaaa",
            parent_event_id=parent,
            lamport=3,
            action="prd.parsed",
            payload=_prd_parsed_payload(title="First Winner"),
        )
        approval = _handcrafted_git_event(
            event_id="E-bbbbbbbbbbbb",
            parent_event_id="E-aaaaaaaaaaaa",
            lamport=4,
            action="prd.approved",
            payload={"project_id": "proj-1", "approver": "human"},
        )
        stale = _handcrafted_git_event(
            event_id="E-cccccccccccc",
            parent_event_id=parent,
            lamport=5,
            action="prd.parsed",
            payload=_prd_parsed_payload(title="Stale Loser"),
        )
        merged = tmp_path / "merged"
        merged.mkdir()
        (merged / "events.jsonl").write_text(
            "\n".join(prefix + [stale, approval, winner]) + "\n",
            encoding="utf-8",
        )
        replayed = _make_backend(merged)
        try:
            prd = replayed.get_prd("default")
            assert prd is not None
            assert prd.title == "First Winner"
            assert prd.status.value == "approved"
            assert prd.revision == 1
        finally:
            replayed.close()

    @pytest.mark.parametrize("material_sorts_first", [False, True])
    def test_material_revision_dominates_stale_title_revision_and_approval(
        self, tmp_path: Path, material_sorts_first: bool
    ) -> None:
        """Sibling material content wins while the independent rename survives."""
        requirement = {
            "id": "R001",
            "prd_section": "requirements",
            "text": "Original requirement.",
            "source_paragraph": None,
            "derived": False,
        }
        base = tmp_path / "base"
        base.mkdir()
        backend = _make_backend(base)
        try:
            _seed_prd(backend)
            # Replace the title-only seed with one carrying a live requirement.
            # This is a legacy destructive refresh solely to keep the fixture
            # compact; the forked events below all use current markers.
            backend.append(
                _draft(
                    "prd.parsed",
                    _prd_parsed_payload(
                        expected_absent=None, requirements=[requirement]
                    ),
                    target_kind="prd",
                    target_id="default",
                )
            )
            backend.append(
                _draft(
                    "prd.reviewed",
                    {
                        "project_id": "proj-1",
                        "expected_revision": 1,
                        "reviewer": "base-reviewer",
                    },
                    target_kind="prd",
                    target_id="default",
                )
            )
            backend.append(
                _draft(
                    "prd.approved",
                    {
                        "project_id": "proj-1",
                        "expected_revision": 1,
                        "approver": "base-approver",
                    },
                    target_kind="prd",
                    target_id="default",
                )
            )
        finally:
            backend.close()
        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parent = json.loads(prefix[-1])["id"]
        material_id, title_id = (
            ("E-aaaaaaaaaaaa", "E-bbbbbbbbbbbb")
            if material_sorts_first
            else ("E-bbbbbbbbbbbb", "E-aaaaaaaaaaaa")
        )
        material_payload = _prd_revised_payload(superseded=[requirement])
        material_payload.update(status="approved", expected_status="approved")
        material = _handcrafted_git_event(
            event_id=material_id,
            parent_event_id=parent,
            lamport=7,
            action="prd.revised",
            payload=material_payload,
        )
        title_payload = _prd_revised_payload(title="Independent Rename")
        title_payload.update(
            status="approved",
            expected_status="approved",
            requirements_unchanged=[requirement],
        )
        title_revision = _handcrafted_git_event(
            event_id=title_id,
            parent_event_id=parent,
            lamport=7,
            action="prd.revised",
            payload=title_payload,
        )
        stale_approval = _handcrafted_git_event(
            event_id="E-cccccccccccc",
            parent_event_id=parent,
            lamport=8,
            action="prd.approved",
            payload={
                "project_id": "proj-1",
                "expected_revision": 1,
                "approver": "stale-approver",
            },
        )

        snapshots: list[str] = []
        for index, suffix in enumerate(
            ([material, title_revision, stale_approval],
             [stale_approval, title_revision, material])
        ):
            merged = tmp_path / f"material-title-{material_sorts_first}-{index}"
            merged.mkdir()
            (merged / "events.jsonl").write_text(
                "\n".join(prefix + list(suffix)) + "\n", encoding="utf-8"
            )
            replayed = _make_backend(merged)
            try:
                prd = replayed.get_prd("default")
                assert prd is not None
                assert prd.title == "Independent Rename"
                assert prd.revision == 2
                assert prd.status.value == "draft"
                assert replayed.list_requirements(prd_id="default") == []
                snapshots.append(_snap(replayed))
            finally:
                replayed.close()
        assert snapshots[0] == snapshots[1]

    @pytest.mark.parametrize("material_sorts_first", [False, True])
    def test_descendant_title_revision_outranks_older_sibling_overlay(
        self, tmp_path: Path, material_sorts_first: bool
    ) -> None:
        """A rev-2 sibling rename must not overwrite a valid rev-3 rename.

        The material rev-2 event owns content lineage, while its title-only
        sibling contributes an overlay because it is skipped as competing
        content. A title-only rev-3 descendant of the material winner is newer
        causal state and must outrank that older overlay in every union-file
        permutation and after a fresh replay.
        """
        requirement = {
            "id": "R001",
            "prd_section": "requirements",
            "text": "Original requirement.",
            "source_paragraph": None,
            "derived": False,
        }
        base = tmp_path / f"descendant-base-{material_sorts_first}"
        base.mkdir()
        backend = _make_backend(base)
        try:
            backend.append(
                _draft(
                    "project.created",
                    {
                        "id": "proj-1",
                        "name": "Git PRD",
                        "description": "",
                        "created_at": _T0.isoformat(),
                        "updated_at": _T0.isoformat(),
                    },
                )
            )
            backend.append(_draft("state.initialized", {}))
            backend.append(
                _draft(
                    "prd.parsed",
                    _prd_parsed_payload(requirements=[requirement]),
                    target_kind="prd",
                    target_id="default",
                )
            )
        finally:
            backend.close()
        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parent = json.loads(prefix[-1])["id"]
        material_id, title_id = (
            ("E-aaaaaaaaaaaa", "E-bbbbbbbbbbbb")
            if material_sorts_first
            else ("E-bbbbbbbbbbbb", "E-aaaaaaaaaaaa")
        )
        material = _handcrafted_git_event(
            event_id=material_id,
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=_prd_revised_payload(
                title="Material Rev2",
                superseded=[requirement],
            ),
        )
        title_payload = _prd_revised_payload(title="Sibling Rev2")
        title_payload["requirements_unchanged"] = [requirement]
        title_revision = _handcrafted_git_event(
            event_id=title_id,
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=title_payload,
        )
        descendant = _handcrafted_git_event(
            event_id="E-cccccccccccc",
            parent_event_id=material_id,
            lamport=5,
            action="prd.revised",
            payload=_prd_revised_payload(
                revision=3,
                title="Descendant Rev3",
            ),
        )
        stale_descendant = _handcrafted_git_event(
            event_id="E-dddddddddddd",
            parent_event_id=title_id,
            lamport=5,
            action="prd.revised",
            payload=_prd_revised_payload(
                revision=3,
                title="Stale Descendant Rev3",
                superseded=[requirement],
            ),
        )

        snapshots: list[str] = []
        for index, suffix in enumerate(
            permutations(
                (material, title_revision, descendant, stale_descendant)
            )
        ):
            merged = tmp_path / f"descendant-{material_sorts_first}-{index}"
            merged.mkdir()
            events_path = merged / "events.jsonl"
            events_path.write_text(
                "\n".join(prefix + list(suffix)) + "\n",
                encoding="utf-8",
            )
            replayed = _make_backend(merged)
            try:
                for replay_again in (False, True):
                    if replay_again:
                        replayed.replay_from_empty(str(events_path))
                    prd = replayed.get_prd("default")
                    assert prd is not None
                    assert prd.title == "Descendant Rev3"
                    assert prd.revision == 3
                    assert prd.status.value == "draft"
                    assert replayed.list_requirements(prd_id="default") == []
                    snapshots.append(_snap(replayed))
            finally:
                replayed.close()
        assert len(set(snapshots)) == 1

    @pytest.mark.parametrize("losing_title_is_file_tail", [False, True])
    def test_append_after_union_uses_canonical_prd_content_parent(
        self, tmp_path: Path, losing_title_is_file_tail: bool
    ) -> None:
        """A real append after union follows material state, not file order."""
        requirement = {
            "id": "R001",
            "prd_section": "requirements",
            "text": "Original requirement.",
            "source_paragraph": None,
            "derived": False,
        }
        base = tmp_path / f"append-union-base-{losing_title_is_file_tail}"
        base.mkdir()
        backend = _make_backend(base)
        try:
            backend.append(
                _draft(
                    "project.created",
                    {
                        "id": "proj-1",
                        "name": "Git PRD",
                        "description": "",
                        "created_at": _T0.isoformat(),
                        "updated_at": _T0.isoformat(),
                    },
                )
            )
            backend.append(_draft("state.initialized", {}))
            backend.append(
                _draft(
                    "prd.parsed",
                    _prd_parsed_payload(requirements=[requirement]),
                    target_kind="prd",
                    target_id="default",
                )
            )
        finally:
            backend.close()
        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parent = json.loads(prefix[-1])["id"]
        material_id = "E-aaaaaaaaaaaa"
        title_id = "E-bbbbbbbbbbbb"
        material = _handcrafted_git_event(
            event_id=material_id,
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=_prd_revised_payload(
                title="Material Rev2",
                superseded=[requirement],
            ),
        )
        title_payload = _prd_revised_payload(title="Sibling Rev2")
        title_payload["requirements_unchanged"] = [requirement]
        title_revision = _handcrafted_git_event(
            event_id=title_id,
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=title_payload,
        )
        suffix = (
            [material, title_revision]
            if losing_title_is_file_tail
            else [title_revision, material]
        )
        merged = tmp_path / f"append-union-{losing_title_is_file_tail}"
        merged.mkdir()
        events_path = merged / "events.jsonl"
        events_path.write_text(
            "\n".join(prefix + suffix) + "\n",
            encoding="utf-8",
        )

        replayed = _make_backend(merged)
        try:
            before = replayed.get_prd("default")
            assert before is not None
            assert before.title == "Sibling Rev2"
            assert before.revision == 2
            assert replayed.list_requirements(prd_id="default") == []

            appended = replayed.append(
                _draft(
                    "prd.revised",
                    _prd_revised_payload(
                        revision=3,
                        title="Appended Rev3",
                    ),
                    target_kind="prd",
                    target_id="default",
                    ts=_T0 + timedelta(seconds=2),
                )
            )
            assert appended is not None
            assert appended.parent_event_id == material_id
            assert appended.lamport == 5
            live = replayed.get_prd("default")
            assert live is not None
            assert live.title == "Appended Rev3"
            assert live.revision == 3

            log_before_stale = events_path.read_bytes()
            with pytest.raises(EventRejected, match=r"current\+1"):
                replayed.append(
                    _draft(
                        "prd.revised",
                        _prd_revised_payload(
                            revision=3,
                            title="Stale Rev3",
                        ),
                        target_kind="prd",
                        target_id="default",
                        ts=_T0 + timedelta(seconds=3),
                    )
                )
            assert events_path.read_bytes() == log_before_stale

            expected = _snap(replayed)
            for _ in range(2):
                replayed.replay_from_empty(str(events_path))
                current = replayed.get_prd("default")
                assert current is not None
                assert current.title == "Appended Rev3"
                assert current.revision == 3
                assert replayed.list_requirements(prd_id="default") == []
                assert _snap(replayed) == expected
        finally:
            replayed.close()

    @pytest.mark.parametrize("new_prd_id", ["default", "v0.2"])
    @pytest.mark.parametrize("losing_other_prd_is_tail", [False, True])
    def test_cross_prd_union_tail_cannot_poison_new_prd_lineage(
        self,
        tmp_path: Path,
        new_prd_id: str,
        losing_other_prd_is_tail: bool,
    ) -> None:
        """A losing B tail cannot suppress valid A content or lifecycle."""
        other_prd_id = "v0.2" if new_prd_id == "default" else "default"
        requirement = {
            "id": "R001",
            "prd_section": "requirements",
            "text": "Other PRD requirement.",
            "source_paragraph": None,
            "derived": False,
        }
        base = tmp_path / (
            f"cross-prd-base-{new_prd_id}-{losing_other_prd_is_tail}"
        )
        base.mkdir()
        backend = _make_backend(base)
        try:
            backend.append(
                _draft(
                    "project.created",
                    {
                        "id": "proj-1",
                        "name": "Git PRD",
                        "description": "",
                        "created_at": _T0.isoformat(),
                        "updated_at": _T0.isoformat(),
                    },
                )
            )
            backend.append(_draft("state.initialized", {}))
            backend.append(
                _draft(
                    "prd.parsed",
                    _prd_parsed_payload(
                        prd_id=other_prd_id,
                        title="Other Base",
                        requirements=[requirement],
                    ),
                    target_kind="prd",
                    target_id=other_prd_id,
                )
            )
        finally:
            backend.close()
        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parent = json.loads(prefix[-1])["id"]
        material_id = "E-aaaaaaaaaaaa"
        title_id = "E-bbbbbbbbbbbb"
        material = _handcrafted_git_event(
            event_id=material_id,
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=_prd_revised_payload(
                prd_id=other_prd_id,
                title="Other Material",
                superseded=[requirement],
            ),
            prd_id=other_prd_id,
        )
        title_payload = _prd_revised_payload(
            prd_id=other_prd_id,
            title="Other Sibling Title",
        )
        title_payload["requirements_unchanged"] = [requirement]
        title_revision = _handcrafted_git_event(
            event_id=title_id,
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=title_payload,
            prd_id=other_prd_id,
        )
        suffix = (
            [material, title_revision]
            if losing_other_prd_is_tail
            else [title_revision, material]
        )
        merged = tmp_path / (
            f"cross-prd-{new_prd_id}-{losing_other_prd_is_tail}"
        )
        merged.mkdir()
        events_path = merged / "events.jsonl"
        events_path.write_text(
            "\n".join(prefix + suffix) + "\n",
            encoding="utf-8",
        )

        replayed = _make_backend(merged)
        try:
            parsed = replayed.append(
                _draft(
                    "prd.parsed",
                    _prd_parsed_payload(
                        prd_id=new_prd_id,
                        title="New Base",
                        requirements=[],
                    ),
                    target_kind="prd",
                    target_id=new_prd_id,
                    ts=_T0 + timedelta(seconds=2),
                )
            )
            assert parsed is not None
            assert parsed.parent_event_id == (
                title_id if losing_other_prd_is_tail else material_id
            )
            revised = replayed.append(
                _draft(
                    "prd.revised",
                    _prd_revised_payload(
                        prd_id=new_prd_id,
                        revision=2,
                        title="New Rev2",
                    ),
                    target_kind="prd",
                    target_id=new_prd_id,
                    ts=_T0 + timedelta(seconds=3),
                )
            )
            assert revised is not None
            assert revised.parent_event_id == parsed.id
            reviewed = replayed.append(
                _draft(
                    "prd.reviewed",
                    {
                        "project_id": "proj-1",
                        "prd_id": new_prd_id,
                        "expected_revision": 2,
                        "expected_status": "draft",
                        "reviewer": "cross-prd-reviewer",
                    },
                    target_kind="prd",
                    target_id=new_prd_id,
                    ts=_T0 + timedelta(seconds=4),
                )
            )
            approved = replayed.append(
                _draft(
                    "prd.approved",
                    {
                        "project_id": "proj-1",
                        "prd_id": new_prd_id,
                        "expected_revision": 2,
                        "expected_status": "reviewed",
                        "approver": "cross-prd-approver",
                    },
                    target_kind="prd",
                    target_id=new_prd_id,
                    ts=_T0 + timedelta(seconds=5),
                )
            )
            assert reviewed is not None and approved is not None
            assert reviewed.parent_event_id == revised.id
            assert approved.parent_event_id == revised.id

            current = replayed.get_prd(new_prd_id)
            assert current is not None
            assert current.title == "New Rev2"
            assert current.revision == 2
            assert current.status.value == "approved"
            expected = _snap(replayed)
            for _ in range(2):
                replayed.replay_from_empty(str(events_path))
                current = replayed.get_prd(new_prd_id)
                assert current is not None
                assert current.title == "New Rev2"
                assert current.revision == 2
                assert current.status.value == "approved"
                assert _snap(replayed) == expected
        finally:
            replayed.close()

    def test_prd_policy_memory_is_linear_for_deep_title_history(
        self, tmp_path: Path
    ) -> None:
        """Deep histories must not retain a transitive ancestor set per event."""
        backend = _make_backend(tmp_path)
        events = [
            Event(
                id="E-000000000001",
                parent_event_id=None,
                lamport=1,
                timestamp=_T0,
                actor="scale",
                action="prd.parsed",
                target_kind="prd",
                target_id="default",
                payload_json=_prd_parsed_payload(title="Revision 1"),
            )
        ]
        parent = events[0].id
        for revision in range(2, 1501):
            event = Event(
                id=f"E-{revision:012x}",
                parent_event_id=parent,
                lamport=revision,
                timestamp=_T0 + timedelta(microseconds=revision),
                actor="scale",
                action="prd.revised",
                target_kind="prd",
                target_id="default",
                payload_json=_prd_revised_payload(
                    revision=revision,
                    title=f"Revision {revision}",
                ),
            )
            events.append(event)
            parent = event.id

        tracemalloc.start()
        try:
            policy = backend._build_git_prd_replay_policy(events)  # noqa: SLF001
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
            backend.close()
        assert policy.content_heads["default"] == (1500, parent)
        # The rejected transitive-frozenset implementation measured >50 MiB
        # for this 1,500-event chain. Keep ample platform headroom while still
        # catching quadratic retention.
        assert peak < 16 * 1024 * 1024

    def test_prd_policy_scales_for_title_history_with_unchanged_requirement(
        self, tmp_path: Path
    ) -> None:
        """A carried requirement must not trigger a full ancestor walk per edit."""
        backend = _make_backend(tmp_path)
        requirement = {
            "id": "R001",
            "prd_section": "requirements",
            "text": "Stable contract.",
            "source_paragraph": None,
            "derived": False,
        }
        events = [
            Event(
                id="E-000000000001",
                parent_event_id=None,
                lamport=1,
                timestamp=_T0,
                actor="scale",
                action="prd.parsed",
                target_kind="prd",
                target_id="default",
                payload_json=_prd_parsed_payload(
                    title="Revision 1",
                    requirements=[requirement],
                ),
            )
        ]
        parent = events[0].id
        for revision in range(2, 5002):
            payload = _prd_revised_payload(
                revision=revision,
                title=f"Revision {revision}",
            )
            payload["requirements_unchanged"] = [requirement]
            event = Event(
                id=f"E-{revision:012x}",
                parent_event_id=parent,
                lamport=revision,
                timestamp=_T0 + timedelta(microseconds=revision),
                actor="scale",
                action="prd.revised",
                target_kind="prd",
                target_id="default",
                payload_json=payload,
            )
            events.append(event)
            parent = event.id

        tracemalloc.start()
        started = time.perf_counter()
        try:
            policy = backend._build_git_prd_replay_policy(events)  # noqa: SLF001
            _current, peak = tracemalloc.get_traced_memory()
            elapsed = time.perf_counter() - started
        finally:
            tracemalloc.stop()
            backend.close()
        assert policy.content_heads["default"] == (5001, parent)
        assert peak < 24 * 1024 * 1024
        assert elapsed < 10

    def test_sibling_additive_revisions_choose_one_replay_lineage(
        self, tmp_path: Path
    ) -> None:
        base = tmp_path / "additive-base"
        base.mkdir()
        backend = _make_backend(base)
        try:
            _seed_prd(backend)
        finally:
            backend.close()
        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parent = json.loads(prefix[-1])["id"]
        requirement = {
            "id": "R001",
            "prd_section": "requirements",
            "text": "Concurrent addition.",
            "source_paragraph": None,
            "derived": False,
        }
        first_payload = _prd_revised_payload(title="First Add")
        first_payload["requirements_added"] = [requirement]
        second_payload = _prd_revised_payload(title="Second Add")
        second_payload["requirements_added"] = [requirement]
        first = _handcrafted_git_event(
            event_id="E-aaaaaaaaaaaa",
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=first_payload,
        )
        second = _handcrafted_git_event(
            event_id="E-bbbbbbbbbbbb",
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=second_payload,
        )

        snapshots = []
        for index, suffix in enumerate(([first, second], [second, first])):
            merged = tmp_path / f"additive-merged-{index}"
            merged.mkdir()
            (merged / "events.jsonl").write_text(
                "\n".join(prefix + suffix) + "\n", encoding="utf-8"
            )
            replayed = _make_backend(merged)
            try:
                prd = replayed.get_prd("default")
                assert prd is not None
                assert prd.title == "First Add"
                assert prd.revision == 2
                assert [r.id for r in replayed.list_requirements()] == ["R001"]
                snapshots.append(_snap(replayed))
            finally:
                replayed.close()
        assert snapshots[0] == snapshots[1]

    def test_material_descendants_of_sibling_title_overlays_choose_one_lineage(
        self, tmp_path: Path
    ) -> None:
        """Independent renames must not make later content edits mergeable."""
        base = tmp_path / "title-descendant-base"
        base.mkdir()
        backend = _make_backend(base)
        try:
            _seed_prd(backend)
        finally:
            backend.close()
        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parent = json.loads(prefix[-1])["id"]

        first_title = _handcrafted_git_event(
            event_id="E-aaaaaaaaaaaa",
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=_prd_revised_payload(title="First Rename"),
        )
        second_title = _handcrafted_git_event(
            event_id="E-bbbbbbbbbbbb",
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=_prd_revised_payload(title="Second Rename"),
        )
        first_requirement = {
            "id": "R101",
            "prd_section": "requirements",
            "text": "First branch contract.",
            "source_paragraph": None,
            "derived": False,
        }
        second_requirement = {
            "id": "R102",
            "prd_section": "requirements",
            "text": "Second branch contract.",
            "source_paragraph": None,
            "derived": False,
        }
        first_material_payload = _prd_revised_payload(
            revision=3,
            title="First Material",
        )
        first_material_payload["requirements_added"] = [first_requirement]
        first_material = _handcrafted_git_event(
            event_id="E-cccccccccccc",
            parent_event_id="E-aaaaaaaaaaaa",
            lamport=5,
            action="prd.revised",
            payload=first_material_payload,
        )
        second_material_payload = _prd_revised_payload(
            revision=3,
            title="Second Material",
        )
        second_material_payload["requirements_added"] = [second_requirement]
        second_material = _handcrafted_git_event(
            event_id="E-dddddddddddd",
            parent_event_id="E-bbbbbbbbbbbb",
            lamport=5,
            action="prd.revised",
            payload=second_material_payload,
        )

        snapshots: list[str] = []
        events = (first_title, second_title, first_material, second_material)
        for index, suffix in enumerate(permutations(events)):
            merged = tmp_path / f"title-descendant-merged-{index}"
            merged.mkdir()
            events_path = merged / "events.jsonl"
            events_path.write_text(
                "\n".join(prefix + list(suffix)) + "\n",
                encoding="utf-8",
            )
            replayed = _make_backend(merged)
            try:
                for _ in range(2):
                    prd = replayed.get_prd("default")
                    assert prd is not None
                    assert prd.title == "First Material"
                    assert prd.revision == 3
                    assert [
                        requirement.id
                        for requirement in replayed.list_requirements(
                            prd_id="default"
                        )
                    ] == ["R101"]
                    snapshots.append(_snap(replayed))
                    replayed.replay_from_empty(str(events_path))
            finally:
                replayed.close()
        assert len(set(snapshots)) == 1

    @pytest.mark.parametrize("reverse", [False, True])
    def test_losing_material_descendant_cannot_overlay_when_sorted_before_parent(
        self, tmp_path: Path, reverse: bool
    ) -> None:
        """Malformed causal clocks cannot synthesize content from two branches.

        The losing branch's title-only child deliberately carries a lower
        Lamport than its material parent. HLC replay therefore sees the child
        first, but lineage resolution must still reject it after selecting the
        competing material winner. Physical union order, bounded replay, a full
        rebuild, and reopen must all converge on the same single branch.
        """
        base = tmp_path / f"causal-order-base-{reverse}"
        base.mkdir()
        backend = _make_backend(base)
        try:
            _seed_prd(backend)
        finally:
            backend.close()
        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parent = json.loads(prefix[-1])["id"]

        winning_requirement = {
            "id": "R101",
            "prd_section": "requirements",
            "text": "Winning branch contract.",
            "source_paragraph": None,
            "derived": False,
        }
        losing_requirement = {
            "id": "R102",
            "prd_section": "requirements",
            "text": "Losing branch contract.",
            "source_paragraph": None,
            "derived": False,
        }
        winning_payload = _prd_revised_payload(title="Winning Material")
        winning_payload["requirements_added"] = [winning_requirement]
        winner = _handcrafted_git_event(
            event_id="E-aaaaaaaaaaaa",
            parent_event_id=parent,
            lamport=5,
            action="prd.revised",
            payload=winning_payload,
        )
        losing_payload = _prd_revised_payload(title="Losing Material")
        losing_payload["requirements_added"] = [losing_requirement]
        loser = _handcrafted_git_event(
            event_id="E-bbbbbbbbbbbb",
            parent_event_id=parent,
            lamport=6,
            action="prd.revised",
            payload=losing_payload,
        )
        descendant_payload = _prd_revised_payload(
            revision=3,
            title="Losing Descendant Rename",
        )
        descendant_payload["requirements_unchanged"] = [losing_requirement]
        losing_descendant = _handcrafted_git_event(
            event_id="E-cccccccccccc",
            parent_event_id="E-bbbbbbbbbbbb",
            lamport=4,
            action="prd.revised",
            payload=descendant_payload,
        )

        suffix = (
            [loser, winner, losing_descendant]
            if reverse
            else [losing_descendant, winner, loser]
        )
        merged = tmp_path / f"causal-order-merged-{reverse}"
        merged.mkdir()
        events_path = merged / "events.jsonl"
        events_path.write_text(
            "\n".join(prefix + suffix) + "\n",
            encoding="utf-8",
        )

        def assert_winning_projection(candidate: SqliteBackend) -> str:
            prd = candidate.get_prd("default")
            assert prd is not None
            assert prd.title == "Winning Material"
            assert prd.revision == 2
            assert [
                requirement.id
                for requirement in candidate.list_requirements(prd_id="default")
            ] == ["R101"]
            return _snap(candidate)

        replayed = _make_backend(merged)
        try:
            snapshots = [assert_winning_projection(replayed)]
            replayed.replay_from_empty(str(events_path))
            snapshots.append(assert_winning_projection(replayed))
            replayed.replay_to_event_id(str(events_path), "E-bbbbbbbbbbbb")
            snapshots.append(assert_winning_projection(replayed))
        finally:
            replayed.close()

        reopened = _make_backend(merged)
        try:
            snapshots.append(assert_winning_projection(reopened))
        finally:
            reopened.close()
        assert len(set(snapshots)) == 1

    @pytest.mark.parametrize(
        (
            "first_material_lamport",
            "second_material_lamport",
            "expected_revision",
            "expected_title",
            "expected_requirement",
            "winning_approver",
            "losing_approver",
        ),
        (
            (5, 10, 3, "Branch A Material", "R101", "branch-a-approver", "branch-b-approver"),
            (10, 5, 2, "Branch A Rename", "R102", "branch-b-approver", "branch-a-approver"),
        ),
    )
    def test_asymmetric_material_fork_keeps_one_lineage_and_its_lifecycle(
        self,
        tmp_path: Path,
        first_material_lamport: int,
        second_material_lamport: int,
        expected_revision: int,
        expected_title: str,
        expected_requirement: str,
        winning_approver: str,
        losing_approver: str,
    ) -> None:
        """A transparent revision cannot make unequal material revisions merge.

        Branch A renames at r2, then makes material r3 content and approves it.
        Branch B works directly from r1, but reaches material r2 at a larger
        Lamport after unrelated history. Both material events share the same
        semantic content base and must compete even though their numeric
        revisions differ. The losing branch's content and lifecycle must not
        leak into the winning projection.
        """
        base = tmp_path / "asymmetric-material-base"
        base.mkdir()
        backend = _make_backend(base)
        try:
            _seed_prd(backend)
        finally:
            backend.close()
        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parent = json.loads(prefix[-1])["id"]

        title_revision = _handcrafted_git_event(
            event_id="E-aaaaaaaaaaaa",
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=_prd_revised_payload(revision=2, title="Branch A Rename"),
        )
        first_requirement = {
            "id": "R101",
            "prd_section": "requirements",
            "text": "Branch A contract.",
            "source_paragraph": None,
            "derived": False,
        }
        first_payload = _prd_revised_payload(
            revision=3,
            title="Branch A Material",
        )
        first_payload["requirements_added"] = [first_requirement]
        first_material = _handcrafted_git_event(
            event_id="E-bbbbbbbbbbbb",
            parent_event_id="E-aaaaaaaaaaaa",
            lamport=first_material_lamport,
            action="prd.revised",
            payload=first_payload,
        )
        first_review = _handcrafted_git_event(
            event_id="E-dddddddddddd",
            parent_event_id="E-bbbbbbbbbbbb",
            lamport=first_material_lamport + 1,
            action="prd.reviewed",
            payload={
                "project_id": "proj-1",
                "expected_revision": 3,
                "expected_status": "draft",
                "reviewer": "branch-a-reviewer",
            },
        )
        first_approval = _handcrafted_git_event(
            event_id="E-eeeeeeeeeeee",
            parent_event_id="E-dddddddddddd",
            lamport=first_material_lamport + 2,
            action="prd.approved",
            payload={
                "project_id": "proj-1",
                "expected_revision": 3,
                "expected_status": "reviewed",
                "approver": "branch-a-approver",
            },
        )

        second_requirement = {
            "id": "R102",
            "prd_section": "requirements",
            "text": "Branch B contract.",
            "source_paragraph": None,
            "derived": False,
        }
        second_payload = _prd_revised_payload(
            revision=2,
            title="Branch B Material",
        )
        second_payload["requirements_added"] = [second_requirement]
        second_material = _handcrafted_git_event(
            event_id="E-cccccccccccc",
            parent_event_id=parent,
            lamport=second_material_lamport,
            action="prd.revised",
            payload=second_payload,
        )
        second_review = _handcrafted_git_event(
            event_id="E-ffffffffffff",
            parent_event_id="E-cccccccccccc",
            lamport=second_material_lamport + 1,
            action="prd.reviewed",
            payload={
                "project_id": "proj-1",
                "expected_revision": 2,
                "expected_status": "draft",
                "reviewer": "branch-b-reviewer",
            },
        )
        second_approval = _handcrafted_git_event(
            event_id="E-999999999999",
            parent_event_id="E-ffffffffffff",
            lamport=second_material_lamport + 2,
            action="prd.approved",
            payload={
                "project_id": "proj-1",
                "expected_revision": 2,
                "expected_status": "reviewed",
                "approver": "branch-b-approver",
            },
        )

        snapshots: list[str] = []
        content_events = (title_revision, first_material, second_material)
        lifecycle_events = (
            first_review,
            first_approval,
            second_review,
            second_approval,
        )
        for index, content_order in enumerate(permutations(content_events)):
            merged = tmp_path / f"asymmetric-material-merged-{index}"
            merged.mkdir()
            events_path = merged / "events.jsonl"
            events_path.write_text(
                "\n".join(prefix + list(content_order) + list(lifecycle_events))
                + "\n",
                encoding="utf-8",
            )
            replayed = _make_backend(merged)
            try:
                for _ in range(2):
                    prd = replayed.get_prd("default")
                    assert prd is not None
                    assert prd.revision == expected_revision
                    assert prd.title == expected_title
                    assert prd.status.value == "approved"
                    assert [
                        requirement.id
                        for requirement in replayed.list_requirements(
                            prd_id="default"
                        )
                    ] == [expected_requirement]
                    conn = replayed._require_conn()  # noqa: SLF001
                    assert conn.execute(
                        "SELECT 1 FROM reviews WHERE reviewed_by = ?",
                        (winning_approver,),
                    ).fetchone() is not None
                    assert conn.execute(
                        "SELECT 1 FROM reviews WHERE reviewed_by = ?",
                        (losing_approver,),
                    ).fetchone() is None
                    snapshots.append(_snap(replayed))
                    replayed.replay_from_empty(str(events_path))
            finally:
                replayed.close()
        assert len(set(snapshots)) == 1

    def test_asymmetric_material_fork_rejects_stale_base_lifecycle(
        self, tmp_path: Path
    ) -> None:
        """A lifecycle sibling cannot bypass a transparent title ancestor."""
        base = tmp_path / "asymmetric-lifecycle-base"
        base.mkdir()
        backend = _make_backend(base)
        try:
            _seed_prd(backend)
        finally:
            backend.close()
        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parent = json.loads(prefix[-1])["id"]

        title_revision = _handcrafted_git_event(
            event_id="E-aaaaaaaaaaaa",
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=_prd_revised_payload(revision=2, title="Transparent Rename"),
        )
        requirement = {
            "id": "R101",
            "prd_section": "requirements",
            "text": "Material descendant contract.",
            "source_paragraph": None,
            "derived": False,
        }
        material_payload = _prd_revised_payload(
            revision=3,
            title="Material Descendant",
        )
        material_payload["requirements_added"] = [requirement]
        material = _handcrafted_git_event(
            event_id="E-bbbbbbbbbbbb",
            parent_event_id="E-aaaaaaaaaaaa",
            lamport=5,
            action="prd.revised",
            payload=material_payload,
        )
        stale_review = _handcrafted_git_event(
            event_id="E-cccccccccccc",
            parent_event_id=parent,
            lamport=10,
            action="prd.reviewed",
            payload={
                "project_id": "proj-1",
                "expected_revision": 1,
                "expected_status": "draft",
                "reviewer": "stale-base-reviewer",
            },
        )
        stale_approval = _handcrafted_git_event(
            event_id="E-dddddddddddd",
            parent_event_id="E-cccccccccccc",
            lamport=11,
            action="prd.approved",
            payload={
                "project_id": "proj-1",
                "expected_revision": 1,
                "expected_status": "reviewed",
                "approver": "stale-base-approver",
            },
        )

        snapshots: list[str] = []
        suffix = (title_revision, material, stale_review, stale_approval)
        for index, physical_order in enumerate(permutations(suffix)):
            merged = tmp_path / f"asymmetric-lifecycle-merged-{index}"
            merged.mkdir()
            events_path = merged / "events.jsonl"
            events_path.write_text(
                "\n".join(prefix + list(physical_order)) + "\n",
                encoding="utf-8",
            )
            replayed = _make_backend(merged)
            try:
                prd = replayed.get_prd("default")
                assert prd is not None
                assert prd.revision == 3
                assert prd.title == "Material Descendant"
                assert prd.status.value == "draft"
                assert [
                    item.id
                    for item in replayed.list_requirements(prd_id="default")
                ] == ["R101"]
                assert replayed._require_conn().execute(  # noqa: SLF001
                    "SELECT 1 FROM reviews WHERE reviewed_by LIKE 'stale-base-%'"
                ).fetchone() is None
                snapshots.append(_snap(replayed))
            finally:
                replayed.close()
        assert len(set(snapshots)) == 1

    def test_material_descendants_of_sibling_noops_choose_one_lineage(
        self, tmp_path: Path
    ) -> None:
        """No-op siblings cannot admit duplicate material descendants."""
        base = tmp_path / "noop-descendant-base"
        base.mkdir()
        backend = _make_backend(base)
        try:
            _seed_prd(backend)
        finally:
            backend.close()
        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parent = json.loads(prefix[-1])["id"]

        first_noop = _handcrafted_git_event(
            event_id="E-aaaaaaaaaaaa",
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=_prd_revised_payload(title="Original PRD"),
        )
        second_noop = _handcrafted_git_event(
            event_id="E-bbbbbbbbbbbb",
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=_prd_revised_payload(title="Original PRD"),
        )
        first_requirement = {
            "id": "R201",
            "prd_section": "requirements",
            "text": "First branch contract.",
            "source_paragraph": None,
            "derived": False,
        }
        second_requirement = {
            **first_requirement,
            "text": "Second branch contract.",
        }
        first_material_payload = _prd_revised_payload(
            revision=3,
            title="First Material",
        )
        first_material_payload["requirements_added"] = [first_requirement]
        first_material = _handcrafted_git_event(
            event_id="E-cccccccccccc",
            parent_event_id="E-aaaaaaaaaaaa",
            lamport=5,
            action="prd.revised",
            payload=first_material_payload,
        )
        second_material_payload = _prd_revised_payload(
            revision=3,
            title="Second Material",
        )
        second_material_payload["requirements_added"] = [second_requirement]
        second_material = _handcrafted_git_event(
            event_id="E-dddddddddddd",
            parent_event_id="E-bbbbbbbbbbbb",
            lamport=5,
            action="prd.revised",
            payload=second_material_payload,
        )

        snapshots: list[str] = []
        events = (first_noop, second_noop, first_material, second_material)
        for index, suffix in enumerate(permutations(events)):
            merged = tmp_path / f"noop-descendant-merged-{index}"
            merged.mkdir()
            events_path = merged / "events.jsonl"
            events_path.write_text(
                "\n".join(prefix + list(suffix)) + "\n",
                encoding="utf-8",
            )
            replayed = _make_backend(merged)
            try:
                for _ in range(2):
                    prd = replayed.get_prd("default")
                    assert prd is not None
                    assert prd.title == "First Material"
                    assert prd.revision == 3
                    assert [
                        (requirement.id, requirement.text)
                        for requirement in replayed.list_requirements(
                            prd_id="default"
                        )
                    ] == [("R201", "First branch contract.")]
                    snapshots.append(_snap(replayed))
                    replayed.replay_from_empty(str(events_path))
            finally:
                replayed.close()
        assert len(set(snapshots)) == 1

    def test_sibling_requirement_edits_choose_one_replay_lineage(
        self, tmp_path: Path
    ) -> None:
        base = tmp_path / "requirement-edit-base"
        base.mkdir()
        requirement = {
            "id": "R001",
            "prd_section": "requirements",
            "text": "Base contract.",
            "source_paragraph": None,
            "derived": False,
        }
        backend = _make_backend(base)
        try:
            backend.append(
                _draft(
                    "project.created",
                    {
                        "id": "proj-1",
                        "name": "Git PRD",
                        "description": "",
                        "created_at": _T0.isoformat(),
                        "updated_at": _T0.isoformat(),
                    },
                )
            )
            backend.append(_draft("state.initialized", {}))
            backend.append(
                _draft(
                    "prd.parsed",
                    _prd_parsed_payload(requirements=[requirement]),
                    target_kind="prd",
                    target_id="default",
                )
            )
        finally:
            backend.close()
        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parent = json.loads(prefix[-1])["id"]

        sibling_events: list[str] = []
        for event_id, title, text in (
            ("E-aaaaaaaaaaaa", "First Edit", "First contract edit."),
            ("E-bbbbbbbbbbbb", "Second Edit", "Second contract edit."),
        ):
            payload = _prd_revised_payload(title=title)
            payload["requirements_unchanged"] = [{**requirement, "text": text}]
            sibling_events.append(
                _handcrafted_git_event(
                    event_id=event_id,
                    parent_event_id=parent,
                    lamport=4,
                    action="prd.revised",
                    payload=payload,
                )
            )

        snapshots: list[str] = []
        for index, suffix in enumerate(
            (sibling_events, list(reversed(sibling_events)))
        ):
            merged = tmp_path / f"requirement-edit-merged-{index}"
            merged.mkdir()
            (merged / "events.jsonl").write_text(
                "\n".join(prefix + suffix) + "\n", encoding="utf-8"
            )
            replayed = _make_backend(merged)
            try:
                prd = replayed.get_prd("default")
                assert prd is not None
                assert prd.title == "First Edit"
                assert prd.revision == 2
                live = replayed.list_requirements(prd_id="default")
                assert [(item.id, item.text) for item in live] == [
                    ("R001", "First contract edit.")
                ]
                snapshots.append(_snap(replayed))
            finally:
                replayed.close()
        assert snapshots[0] == snapshots[1]

    def test_overlapping_requirement_diff_is_rejected_during_replay(
        self, tmp_path: Path
    ) -> None:
        base = tmp_path / "overlapping-diff-base"
        base.mkdir()
        requirement = {
            "id": "R001",
            "prd_section": "requirements",
            "text": "Base contract.",
            "source_paragraph": None,
            "derived": False,
        }
        backend = _make_backend(base)
        try:
            backend.append(
                _draft(
                    "project.created",
                    {
                        "id": "proj-1",
                        "name": "Git PRD",
                        "description": "",
                        "created_at": _T0.isoformat(),
                        "updated_at": _T0.isoformat(),
                    },
                )
            )
            backend.append(_draft("state.initialized", {}))
            backend.append(
                _draft(
                    "prd.parsed",
                    _prd_parsed_payload(requirements=[requirement]),
                    target_kind="prd",
                    target_id="default",
                )
            )
        finally:
            backend.close()
        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        payload = _prd_revised_payload(superseded=[requirement])
        payload["requirements_unchanged"] = [requirement]
        overlapping = _handcrafted_git_event(
            event_id="E-aaaaaaaaaaaa",
            parent_event_id=json.loads(prefix[-1])["id"],
            lamport=4,
            action="prd.revised",
            payload=payload,
        )
        merged = tmp_path / "overlapping-diff-merged"
        merged.mkdir()
        (merged / "events.jsonl").write_text(
            "\n".join(prefix + [overlapping]) + "\n", encoding="utf-8"
        )

        with pytest.raises(TransactionAborted, match="unique and disjoint"):
            _make_backend(merged)

    def test_sibling_summary_edits_choose_one_replay_lineage(
        self, tmp_path: Path
    ) -> None:
        base = tmp_path / "summary-edit-base"
        base.mkdir()
        backend = _make_backend(base)
        try:
            _seed_prd(backend)
        finally:
            backend.close()
        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parent = json.loads(prefix[-1])["id"]

        sibling_events: list[str] = []
        for event_id, title, summary in (
            ("E-aaaaaaaaaaaa", "First Summary", "First summary edit."),
            ("E-bbbbbbbbbbbb", "Second Summary", "Second summary edit."),
        ):
            payload = _prd_revised_payload(title=title)
            payload["summary"] = summary
            sibling_events.append(
                _handcrafted_git_event(
                    event_id=event_id,
                    parent_event_id=parent,
                    lamport=4,
                    action="prd.revised",
                    payload=payload,
                )
            )

        snapshots: list[str] = []
        for index, suffix in enumerate(
            (sibling_events, list(reversed(sibling_events)))
        ):
            merged = tmp_path / f"summary-edit-merged-{index}"
            merged.mkdir()
            (merged / "events.jsonl").write_text(
                "\n".join(prefix + suffix) + "\n", encoding="utf-8"
            )
            replayed = _make_backend(merged)
            try:
                prd = replayed.get_prd("default")
                assert prd is not None
                assert prd.title == "First Summary"
                assert prd.summary == "First summary edit."
                assert prd.revision == 2
                snapshots.append(_snap(replayed))
            finally:
                replayed.close()
        assert snapshots[0] == snapshots[1]

    def test_title_overlay_advances_updated_at(self, tmp_path: Path) -> None:
        base = tmp_path / "overlay-time-base"
        base.mkdir()
        backend = _make_backend(base)
        try:
            _seed_prd(backend)
        finally:
            backend.close()
        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parent = json.loads(prefix[-1])["id"]
        requirement = {
            "id": "R001",
            "prd_section": "requirements",
            "text": "Added material.",
            "source_paragraph": None,
            "derived": False,
        }
        material_payload = _prd_revised_payload(title="Material")
        material_payload["requirements_added"] = [requirement]
        material = _handcrafted_git_event(
            event_id="E-aaaaaaaaaaaa",
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=material_payload,
            timestamp_offset=10,
        )
        title_only = _handcrafted_git_event(
            event_id="E-bbbbbbbbbbbb",
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=_prd_revised_payload(title="Later Rename"),
            timestamp_offset=20,
        )
        merged = tmp_path / "overlay-time-merged"
        merged.mkdir()
        (merged / "events.jsonl").write_text(
            "\n".join(prefix + [material, title_only]) + "\n", encoding="utf-8"
        )

        replayed = _make_backend(merged)
        try:
            prd = replayed.get_prd("default")
            assert prd is not None
            assert prd.title == "Later Rename"
            assert prd.updated_at == _T0 + timedelta(seconds=20)
        finally:
            replayed.close()

    def test_title_overlay_canonicalizes_equivalent_requirement_shapes(
        self, tmp_path: Path
    ) -> None:
        """Omitted Requirement defaults must equal their explicit form."""
        base = tmp_path / "overlay-requirement-shape-base"
        base.mkdir()
        backend = _make_backend(base)
        minimal_requirement = {
            "id": "R001",
            "prd_section": "requirements",
            "text": "Existing requirement.",
        }
        try:
            _seed_prd(backend)
            backend.append(
                _draft(
                    "prd.parsed",
                    _prd_parsed_payload(
                        title="Base",
                        expected_absent=None,
                        requirements=[minimal_requirement],
                    ),
                    target_kind="prd",
                    target_id="default",
                )
            )
        finally:
            backend.close()

        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parent = json.loads(prefix[-1])["id"]
        material_payload = _prd_revised_payload(
            title="Base",
            superseded=[minimal_requirement],
        )
        material = _handcrafted_git_event(
            event_id="E-aaaaaaaaaaaa",
            parent_event_id=parent,
            lamport=5,
            action="prd.revised",
            payload=material_payload,
        )

        title_payload = _prd_revised_payload(title="Concurrent Rename")
        title_payload["requirements_unchanged"] = [
            {
                **minimal_requirement,
                "source_paragraph": None,
                "derived": False,
            }
        ]
        title_only = _handcrafted_git_event(
            event_id="E-bbbbbbbbbbbb",
            parent_event_id=parent,
            lamport=5,
            action="prd.revised",
            payload=title_payload,
        )

        snapshots: list[str] = []
        for index, suffix in enumerate(
            ([material, title_only], [title_only, material])
        ):
            merged = tmp_path / f"overlay-requirement-shape-{index}"
            merged.mkdir()
            (merged / "events.jsonl").write_text(
                "\n".join(prefix + suffix) + "\n", encoding="utf-8"
            )
            replayed = _make_backend(merged)
            try:
                prd = replayed.get_prd("default")
                assert prd is not None
                assert prd.title == "Concurrent Rename"
                assert replayed.list_requirements(prd_id="default") == []
                snapshots.append(_snap(replayed))
            finally:
                replayed.close()
        assert snapshots[0] == snapshots[1]

    def test_prd_policy_memory_is_linear_for_additive_requirement_history(
        self, tmp_path: Path
    ) -> None:
        backend = _make_backend(tmp_path)
        events = [
            Event(
                id="E-000000000001",
                parent_event_id=None,
                lamport=1,
                timestamp=_T0,
                actor="scale",
                action="prd.parsed",
                target_kind="prd",
                target_id="default",
                payload_json=_prd_parsed_payload(),
            )
        ]
        parent = events[0].id
        for revision in range(2, 5002):
            requirement = {
                "id": f"R{revision:04d}",
                "prd_section": "requirements",
                "text": f"Requirement {revision}.",
                "source_paragraph": None,
                "derived": False,
            }
            payload = _prd_revised_payload(revision=revision)
            payload["requirements_added"] = [requirement]
            event = Event(
                id=f"E-{revision:012x}",
                parent_event_id=parent,
                lamport=revision,
                timestamp=_T0 + timedelta(microseconds=revision),
                actor="scale",
                action="prd.revised",
                target_kind="prd",
                target_id="default",
                payload_json=payload,
            )
            events.append(event)
            parent = event.id

        tracemalloc.start()
        started = time.perf_counter()
        try:
            policy = backend._build_git_prd_replay_policy(events)  # noqa: SLF001
            _current, peak = tracemalloc.get_traced_memory()
            elapsed = time.perf_counter() - started
        finally:
            tracemalloc.stop()
            backend.close()
        assert policy.content_heads["default"] == (5001, parent)
        assert peak < 24 * 1024 * 1024
        assert elapsed < 10

    def test_prd_policy_scales_for_deep_material_lifecycle_history(
        self, tmp_path: Path
    ) -> None:
        """Repeated material/review/approval history must avoid cubic walks."""
        backend = _make_backend(tmp_path)
        events = [
            Event(
                id="E-000000000001",
                parent_event_id=None,
                lamport=1,
                timestamp=_T0,
                actor="scale",
                action="prd.parsed",
                target_kind="prd",
                target_id="default",
                payload_json=_prd_parsed_payload(title="Revision 1"),
            )
        ]
        parent = events[0].id
        lamport = 1
        for revision in range(2, 1202):
            lamport += 1
            revised = Event(
                id=f"E-{lamport:012x}",
                parent_event_id=parent,
                lamport=lamport,
                timestamp=_T0 + timedelta(microseconds=lamport),
                actor="scale",
                action="prd.revised",
                target_kind="prd",
                target_id="default",
                payload_json=_prd_revised_payload(
                    revision=revision,
                    expected_status="approved" if revision > 2 else "draft",
                    superseded=[{"id": f"R{revision:04d}"}],
                ),
            )
            events.append(revised)
            lamport += 1
            reviewed = Event(
                id=f"E-{lamport:012x}",
                parent_event_id=revised.id,
                lamport=lamport,
                timestamp=_T0 + timedelta(microseconds=lamport),
                actor="scale",
                action="prd.reviewed",
                target_kind="prd",
                target_id="default",
                payload_json={
                    "project_id": "proj-1",
                    "expected_revision": revision,
                    "reviewer": "scale",
                },
            )
            events.append(reviewed)
            lamport += 1
            approved = Event(
                id=f"E-{lamport:012x}",
                parent_event_id=reviewed.id,
                lamport=lamport,
                timestamp=_T0 + timedelta(microseconds=lamport),
                actor="scale",
                action="prd.approved",
                target_kind="prd",
                target_id="default",
                payload_json={
                    "project_id": "proj-1",
                    "expected_revision": revision,
                    "approver": "scale",
                },
            )
            events.append(approved)
            parent = approved.id

        started = time.perf_counter()
        try:
            policy = backend._build_git_prd_replay_policy(events)  # noqa: SLF001
        finally:
            backend.close()
        assert policy.content_heads["default"] == (1201, events[-3].id)
        assert time.perf_counter() - started < 10

    def test_cached_prd_head_avoids_full_log_scan_during_append(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A normal append hashes bytes but does not reparse/rebuild replay policy."""
        source = tmp_path / "cached-head-source"
        source.mkdir()
        backend = _make_backend(source)
        try:
            backend.append(
                _draft(
                    "project.created",
                    {
                        "id": "proj-1",
                        "name": "Git PRD",
                        "description": "",
                        "created_at": _T0.isoformat(),
                        "updated_at": _T0.isoformat(),
                    },
                )
            )
            backend.append(_draft("state.initialized", {}))
            backend.append(
                _draft(
                    "prd.parsed",
                    _prd_parsed_payload(title="Revision 1"),
                    target_kind="prd",
                    target_id="default",
                )
            )
            for revision in range(2, 41):
                backend.append(
                    _draft(
                        "prd.revised",
                        _prd_revised_payload(
                            revision=revision,
                            title=f"Revision {revision}",
                        ),
                        target_kind="prd",
                        target_id="default",
                        ts=_T0 + timedelta(microseconds=revision),
                    )
                )
        finally:
            backend.close()

        reopened = _make_backend(source)
        try:
            def forbidden_scan() -> list[Event]:
                pytest.fail("cached append unexpectedly scanned the full Git log")

            monkeypatch.setattr(reopened, "_read_git_events_ordered", forbidden_scan)
            prior_head = reopened._git_prd_content_heads["default"][1]  # noqa: SLF001
            appended = reopened.append(
                _draft(
                    "prd.revised",
                    _prd_revised_payload(
                        revision=41,
                        title="Revision 41",
                    ),
                    target_kind="prd",
                    target_id="default",
                    ts=_T0 + timedelta(microseconds=41),
                )
            )
            assert appended is not None
            assert appended.parent_event_id == prior_head
            assert reopened._git_prd_content_heads["default"] == (  # noqa: SLF001
                41,
                appended.id,
            )
            assert reopened.get_prd("default").revision == 41  # type: ignore[union-attr]
        finally:
            reopened.close()

    def test_prd_parent_cycle_fails_closed_before_projection(
        self, tmp_path: Path
    ) -> None:
        first = Event(
            id="E-aaaaaaaaaaaa",
            parent_event_id="E-bbbbbbbbbbbb",
            lamport=1,
            timestamp=_T0,
            actor="cycle",
            action="prd.parsed",
            target_kind="prd",
            target_id="default",
            payload_json=_prd_parsed_payload(),
        )
        second = Event(
            id="E-bbbbbbbbbbbb",
            parent_event_id="E-aaaaaaaaaaaa",
            lamport=2,
            timestamp=_T0 + timedelta(seconds=1),
            actor="cycle",
            action="prd.revised",
            target_kind="prd",
            target_id="default",
            payload_json=_prd_revised_payload(),
        )
        (tmp_path / "events.jsonl").write_text(
            first.model_dump_json() + "\n" + second.model_dump_json() + "\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="parent_event_id cycle"):
            _make_backend(tmp_path)

    def test_newer_material_title_outranks_older_title_only_overlay(
        self, tmp_path: Path
    ) -> None:
        """Sequential mixed revisions converge in either physical file order."""
        requirement = {
            "id": "R001",
            "prd_section": "requirements",
            "text": "Original requirement.",
            "source_paragraph": None,
            "derived": False,
        }
        source = tmp_path / "mixed-title-source"
        source.mkdir()
        backend = _make_backend(source)
        try:
            backend.append(
                _draft(
                    "project.created",
                    {
                        "id": "proj-1",
                        "name": "Git PRD",
                        "description": "",
                        "created_at": _T0.isoformat(),
                        "updated_at": _T0.isoformat(),
                    },
                )
            )
            backend.append(_draft("state.initialized", {}))
            backend.append(
                _draft(
                    "prd.parsed",
                    _prd_parsed_payload(requirements=[requirement]),
                    target_kind="prd",
                    target_id="default",
                )
            )
            title_payload = _prd_revised_payload(title="Title Rev2")
            title_payload["requirements_unchanged"] = [requirement]
            title_revision = backend.append(
                _draft(
                    "prd.revised",
                    title_payload,
                    target_kind="prd",
                    target_id="default",
                    ts=_T0 + timedelta(seconds=2),
                )
            )
            material_revision = backend.append(
                _draft(
                    "prd.revised",
                    _prd_revised_payload(
                        revision=3,
                        title="Title Rev3 Material",
                        superseded=[requirement],
                    ),
                    target_kind="prd",
                    target_id="default",
                    ts=_T0 + timedelta(seconds=3),
                )
            )
            assert title_revision is not None and material_revision is not None
            assert material_revision.parent_event_id == title_revision.id
            live = backend.get_prd("default")
            assert live is not None
            assert live.title == "Title Rev3 Material"
            assert live.revision == 3
            expected = _snap(backend)
        finally:
            backend.close()

        lines = (source / "events.jsonl").read_text(encoding="utf-8").splitlines()
        prefix, revisions = lines[:-2], lines[-2:]
        for index, suffix in enumerate((revisions, list(reversed(revisions)))):
            replay_dir = tmp_path / f"mixed-title-replay-{index}"
            replay_dir.mkdir()
            events_path = replay_dir / "events.jsonl"
            events_path.write_text(
                "\n".join(prefix + suffix) + "\n",
                encoding="utf-8",
            )
            replayed = _make_backend(replay_dir)
            try:
                for _ in range(2):
                    current = replayed.get_prd("default")
                    assert current is not None
                    assert current.title == "Title Rev3 Material"
                    assert current.revision == 3
                    assert replayed.list_requirements(prd_id="default") == []
                    assert _snap(replayed) == expected
                    replayed.replay_from_empty(str(events_path))
            finally:
                replayed.close()

    def test_approval_descended_from_material_revision_promotes(
        self, tmp_path: Path
    ) -> None:
        requirement = {
            "id": "R001",
            "prd_section": "requirements",
            "text": "Original requirement.",
            "source_paragraph": None,
            "derived": False,
        }
        base = tmp_path / "base-descendant"
        base.mkdir()
        backend = _make_backend(base)
        try:
            backend.append(
                _draft(
                    "project.created",
                    {
                        "id": "proj-1",
                        "name": "Git PRD",
                        "description": "",
                        "created_at": _T0.isoformat(),
                        "updated_at": _T0.isoformat(),
                    },
                )
            )
            backend.append(_draft("state.initialized", {}))
            backend.append(
                _draft(
                    "prd.parsed",
                    _prd_parsed_payload(requirements=[requirement]),
                    target_kind="prd",
                    target_id="default",
                )
            )
        finally:
            backend.close()
        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parent = json.loads(prefix[-1])["id"]
        material = _handcrafted_git_event(
            event_id="E-aaaaaaaaaaaa",
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=_prd_revised_payload(superseded=[requirement]),
        )
        title_payload = _prd_revised_payload(title="Reviewed Rename")
        title_payload["requirements_unchanged"] = [requirement]
        title_revision = _handcrafted_git_event(
            event_id="E-bbbbbbbbbbbb",
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=title_payload,
        )
        review = _handcrafted_git_event(
            event_id="E-cccccccccccc",
            parent_event_id="E-aaaaaaaaaaaa",
            lamport=5,
            action="prd.reviewed",
            payload={
                "project_id": "proj-1",
                "expected_revision": 2,
                "reviewer": "causal-reviewer",
            },
        )
        approval = _handcrafted_git_event(
            event_id="E-dddddddddddd",
            parent_event_id="E-cccccccccccc",
            lamport=6,
            action="prd.approved",
            payload={
                "project_id": "proj-1",
                "expected_revision": 2,
                "approver": "causal-approver",
            },
        )
        merged = tmp_path / "material-descendant"
        merged.mkdir()
        (merged / "events.jsonl").write_text(
            "\n".join(prefix + [approval, title_revision, material, review]) + "\n",
            encoding="utf-8",
        )
        replayed = _make_backend(merged)
        try:
            prd = replayed.get_prd("default")
            assert prd is not None
            assert prd.title == "Reviewed Rename"
            assert prd.revision == 2
            assert prd.status.value == "approved"
            assert replayed.list_requirements(prd_id="default") == []
            row = replayed._require_conn().execute(  # noqa: SLF001
                "SELECT reviewed_by FROM reviews WHERE id = ?",
                ("RV-E-dddddddddddd",),
            ).fetchone()
            assert row is not None and row[0] == "causal-approver"
        finally:
            replayed.close()

    def test_lifecycle_revision_binding_rejects_stale_declared_revision(
        self, tmp_path: Path
    ) -> None:
        """Causal descent cannot compensate for a stale revision precondition."""
        base = tmp_path / "stale-lifecycle-base"
        base.mkdir()
        backend = _make_backend(base)
        try:
            _seed_prd(backend)
        finally:
            backend.close()
        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parent = json.loads(prefix[-1])["id"]
        material_payload = _prd_revised_payload(revision=2)
        material_payload["assumptions"] = [
            {
                "id": "A001",
                "statement": "Material revision.",
                "rationale": "Makes the revision non-title-only.",
                "requirement_ids": [],
            }
        ]
        material = _handcrafted_git_event(
            event_id="E-aaaaaaaaaaaa",
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=material_payload,
        )
        stale_review = _handcrafted_git_event(
            event_id="E-bbbbbbbbbbbb",
            parent_event_id="E-aaaaaaaaaaaa",
            lamport=5,
            action="prd.reviewed",
            payload={
                "project_id": "proj-1",
                "expected_revision": 1,
                "reviewer": "stale-reviewer",
            },
        )
        stale_approval = _handcrafted_git_event(
            event_id="E-cccccccccccc",
            parent_event_id="E-bbbbbbbbbbbb",
            lamport=6,
            action="prd.approved",
            payload={
                "project_id": "proj-1",
                "expected_revision": 1,
                "approver": "stale-approver",
            },
        )
        merged = tmp_path / "stale-lifecycle-replay"
        merged.mkdir()
        events_path = merged / "events.jsonl"
        events_path.write_text(
            "\n".join(prefix + [material, stale_review, stale_approval]) + "\n",
            encoding="utf-8",
        )

        replayed = _make_backend(merged)
        try:
            prd = replayed.get_prd("default")
            assert prd is not None
            assert prd.revision == 2
            assert prd.status.value == "draft"
            assert replayed._require_conn().execute(  # noqa: SLF001
                "SELECT 1 FROM reviews WHERE reviewed_by LIKE 'stale-%'"
            ).fetchone() is None
            replayed.replay_from_empty(str(events_path))
            assert replayed.get_prd("default").status.value == "draft"
        finally:
            replayed.close()

    def test_losing_current_first_parse_descendant_lifecycle_is_ignored(
        self, tmp_path: Path
    ) -> None:
        base = tmp_path / "base-first-parse"
        base.mkdir()
        backend = _make_backend(base)
        try:
            backend.append(
                _draft(
                    "project.created",
                    {
                        "id": "proj-1",
                        "name": "Git PRD",
                        "description": "",
                        "created_at": _T0.isoformat(),
                        "updated_at": _T0.isoformat(),
                    },
                )
            )
            backend.append(_draft("state.initialized", {}))
        finally:
            backend.close()
        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parent = json.loads(prefix[-1])["id"]
        winner = _handcrafted_git_event(
            event_id="E-aaaaaaaaaaaa",
            parent_event_id=parent,
            lamport=3,
            action="prd.parsed",
            payload=_prd_parsed_payload(title="Winning Content"),
        )
        loser = _handcrafted_git_event(
            event_id="E-bbbbbbbbbbbb",
            parent_event_id=parent,
            lamport=3,
            action="prd.parsed",
            payload=_prd_parsed_payload(title="Losing Content"),
        )
        review = _handcrafted_git_event(
            event_id="E-cccccccccccc",
            parent_event_id="E-bbbbbbbbbbbb",
            lamport=4,
            action="prd.reviewed",
            payload={
                "project_id": "proj-1",
                "expected_revision": 1,
                "reviewer": "losing-reviewer",
            },
        )
        approval = _handcrafted_git_event(
            event_id="E-dddddddddddd",
            parent_event_id="E-cccccccccccc",
            lamport=5,
            action="prd.approved",
            payload={
                "project_id": "proj-1",
                "expected_revision": 1,
                "approver": "losing-approver",
            },
        )
        snapshots: list[str] = []
        for index, suffix in enumerate(
            ([winner, loser, review, approval], [approval, review, loser, winner])
        ):
            merged = tmp_path / f"first-parse-lineage-{index}"
            merged.mkdir()
            (merged / "events.jsonl").write_text(
                "\n".join(prefix + list(suffix)) + "\n", encoding="utf-8"
            )
            replayed = _make_backend(merged)
            try:
                prd = replayed.get_prd("default")
                assert prd is not None
                assert prd.title == "Winning Content"
                assert prd.status.value == "draft"
                assert replayed._require_conn().execute(  # noqa: SLF001
                    "SELECT 1 FROM reviews WHERE id = ?",
                    ("RV-E-dddddddddddd",),
                ).fetchone() is None
                snapshots.append(_snap(replayed))
            finally:
                replayed.close()
        assert snapshots[0] == snapshots[1]

    def test_bounded_replay_policy_does_not_consult_future_material_revision(
        self, tmp_path: Path
    ) -> None:
        requirement = {
            "id": "R001",
            "prd_section": "requirements",
            "text": "Original requirement.",
            "source_paragraph": None,
            "derived": False,
        }
        base = tmp_path / "base-bounded-policy"
        base.mkdir()
        backend = _make_backend(base)
        try:
            backend.append(
                _draft(
                    "project.created",
                    {
                        "id": "proj-1",
                        "name": "Git PRD",
                        "description": "",
                        "created_at": _T0.isoformat(),
                        "updated_at": _T0.isoformat(),
                    },
                )
            )
            backend.append(_draft("state.initialized", {}))
            backend.append(
                _draft(
                    "prd.parsed",
                    _prd_parsed_payload(requirements=[requirement]),
                    target_kind="prd",
                    target_id="default",
                )
            )
        finally:
            backend.close()
        prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parent = json.loads(prefix[-1])["id"]
        title_payload = _prd_revised_payload(title="Prefix Rename")
        title_payload["requirements_unchanged"] = [requirement]
        title_revision = _handcrafted_git_event(
            event_id="E-aaaaaaaaaaaa",
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=title_payload,
        )
        material = _handcrafted_git_event(
            event_id="E-bbbbbbbbbbbb",
            parent_event_id=parent,
            lamport=4,
            action="prd.revised",
            payload=_prd_revised_payload(superseded=[requirement]),
        )
        merged = tmp_path / "bounded-policy"
        merged.mkdir()
        events_path = merged / "events.jsonl"
        events_path.write_text(
            "\n".join(prefix + [material, title_revision]) + "\n",
            encoding="utf-8",
        )
        replayed = _make_backend(merged)
        try:
            replayed.replay_to_event_id(str(events_path), "E-aaaaaaaaaaaa")
            prd = replayed.get_prd("default")
            assert prd is not None
            assert prd.title == "Prefix Rename"
            assert prd.revision == 2
            assert [
                requirement.id
                for requirement in replayed.list_requirements(prd_id="default")
            ] == ["R001"]
        finally:
            replayed.close()


# ---------------------------------------------------------------------------
# 3. Divergent-merge simulation
# ---------------------------------------------------------------------------


def _fork_and_claim(
    tmp_path: Path,
    *,
    claim_id_a: str,
    claim_id_b: str,
) -> tuple[list[str], list[str], list[str]]:
    """Build (prefix, suffix_a, suffix_b) line lists.

    Base project seeds the 7-event prefix; branch A and branch B each start
    from a copy of that log (a fork) and independently append one
    ``claim.created`` on T001 — A at T0+60s by agent-a, B at T0+120s by
    agent-b. Because each writer saw only the prefix, both claims carry
    lamport 8 (a tie) and the same parent — exactly what two branches
    produce before a merge.
    """
    base = tmp_path / "base"
    base.mkdir()
    b = _make_backend(base)
    try:
        _seed_ready_task(b)
    finally:
        b.close()
    prefix = (base / "events.jsonl").read_text(encoding="utf-8").splitlines()

    suffixes: dict[str, list[str]] = {}
    for branch, claim_id, actor, offset in (
        ("a", claim_id_a, "agent-a", 60),
        ("b", claim_id_b, "agent-b", 120),
    ):
        branch_dir = tmp_path / f"branch-{branch}"
        branch_dir.mkdir()
        shutil.copy(base / "events.jsonl", branch_dir / "events.jsonl")
        bb = _make_backend(branch_dir)
        try:
            ts = _T0 + timedelta(seconds=offset)
            bb.append(
                _draft(
                    "claim.created",
                    _claim_payload(claim_id, claimed_by=actor, ts=ts),
                    target_kind="claim",
                    target_id=claim_id,
                    ts=ts,
                    actor=actor,
                )
            )
        finally:
            bb.close()
        all_lines = (
            (branch_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        )
        suffixes[branch] = all_lines[len(prefix) :]
    return prefix, suffixes["a"], suffixes["b"]


def _replay_merged(tmp_path: Path, name: str, lines: list[str]) -> SqliteBackend:
    merged_dir = tmp_path / name
    merged_dir.mkdir()
    (merged_dir / "events.jsonl").write_text(
        "".join(line + "\n" for line in lines), encoding="utf-8"
    )
    return _make_backend(merged_dir)


class TestDivergentMerge:
    def test_both_union_orders_converge_to_identical_state(
        self, tmp_path: Path
    ) -> None:
        """prefix+A+B and prefix+B+A replay to byte-identical snapshots."""
        prefix, sa, sb = _fork_and_claim(
            tmp_path, claim_id_a="C-A1", claim_id_b="C-B1"
        )
        # Sanity: the fork produced a genuine lamport tie with one parent.
        claim_a, claim_b = json.loads(sa[0]), json.loads(sb[0])
        assert claim_a["lamport"] == claim_b["lamport"] == 8
        assert claim_a["parent_event_id"] == claim_b["parent_event_id"]
        assert claim_a["id"] != claim_b["id"]

        m1 = _replay_merged(tmp_path, "m1", prefix + sa + sb)
        m2 = _replay_merged(tmp_path, "m2", prefix + sb + sa)
        try:
            assert _snap(m1) == _snap(m2)
        finally:
            m1.close()
            m2.close()
        # Display order is HLC order in both, regardless of file order.
        assert _events_table(tmp_path / "m1") == _events_table(tmp_path / "m2")

    def test_earliest_claim_wins_the_task_transition(self, tmp_path: Path) -> None:
        """Distinct claim ids: both rows land, but the earliest one claimed it.

        Replay applies A (T0+60s) before B (T0+120s); A's ready→claimed
        UPDATE wins and stamps the task; B's UPDATE no-ops on the guard. Both
        claim rows persist as active — surfacing the loser as
        ``claim.superseded`` is deliberately Phase B (reconciler), NOT built
        here.
        """
        prefix, sa, sb = _fork_and_claim(
            tmp_path, claim_id_a="C-A1", claim_id_b="C-B1"
        )
        m = _replay_merged(tmp_path, "merged", prefix + sb + sa)  # B first in file!
        try:
            task = m.get_task("T001")
            assert task is not None
            assert task.status == "claimed"
            # The winner's timestamp is on the task — earliest (lamport, ts, id).
            assert task.updated_at == _T0 + timedelta(seconds=60)
            claims = {c.id: c for c in m.list_claims()}
            assert set(claims) == {"C-A1", "C-B1"}
        finally:
            m.close()

    def test_colliding_claim_ids_resolve_to_earliest_writer(
        self, tmp_path: Path
    ) -> None:
        """Same claim id on both branches (realistic per-machine counters).

        Phase A does not hash ENTITY ids, so two branches both mint C001.
        Replay's INSERT OR IGNORE keeps the first-applied row — the earliest
        (lamport, ts, id) event — so the surviving claim is deterministic in
        both union orders.
        """
        prefix, sa, sb = _fork_and_claim(
            tmp_path, claim_id_a="C001", claim_id_b="C001"
        )
        m1 = _replay_merged(tmp_path, "m1", prefix + sa + sb)
        m2 = _replay_merged(tmp_path, "m2", prefix + sb + sa)
        try:
            for m in (m1, m2):
                claims = m.list_claims()
                assert len(claims) == 1
                assert claims[0].id == "C001"
                assert claims[0].claimed_by == "agent-a"  # earliest ts wins
        finally:
            m1.close()
            m2.close()

    def test_append_after_merge_continues_the_chain(self, tmp_path: Path) -> None:
        """A writer on the merged log links to the file tail and bumps lamport."""
        prefix, sa, sb = _fork_and_claim(
            tmp_path, claim_id_a="C-A1", claim_id_b="C-B1"
        )
        m = _replay_merged(tmp_path, "merged", prefix + sa + sb)
        try:
            ts = _T0 + timedelta(seconds=180)
            event = m.append(
                _draft(
                    "progress.noted",
                    {
                        "task_id": "T001",
                        "actor": "test",
                        "notes": "post-merge",
                        "noted_at": ts.isoformat(),
                    },
                    target_kind="task",
                    target_id="T001",
                    ts=ts,
                )
            )
        finally:
            m.close()
        assert event is not None
        # Parent = last FILE line (B's claim in this union order); lamport =
        # max-seen (8, the tie) + 1.
        assert event.parent_event_id == json.loads(sb[0])["id"]
        assert event.lamport == 9


# ---------------------------------------------------------------------------
# Fresh-clone convergence (initialize() heals the projection)
# ---------------------------------------------------------------------------


class TestGitConvergenceOnInitialize:
    def test_reopen_rejects_append_after_ordered_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Convergence never caches bytes appended after its ordered read."""
        seeded = _make_backend(tmp_path)
        try:
            _seed_ready_task(seeded)
        finally:
            seeded.close()

        events_path = tmp_path / "events.jsonl"
        injected_id = "E-facefeedface"
        reopening = SqliteBackend(
            db_path=str(tmp_path / "state.db"),
            events_path=str(events_path),
            clock=FrozenClock(_T0),
            events_storage="git",
        )
        original_reader = reopening._read_git_events_ordered  # noqa: SLF001

        def read_then_append(*, context: str = "git append parent scan") -> list[Event]:
            ordered = original_reader(context=context)
            _append_external_initialized(events_path, injected_id)
            return ordered

        monkeypatch.setattr(
            reopening,
            "_read_git_events_ordered",
            read_then_append,
        )
        with pytest.raises(
            TransactionAborted,
            match="events.jsonl changed while its material was being projected",
        ):
            reopening.initialize()
        assert reopening._git_validated_log_signature is None  # noqa: SLF001
        assert reopening._require_conn().execute(  # noqa: SLF001
            "SELECT 1 FROM events WHERE id = ?", (injected_id,)
        ).fetchone() is None
        reopening.close()

        converged = _make_backend(tmp_path)
        try:
            assert converged._require_conn().execute(  # noqa: SLF001
                "SELECT 1 FROM events WHERE id = ?", (injected_id,)
            ).fetchone() is not None
        finally:
            converged.close()

    def test_replay_rejects_append_during_projection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Full replay caches only the source snapshot it actually projected."""
        backend = _make_backend(tmp_path)
        _seed_ready_task(backend)
        events_path = tmp_path / "events.jsonl"
        injected_id = "E-deadfeedbeef"
        original_policy = backend._build_git_prd_replay_policy  # noqa: SLF001
        injected = False

        def policy_then_append(ordered: list[Event]) -> Any:
            nonlocal injected
            policy = original_policy(ordered)
            if not injected:
                injected = True
                _append_external_initialized(events_path, injected_id)
            return policy

        monkeypatch.setattr(
            backend,
            "_build_git_prd_replay_policy",
            policy_then_append,
        )
        with pytest.raises(
            TransactionAborted,
            match="events source changed while the projection was being rebuilt",
        ):
            backend.replay_from_empty(str(events_path))
        assert backend._git_validated_log_signature is None  # noqa: SLF001
        assert backend._require_conn().execute(  # noqa: SLF001
            "SELECT 1 FROM events WHERE id = ?", (injected_id,)
        ).fetchone() is None
        backend.close()

        converged = _make_backend(tmp_path)
        try:
            assert converged._require_conn().execute(  # noqa: SLF001
                "SELECT 1 FROM events WHERE id = ?", (injected_id,)
            ).fetchone() is not None
        finally:
            converged.close()

    def test_fresh_clone_builds_projection_from_log(self, tmp_path: Path) -> None:
        """events.jsonl present + no state.db (a clone) → initialize rebuilds."""
        src = tmp_path / "src"
        src.mkdir()
        b = _make_backend(src)
        try:
            _seed_ready_task(b)
            expected = _snap(b)
        finally:
            b.close()

        clone = tmp_path / "clone"
        clone.mkdir()
        shutil.copy(src / "events.jsonl", clone / "events.jsonl")
        b2 = _make_backend(clone)
        try:
            assert _snap(b2) == expected
        finally:
            b2.close()

    def test_converged_projection_is_not_rebuilt(self, tmp_path: Path) -> None:
        """Set-equal log/table → reopen does not delete/recreate the db file.

        mtime is useless here (merely opening SQLite touches the file), but a
        rebuild goes through os.remove + create, which allocates a new inode.
        """
        b = _make_backend(tmp_path)
        try:
            _seed_ready_task(b)
        finally:
            b.close()
        inode_before = os.stat(tmp_path / "state.db").st_ino
        b2 = _make_backend(tmp_path)
        b2.close()
        assert os.stat(tmp_path / "state.db").st_ino == inode_before


# ---------------------------------------------------------------------------
# 4. migrate-events CLI
# ---------------------------------------------------------------------------


def _build_local_project(project_dir: Path) -> str:
    """A local-mode project whose log is the committed replay fixture.

    Returns the pre-migration serialize_state JSON. The fixture exercises the
    full event vocabulary (claims, evidence, task.applied review, sync
    mapping), so the round-trip check covers event-id-derived state (the
    RV-E{n} review ids) — exactly what the id mapping exists for.
    """
    state_dir = project_dir / ".anvil"
    state_dir.mkdir(parents=True)
    (state_dir / "config.yaml").write_text(
        "project_name: 'Migrate Me'\nproject_id: 'proj-1'\n",
        encoding="utf-8",
    )
    shutil.copy(_FIXTURE_EVENTS, state_dir / "events.jsonl")
    # Build the projection (initialize forward-catches-up from the log).
    b = SqliteBackend(
        db_path=str(state_dir / "state.db"),
        events_path=str(state_dir / "events.jsonl"),
        clock=FrozenClock(_T0),
        events_storage="local",
    )
    b.initialize()
    try:
        return _snap(b)
    finally:
        b.close()


def _run_in(project_dir: Path, args: list[str]) -> Any:
    """Invoke the CLI with cwd switched to *project_dir* (commands use Path.cwd)."""
    original = os.getcwd()
    os.chdir(project_dir)
    try:
        return runner.invoke(app, args, catch_exceptions=False)
    finally:
        os.chdir(original)


def _map_pre_state_ids(pre_state: str, id_mapping: dict[str, str]) -> str:
    """Rewrite event-id-derived bits of a snapshot through the id mapping.

    The only event-id-derived canonical state is review ids (``RV-E{n}``,
    assigned from the task.applied / prd.approved event id at write time).
    Entity ids (T/F/C/EV) are caller-assigned payload data and unaffected by
    Phase A.
    """
    snap = json.loads(pre_state)
    for review in snap["reviews"]:
        old_event_id = review["id"].removeprefix("RV-")
        review["id"] = "RV-" + id_mapping[old_event_id]
    return json.dumps(snap, sort_keys=True)


class TestMigrateEvents:
    def test_dry_run_is_the_default_and_writes_nothing(self, tmp_path: Path) -> None:
        _build_local_project(tmp_path)
        state_dir = tmp_path / ".anvil"
        log_before = (state_dir / "events.jsonl").read_bytes()
        config_before = (state_dir / "config.yaml").read_text(encoding="utf-8")

        result = _run_in(tmp_path, ["migrate-events", "--to", "git"])

        assert result.exit_code == 0, result.output
        assert "Dry run" in result.output
        assert ".gitignore guidance" in result.output
        assert (state_dir / "events.jsonl").read_bytes() == log_before
        assert (state_dir / "config.yaml").read_text(encoding="utf-8") == config_before
        assert not (state_dir / ".gitattributes").exists()
        assert not (state_dir / "id_mapping.json").exists()

    def test_yes_applies_full_migration(self, tmp_path: Path) -> None:
        pre_state = _build_local_project(tmp_path)
        state_dir = tmp_path / ".anvil"
        old_ids = [line["id"] for line in _log_lines(state_dir)]

        result = _run_in(tmp_path, ["migrate-events", "--to", "git", "--yes"])
        assert result.exit_code == 0, result.output

        # Log rewritten: hash ids, linear chain, lamport 1..N, order preserved.
        lines = _log_lines(state_dir)
        assert len(lines) == len(old_ids) == 24
        assert all(_HASH_ID_RE.fullmatch(line["id"]) for line in lines)
        assert lines[0]["parent_event_id"] is None
        for prev, cur in zip(lines, lines[1:], strict=False):
            assert cur["parent_event_id"] == prev["id"]
        assert [line["lamport"] for line in lines] == list(range(1, 25))
        assert [line["action"] for line in lines] == [
            json.loads(raw)["action"]
            for raw in _FIXTURE_EVENTS.read_text(encoding="utf-8").splitlines()
        ]

        # id_mapping: bijective old → new, covering every original id.
        id_mapping = json.loads(
            (state_dir / "id_mapping.json").read_text(encoding="utf-8")
        )
        assert sorted(id_mapping) == sorted(old_ids)
        assert sorted(id_mapping.values()) == sorted(line["id"] for line in lines)

        # Side files + config flip + backup.
        assert "events.jsonl merge=union" in (
            (state_dir / ".gitattributes").read_text(encoding="utf-8")
        )
        config = yaml.safe_load((state_dir / "config.yaml").read_text(encoding="utf-8"))
        assert config["events_storage"] == "git"
        assert (state_dir / "events.jsonl.pre-git-migration.bak").exists()

        # ROUND TRIP: replaying the migrated log reproduces the
        # pre-migration state modulo ids (mapped via id_mapping).
        b = SqliteBackend(
            db_path=str(state_dir / "state.db"),
            events_path=str(state_dir / "events.jsonl"),
            clock=FrozenClock(_T0),
            events_storage="git",
        )
        b.initialize()
        try:
            post_state = _snap(b)
        finally:
            b.close()
        assert post_state == _map_pre_state_ids(pre_state, id_mapping)

    def test_second_run_is_an_idempotent_no_op(self, tmp_path: Path) -> None:
        _build_local_project(tmp_path)
        first = _run_in(tmp_path, ["migrate-events", "--to", "git", "--yes"])
        assert first.exit_code == 0, first.output
        log_after_first = (tmp_path / ".anvil" / "events.jsonl").read_bytes()

        second = _run_in(tmp_path, ["migrate-events", "--to", "git", "--yes"])
        assert second.exit_code == 0, second.output
        assert "already 'git'" in second.output
        assert (
            tmp_path / ".anvil" / "events.jsonl"
        ).read_bytes() == log_after_first

    def test_refuses_while_claims_are_active(self, tmp_path: Path) -> None:
        """A mid-flight agent's log must not be rewritten under it."""
        state_dir = tmp_path / ".anvil"
        state_dir.mkdir(parents=True)
        (state_dir / "config.yaml").write_text(
            "project_name: 'Busy'\nproject_id: 'proj-1'\n", encoding="utf-8"
        )
        b = SqliteBackend(
            db_path=str(state_dir / "state.db"),
            events_path=str(state_dir / "events.jsonl"),
            clock=FrozenClock(_T0),
            events_storage="local",
        )
        b.initialize()
        try:
            _seed_ready_task(b)
            b.append(
                _draft(
                    "claim.created",
                    _claim_payload("C001"),
                    target_kind="claim",
                    target_id="C001",
                )
            )
        finally:
            b.close()

        result = _run_in(tmp_path, ["migrate-events", "--to", "git", "--yes"])
        assert result.exit_code == 1
        assert "active claim" in result.output
        assert "C001" in result.output
        # Nothing was touched.
        config = yaml.safe_load((state_dir / "config.yaml").read_text(encoding="utf-8"))
        assert "events_storage" not in config

    def test_to_local_is_rejected(self, tmp_path: Path) -> None:
        _build_local_project(tmp_path)
        result = _run_in(tmp_path, ["migrate-events", "--to", "local", "--yes"])
        assert result.exit_code == 1
        assert "Only 'git' is supported" in result.output

    def test_replay_reads_mode_from_events_dir_not_cwd(self, tmp_path: Path) -> None:
        """Greptile P1: replaying a git-backed log from a different CWD must still
        use the order-tolerant (dedup) replay — the mode is read from the config
        beside the events file, not from the working directory.

        A union-merged git log can contain duplicate lines; local-mode replay
        does not dedupe, so the wrong mode double-writes each duplicate.
        """
        # A migrated (git-mode) project lives at tmp_path.
        _build_local_project(tmp_path)
        assert _run_in(tmp_path, ["migrate-events", "--to", "git", "--yes"]).exit_code == 0
        state_dir = tmp_path / ".anvil"

        # Simulate a merge=union duplicate: append a verbatim copy of the last line.
        log_path = state_dir / "events.jsonl"
        raw = log_path.read_text(encoding="utf-8").splitlines()
        (log_path).write_text("\n".join(raw + [raw[-1]]) + "\n", encoding="utf-8")

        # Replay from a SCRATCH cwd that has no .anvil/config.yaml of its own.
        scratch = tmp_path / "elsewhere"
        scratch.mkdir()
        into = scratch / "rebuilt.db"
        result = _run_in(
            scratch,
            ["replay", "--from-events", str(log_path), "--into", str(into)],
        )
        assert result.exit_code == 0, result.output

        # The duplicate was deduped (git mode), so the rebuilt projection equals
        # the canonical one — i.e. local-mode-double-write did NOT happen.
        canonical = SqliteBackend(
            db_path=str(state_dir / "state.db"),
            events_path=str(state_dir / "events.jsonl"),
            clock=FrozenClock(_T0),
            events_storage="git",
        )
        canonical.initialize()
        try:
            expected = _snap(canonical)
        finally:
            canonical.close()

        # Read the replayed projection back. Point events_path at the real git
        # log so initialize()'s git-mode convergence sees matching event ids and
        # does not rebuild from an empty file.
        rebuilt = SqliteBackend(
            db_path=str(into),
            events_path=str(log_path),
            clock=FrozenClock(_T0),
            events_storage="git",
        )
        rebuilt.initialize()
        try:
            assert _snap(rebuilt) == expected
        finally:
            rebuilt.close()

    def test_config_rewrite_preserves_crlf(self, tmp_path: Path) -> None:
        """Greptile P2: a CRLF config.yaml stays CRLF after the migration edit."""
        state_dir = tmp_path / ".anvil"
        _build_local_project(tmp_path)
        config_path = state_dir / "config.yaml"
        # Rewrite the existing config with CRLF endings.
        lf_text = config_path.read_text(encoding="utf-8")
        config_path.write_text(lf_text.replace("\n", "\r\n"), encoding="utf-8")

        assert _run_in(tmp_path, ["migrate-events", "--to", "git", "--yes"]).exit_code == 0

        out = config_path.read_bytes()
        assert b"\r\n" in out, "CRLF line endings were lost"
        assert b"\n" not in out.replace(b"\r\n", b""), "mixed LF/CRLF introduced"
        config = yaml.safe_load(out.decode("utf-8"))
        assert config["events_storage"] == "git"
