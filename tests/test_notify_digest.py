"""Tests for ``anvil notify-digest`` (backlog B42 Phase 1).

``notify-digest`` is the one-line needs_review + blockers summary built for an
OpenClaw Gateway cron (`cron add … --announce`): it prints a single line ONLY when
something needs attention, and NOTHING on a clean queue, so a recurring announce job
stays silent instead of pinging a channel every interval. It always exits 0 (a
notifier must never fail a cron run).

The zero/uninitialized cases run against a real backend (integration); the non-zero
counting + formatting is exercised by stubbing ``list_tasks`` — building real
``needs_review``/``blocked`` tasks would require the full event-sourced lifecycle,
which is well beyond what this counting logic needs.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

from typer.testing import CliRunner

import anvil.cli.notify_digest  # noqa: F401  (ensure submodule is in sys.modules)
from anvil.cli import app

# `anvil.cli` re-exports the `notify_digest` FUNCTION, shadowing the submodule
# attribute, so `anvil.cli.notify_digest` resolves to the function. Grab the real
# module to patch its imported `_resolve_state_dir`/`_open_backend` names.
notify_mod = sys.modules["anvil.cli.notify_digest"]

runner = CliRunner()


def _stub_backend(*statuses: str, claims: list | None = None) -> object:
    """A minimal backend whose ``list_tasks`` returns objects with just ``.status``
    and whose ``list_active_claims`` returns *claims* (T008 lease warnings)."""
    tasks = [types.SimpleNamespace(status=s) for s in statuses]
    active_claims = claims or []

    class _B:
        def list_tasks(self) -> list:  # noqa: D401
            return tasks

        def list_active_claims(self) -> list:
            return active_claims

        def close(self) -> None:
            pass

    return _B()


def _claim_expiring_in(minutes: float) -> object:
    from datetime import UTC, datetime, timedelta

    return types.SimpleNamespace(
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=minutes)
    )


# --- real-backend integration: the silent paths ---------------------------------


def test_notify_digest_silent_on_clean_initialized_project(tmp_path: Path) -> None:
    """A freshly-initialized project has no tasks → nothing to announce → silent."""
    original = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert runner.invoke(
            app, ["init", "--name", "DigestTest"], catch_exceptions=False
        ).exit_code == 0
        r = runner.invoke(app, ["notify-digest"], catch_exceptions=False)
    finally:
        os.chdir(original)
    assert r.exit_code == 0
    assert r.stdout.strip() == ""  # silent on a clean queue


def test_notify_digest_silent_when_uninitialized(tmp_path: Path) -> None:
    """An uninitialized project has nothing to announce and must not fail the cron."""
    original = os.getcwd()
    os.chdir(tmp_path)
    try:
        r = runner.invoke(app, ["notify-digest"], catch_exceptions=False)
    finally:
        os.chdir(original)
    assert r.exit_code == 0
    assert r.stdout.strip() == ""


def test_notify_digest_survives_a_corrupt_state_db(tmp_path: Path) -> None:
    """A corrupt/unopenable state.db must NOT crash a cron run — silent + exit 0,
    and --json still emits a valid envelope (regression for the adversarial-review
    find: unguarded TransactionAborted / sqlite3.DatabaseError turned a cron red)."""
    original = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert runner.invoke(
            app, ["init", "--name", "Corrupt"], catch_exceptions=False
        ).exit_code == 0
        # "file is not a database" — exactly the bad-queue shape a watchdog must survive.
        (tmp_path / ".anvil" / "state.db").write_bytes(b"not a sqlite database")
        r = runner.invoke(app, ["notify-digest"], catch_exceptions=False)
        rj = runner.invoke(app, ["notify-digest", "--json"], catch_exceptions=False)
    finally:
        os.chdir(original)
    assert r.exit_code == 0
    assert r.stdout.strip() == ""  # silent, not a crash
    assert rj.exit_code == 0
    assert json.loads(rj.stdout)["ok"] is True  # valid envelope despite corruption


# --- counting + formatting: stubbed list_tasks ----------------------------------


def test_notify_digest_reports_needs_review_and_blocked(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(notify_mod, "_resolve_state_dir", lambda cwd: tmp_path)
    monkeypatch.setattr(
        notify_mod,
        "_open_backend",
        lambda sd: _stub_backend(
            "needs_review", "needs_review", "blocked", "ready", "in_progress"
        ),
    )
    r = runner.invoke(app, ["notify-digest"], catch_exceptions=False)
    assert r.exit_code == 0
    assert "2 task(s) need review" in r.stdout
    assert "1 blocked" in r.stdout


def test_notify_digest_silent_when_only_other_statuses(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(notify_mod, "_resolve_state_dir", lambda cwd: tmp_path)
    monkeypatch.setattr(
        notify_mod, "_open_backend",
        lambda sd: _stub_backend("ready", "in_progress", "accepted", "done"),
    )
    r = runner.invoke(app, ["notify-digest"], catch_exceptions=False)
    assert r.exit_code == 0
    assert r.stdout.strip() == ""  # nothing needs review/unblocking → silent


def test_notify_digest_json_envelope_always_emits(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(notify_mod, "_resolve_state_dir", lambda cwd: tmp_path)
    monkeypatch.setattr(
        notify_mod, "_open_backend",
        lambda sd: _stub_backend("needs_review", "blocked", "blocked", "ready"),
    )
    r = runner.invoke(app, ["notify-digest", "--json"], catch_exceptions=False)
    assert r.exit_code == 0
    data = json.loads(r.stdout)["data"]
    assert data == {
        "needs_review": 1,
        "blocked": 2,
        "expiring_soon": 0,
        "total": 3,
    }


# --- T008: expiring-lease counts ------------------------------------------------


def test_notify_digest_counts_expiring_leases(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(notify_mod, "_resolve_state_dir", lambda cwd: tmp_path)
    monkeypatch.setattr(
        notify_mod,
        "_open_backend",
        lambda sd: _stub_backend(
            "ready", claims=[_claim_expiring_in(5), _claim_expiring_in(60)]
        ),
    )
    r = runner.invoke(app, ["notify-digest"], catch_exceptions=False)
    assert r.exit_code == 0
    assert "1 lease(s) expiring soon" in r.stdout

    rj = runner.invoke(app, ["notify-digest", "--json"], catch_exceptions=False)
    data = json.loads(rj.stdout)["data"]
    assert data["expiring_soon"] == 1


def test_notify_digest_expiring_leases_silent_when_all_healthy(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(notify_mod, "_resolve_state_dir", lambda cwd: tmp_path)
    monkeypatch.setattr(
        notify_mod,
        "_open_backend",
        lambda sd: _stub_backend("ready", claims=[_claim_expiring_in(120)]),
    )
    r = runner.invoke(app, ["notify-digest"], catch_exceptions=False)
    assert r.exit_code == 0
    assert r.stdout.strip() == ""
