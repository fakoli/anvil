"""CLI contract coverage for execution bundles."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from anvil.cli import app
from tests.test_bundle_state import _backend, _seed


def _seed_cli_project(tmp_path):
    import hashlib
    import sqlite3
    from types import SimpleNamespace

    from anvil.cli._helpers import prd_source_path
    from anvil.planning.prd_persistence import material_content_sha256

    state_dir = tmp_path / ".anvil"
    state_dir.mkdir()
    backend = _backend(state_dir)
    try:
        _seed(backend)
    finally:
        backend.close()
    source = (
        b"# Project: release\n\n"
        b"## Summary\nBundle test.\n\n"
        b"## Goals\n- Test bundles.\n\n"
        b"## Non-Goals\n- None.\n\n"
        b"## Requirements\n- R001: Bundle tasks.\n\n"
        b"## Acceptance Criteria\n- Bundle persists.\n\n"
        b"## Risks\n- None.\n"
    )
    source_path = prd_source_path(state_dir, "release")
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(source)
    source_sha256 = hashlib.sha256(source).hexdigest()
    material_sha256 = material_content_sha256(
        SimpleNamespace(
            source_bytes=source,
            markdown=source.decode(),
            source_sha256=source_sha256,
            source_size_bytes=len(source),
            source_encoding="utf-8",
        ),
        "release",
    )
    with sqlite3.connect(state_dir / "state.db") as conn:
        conn.execute(
            "UPDATE prds SET source_bytes=?, source_sha256=?, source_size_bytes=?, "
            "source_encoding='utf-8', source_revision=revision, "
            "provenance_state='available', content_available=1, "
            "material_sha256=?, content_event_id='E-TEST-CONTENT-release', "
            "lifecycle_revision=revision, lifecycle_source_sha256=?, "
            "lifecycle_material_sha256=?, "
            "lifecycle_content_event_id='E-TEST-CONTENT-release', "
            "review_event_id='E-TEST-REVIEW-release' WHERE id='release'",
            (source, source_sha256, len(source), material_sha256, source_sha256, material_sha256),
        )
    return state_dir


def test_bundle_claim_releases_when_source_changes_during_linearization(
    tmp_path, monkeypatch
) -> None:
    import anvil.planning.prd_persistence as persistence
    from anvil.cli._helpers import prd_source_path

    state_dir = _seed_cli_project(tmp_path)
    created = _invoke(
        tmp_path,
        [
            "bundle",
            "create",
            "B001",
            "release:T001",
            "--prd",
            "release",
            "--coordinator",
            "coordinator",
            "--actor",
            "planner",
            "--json",
        ],
    )
    assert created.exit_code == 0, created.output
    before_events = (state_dir / "events.jsonl").read_bytes()
    source_path = prd_source_path(state_dir, "release")
    original = persistence.require_canonical_prd_claim_binding
    calls = 0

    def drift_after_precheck(state_dir_arg, prd):  # type: ignore[no-untyped-def]
        nonlocal calls
        original(state_dir_arg, prd)
        calls += 1
        if calls == 1:
            source_path.write_bytes(
                source_path.read_bytes() + b"\n<!-- concurrent state-only drift -->\n"
            )

    monkeypatch.setattr(
        persistence, "require_canonical_prd_claim_binding", drift_after_precheck
    )

    refused = _invoke(
        tmp_path,
        ["bundle", "claim", "B001", "--actor", "coordinator", "--json"],
    )

    assert refused.exit_code == 1
    assert json.loads(refused.output)["error"]["code"] == "prd_source_unapproved"
    assert (state_dir / "events.jsonl").read_bytes() == before_events
    import sqlite3

    with sqlite3.connect(state_dir / "state.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM bundle_claims WHERE status='active'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM claims WHERE status='active'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM execution_bundles WHERE id='B001'"
        ).fetchone()[0] == "planned"


def _invoke(tmp_path, args):
    return CliRunner().invoke(
        app,
        [*args, "--cwd", str(tmp_path)],
        env={"ANVIL_STATE_LAYOUT": "local"},
    )


def test_bundle_create_show_and_list_human_json_contracts(tmp_path) -> None:
    _seed_cli_project(tmp_path)

    created = _invoke(
        tmp_path,
        [
            "bundle",
            "create",
            "B001",
            "release:T001",
            "release:T002",
            "--prd",
            "release",
            "--coordinator",
            "coordinator",
            "--actor",
            "planner",
            "--json",
        ],
    )
    assert created.exit_code == 0, created.output
    created_data = json.loads(created.output)["data"]["bundle"]
    assert created_data["id"] == "B001"
    assert created_data["task_ids"] == ["release:T001", "release:T002"]

    shown = _invoke(tmp_path, ["bundle", "show", "B001", "--json"])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["data"]["claim"] is None

    listed = _invoke(tmp_path, ["bundle", "list", "--prd", "release", "--json"])
    assert listed.exit_code == 0, listed.output
    assert [item["id"] for item in json.loads(listed.output)["data"]["bundles"]] == [
        "B001"
    ]

    human = _invoke(tmp_path, ["bundle", "show", "B001"])
    assert human.exit_code == 0, human.output
    assert "Bundle B001: planned" in human.output
    assert "Members: release:T001, release:T002" in human.output

    status = _invoke(tmp_path, ["bundle", "status", "B001", "--json"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["data"]["bundles"][0]["claimable"] is True


    claimed = _invoke(
        tmp_path,
        ["bundle", "claim", "B001", "--actor", "coordinator", "--json"],
    )
    assert claimed.exit_code == 0, claimed.output
    claim_data = json.loads(claimed.output)["data"]
    assert claim_data["bundle"]["status"] == "active"
    assert claim_data["actor_identity"]["actor"] == "coordinator"
    continuation = claim_data["continuation"]
    assert set(continuation) == {
        "environment",
        "renew",
        "release",
        "progress",
        "complete",
        "identity_notice",
    }
    assert continuation["renew"]["argv"][:4] == [
        "anvil", "bundle", "renew", "B001",
    ]
    assert continuation["complete"]["argv"][:4] == [
        "anvil", "bundle", "complete", "B001",
    ]
    assert "submit" not in continuation

    packet = _invoke(
        tmp_path,
        [
            "bundle",
            "packet",
            "B001",
            "--actor",
            "coordinator",
            "--format",
            "json",
            "--json",
        ],
    )
    assert packet.exit_code == 0, packet.output
    assert json.loads(packet.output)["data"]["content"]["bundle"]["id"] == "B001"

    progress = _invoke(
        tmp_path,
        [
            "bundle",
            "progress",
            "B001",
            "implementing",
            "--actor",
            "coordinator",
            "--json",
        ],
    )
    assert progress.exit_code == 0, progress.output
    assert json.loads(progress.output)["data"]["recorded"] is True


def test_bundle_create_preserves_spaces_and_rejects_explicit_empty_coordinator(
    tmp_path: Path,
) -> None:
    _seed_cli_project(tmp_path)
    created = _invoke(
        tmp_path,
        [
            "bundle", "create", "BSPACE", "release:T001", "--prd", "release",
            "--coordinator", " actor ", "--actor", "planner", "--json",
        ],
    )
    assert created.exit_code == 0, created.output
    assert json.loads(created.output)["data"]["bundle"]["coordinator"] == " actor "

    human_claim = _invoke(
        tmp_path,
        ["bundle", "claim", "BSPACE", "--actor", " actor "],
    )
    assert human_claim.exit_code == 0, human_claim.output
    assert "ANVIL_ACTOR" in human_claim.output
    assert "anvil bundle renew " in human_claim.output
    assert "anvil bundle release " in human_claim.output
    assert "anvil bundle progress " in human_claim.output
    assert "anvil bundle complete " in human_claim.output
    assert "BSPACE" in human_claim.output
    assert "anvil submit BSPACE" not in human_claim.output

    empty = _invoke(
        tmp_path,
        [
            "bundle", "create", "BEMPTY", "release:T002", "--prd", "release",
            "--coordinator", "", "--actor", "planner", "--json",
        ],
    )
    assert empty.exit_code != 0
    assert "Invalid coordinator identity" in empty.output


def test_bundle_create_errors_match_stable_json_code(tmp_path) -> None:
    _seed_cli_project(tmp_path)
    result = _invoke(
        tmp_path,
        [
            "bundle",
            "create",
            "B001",
            "missing:T001",
            "--prd",
            "release",
            "--coordinator",
            "coordinator",
            "--json",
        ],
    )
    assert result.exit_code != 0
    error = json.loads(result.output)["error"]
    assert error["code"] == "bundle_error"
    assert "member tasks not found" in error["message"]


def test_bundle_delivery_errors_match_stable_json_code(tmp_path) -> None:
    _seed_cli_project(tmp_path)
    result = _invoke(
        tmp_path,
        [
            "bundle",
            "checkpoint",
            "missing",
            "--commit",
            "abc123",
            "--actor",
            "coordinator",
            "--json",
        ],
    )
    assert result.exit_code != 0
    assert json.loads(result.output)["error"]["code"] == "bundle_error"


def test_bundle_supersede_uses_required_replacement_option(tmp_path) -> None:
    _seed_cli_project(tmp_path)
    for bundle_id, task_id in (
        ("B001", "release:T001"),
        ("B002", "release:T002"),
    ):
        created = _invoke(
            tmp_path,
            [
                "bundle",
                "create",
                bundle_id,
                task_id,
                "--prd",
                "release",
                "--coordinator",
                "coordinator",
                "--actor",
                "planner",
                "--json",
            ],
        )
        assert created.exit_code == 0, created.output

    result = _invoke(
        tmp_path,
        [
            "bundle",
            "supersede",
            "B001",
            "--replacement",
            "B002",
            "--actor",
            "coordinator",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["bundle"]["superseded_by"] == "B002"


def test_bundle_review_finalize_checkpoint_and_reconcile_json(tmp_path) -> None:
    _seed_cli_project(tmp_path)
    created = _invoke(
        tmp_path,
        [
            "bundle",
            "create",
            "B001",
            "release:T001",
            "release:T002",
            "--prd",
            "release",
            "--coordinator",
            "coordinator",
            "--actor",
            "planner",
            "--json",
        ],
    )
    assert created.exit_code == 0, created.output
    claimed = _invoke(
        tmp_path,
        ["bundle", "claim", "B001", "--actor", "coordinator", "--json"],
    )
    assert claimed.exit_code == 0, claimed.output
    for task_id in ("release:T001", "release:T002"):
        submitted = _invoke(
            tmp_path,
            [
                "submit",
                task_id,
                "--commands",
                "pytest -q",
                "--files-changed",
                f"src/{task_id[-1]}.py",
                "--actor",
                "coordinator",
                "--json",
            ],
        )
        assert submitted.exit_code == 0, submitted.output
    completed = _invoke(
        tmp_path,
        ["bundle", "complete", "B001", "--actor", "coordinator", "--json"],
    )
    assert completed.exit_code == 0, completed.output
    assert (
        json.loads(completed.output)["data"]["bundle"]["status"]
        == "implemented_unreviewed"
    )

    for reviewer, angle in (
        ("reviewer-a", "correctness"),
        ("reviewer-b", "security"),
        ("reviewer-c", "integration"),
    ):
        reviewed = _invoke(
            tmp_path,
            [
                "bundle",
                "review",
                "B001",
                "--round",
                "1",
                "--angle",
                angle,
                "--decision",
                "approve",
                "--actor",
                reviewer,
                "--json",
            ],
        )
        assert reviewed.exit_code == 0, reviewed.output

    finalized = _invoke(
        tmp_path,
        [
            "bundle",
            "finalize-review",
            "B001",
            "--actor",
            "coordinator",
            "--json",
        ],
    )
    assert finalized.exit_code == 0, finalized.output
    assert (
        json.loads(finalized.output)["data"]["bundle"]["status"]
        == "reviewed_unintegrated"
    )

    checkpoint = _invoke(
        tmp_path,
        [
            "bundle",
            "checkpoint",
            "B001",
            "--commit",
            "abc123",
            "--actor",
            "coordinator",
            "--json",
        ],
    )
    assert checkpoint.exit_code == 0, checkpoint.output
    assert json.loads(checkpoint.output)["data"]["checkpoint"]["commit_sha"] == "abc123"

    reconciled = _invoke(
        tmp_path,
        [
            "bundle",
            "reconcile",
            "B001",
            "--commit",
            "abc123",
            "--actor",
            "coordinator",
            "--json",
        ],
    )
    assert reconciled.exit_code == 0, reconciled.output
    assert json.loads(reconciled.output)["data"]["bundle"]["status"] == "integrated"


def test_bundle_manager_resolves_artifacts_against_explicit_checkout(tmp_path) -> None:
    """HOME-workspace state must never become the artifact assertion root."""
    from anvil.cli.bundle import _manager

    state_dir = tmp_path / "home" / ".anvil" / "workspaces" / "project" / ".anvil"
    state_dir.mkdir(parents=True)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    backend = _backend(state_dir)
    try:
        manager = _manager(backend, state_dir, "coordinator", cwd=Path(checkout))
        assert manager._project_root == checkout.resolve()
        assert manager._project_root != state_dir.parent
    finally:
        backend.close()


def test_bundle_lease_mutations_run_stale_reaper(tmp_path, monkeypatch) -> None:
    """Bundle claim/renew/release have the same stale-state preflight as tasks."""
    import anvil.cli.bundle as bundle_cli

    _seed_cli_project(tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(bundle_cli, "_reap_stale_claims", calls.append)

    created = _invoke(
        tmp_path,
        [
            "bundle",
            "create",
            "B001",
            "release:T001",
            "--prd",
            "release",
            "--coordinator",
            "coordinator",
            "--actor",
            "planner",
            "--json",
        ],
    )
    assert created.exit_code == 0, created.output

    for action in ("claim", "renew", "release"):
        result = _invoke(
            tmp_path,
            ["bundle", action, "B001", "--actor", "coordinator", "--json"],
        )
        assert result.exit_code == 0, result.output

    assert len(calls) == 3
