"""Execution HTTP trust boundaries and real native worker journeys; no model calls."""

from __future__ import annotations

import http.client
import json
import os
import shlex
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from anvil import web
from anvil.execution import git, journal_root, native_cli
from tests.test_bundle_state import _event
from tests.test_execution import execution_project as execution_project
from tests.test_execution import saved_execution_project as saved_execution_project
from tests.test_execution_runner import _wait, _wait_adapter
from tests.test_web_actions import _running_server
from tests.test_web_actions import action_catalog as action_catalog


def _request(server, path, payload=None, *, token=None, headers=None):
    # Native admission has a 20-second server bound plus bounded cleanup. The
    # client must allow that response; async behavior is checked with a held adapter.
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=30)
    request_headers = {} if payload is None else {
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
        connection.request("GET" if payload is None else "POST", path,
                           None if payload is None else json.dumps(payload), request_headers)
        response = connection.getresponse()
        raw = response.read()
        data = json.loads(raw) if response.getheader("Content-Type") == "application/json" else raw
        return response.status, data
    finally:
        connection.close()


@pytest.fixture
def execution_custody(execution_project, tmp_path, monkeypatch):
    _, repo, _, _ = execution_project
    # server_close joins all handlers before fixture teardown inspects launches,
    # including admissions whose response never supplied a run ID to the test.
    monkeypatch.setattr(web.WebHTTPServer, "daemon_threads", False)
    release = tmp_path / "release-adapter"
    try:
        yield release
    finally:
        release.touch(exist_ok=True)
        failures = []
        for launched in sorted(journal_root(repo).glob("runs/*/launched.json")):
            try:
                _wait(launched.parent)
            except BaseException as error:
                failures.append(error)
        if failures:
            raise BaseExceptionGroup("Fixture execution cleanup failed", failures)


def _selection(server, repository, task_id="T001", **extra):
    status, value = _request(server, f"/api/repositories/{repository.id}/snapshot")
    assert status == 200, value
    return {
        "task_id": task_id, "prd_id": "release", "operation": "run",
        "task_version": value["workflow"]["versions"]["release:" + task_id],
        **extra,
    }


def _preview(server, repository, token, **extra):
    status, value = _request(
        server, f"/api/repositories/{repository.id}/execution-preview",
        _selection(server, repository, **extra), token=token,
    )
    assert status == 200, value
    assert "cwd" not in json.dumps(value) and "binding" not in value
    return value


def _status(server, repository, preview):
    status, value = _request(
        server, f"/api/repositories/{repository.id}/execution-status?"
        + "inspection_id=" + preview["inspection_id"],
    )
    assert status == 200, value
    return value


def _confirm(server, repository, token, preview, route="execution-confirm"):
    return _request(server, f"/api/repositories/{repository.id}/{route}", {
        "preview_id": preview["preview_id"], "confirmed": True,
    }, token=token)


def test_real_saved_run_lost_response_and_continue(
    saved_execution_project, tmp_path, monkeypatch, execution_custody,
):
    backend, repo, state, _ = saved_execution_project
    config_path = tmp_path / "web.json"
    repository = web.enroll_repository(config_path, repo)
    with _running_server(config_path, allow_writes=True, actor="synthetic-owner") as server:
        token = _request(server, "/api/catalog")[1]["csrf_token"]
        preview = _preview(server, repository, token)
        assert preview["options"] == {}
        assert backend.list_active_claims() == []
        original_send = server.RequestHandlerClass._send
        response_dropped = threading.Event()

        def lose_dispatch_response(handler, body, content_type="application/json", status=200):
            if status == 202:
                response_dropped.set()
                handler.close_connection = True
                return
            original_send(handler, body, content_type, status)

        monkeypatch.setattr(server.RequestHandlerClass, "_send", lose_dispatch_response)
        with pytest.raises(http.client.RemoteDisconnected):
            _confirm(server, repository, token, preview)
        assert response_dropped.is_set()
        monkeypatch.setattr(server.RequestHandlerClass, "_send", original_send)
        recovered = _status(server, repository, preview)
        assert recovered["dispatch_state"] == "consumed"
        run_id = recovered["run_id"]
        directory = journal_root(repo) / "runs" / run_id
        try:
            assert _confirm(server, repository, token, preview)[0] == 409
            finished = _wait(directory)
            assert finished["state"] == "needs_review", finished
            assert backend.get_task("release:T001").status.value == "needs_review"
            first_claim = backend.get_latest_evidence("release:T001").claim_id
            assert backend.get_claim(first_claim).status.value == "released"
            assert _status(server, repository, preview)["continuation"]["state"] == "requires_preview"
            retained = (state / "events.jsonl").read_bytes()
            # Exact readback remains usable after the confirmation window closes.
            original_clock = web.time.monotonic
            monkeypatch.setattr(web.time, "monotonic", lambda: original_clock() + 301)
            assert _status(server, repository, preview)["run_id"] == run_id
            monkeypatch.setattr(web.time, "monotonic", original_clock)
            assert (state / "events.jsonl").read_bytes() == retained
            status, imported = _request(server,
                f"/api/repositories/{repository.id}/execution-inspect", {"run_id": run_id},
                token=token,
            )
            assert status == 200 and imported["run_id"] == run_id, imported
            # The writer can inspect a known project ID under native CLI status
            # authority, without claiming authenticated ownership of its actor.
            assert "actor" not in imported and "official_session_id" not in json.dumps(imported)
            checkout = Path(backend.get_claim(first_claim).worktree_path)
            git(checkout, "add", "README.md")
            git(checkout, "commit", "-qm", "Keep first returned edit")
            git(repo, "merge", "--ff-only", git(checkout, "rev-parse", "HEAD").decode().strip())
            native_cli(repo, "apply", "release:T001", "--reject", "--reviewer", "reviewer",
                       "--reason", "Make the follow-up change", "--json")
            task = backend.get_task("release:T001")
            command = shlex.join([sys.executable, "-I", "-c",
                                 "from pathlib import Path; assert Path('README.md').read_text() == 'continued\\n'"])
            backend.append(_event("task.created", {
                **task.model_dump(mode="json"), "prd_id": task.prd_id,
                "verification": {"commands": [command], "required_proofs": [
                    {"kind": "command", "command": command,
                     "passing_exit_codes": [0], "label": "continued edit"},
                ]},
            }, target_kind="task", target_id=task.id))
            native_cli(repo, "review", "tasks", "--prd", "release", "--json")
            continuation = _preview(server, repository, token, operation="continue",
                                    prior_inspection_id=imported["inspection_id"])
            status, dispatched = _confirm(server, repository, token, continuation)
            assert status == 202, dispatched
            next_directory = journal_root(repo) / "runs" / dispatched["run_id"]
            assert _wait(next_directory)["state"] == "needs_review"
            assert _status(server, repository, continuation)["operation"] == "continue"
            assert _confirm(server, repository, token, continuation)[0] == 409
            evidence = backend.get_latest_evidence("release:T001")
            assert evidence.claim_id != first_claim and evidence.proofs[0].evidence_core.generation == 2
            assert len(list(journal_root(repo).glob("runs/*/dispatch.json"))) == 2
            assert backend.get_task("release:T001").status.value == "needs_review"
        finally:
            # The normal native observer joins the exact fixture runner. No
            # browser/server closure sends a cancellation to its coordinator.
            if (directory / "launched.json").exists():
                _wait(directory)


def test_metadata_cas_verified_options_and_unknown_fields(
    saved_execution_project, tmp_path, execution_custody,
):
    _, repo, _, _ = saved_execution_project
    config_path = tmp_path / "web.json"
    repository = web.enroll_repository(config_path, repo)
    prefix = f"/api/repositories/{repository.id}"
    with _running_server(config_path, allow_writes=True, actor="synthetic-owner") as server:
        token = _request(server, "/api/catalog")[1]["csrf_token"]
        assert _request(server, prefix + "/execution-capabilities")[1]["status"] == "unknown"
        assert not list(journal_root(repo).glob("runs/*/dispatch.json"))
        status, preview = _request(server, prefix + "/metadata-preview", {}, token=token)
        assert status == 200, preview
        config = Path(os.environ["ANVIL_GLOBAL_CONFIG"])
        original = config.read_bytes()
        changed = yaml.safe_load(original)
        old_profile = Path(changed["execution_adapter"]["registration"]["profile"])
        next_profile = tmp_path / "replacement-profile.json"
        next_profile.write_bytes(old_profile.read_bytes())
        next_profile.chmod(0o600)
        changed["execution_adapter"]["registration"]["profile"] = str(next_profile)
        config.write_text(yaml.safe_dump(changed))
        status, refusal = _confirm(server, repository, token, preview, "metadata-confirm")
        assert status == 409 and refusal["error"]["code"] == "adapter_changed", refusal
        assert not list(journal_root(repo).glob("runs/*/dispatch.json"))
        config.write_bytes(original)
        status, preview = _request(server, prefix + "/metadata-preview", {
            "provider": "synthetic", "model": "model",
        }, token=token)
        assert status == 200, preview
        status, dispatched = _confirm(server, repository, token, preview, "metadata-confirm")
        assert status == 202, dispatched
        assert _wait(journal_root(repo) / "runs" / dispatched["run_id"])["state"] == "capabilities_ready"
        assert _status(server, repository, preview)["state"] == "capabilities_ready"
        capabilities = _request(server, prefix + "/execution-capabilities")[1]
        assert capabilities["status"] == "fresh"
        assert capabilities["thinking"] == {"provider": "synthetic", "model": "model",
                                             "levels": ["off", "low"]}
        assert "profile" not in json.dumps(capabilities) and "registration" not in capabilities
        selection = _selection(server, repository, options={
            "provider": "synthetic", "model": "model", "thinking": "low",
        })
        assert _request(server, prefix + "/execution-preview", selection, token=token)[0] == 200
        for options in ({"provider": "synthetic"}, {"thinking": "low"},
                        {"provider": "synthetic", "model": "model", "thinking": "dynamic"}):
            status, refusal = _request(server, prefix + "/execution-preview",
                                       {**selection, "options": options}, token=token)
            assert status == 409, refusal
        for added in ({"cwd": str(repo)}, {"options": {"argv": ["unsafe"]}},
                      {"wall_seconds": True}, {"prior_inspection_id": "a" * 43}):
            assert _request(server, prefix + "/execution-preview",
                            {**selection, **added}, token=token)[0] == 409
        assert len(list(journal_root(repo).glob("runs/*/dispatch.json"))) == 1


def test_running_http_returns_before_work_and_other_task_remains_usable(
    execution_project, tmp_path, execution_custody,
):
    backend, repo, _, registration = execution_project
    independent_task = backend.get_task("release:T002")
    independent_command = shlex.join([
        sys.executable, "-I", "-c",
        "from pathlib import Path; assert Path('src/T002.py').read_text() == 'implemented\\n'",
    ])
    backend.append(_event("task.created", {
        **independent_task.model_dump(mode="json"), "prd_id": independent_task.prd_id,
        "likely_files": ["src/T002.py"],
        "verification": {"commands": [independent_command], "required_proofs": [
            {"kind": "command", "command": independent_command,
             "passing_exit_codes": [0], "label": "independent edit"},
        ]},
    }, target_kind="task", target_id=independent_task.id))
    release = execution_custody
    Path(registration.profile).write_text(json.dumps({"wait_file": str(release)}))
    # Registration pins the profile; refresh the synthetic owner registration
    # before preview, exactly as an explicit native setup would.
    import dataclasses

    from anvil.config import register_execution_adapter

    configured = register_execution_adapter(
        Path(registration.script), Path(registration.interpreter), Path(registration.profile),
    )
    Path(os.environ["ANVIL_GLOBAL_CONFIG"]).write_text(yaml.safe_dump({
        "execution_adapter": dataclasses.asdict(configured),
    }))
    config_path = tmp_path / "web.json"
    repository = web.enroll_repository(config_path, repo)
    directory = None
    try:
        with _running_server(config_path, allow_writes=True, actor="synthetic-owner") as server:
            token = _request(server, "/api/catalog")[1]["csrf_token"]
            preview = _preview(server, repository, token)
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(
                    lambda _: _confirm(server, repository, token, preview), range(2),
                ))
            assert sorted(status for status, _ in responses) == [202, 409], responses
            admitted = next(value for status, value in responses if status == 202)
            directory = journal_root(repo) / "runs" / admitted["run_id"]
            _wait_adapter(directory)
            assert not release.exists()
            assert not (directory / "result.json").exists()
            assert _request(server, f"/api/repositories/{repository.id}/snapshot")[0] == 200
            independent = json.loads(native_cli(repo, "claim", "release:T002", "--worktree",
                                                "--actor", "independent", "--json"))
            assert independent["ok"] is True
            assert backend.get_task("release:T002").status.value == "claimed"
            assert not (directory / "result.json").exists()
        # Closing the HTTP server leaves the native run alive and able to submit.
        release.touch()
        assert _wait(directory)["state"] == "needs_review"
        independent_checkout = Path(independent["data"]["claim"]["worktree_path"])
        next_preview = json.loads(native_cli(
            independent_checkout, "run", "release:T002", "--preview", "--actor", "independent",
            "--json",
        ))["data"]
        # Admission returns before the detached coordinator. The synchronous
        # native_cli capture helper intentionally reaps descendants, so use the
        # actual CLI process and let this test's owned observer join the run.
        confirmed = subprocess.run([
            sys.executable, "-I", "-m", "anvil.cli", "run", "release:T002",
            "--confirm", next_preview["preview_id"], "--actor", "independent", "--json",
            "--cwd", str(independent_checkout),
        ], cwd=independent_checkout, capture_output=True, text=True, timeout=20, check=True)
        next_run = json.loads(confirmed.stdout)["data"]
        assert _wait(journal_root(repo) / "runs" / next_run["run_id"])["state"] == "needs_review"
        assert all(backend.get_task(task).status.value == "needs_review"
                   for task in ("release:T001", "release:T002"))
        assert backend.get_latest_evidence("release:T002").proofs[0].evidence_core.claimed_by == "independent"
    finally:
        release.touch(exist_ok=True)
        if directory is not None:
            _wait(directory)


