"""Every bounded batch remains reachable as campaigns and current membership change."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from anvil import web, web_actions
from anvil.bundles.delivery import BundleDeliveryManager
from anvil.clock import FrozenClock, SystemClock
from anvil.state.sqlite import SqliteBackend
from tests.test_bundle_state import _T0, _bundle_payload, _event, _seed
from tests.test_web_actions import (
    _request,
    _running_server,
)
from tests.test_web_actions import (
    action_catalog as action_catalog,
)


@pytest.fixture
def many_batches(git_repo, tmp_path, monkeypatch):
    from anvil.cli._helpers import _resolve_state_dir

    monkeypatch.delenv("ANVIL_STATE_LAYOUT", raising=False)
    state_dir = _resolve_state_dir(git_repo)
    state_dir.mkdir(parents=True)
    events_path = state_dir / "events.jsonl"
    events_path.touch()
    backend = SqliteBackend(
        db_path=str(state_dir / "state.db"), events_path=str(events_path),
        clock=SystemClock(), project_root=str(git_repo),
    )
    backend.initialize()
    try:
        _seed(backend, second_prd=True)
        template = backend.get_task("release:T001").model_dump(mode="json")
        for index in range(1, 56):
            task_id = f"release:T{index:03d}"
            if index > 2:
                backend.append(_event("task.created", {
                    **template, "id": task_id, "title": task_id, "prd_id": "release",
                }, target_kind="task", target_id=task_id))
            bundle_id = f"B{index:03d}"
            payload = _bundle_payload(bundle_id, [task_id])
            payload.update(coordinator="human", branch=None, worktree_path=None)
            backend.append(_event(
                "bundle.created", payload, target_kind="bundle", target_id=bundle_id,
            ))
        payload = _bundle_payload("A-other", ["other:T001"])
        payload.update(prd_id="other", coordinator="human", branch=None, worktree_path=None)
        backend.append(_event(
            "bundle.created", payload, target_kind="bundle", target_id="A-other",
        ))
        config_path = tmp_path / "web.json"
        repository = web.enroll_repository(config_path, git_repo)
        yield backend, config_path, repository
    finally:
        backend.close()


def test_later_batches_remain_reachable_and_claimable_after_earlier_status_change(many_batches):
    backend, config_path, repository = many_batches
    prefix = f"/api/repositories/{repository.id}"
    with _running_server(config_path, allow_writes=True, actor="human") as server:
        status, first = _request(server, prefix + "/bundles?prd_id=release")
        assert status == 200, first
        assert [plan["bundle_id"] for plan in first["plans"]] == [
            f"B{index:03d}" for index in range(1, 51)
        ]
        assert first["pagination"] == {
            "after_bundle_id": None, "next_after_bundle_id": "B050", "page_size": 50,
        }
        assert first["truncated"] is True
        assert first["repository_id"] == repository.id
        assert first["project_id"] == repository.project_id and first["prd_id"] == "release"

        # Removing an earlier entry must not shift B051 out of the next page.
        BundleDeliveryManager(backend, FrozenClock(_T0), actor="human").supersede(
            "B001", replacement_bundle_id="B002",
        )
        status, second = _request(
            server, prefix + "/bundles?prd_id=release&after_bundle_id=B050",
        )
        assert status == 200, second
        assert [plan["bundle_id"] for plan in second["plans"]] == [
            f"B{index:03d}" for index in range(51, 56)
        ]
        assert second["pagination"]["next_after_bundle_id"] is None
        assert second["truncated"] is False
        assert all(plan["prd"]["id"] == "release" for plan in second["plans"])

        token = _request(server, "/api/catalog")[1]["csrf_token"]
        status, preview = _request(
            server, prefix + "/bundle-preview", {"bundle_id": "B055"}, token=token,
        )
        assert status == 200, preview
        status, claimed = _request(
            server, prefix + "/bundle-claim",
            {"preview_id": preview["preview_id"], "confirmed": True}, token=token,
        )
        assert status == 200, claimed
        assert backend.get_bundle("B055").status.value == "active"
        assert _request(server, prefix + "/bundles?prd_id=missing")[0] == 400
        other = _request(server, prefix + "/bundles?prd_id=other")[1]
        assert [plan["bundle_id"] for plan in other["plans"]] == ["A-other"]


def test_batch_projection_is_bounded_and_default_listing_is_compatible(many_batches, monkeypatch):
    from anvil.bundles import execution

    _, _, repository = many_batches
    original = execution.build_bundle_execution_plan
    projected = []

    def observed(backend, bundle_id):
        projected.append(bundle_id)
        return original(backend, bundle_id)

    monkeypatch.setattr(execution, "build_bundle_execution_plan", observed)
    listing = web_actions.inspect_bundles(repository, "human")
    assert len(projected) == len(listing["plans"]) == 50
    assert projected[0] == "A-other" and projected[-1] == "B049"
    assert listing["actor"] == "human" and listing["truncated"] is True
    assert listing["prd_id"] is None
    assert listing["pagination"]["next_after_bundle_id"] == "B049"


@pytest.mark.parametrize("query", [
    "prd_id=release&prd_id=other", "after_bundle_id=B001&after_bundle_id=B002",
    "unknown=value", "prd_id=", "after_bundle_id=", "prd_id=%FF",
    "after_bundle_id=../private", "after_bundle_id=" + "B" * 129,
    "prd_id=release&after_bundle_id=B001&extra=yes", "malformed",
])
def test_invalid_page_queries_refuse_before_starting_a_worker(action_catalog, monkeypatch, query):
    config_path, repository = action_catalog

    def unexpected(*args, **kwargs):
        raise AssertionError("Invalid query reached a worker")

    monkeypatch.setattr(web, "_action_worker", unexpected)
    with _running_server(config_path, allow_writes=True, actor="human") as server:
        status, response = _request(server, f"/api/repositories/{repository.id}/bundles?{query}")
        assert status == 400
        assert response["error"]["code"] == "invalid_batch_query"


def test_overlapping_different_pages_and_campaigns_do_not_coalesce(action_catalog, monkeypatch):
    config_path, repository = action_catalog
    both_started = threading.Barrier(2)

    def worker(repository, actor, operation, request=None):
        both_started.wait(timeout=5)
        return {"selection": request}

    monkeypatch.setattr(web, "_action_worker", worker)
    paths = [
        f"/api/repositories/{repository.id}/bundles?prd_id=release",
        f"/api/repositories/{repository.id}/bundles?prd_id=other&after_bundle_id=B050",
    ]
    with _running_server(config_path, allow_writes=True, actor="human") as server:
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda path: _request(server, path), paths))
    assert [status for status, _ in responses] == [200, 200]
    assert [response["selection"] for _, response in responses] == [
        {"prd_id": "release", "after_bundle_id": None},
        {"prd_id": "other", "after_bundle_id": "B050"},
    ]
