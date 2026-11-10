"""Batch identity and refresh admission regressions at the web boundary."""

import threading
from concurrent.futures import Future, ThreadPoolExecutor

import pytest

from anvil import web, web_actions
from tests.test_execution_journeys import execution_project as execution_project
from tests.test_project_snapshot import cli_populated as cli_populated
from tests.test_web import enrolled as enrolled
from tests.test_web_actions import _request, _running_server


def test_bundle_confirmation_rechecks_enrolled_project_identity(execution_project):
    backend, _, repository, events_path, _ = execution_project
    preview = web_actions.preview_bundle_claim(repository, "human", {"bundle_id": "B001"})
    before = events_path.read_bytes()
    replaced = repository.model_copy(update={"project_id": "different-project"})
    with pytest.raises(web.WebError, match="Repository identity changed"):
        web_actions.execute_action(replaced, "human", {"binding": preview["binding"]})
    assert events_path.read_bytes() == before
    assert backend.get_bundle_claim("B001") is None


def test_coalesced_waiters_leave_http_capacity_for_other_requests(enrolled, monkeypatch):
    config_path, repositories = enrolled
    original_read = web.read_snapshot
    reader_entered, three_waiters, release = threading.Event(), threading.Event(), threading.Event()
    waiter_count = []

    class ObservedFuture(Future):
        def result(self, timeout=None):
            if not self.done():
                waiter_count.append(True)
                if len(waiter_count) == 3:
                    three_waiters.set()
            return super().result(timeout)

    def blocked_read(repository):
        reader_entered.set()
        assert release.wait(10)
        return original_read(repository)

    monkeypatch.setattr(web, "Future", ObservedFuture)
    monkeypatch.setattr(web, "read_snapshot", blocked_read)
    with _running_server(config_path) as server, ThreadPoolExecutor(max_workers=4) as executor:
        path = f"/api/repositories/{repositories[0].id}/snapshot"
        requests = [executor.submit(_request, server, path)]
        try:
            assert reader_entered.wait(5)
            requests += [executor.submit(_request, server, path) for _ in range(3)]
            assert three_waiters.wait(5)
            assert _request(server, path)[0] == 503
            assert _request(server, "/api/catalog")[0] == 200
        finally:
            release.set()
        assert all(request.result()[0] == 200 for request in requests)