def test_execution_access_tokens_and_poll_capacity(action_catalog, monkeypatch):
    config, repository = action_catalog
    prefix = f"/api/repositories/{repository.id}"
    calls = []

    def worker(repository, actor, operation, request=None):
        calls.append(operation)
        if operation in {"execution_preview", "metadata_preview"}:
            metadata = operation == "metadata_preview"
            return {"operation": "metadata" if metadata else "run", "title": "Preview",
                    "details": [], "binding": {
                        "kind": "execution_metadata" if metadata else "execution_run",
                        "operation": "metadata" if metadata else "run",
                    }}
        if operation == "preview":
            return {"binding": {"kind": "task_transition"}}
        return {"operation": "run", "state": "unknown"}

    monkeypatch.setattr(web, "_action_worker", worker)
    request = {"task_id": "T001", "prd_id": "default", "task_version": "a" * 64,
               "operation": "run"}
    with _running_server(config) as server:
        token = _request(server, "/api/catalog")[1]["csrf_token"]
        assert _request(server, prefix + "/execution-capabilities")[0] == 502
        assert _request(server, prefix + "/execution-preview", request, token=token)[0] == 405
    with _running_server(config, allow_writes=True, actor="operator") as server:
        token = _request(server, "/api/catalog")[1]["csrf_token"]
        for headers in ({"Origin": None}, {"X-Anvil-CSRF": "wrong"},
                        {"Origin": "https://foreign.invalid"}):
            assert _request(server, prefix + "/execution-preview", request,
                            token=token, headers=headers)[0] == 403
        preview = _request(server, prefix + "/execution-preview", request, token=token)[1]
        assert _confirm(server, repository, token, preview, "transition")[0] == 409
        preview = _request(server, prefix + "/execution-preview", request, token=token)[1]
        assert _confirm(server, repository, token, preview, "metadata-confirm")[0] == 409
        assert "execution_confirm" not in calls and "metadata_confirm" not in calls
        preview = _request(server, prefix + "/execution-preview", request, token=token)[1]
        for extra in ({"confirmed": 1}, {"reason": "no extra authority"}, {"run_id": "a" * 32}):
            assert _request(server, prefix + "/execution-confirm", {
                "preview_id": preview["preview_id"], "confirmed": True, **extra,
            }, token=token)[0] == 409
        assert _request(server, prefix + "/execution-status?inspection_id="
                        + preview["inspection_id"] + "&extra=1")[0] == 502
        assert _status(server, repository, preview)["state"] == "unknown"
        # Filling reader workers does not consume the reserved writer slots.
        for _ in range(4):
            assert web._READER_SLOTS.acquire(blocking=False)
        try:
            monkeypatch.setattr(web, "_bounded_reader_output", lambda *args, **kwargs: b"{}")
            assert web._reader_output(repository, mutation=True) == b"{}"
        finally:
            for _ in range(4):
                web._READER_SLOTS.release()
    configured = web.load_config(config)
    configured.repositories[0].reader_argv = [sys.executable, "-I", "-c", "print('{}')"]
    web.save_config(config, configured)
    with _running_server(config, allow_writes=True, actor="operator") as server:
        assert _request(server, prefix + "/execution-capabilities")[0] == 502


