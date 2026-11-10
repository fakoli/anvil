"""Bounded readers and repository isolation at the native web boundary."""

import http.client
import json
import shutil
import sys
import threading
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import anvil.web as web
from anvil.cli import app
from anvil.cli.project import API_VERSION
from anvil.project_snapshot import read_project_snapshot
from anvil.read_contracts import ProjectSnapshotPayloadV1, snapshot_digest
from anvil.state.schema import get_schema_version
from tests.test_project_snapshot import cli_populated as cli_populated


@pytest.fixture
def enrolled(tmp_path, cli_populated, monkeypatch):
    _, state = cli_populated
    snapshot = read_project_snapshot(state).model_dump(mode="json")
    snapshot.update(
        operation_id="state.project.snapshot",
        operation_version=1,
        output_schema_id="anvil.state.project-snapshot.v1",
        api_version=API_VERSION,
        schema_version=get_schema_version(),
        digest_algorithm="sha256",
        truncated=False,
    )
    script = tmp_path / "reader.py"
    script.write_text(
        "import json, os, pathlib\n"
        "assert not any(key in os.environ for key in "
        '("ANVIL_ROOT", "ANVIL_PRD", "ANVIL_STATE_LAYOUT", "PYTHONPATH"))\n'
        'print(pathlib.Path("snapshot.json").read_text())\n'
    )
    config_path = tmp_path / "web.json"
    repositories = []
    for index in range(2):
        root = tmp_path / f"repository-{index}"
        root.mkdir()
        data = deepcopy(snapshot)
        data["payload"]["project"]["name"] = f"Repository {index}"
        data["payload"]["tasks"][0]["title"] = f"Unique task {index}"
        data["snapshot_digest"] = snapshot_digest(
            ProjectSnapshotPayloadV1.model_validate(data["payload"])
        )
        (root / "snapshot.json").write_text(json.dumps({"ok": True, "data": data}))
        repositories.append(
            web.enroll_repository(
                config_path, root, reader_argv=[sys.executable, str(script)]
            )
        )
    return config_path, repositories


def test_enrollment_survives_cwd_and_clears_project_environment(
    enrolled, tmp_path, monkeypatch
):
    config_path, repositories = enrolled
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    for key in ("ANVIL_ROOT", "ANVIL_PRD", "ANVIL_STATE_LAYOUT", "PYTHONPATH"):
        monkeypatch.setenv(key, "must-not-reach-reader")
    config = web.load_config(config_path)
    assert config.repositories == repositories
    assert len(web.catalog(config)["repositories"]) == 2
    assert (
        web.read_snapshot(repositories[0])["payload"]["tasks"][0]["title"]
        == "Unique task 0"
    )
    assert not (elsewhere / ".anvil").exists()


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("malformed", "invalid_snapshot"),
        ("digest", "snapshot_digest_mismatch"),
        ("identity", "project_identity_changed"),
        ("schema", "reader_schema_changed"),
    ],
)
def test_reader_refuses_changed_or_malformed_snapshot(enrolled, change, code):
    _, repositories = enrolled
    repository = repositories[0]
    path = Path(repository.root) / "snapshot.json"
    response = json.loads(path.read_text())
    if change == "malformed":
        path.write_text("not JSON")
    else:
        data = response["data"]
        if change == "digest":
            data["payload"]["tasks"][0]["title"] = "Unbound change"
        elif change == "identity":
            data["payload"]["project"]["project_id"] = "different-project"

            # All scoped references must remain internally consistent.
            def replace_identity(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        if key == "project_id":
                            value[key] = "different-project"
                        else:
                            replace_identity(item)
                elif isinstance(value, list):
                    for item in value:
                        replace_identity(item)

            replace_identity(data["payload"])
            data["snapshot_digest"] = snapshot_digest(
                ProjectSnapshotPayloadV1.model_validate(data["payload"])
            )
        else:
            data["schema_version"] += 1
        path.write_text(json.dumps(response))
    with pytest.raises(web.WebError) as failure:
        web.read_snapshot(repository)
    assert failure.value.code == code


@pytest.mark.parametrize(
    ("program", "code"),
    [
        ("import time; time.sleep(10)", "reader_timeout"),
        (
            'import sys; sys.stdout.write("x" * 100000); sys.stdout.flush()',
            "reader_output_limit",
        ),
    ],
)
def test_real_child_reader_budgets(enrolled, monkeypatch, program, code):
    _, repositories = enrolled
    monkeypatch.setattr(web, "READER_TIMEOUT_SECONDS", 0.7)
    monkeypatch.setattr(web, "MAX_OUTPUT_BYTES", 1024)
    repository = repositories[0].model_copy(
        update={"reader_argv": [sys.executable, "-c", program]}
    )
    with pytest.raises(web.WebError) as failure:
        web.read_snapshot(repository)
    assert failure.value.code == code


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.test",
        "https://example.test/path",
        "https://user@example.test",
        "https://example.test?query",
        "https://example.test/",
    ],
)
def test_https_allowlist_requires_exact_origin(origin):
    with pytest.raises(ValidationError):
        web.WebConfig(allowed_https_origins=[origin])


