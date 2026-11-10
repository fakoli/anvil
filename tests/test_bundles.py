"""Regression coverage for guarded pristine bundle withdrawal."""

from __future__ import annotations

from datetime import timedelta

import pytest

from anvil.bundles.delivery import BundleDeliveryError, BundleDeliveryManager
from anvil.clock import FrozenClock
from anvil.state.backend import EventRejected
from anvil.state.models import BundleStatus
from tests.test_bundle_state import (
    _T0,
    _backend,
    _claim_payload,
    _create_bundle,
    _event,
    _event_count,
    _seed,
)


def test_withdrawal_rejects_wrong_actor_stale_generation_and_repeat(tmp_path) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        _create_bundle(backend)
        bundle = backend.get_bundle("B001")
        assert bundle is not None
        now = _T0 + timedelta(seconds=1)
        with pytest.raises(EventRejected, match="only the coordinator"):
            backend.append(
                _event(
                    "bundle.withdrawn",
                    {
                        "bundle_id": "B001",
                        "creation_event_id": bundle.creation_event_id,
                        "withdrawn_by_actor": "other",
                        "reason": "operator correction",
                        "withdrawn_at": now.isoformat(),
                    },
                    target_kind="bundle",
                    target_id="B001",
                    timestamp=now,
                    actor="other",
                )
            )
        with pytest.raises(EventRejected, match="generation not found"):
            backend.append(
                _event(
                    "bundle.withdrawn",
                    {
                        "bundle_id": "B001",
                        "creation_event_id": "E999999",
                        "withdrawn_by_actor": "codex-main",
                        "reason": "operator correction",
                        "withdrawn_at": now.isoformat(),
                    },
                    target_kind="bundle",
                    target_id="B001",
                    timestamp=now,
                    actor="codex-main",
                )
            )
        delivery = BundleDeliveryManager(backend, FrozenClock(now), actor="codex-main")
        delivery.withdraw("B001", reason="operator correction")
        before = _event_count(tmp_path)
        with pytest.raises(BundleDeliveryError, match="must be planned"):
            delivery.withdraw("B001", reason="repeat")
        assert _event_count(tmp_path) == before
    finally:
        backend.close()


def test_withdrawal_refuses_released_member_claim_history(tmp_path) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        backend.append(
            _event(
                "claim.created",
                _claim_payload("C001", "release:T001"),
                target_kind="claim",
                target_id="C001",
                actor="worker",
            )
        )
        backend.append(
            _event(
                "claim.released",
                {"claim_id": "C001", "released_by": "worker", "release_reason": "done"},
                target_kind="claim",
                target_id="C001",
                actor="worker",
            )
        )
        _create_bundle(backend, task_ids=["release:T001"])
        with pytest.raises(BundleDeliveryError, match="member tasks have claim history"):
            BundleDeliveryManager(
                backend, FrozenClock(_T0 + timedelta(seconds=1)), actor="codex-main"
            ).withdraw("B001", reason="operator correction")
    finally:
        backend.close()


def test_withdrawal_refuses_member_evidence_history(tmp_path) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        _create_bundle(backend, task_ids=["release:T001"])
        # Simulate a legacy/imported projection containing history that predates
        # the bundle guard.  The withdrawal check must protect this state too.
        conn = backend._require_conn()  # noqa: SLF001
        conn.execute(
            """INSERT INTO claims(
                    id, task_id, claimed_by, claim_type, status, expected_files,
                    generation, created_at, lease_expires_at, last_heartbeat_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "C-evidence",
                "release:T001",
                "worker",
                "task",
                "released",
                "[]",
                1,
                _T0.isoformat(),
                (_T0 + timedelta(hours=1)).isoformat(),
                _T0.isoformat(),
            ),
        )
        conn.execute(
            """INSERT INTO evidence(
                    id, task_id, claim_id, commands_run, files_changed, screenshots,
                    proofs, submitted_at, submitted_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "EV-evidence",
                "release:T001",
                "C-evidence",
                "[]",
                "[]",
                "[]",
                "[]",
                _T0.isoformat(),
                "worker",
            ),
        )
        conn.commit()
        with pytest.raises(BundleDeliveryError, match="member tasks have evidence history"):
            BundleDeliveryManager(
                backend, FrozenClock(_T0 + timedelta(seconds=1)), actor="codex-main"
            ).withdraw("B001", reason="operator correction")
    finally:
        backend.close()