def test_native_errors_are_sanitized_and_preview_task_cas(execution_project, tmp_path):
    backend, repo, _, _ = execution_project
    config_path = tmp_path / "web.json"
    repository = web.enroll_repository(config_path, repo)
    with _running_server(config_path, allow_writes=True, actor="synthetic-owner") as server:
        token = _request(server, "/api/catalog")[1]["csrf_token"]
        selection = _selection(server, repository)
        task = backend.get_task("release:T001")
        backend.append(_event("task.created", {
            **task.model_dump(mode="json"), "prd_id": task.prd_id, "title": "Changed title",
        }, target_kind="task", target_id=task.id))
        status, refusal = _request(server, f"/api/repositories/{repository.id}/execution-preview",
                                   selection, token=token)
        assert status == 409 and refusal["error"]["code"] == "stale_preview"
        Path(repo / "private-untracked.txt").write_text("private")
        status, refusal = _request(server, f"/api/repositories/{repository.id}/execution-preview",
                                   _selection(server, repository), token=token)
        assert status == 409 and refusal["error"]["code"] == "dirty_source", refusal
        assert str(repo) not in json.dumps(refusal) and "private-untracked" not in json.dumps(refusal)
        assert backend.list_active_claims() == []


def test_writer_preserves_temporary_directory_beyond_http_request(action_catalog, tmp_path, monkeypatch):
    _, repository = action_catalog
    owner_temp = tmp_path / "owner-temp"
    owner_temp.mkdir()
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(name, str(owner_temp))
    worker = repository.model_copy(update={"reader_argv": [
        sys.executable, "-I", "-c",
        "import json,os,tempfile; print(json.dumps([tempfile.gettempdir(),os.environ['TMPDIR']]))",
    ]})
    assert json.loads(web._reader_output(worker, mutation=True)) == [str(owner_temp)] * 2
    assert owner_temp.is_dir()
    scratch = json.loads(web._reader_output(worker))
    assert scratch[0] != str(owner_temp) and scratch[0] == scratch[1]
    assert not Path(scratch[0]).exists()


