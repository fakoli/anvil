"""Native board actions, HTTP trust boundaries, and bounded concurrent requests."""

from __future__ import annotations

import http.client
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from typer.testing import CliRunner

from anvil import web
from anvil.cli import app
from anvil.cli._helpers import _resolve_state_dir
from anvil.state.schema import get_schema_version
from tests.test_attempt_view import _reclaim, _submit
from tests.test_sqlite import (
    _make_backend,
    _make_event,
    _make_task_payload,
    _setup_claimable_task_and_claim,
)


def _request(server, path, payload=None, *, token=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    request_headers = {}
    body = None
    if payload is not None:
        body = json.dumps(payload)
        request_headers = {
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{server.server_port}",
            "X-Anvil-CSRF": token or "",
        }
    for name, value in (headers or {}).items():
        if value is None:
            request_headers.pop(name, None)
        else:
            request_headers[name] = value
    try:
        connection.request("POST" if payload is not None else "GET", path, body, request_headers)
        response = connection.getresponse()
        raw = response.read()
        data = json.loads(raw) if response.getheader("Content-Type") == "application/json" else raw
        return response.status, data
    finally:
        connection.close()


@contextmanager
def _running_server(config_path, **options):
    server = web.make_server(config_path, port=0, **options)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def action_catalog(tmp_path):
    repository = web.Repository(
        id="a" * 24, label="Synthetic", root=str(tmp_path), project_id="proj-1",
        reader_argv=[sys.executable, "-I", "-m", "anvil.cli", "project", "snapshot", "--json"],
        reader_label="Native test reader", expected_schema_version=get_schema_version(),
    )
    config_path = tmp_path / "web.json"
    web.save_config(config_path, web.WebConfig(repositories=[repository]))
    return config_path, repository


def _preview_request(task_id="T001", action="close", *, version="a" * 64, digest="b" * 64):
    return {
        "task_id": task_id, "prd_id": "default", "action": action,
        "task_version": version, "snapshot_digest": digest,
    }


@pytest.fixture
def stub_worker(monkeypatch):
    calls = []

    def worker(repository, actor, operation, request=None):
        calls.append((repository, actor, operation, request))
        if operation == "preview":
            return {"title": "Close task", "binding": {"task_id": "T001", "action": "close"}}
        return {"ok": True}

    monkeypatch.setattr(web, "_action_worker", worker)
    return calls


def test_post_requires_same_origin_json_and_csrf_before_work(action_catalog, stub_worker):
    config_path, repository = action_catalog
    with _running_server(config_path, allow_writes=True, actor="human") as server:
        token = _request(server, "/api/catalog")[1]["csrf_token"]
        path = f"/api/repositories/{repository.id}/preview"
        for overrides, expected_status in [
            ({"Origin": None}, 403),
            ({"Origin": "https://foreign.example"}, 403),
            ({"Host": "foreign.example"}, 403),
            ({"Origin": f"http://localhost:{server.server_port}"}, 403),
            ({"Sec-Fetch-Site": "cross-site"}, 403),
            ({"X-Anvil-CSRF": "wrong"}, 403),
            ({"X-Anvil-CSRF": "é"}, 403),
            ({"X-Anvil-CSRF": None}, 403),
            ({"Content-Type": "text/plain"}, 400),
            ({"Transfer-Encoding": "chunked"}, 400),
            ({"Content-Length": "0"}, 413),
            ({"Content-Length": str(web.MAX_ACTION_BYTES + 1)}, 413),
        ]:
            assert _request(server, path, _preview_request(), token=token, headers=overrides)[0] == expected_status
        assert stub_worker == []
        status, value = _request(server, path, {**_preview_request(), "argv": ["unexpected"]}, token=token)
        assert status == 409 and value["error"]["code"] == "invalid_request"
        assert _request(server, path.replace(repository.id, "b" * 24), _preview_request(), token=token)[0] == 409
        assert stub_worker == []


def test_read_only_and_custom_readers_cannot_write(action_catalog, stub_worker):
    config_path, repository = action_catalog
    path = f"/api/repositories/{repository.id}/preview"
    with _running_server(config_path) as server:
        catalog = _request(server, "/api/catalog")[1]
        assert catalog["workflow"]["enabled"] is False
        assert _request(server, path, _preview_request(), token=catalog["csrf_token"])[0] == 405
    config = web.load_config(config_path)
    config.repositories[0].reader_argv = [sys.executable, "-I", "-c", "print('read only')"]
    web.save_config(config_path, config)
    with _running_server(config_path, allow_writes=True, actor="human") as server:
        token = _request(server, "/api/catalog")[1]["csrf_token"]
        status, value = _request(server, path, _preview_request(), token=token)
        assert status == 409 and value["error"]["code"] == "read_only_repository"
    assert stub_worker == []


def test_confirmation_is_explicit_single_use_and_config_bound(action_catalog, stub_worker):
    config_path, repository = action_catalog
    with _running_server(config_path, allow_writes=True, actor="human") as server:
        token = _request(server, "/api/catalog")[1]["csrf_token"]
        prefix = f"/api/repositories/{repository.id}"
        preview = _request(server, prefix + "/preview", _preview_request(), token=token)[1]
        confirmation = {"preview_id": preview["preview_id"], "confirmed": True, "reason": "Duplicate"}
        assert _request(server, prefix + "/transition", {**confirmation, "confirmed": False}, token=token)[0] == 409
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(
                lambda _: _request(server, prefix + "/transition", confirmation, token=token),
                range(2),
            ))
        assert sorted(response[0] for response in responses) == [200, 409]
        assert len([call for call in stub_worker if call[2] == "execute"]) == 1

        preview = _request(server, prefix + "/preview", _preview_request(), token=token)[1]
        config = web.load_config(config_path)
        config.repositories[0].label = "Changed enrollment"
        web.save_config(config_path, config)
        status, value = _request(server, prefix + "/transition", {
            "preview_id": preview["preview_id"], "confirmed": True,
        }, token=token)
        assert status == 409 and value["error"]["code"] == "repository_changed"
        assert len([call for call in stub_worker if call[2] == "execute"]) == 1


