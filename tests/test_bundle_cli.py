"""CLI contract coverage for execution bundles."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from anvil.cli import app
from tests.test_bundle_state import _backend, _seed


def _seed_cli_project(tmp_path):
    state_dir = tmp_path / ".anvil"
    state_dir.mkdir()
    backend = _backend(state_dir)
    try:
        _seed(backend)
    finally:
        backend.close()
    return state_dir


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
    assert json.loads(claimed.output)["data"]["bundle"]["status"] == "active"

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