def test_withdrawal_releases_members_for_ordinary_claim_and_bounds_reason(tmp_path) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        _create_bundle(backend, task_ids=["release:T001"])
        BundleDeliveryManager(
            backend, FrozenClock(_T0 + timedelta(seconds=1)), actor="codex-main"
        ).withdraw("B001", reason="operator correction")
        assert backend.get_bundle("B001").status is BundleStatus.withdrawn  # type: ignore[union-attr]
        backend.append(
            _event(
                "claim.created",
                _claim_payload("C001", "release:T001"),
                target_kind="claim",
                target_id="C001",
                timestamp=_T0 + timedelta(seconds=2),
                actor="worker",
            )
        )
        assert backend.get_claim("C001") is not None
        _create_bundle(backend, bundle_id="B002", task_ids=["release:T002"])
        with pytest.raises(BundleDeliveryError, match="up to 4096 bytes"):
            BundleDeliveryManager(
                backend, FrozenClock(_T0 + timedelta(seconds=3)), actor="codex-main"
            ).withdraw("B002", reason="x" * 4097)
    finally:
        backend.close()


def test_withdrawn_bundle_does_not_block_replacement_reopen_or_replay(tmp_path) -> None:
    source_root = tmp_path / "source"
    replay_root = tmp_path / "replay"
    source_root.mkdir()
    replay_root.mkdir()
    backend = _backend(source_root)
    try:
        _seed(backend)
        _create_bundle(backend, task_ids=["release:T001"])
        BundleDeliveryManager(
            backend, FrozenClock(_T0 + timedelta(seconds=1)), actor="codex-main"
        ).withdraw("B001", reason="operator correction")
        creation_event_id = _create_bundle(
            backend, bundle_id="B002", task_ids=["release:T001"]
        )
        backend.append(
            _event(
                "bundle.status_changed",
                {
                    "bundle_id": "B002",
                    "creation_event_id": creation_event_id,
                    "from": "planned",
                    "to": "replan_required",
                    "changed_at": (_T0 + timedelta(seconds=2)).isoformat(),
                },
                target_kind="bundle",
                target_id="B002",
                timestamp=_T0 + timedelta(seconds=2),
                actor="codex-main",
            )
        )
        backend.append(
            _event(
                "bundle.status_changed",
                {
                    "bundle_id": "B002",
                    "creation_event_id": creation_event_id,
                    "from": "replan_required",
                    "to": "planned",
                    "changed_at": (_T0 + timedelta(seconds=3)).isoformat(),
                },
                target_kind="bundle",
                target_id="B002",
                timestamp=_T0 + timedelta(seconds=3),
                actor="codex-main",
            )
        )
        assert backend.get_bundle("B002").status is BundleStatus.planned  # type: ignore[union-attr]
    finally:
        backend.close()

    replay = _backend(replay_root)
    try:
        replay.replay_from_empty(str(source_root / "events.jsonl"))
        assert replay.get_bundle("B002").status is BundleStatus.planned  # type: ignore[union-attr]
    finally:
        replay.close()


def test_withdrawal_refuses_control_char_bundle_identifier_without_echoing_it(tmp_path) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        _create_bundle(backend)
        before = _event_count(tmp_path)
        with pytest.raises(BundleDeliveryError, match="identifier is invalid") as exc_info:
            BundleDeliveryManager(backend, FrozenClock(_T0), actor="codex-main").withdraw(
                "B001\rforged", reason="operator correction"
            )
        assert "\r" not in str(exc_info.value)
        assert _event_count(tmp_path) == before
    finally:
        backend.close()