def test_expired_preview_and_request_capacity_are_bounded(action_catalog, stub_worker, monkeypatch):
    config_path, repository = action_catalog
    monkeypatch.setattr(web, "PREVIEW_LIFETIME_SECONDS", -1)
    with _running_server(config_path, allow_writes=True, actor="human") as server:
        token = _request(server, "/api/catalog")[1]["csrf_token"]
        prefix = f"/api/repositories/{repository.id}"
        preview = _request(server, prefix + "/preview", _preview_request(), token=token)[1]
        status, value = _request(server, prefix + "/transition", {
            "preview_id": preview["preview_id"], "confirmed": True,
        }, token=token)
        assert status == 409 and value["error"]["code"] == "preview_expired"
        for _ in range(8):
            assert server.request_slots.acquire(timeout=2)
        try:
            assert _request(server, "/api/catalog")[0] == 503
        finally:
            for _ in range(8):
                server.request_slots.release()


def test_reader_capacity_refuses_excess_work(action_catalog, monkeypatch):
    _, repository = action_catalog
    started = threading.Barrier(5)
    release = threading.Event()

    def bounded_reader(repository, *, mutation=False):
        started.wait(timeout=5)
        assert release.wait(timeout=5)
        return b"{}"

    monkeypatch.setattr(web, "_bounded_reader_output", bounded_reader)
    with ThreadPoolExecutor(max_workers=4) as executor:
        reads = [executor.submit(web._reader_output, repository) for _ in range(4)]
        try:
            started.wait(timeout=5)
            with pytest.raises(web.WebError, match="busy"):
                web._reader_output(repository)
        finally:
            release.set()
        assert [read.result(timeout=5) for read in reads] == [b"{}"] * 4


@pytest.fixture
def native_action_project(git_repo, monkeypatch, tmp_path):
    monkeypatch.delenv("ANVIL_STATE_LAYOUT", raising=False)
    state_dir = _resolve_state_dir(git_repo)
    state_dir.mkdir(parents=True)
    backend = _make_backend(state_dir)
    _setup_claimable_task_and_claim(backend)
    _submit(backend)
    backend.append(_make_event(
        "task.created", _make_task_payload(task_id="T002", status="drafted"),
        target_kind="task", target_id="T002",
    ))
    config_path = tmp_path / "native-web.json"
    repository = web.enroll_repository(config_path, git_repo)
    try:
        yield backend, config_path, repository
    finally:
        backend.close()


def _native_preview(server, repository, token, task_id, action):
    prefix = f"/api/repositories/{repository.id}"
    status, snapshot = _request(server, prefix + "/snapshot")
    assert status == 200, snapshot
    request = _preview_request(
        task_id, action, version=snapshot["workflow"]["versions"]["default:" + task_id],
        digest=snapshot["snapshot"]["snapshot_digest"],
    )
    return _request(server, prefix + "/preview", request, token=token)