def test_http_scope_and_read_only_boundary(enrolled):
    config_path, repositories = enrolled
    config = web.load_config(config_path)
    config.allowed_https_origins = ["https://ledger.example.test"]
    web.save_config(config_path, config)
    server = web.make_server(config_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path, headers=None, method="GET"):
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=5
        )
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read(), response.headers
        finally:
            connection.close()

    try:
        for index, repository in enumerate(repositories):
            status, body, headers = request(
                f"/api/repositories/{repository.id}/snapshot"
            )
            assert status == 200
            value = json.loads(body)
            assert value["repository"]["id"] == repository.id
            assert (
                value["snapshot"]["payload"]["tasks"][0]["title"]
                == f"Unique task {index}"
            )
            assert f"Unique task {1 - index}".encode() not in body
            assert headers["Cache-Control"] == "no-store"
        assert request("/api/catalog", {"Host": "foreign.example.test"})[0] == 403
        assert (
            request("/api/catalog", {"Origin": "https://foreign.example.test"})[0]
            == 403
        )
        assert request("/api/catalog", {"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert (
            request(
                "/api/catalog",
                {
                    "Host": "ledger.example.test",
                    "Origin": "https://ledger.example.test",
                },
            )[0]
            == 200
        )
        assert request("/api/catalog", method="POST")[0] in (403, 405, 501)
        assert request("/api/repositories/unknown/snapshot")[0] == 404
        (Path(repositories[0].root) / "snapshot.json").write_text("bad JSON")
        status, body, _ = request(f"/api/repositories/{repositories[0].id}/snapshot")
        assert status == 502
        assert json.loads(body)["error"]["code"] == "invalid_snapshot"
        assert request(f"/api/repositories/{repositories[1].id}/snapshot")[0] == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_cli_enrollment_listing_and_removal(enrolled, tmp_path, monkeypatch):
    _, repositories = enrolled
    repository = repositories[0]
    config_path = tmp_path / "cli-web.json"
    runner = CliRunner()
    prefix = ["web", "--config", str(config_path)]
    result = runner.invoke(
        app,
        [
            *prefix,
            "enroll",
            repository.root,
            "--label",
            "Chosen repository",
            "--reader-argv",
            json.dumps(repository.reader_argv),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert web.load_config(config_path).repositories[0].label == "Chosen repository"
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, [*prefix, "list", "--json"])
    assert result.exit_code == 0, result.output
    assert "Chosen repository" in result.stdout
    result = runner.invoke(app, [*prefix, "remove", repository.id, "--json"])
    assert result.exit_code == 0, result.output
    assert web.load_config(config_path).repositories == []


def test_reader_exit_failure_cannot_claim_a_successful_snapshot(enrolled):
    _, repositories = enrolled
    repository = repositories[0]
    script = Path(repository.reader_argv[-1])
    script.write_text(script.read_text() + "\nraise SystemExit(7)\n")
    with pytest.raises(web.WebError, match="exited unsuccessfully"):
        web.read_snapshot(repository)


@pytest.mark.parametrize("reader", ["[]", "null", "false", "{}"])
def test_explicit_invalid_reader_never_falls_back(tmp_path, reader):
    config = tmp_path / "web.json"
    result = CliRunner().invoke(
        app,
        [
            "web",
            "--config",
            str(config),
            "enroll",
            str(tmp_path),
            "--reader-argv",
            reader,
            "--json",
        ],
    )
    assert result.exit_code != 0
    assert json.loads(result.stdout)["error"]["code"] == "invalid_reader"
    assert not config.exists()
    assert not (tmp_path / ".anvil").exists()


def test_default_reader_cannot_execute_repository_modules_or_sitecustomize(
    tmp_path, cli_populated, monkeypatch
):
    from anvil.cli._helpers import _home_workspace_base

    root, state = cli_populated
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("ANVIL_STATE_LAYOUT", raising=False)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong-repository"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "wrong-worktree"))
    monkeypatch.delenv("GIT_DIR")
    monkeypatch.delenv("GIT_WORK_TREE")
    workspace = _home_workspace_base(root)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong-repository"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "wrong-worktree"))
    workspace.mkdir(parents=True)
    shutil.move(str(state), str(workspace / ".anvil"))
    state = workspace / ".anvil"
    package = root / "anvil"
    package.mkdir()
    marker = root / "repository-code-executed"
    malicious = (
        f"from pathlib import Path; Path({str(marker)!r}).write_text('executed')\n"
    )
    (package / "__init__.py").write_text(malicious)
    (package / "web.py").write_text(malicious)
    (package / "cli.py").write_text(malicious)
    (root / "sitecustomize.py").write_text(malicious)
    before = (state / "state.db").read_bytes(), (state / "events.jsonl").read_bytes()
    repository = web.enroll_repository(tmp_path / "catalog.json", root)
    assert "-I" in repository.reader_argv
    assert (
        web.read_snapshot(repository)["payload"]["project"]["project_id"] == "project-1"
    )
    assert not marker.exists()
    assert before == (
        (state / "state.db").read_bytes(),
        (state / "events.jsonl").read_bytes(),
    )


def test_reader_materializes_valid_optional_task_defaults(enrolled):
    _, repositories = enrolled
    repository = repositories[0]
    path = Path(repository.root) / "snapshot.json"
    data = json.loads(path.read_text())
    task = data["data"]["payload"]["tasks"][0]
    for field in ("dependency_refs", "acceptance_criteria", "verification_summaries"):
        task.pop(field, None)
    data["data"]["snapshot_digest"] = snapshot_digest(
        ProjectSnapshotPayloadV1.model_validate(data["data"]["payload"])
    )
    path.write_text(json.dumps(data))
    observed = web.read_snapshot(repository)["payload"]["tasks"][0]
    for field in ("dependency_refs", "acceptance_criteria", "verification_summaries"):
        assert observed[field] == []


@pytest.mark.parametrize("change", ["missing_limits", "lower_task_ceiling"])
def test_reader_honors_complete_applied_limits(enrolled, change):
    _, repositories = enrolled
    repository = repositories[0]
    path = Path(repository.root) / "snapshot.json"
    data = json.loads(path.read_text())
    if change == "missing_limits":
        del data["data"]["applied_limits"]
    else:
        data["data"]["applied_limits"]["max_tasks"] = 1
    path.write_text(json.dumps(data))
    with pytest.raises(web.WebError, match="malformed"):
        web.read_snapshot(repository)


@pytest.mark.parametrize(
    "field,value",
    [
        ("api_version", None),
        ("api_version", True),
        ("api_version", 0),
        ("operation_version", True),
    ],
)
def test_reader_refuses_incomplete_or_noninteger_provenance(enrolled, field, value):
    config_path, repositories = enrolled
    repository = repositories[0]
    path = Path(repository.root) / "snapshot.json"
    data = json.loads(path.read_text())
    data["data"][field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(web.WebError, match="incomplete"):
        web.read_snapshot(repository)
    with pytest.raises(web.WebError, match="incomplete"):
        web.enroll_repository(
            config_path, Path(repository.root), reader_argv=repository.reader_argv
        )


def test_reader_never_forwards_unknown_envelope_fields(enrolled):
    _, repositories = enrolled
    repository = repositories[0]
    path = Path(repository.root) / "snapshot.json"
    data = json.loads(path.read_text())
    original_digest = data["data"]["snapshot_digest"]
    data["data"]["unexpected_private_reader_detail"] = {"never_forward": "reader-only"}
    path.write_text(json.dumps(data))
    observed = web.read_snapshot(repository)
    assert "unexpected_private_reader_detail" not in observed
    assert observed["snapshot_digest"] == original_digest
    assert set(observed) == {
        "operation_id",
        "operation_version",
        "output_schema_id",
        "api_version",
        "schema_version",
        "digest_algorithm",
        "truncated",
        "payload",
        "event_cursor",
        "applied_limits",
        "snapshot_digest",
    }
