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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from anvil.cli import app
from anvil.clock import FrozenClock
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