def test_native_approval_and_duplicate_closure_use_canonical_state(native_action_project):
    backend, config_path, repository = native_action_project
    with _running_server(config_path, allow_writes=True, actor="human") as server:
        token = _request(server, "/api/catalog")[1]["csrf_token"]
        prefix = f"/api/repositories/{repository.id}"
        status, preview = _native_preview(server, repository, token, "T002", "approve")
        assert status == 409 and preview["error"]["code"] == "transition_refused"
        status, preview = _native_preview(server, repository, token, "T001", "approve")
        assert status == 200, preview
        assert any("EV001" in detail for detail in preview["details"])
        status, result = _request(server, prefix + "/transition", {
            "preview_id": preview["preview_id"], "confirmed": True,
        }, token=token)
        assert status == 200, result
        assert backend.get_task("T001").status.value == "done"
        status, preview = _native_preview(server, repository, token, "T002", "close")
        assert status == 200, preview
        status, result = _request(server, prefix + "/transition", {
            "preview_id": preview["preview_id"], "confirmed": True,
            "resolution": "duplicate", "reason": "Already covered", "duplicate_of": "T001",
        }, token=token)
        assert status == 200, result
        closed = backend.get_task("T002")
        assert closed.status.value == "closed"
        assert closed.closure.resolution == "duplicate"
        assert closed.closure.duplicate_of == "T001"
        status, snapshot = _request(server, prefix + "/snapshot")
        assert status == 200, snapshot
        assert snapshot["workflow"]["actions"]["default:T002"] == []
        assert snapshot["workflow"]["closures"]["default:T002"]["resolution"] == "duplicate"


def test_native_approval_refuses_new_evidence_after_preview(native_action_project):
    backend, config_path, repository = native_action_project
    with _running_server(config_path, allow_writes=True, actor="human") as server:
        token = _request(server, "/api/catalog")[1]["csrf_token"]
        status, preview = _native_preview(server, repository, token, "T001", "approve")
        assert status == 200, preview
        _reclaim(backend)
        _submit(backend, evidence_id="EV002", claim_id="C002")
        status, result = _request(server, f"/api/repositories/{repository.id}/transition", {
            "preview_id": preview["preview_id"], "confirmed": True,
        }, token=token)
        assert status == 409 and result["error"]["code"] == "stale_preview"
        assert backend.get_task("T001").status.value == "needs_review"


def test_cli_task_action_uses_the_same_canonical_transition(native_action_project):
    backend, _, repository = native_action_project
    result = CliRunner().invoke(app, [
        "task-action", "T002", "ready", "--actor", "human",
        "--cwd", repository.root, "--json",
    ])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["status"] == "ready"
    assert backend.get_task("T002").status.value == "ready"
    refused = CliRunner().invoke(app, [
        "task-action", "T002", "start", "--actor", "human",
        "--cwd", repository.root, "--json",
    ])
    assert refused.exit_code == 1
    assert json.loads(refused.output)["error"]["code"] == "transition_refused"
    assert backend.get_task("T002").status.value == "ready"


@pytest.mark.skipif(os.name != "posix", reason="POSIX process resource limits")
def test_writer_can_append_beyond_reader_scratch_limit(action_catalog, tmp_path):
    import resource

    _, repository = action_catalog
    ledger = tmp_path / "large-events.jsonl"
    with ledger.open("wb") as stream:
        stream.truncate(web.MAX_SCRATCH_FILE_BYTES - 16)
    program = (
        "import json,resource,sys; "
        "print(json.dumps(resource.getrlimit(resource.RLIMIT_FSIZE))); "
        "open(sys.argv[1],'ab').write(b'{\\\"id\\\":\\\"E999999\\\",\\\"complete\\\":true}\\n')"
    )
    writer = repository.model_copy(update={"reader_argv": [sys.executable, "-I", "-c", program, str(ledger)]})
    result = json.loads(web._reader_output(writer, mutation=True))
    assert result == list(resource.getrlimit(resource.RLIMIT_FSIZE))
    assert ledger.stat().st_size > web.MAX_SCRATCH_FILE_BYTES
    reader = repository.model_copy(update={"reader_argv": [sys.executable, "-I", "-c",
        "import json,resource; print(json.dumps(resource.getrlimit(resource.RLIMIT_FSIZE)))"]})
    assert json.loads(web._reader_output(reader)) == [web.MAX_SCRATCH_FILE_BYTES] * 2


def test_failure_after_native_commit_reports_unknown_not_refusal(native_action_project, monkeypatch, capsys):
    from anvil import web_actions
    from anvil.task_actions import transition_task

    backend, _, repository = native_action_project

    def commit_then_fail(*args):
        task = backend.get_task("T002")
        transition_task(backend, task.id, "ready", "human",
                        expected_status=task.status.value, expected_updated_at=task.updated_at)
        raise OSError("simulated result delivery failure")

    monkeypatch.setattr(web_actions, "execute_action", commit_then_fail)
    monkeypatch.setattr(sys, "argv", ["worker", "execute", json.dumps({
        "repository": repository.model_dump(), "actor": "human", "request": {},
    })])
    web_actions.main()
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "outcome_unknown"
    assert backend.get_task("T002").status.value == "ready"


@pytest.mark.parametrize("response", [[], {}, {"ok": True, "data": []}, {"ok": False, "error": []}])
def test_malformed_writer_result_is_unknown(action_catalog, monkeypatch, response):
    _, repository = action_catalog
    monkeypatch.setattr(web, "_reader_output", lambda *args, **kwargs: json.dumps(response))
    with pytest.raises(web.WebError) as failure:
        web._action_worker(repository, "human", "execute", {})
    assert failure.value.code == "outcome_unknown"
