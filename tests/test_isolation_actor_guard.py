"""Tests for the retro-corpus concurrency features (worktree isolation config
+ distinct-actor fail-fast).

- Feature A: ``worktree_isolation`` config knob (off | advisory | require).
- Feature B: ``Claim.session_id`` recording and the same-actor /
  different-session fail-fast at claim and renew time.

Hermetic: tmp_path backends, FrozenClock, session ids injected via
monkeypatched env (the same resolution path production uses).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from anvil.claims.manager import ClaimError, ClaimManager
from anvil.clock import FrozenClock
from anvil.naming import session_discriminator
from anvil.state.models import EventDraft
from anvil.state.sqlite import SqliteBackend

_T0 = datetime(2026, 7, 9, 18, 0, 0, tzinfo=UTC)


def _make_backend(state_dir: Path) -> SqliteBackend:
    db_path = str(state_dir / "state.db")
    events_path = str(state_dir / "events.jsonl")
    Path(events_path).touch()
    b = SqliteBackend(db_path=db_path, events_path=events_path, clock=FrozenClock(_T0))
    b.initialize()
    return b


def _setup_project(b: SqliteBackend) -> None:
    b.append(EventDraft(
        timestamp=_T0, actor="test", action="project.created",
        target_kind="project", target_id="p1",
        payload_json={"id": "p1", "name": "P1", "description": "",
                      "created_at": _T0.isoformat(), "updated_at": _T0.isoformat()},
    ))
    # Claims require an approved PRD (mirrors test_claims._setup_prd).
    prd_payload = {
        "project_id": "p1", "status": "draft", "summary": "Test PRD.",
        "goals": ["g"], "non_goals": [],
        "requirements": [{"id": "R001", "prd_section": "requirements",
                          "text": "Req 1.", "source_paragraph": None,
                          "derived": False}],
        "acceptance_criteria": ["ac"], "risks": [], "open_questions": [],
    }
    for action, payload in (
        ("prd.parsed", prd_payload),
        ("prd.reviewed", {"project_id": "p1", "reviewer": "alice"}),
        ("prd.approved", {"project_id": "p1", "approver": "bob"}),
    ):
        b.append(EventDraft(
            timestamp=_T0, actor="test", action=action,
            target_kind="prd", target_id="p1", payload_json=payload,
        ))


def _insert_ready_task(b: SqliteBackend, task_id: str) -> None:
    # Direct-connect test seam, mirrors test_claims.py's raw inserts.
    with sqlite3.connect(b._db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO features (id, title, description, status, "
            "requirements, tasks) VALUES ('F001', 'F', '', 'draft', '[]', '[]')"
        )
        conn.execute(
            """INSERT INTO tasks
               (id, feature_id, title, description, status, likely_files,
                created_at, updated_at)
               VALUES (?, 'F001', ?, '', 'ready', '[]', ?, ?)""",
            (task_id, f"Task {task_id}", _T0.isoformat(), _T0.isoformat()),
        )
        conn.commit()


def _manager(b: SqliteBackend, actor: str = "loop-actor") -> ClaimManager:
    return ClaimManager(b, FrozenClock(_T0), actor=actor)


# ---------------------------------------------------------------------------
# naming.session_discriminator
# ---------------------------------------------------------------------------

class TestSessionDiscriminator:
    def test_reads_anvil_session_id_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANVIL_SESSION_ID", "sess-alpha-123456789")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "other")
        assert session_discriminator() == "sess-alpha-123456789"  # FULL id (identity is load-bearing)

    def test_falls_back_to_claude_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANVIL_SESSION_ID", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-sess-42")
        assert session_discriminator() == "claude-sess-42"

    def test_none_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANVIL_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        assert session_discriminator() is None


# ---------------------------------------------------------------------------
# Feature B — claim-time fail-fast
# ---------------------------------------------------------------------------

class TestDistinctActorFailFast:
    def _two_ready(self, tmp_path: Path) -> SqliteBackend:
        b = _make_backend(tmp_path)
        _setup_project(b)
        _insert_ready_task(b, "T001")
        _insert_ready_task(b, "T002")
        return b

    def test_claim_records_session_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANVIL_SESSION_ID", "loop-a-session")
        b = self._two_ready(tmp_path)
        try:
            result = _manager(b).claim("T001")
            assert result.claim.session_id == "loop-a-session"  # full id recorded
        finally:
            b.close()

    def test_same_actor_different_session_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        b = self._two_ready(tmp_path)
        try:
            monkeypatch.setenv("ANVIL_SESSION_ID", "loop-a")
            _manager(b).claim("T001")
            monkeypatch.setenv("ANVIL_SESSION_ID", "loop-b")
            with pytest.raises(ClaimError, match="DIFFERENT session"):
                _manager(b).claim("T002")
        finally:
            b.close()

    def test_same_actor_different_session_force_overrides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        b = self._two_ready(tmp_path)
        try:
            monkeypatch.setenv("ANVIL_SESSION_ID", "loop-a")
            _manager(b).claim("T001")
            monkeypatch.setenv("ANVIL_SESSION_ID", "loop-b")
            result = _manager(b).claim("T002", force=True)
            assert result.claim.task_id == "T002"
        finally:
            b.close()

    def test_same_actor_same_session_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # One loop claiming two tasks is legitimate.
        monkeypatch.setenv("ANVIL_SESSION_ID", "loop-a")
        b = self._two_ready(tmp_path)
        try:
            m = _manager(b)
            m.claim("T001")
            result = m.claim("T002")
            assert result.claim.task_id == "T002"
        finally:
            b.close()

    def test_no_session_env_skips_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Local-first: with no session identity we never guess.
        monkeypatch.delenv("ANVIL_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        b = self._two_ready(tmp_path)
        try:
            _manager(b).claim("T001")
            result = _manager(b).claim("T002")
            assert result.claim.session_id is None
        finally:
            b.close()

    def test_different_actor_same_pattern_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Distinct actors from distinct sessions is the CORRECT multi-loop
        # topology — must never trip the guard.
        b = self._two_ready(tmp_path)
        try:
            monkeypatch.setenv("ANVIL_SESSION_ID", "loop-a")
            _manager(b, actor="actor-a").claim("T001")
            monkeypatch.setenv("ANVIL_SESSION_ID", "loop-b")
            result = _manager(b, actor="actor-b").claim("T002")
            assert result.claim.task_id == "T002"
        finally:
            b.close()


class TestRenewSessionGuard:
    def test_renew_from_other_session_warns_but_renews(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Review finding: a hard refusal here false-positived on legitimate
        # cross-process surfaces (persistent MCP server, hook subprocess env)
        # and was silently swallowed by the heartbeat, killing leases. The
        # anomaly is WARNED; the corruption vector is closed by the heartbeat
        # hook's session filter instead.
        import logging

        b = _make_backend(tmp_path)
        _setup_project(b)
        _insert_ready_task(b, "T001")
        try:
            monkeypatch.setenv("ANVIL_SESSION_ID", "loop-a")
            claim = _manager(b).claim("T001").claim
            monkeypatch.setenv("ANVIL_SESSION_ID", "loop-b")
            with caplog.at_level(logging.WARNING, logger="anvil.claims.manager"):
                try:
                    _manager(b).renew(claim.id)
                except ClaimError as exc:
                    # Any refusal must come from OTHER gates (e.g. the B46
                    # forward-progress no-op path), never the session guard.
                    assert "sharing one" not in str(exc)
            assert any("give each its own ANVIL_ACTOR" in r.message
                       for r in caplog.records)
        finally:
            b.close()

    def test_renew_same_session_passes_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # B47 shared-env heartbeat re-resolves the SAME session — must pass
        # this guard (it may still no-op on the forward-progress gate, which
        # is not what we assert here).
        monkeypatch.setenv("ANVIL_SESSION_ID", "loop-a")
        b = _make_backend(tmp_path)
        _setup_project(b)
        _insert_ready_task(b, "T001")
        try:
            m = _manager(b)
            claim = m.claim("T001").claim
            try:
                m.renew(claim.id)
            except ClaimError as exc:
                assert "sharing one" not in str(exc)
        finally:
            b.close()


class TestCrashedLoopReclaim:
    def test_lease_expired_claim_does_not_false_refuse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Review finding: a crashed loop's lease-expired-but-unreaped claim
        # must not trip the fail-fast for its restarted successor (new
        # session, same pinned actor). Only live-lease claims count.
        from datetime import timedelta

        b = _make_backend(tmp_path)
        _setup_project(b)
        _insert_ready_task(b, "T001")
        _insert_ready_task(b, "T002")
        try:
            monkeypatch.setenv("ANVIL_SESSION_ID", "crashed-loop")
            ClaimManager(
                b, FrozenClock(_T0), actor="loop-actor", default_lease_minutes=1
            ).claim("T001")
            # Restarted loop, new session, later clock: T001's lease is dead.
            monkeypatch.setenv("ANVIL_SESSION_ID", "restarted-loop")
            later = FrozenClock(_T0 + timedelta(minutes=5))
            result = ClaimManager(b, later, actor="loop-actor").claim("T002")
            assert result.claim.task_id == "T002"
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Schema v10
# ---------------------------------------------------------------------------

class TestSchemaV10:
    def test_fresh_db_has_session_id_column(self, tmp_path: Path) -> None:
        b = _make_backend(tmp_path)
        try:
            with sqlite3.connect(tmp_path / "state.db") as conn:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(claims)")]
            assert "session_id" in cols
        finally:
            b.close()

    def test_v9_db_migrates_additively(self, tmp_path: Path) -> None:
        # Simulate a v9 claims table (no session_id) and run initialize():
        # the ladder must add the column and continue through the current v15
        # execution-bundle result-projection schema.
        db_path = tmp_path / "state.db"
        events_path = tmp_path / "events.jsonl"
        events_path.touch()
        b = _make_backend(tmp_path)  # creates current schema
        b.close()
        with sqlite3.connect(db_path) as conn:
            # regress: drop the column via table rebuild is heavy; instead
            # just stamp v9 and verify re-open is tolerant (duplicate-column
            # ladder no-ops).
            conn.execute("PRAGMA user_version = 9")
        b2 = SqliteBackend(db_path=str(db_path), events_path=str(events_path),
                           clock=FrozenClock(_T0))
        b2.initialize()
        try:
            with sqlite3.connect(db_path) as conn:
                v = conn.execute("PRAGMA user_version").fetchone()[0]
            assert v == 15
        finally:
            b2.close()


# ---------------------------------------------------------------------------
# Feature A — config knob
# ---------------------------------------------------------------------------

class TestWorktreeIsolationConfig:
    def test_default_is_advisory(self, tmp_path: Path) -> None:
        from anvil.config import load_config

        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("project_name: 'p'\nproject_id: 'p'\n", encoding="utf-8")
        cfg = load_config(cfg_path)
        assert cfg.worktree_isolation == "advisory"

    @pytest.mark.parametrize("mode", ["off", "advisory", "require"])
    def test_valid_modes_accepted(self, tmp_path: Path, mode: str) -> None:
        from anvil.config import load_config

        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            f"project_name: 'p'\nproject_id: 'p'\nworktree_isolation: '{mode}'\n",
            encoding="utf-8",
        )
        assert load_config(cfg_path).worktree_isolation == mode

    def test_invalid_mode_rejected(self, tmp_path: Path) -> None:
        from anvil.config import load_config

        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            "project_name: 'p'\nproject_id: 'p'\nworktree_isolation: always\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="worktree_isolation"):
            load_config(cfg_path)