def test_concurrent_known_run_inspection_has_independent_handles(
    saved_execution_project, tmp_path, monkeypatch, execution_custody,
):
    from tests.test_execution import _completed_saved_run

    _, repo, _, _ = saved_execution_project
    _, admitted, _ = _completed_saved_run(saved_execution_project)
    config = tmp_path / "web.json"
    repository = web.enroll_repository(config, repo)
    worker_started, follower_waiting, release = (threading.Event() for _ in range(3))
    original_worker = web._action_worker
    calls = []

    class ObservedFuture(web.Future):
        def result(self, *args, **kwargs):
            follower_waiting.set()
            return super().result(*args, **kwargs)

    def blocked_worker(repository, actor, operation, request=None):
        calls.append(operation)
        worker_started.set()
        assert release.wait(timeout=10)
        return original_worker(repository, actor, operation, request)

    monkeypatch.setattr(web, "Future", ObservedFuture)
    monkeypatch.setattr(web, "_action_worker", blocked_worker)
    with _running_server(config, allow_writes=True, actor="synthetic-owner") as server:
        token = _request(server, "/api/catalog")[1]["csrf_token"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            requests = [pool.submit(
                _request, server, f"/api/repositories/{repository.id}/execution-inspect",
                {"run_id": admitted["run_id"]}, token=token,
            ) for _ in range(2)]
            try:
                assert worker_started.wait(timeout=5)
                assert follower_waiting.wait(timeout=5)
            finally:
                release.set()
            responses = [request.result(timeout=10) for request in requests]
        assert calls == ["execution_inspect"]
        assert [status for status, _ in responses] == [200, 200]
        assert len({value["inspection_id"] for _, value in responses}) == 2
        for _, value in responses:
            assert value["run_id"] == admitted["run_id"] and "binding" not in value
            assert _status(server, repository, value)["run_id"] == admitted["run_id"]
