"""Control-plane refusal tests use a fake runtime; they never create containers."""

import hashlib
import json
from pathlib import Path

import pytest
from benchmarks import openshell_execution_scale as adapter

IMAGE_ID = "sha256:" + "a" * 64


@pytest.fixture
def options():
    return {"workers": 2, "tasks": 4, "cpu_milli": 250, "memory_mib": 128,
            "max_total_cpu": 4, "max_total_memory_mib": 4096, "image": "fixture:test",
            "image_id": IMAGE_ID, "openshell": "/usr/bin/openshell", "gateway": "fixture",
            "workspace": "default", "command_timeout": 30, "durability": "relaxed",
            "events_storage": "local", "previous_receipt": None}


def _resource(sandbox_id, role):
    return {"id": sandbox_id + "-" + role, "image": IMAGE_ID if role == "sandbox" else "supervisor-image",
            "labels": {adapter.SANDBOX_LABEL: sandbox_id, adapter.ROLE_LABEL: role},
            "nano_cpus": 250_000_000 if role == "sandbox" else 0,
            "memory": 128 * 1024 * 1024 if role == "sandbox" else 0,
            "memory_swap": 0, "pids_limit": 2048 if role == "sandbox" else None,
            "device_requests": None, "devices": [], "mounts": [], "privileged": False,
            "network": "none" if role == "sandbox" else "host", "running": True}


class FakeRuntime:
    def __init__(self):
        self.sandboxes = {}
        self.resources = {}
        self.uploads = {}
        self.calls = []
        self.ignore_update = False
        self.fail_after_create = False

    def run(self, arguments, **kwargs):
        self.calls.append(arguments)
        if arguments[:3] == ["docker", "image", "inspect"]:
            return IMAGE_ID.encode()
        if arguments[:2] == ["docker", "ps"]:
            selector = arguments[arguments.index("--filter") + 1]
            label, value = selector.removeprefix("label=").split("=", 1)
            if label == "openshell.ai/sandbox-name":
                value = self.sandboxes.get(value, {}).get("id", "absent")
            return "\n".join(item["id"] for item in self.resources.get(value, [])).encode()
        if arguments[:2] == ["docker", "inspect"]:
            item = next(item for resources in self.resources.values() for item in resources
                        if item["id"] == arguments[-1])
            return json.dumps(item).encode()
        if arguments[:2] == ["docker", "update"]:
            item = next(item for resources in self.resources.values() for item in resources
                        if item["id"] == arguments[-1])
            assert item["labels"][adapter.ROLE_LABEL] == "supervisor"
            if not self.ignore_update:
                item.update(nano_cpus=250_000_000, memory=128 * 1024 * 1024, pids_limit=128)
            return b"updated"
        command = arguments[arguments.index("sandbox") + 1:]
        operation = command[0]
        if operation == "list":
            return json.dumps({"sandboxes": list(self.sandboxes.values()), "next_page_token": ""}).encode()
        if operation == "create":
            name = command[command.index("--name") + 1]
            nonce = command[command.index("--label") + 1].split("=", 1)[1]
            sandbox_id = f"id-{len(self.sandboxes)}"
            self.sandboxes[name] = {"id": sandbox_id, "name": name, "workspace": "default",
                                    "labels": {adapter.RUN_LABEL: nonce}, "created_at": "now"}
            self.resources[sandbox_id] = [_resource(sandbox_id, role) for role in ("sandbox", "supervisor")]
            if self.fail_after_create:
                raise TimeoutError("create response lost after owned resource appeared")
            return b"created"
        if operation == "get":
            return json.dumps(self.sandboxes[command[1]]).encode()
        if operation == "provider":
            return b'{"providers": [], "next_page_token": ""}'
        if operation == "upload":
            assert command[1] == "--no-git-ignore"
            self.uploads[command[2]] = Path(command[3]).read_bytes()
            return b"uploaded"
        if operation == "exec":
            return json.dumps({"sha256": hashlib.sha256(self.uploads[command[1]]).hexdigest(),
                               "flock": "/usr/bin/flock"}).encode()
        if operation == "delete":
            self.resources.pop(self.sandboxes.pop(command[1])["id"])
            return b"deleted"
        raise AssertionError(f"Unexpected control command: {arguments}")


@pytest.fixture
def fake_runtime(monkeypatch):
    runtime = FakeRuntime()
    monkeypatch.setattr(adapter, "_run", runtime.run)
    monkeypatch.setattr(adapter, "_resource_budget", lambda options: {"requested_cpu": 1})
    monkeypatch.setattr(adapter.execution_scale, "_git", lambda *arguments: "fixture-commit")
    return runtime


def test_prepare_pins_uploaded_bytes_caps_both_roles_and_declares_actual_cli(tmp_path, options, fake_runtime):
    project = tmp_path / "project"
    project.mkdir()
    source = b"print('fixture')\n"
    (project / "verify.py").write_bytes(source)
    owner = adapter.OwnedSandboxes(tmp_path / "owned.json", options)
    command = owner.prepare(project)
    first = command("scale:T001")
    second = command("scale:T002.1")
    assert first != second
    assert first[-3:] == ["python3", "verify.py", "scale:T001"]
    assert first[-5:-3] == ["/usr/bin/flock", "/sandbox/execution.lock"]
    assert "--no-login-shell" in first and "--no-tty" in first
    assert first[first.index("--timeout") + 1] == "28"
    assert all(payload == source for payload in fake_runtime.uploads.values())
    assert len(owner.journal["sandboxes"]) == 2
    for record in owner.journal["sandboxes"]:
        assert len(record["name"]) <= 19
        adapter._verify_resources(record["containers"], record["id"], IMAGE_ID, 250, 128)
        assert record["fixture_sha256"] == hashlib.sha256(source).hexdigest()
    assert owner.cleanup()["complete"]
    assert not fake_runtime.sandboxes and not fake_runtime.resources


def test_uncapped_companion_refuses_before_upload_or_work(tmp_path, options, fake_runtime):
    (tmp_path / "verify.py").write_text("pass\n")
    fake_runtime.ignore_update = True
    owner = adapter.OwnedSandboxes(tmp_path / "owned.json", options)
    with pytest.raises(ValueError, match="Resource/isolation"):
        owner.prepare(tmp_path)
    assert not fake_runtime.uploads
    assert owner.cleanup()["complete"]


def test_restart_cleanup_reconciles_lost_create_receipt_from_owned_nonce(tmp_path, options, fake_runtime):
    (tmp_path / "verify.py").write_text("pass\n")
    fake_runtime.fail_after_create = True
    owner = adapter.OwnedSandboxes(tmp_path / "owned.json", options)
    with pytest.raises(TimeoutError):
        owner.prepare(tmp_path)
    assert owner.journal["sandboxes"][0]["id"] is None
    recovered = adapter.OwnedSandboxes(owner.path)
    assert recovered.cleanup()["complete"]
    assert not fake_runtime.sandboxes and not fake_runtime.resources


def test_cleanup_refuses_reused_name_with_other_identity(tmp_path, options, fake_runtime):
    (tmp_path / "verify.py").write_text("pass\n")
    owner = adapter.OwnedSandboxes(tmp_path / "owned.json", options)
    owner.prepare(tmp_path)
    first = owner.journal["sandboxes"][0]
    fake_runtime.sandboxes[first["name"]]["id"] = "replacement-id"
    receipt = adapter.OwnedSandboxes(owner.path).cleanup()
    assert not receipt["complete"]
    assert first["name"] in fake_runtime.sandboxes
    assert not any(call[-2:] == ["delete", first["name"]] for call in fake_runtime.calls)


def test_invisible_pending_create_remains_unresolved_until_owned_resource_appears(
    tmp_path, options, fake_runtime,
):
    (tmp_path / "verify.py").write_text("pass\n")
    fake_runtime.fail_after_create = True
    owner = adapter.OwnedSandboxes(tmp_path / "owned.json", options)
    with pytest.raises(TimeoutError):
        owner.prepare(tmp_path)
    pending_catalog, pending_resources = fake_runtime.sandboxes, fake_runtime.resources
    fake_runtime.sandboxes, fake_runtime.resources = {}, {}
    recovered = adapter.OwnedSandboxes(owner.path)
    assert not recovered.cleanup()["complete"]
    assert "unresolved" in recovered.journal["cleanup"]["failures"][0]["error"]
    assert not recovered.journal["sandboxes"][0].get("deleted", False)
    fake_runtime.sandboxes, fake_runtime.resources = pending_catalog, pending_resources
    assert adapter.OwnedSandboxes(owner.path).cleanup()["complete"]
    assert not fake_runtime.sandboxes and not fake_runtime.resources


@pytest.mark.parametrize("change", [
    {"nano_cpus": 0}, {"memory": 0}, {"pids_limit": None},
    {"device_requests": [{"Count": 1}]}, {"privileged": True},
    {"mounts": [{"Type": "bind", "Destination": "/home"}]},
])
def test_resource_readback_fails_closed(change):
    resources = [_resource("owned", role) for role in ("sandbox", "supervisor")]
    resources[1].update(nano_cpus=250_000_000, memory=128 * 1024 * 1024, pids_limit=128)
    resources[1].update(change)
    with pytest.raises(ValueError):
        adapter._verify_resources(resources, "owned", IMAGE_ID, 250, 128)


def test_step_up_requires_prior_matching_wave_and_latency(tmp_path, options, monkeypatch):
    source_identity = {"fixture": "hash"}
    monkeypatch.setattr(adapter.execution_scale, "_source_identity", lambda: source_identity)
    receipt = {"validated": True, "cleanup": {"complete": True}, "sandbox_workers": 2,
               "openshell": {"image_id": IMAGE_ID}, "source_sha256": source_identity,
               "adapter_sha256": hashlib.sha256(Path(adapter.__file__).read_bytes()).hexdigest(),
               "durability": "relaxed", "events_storage": "local", "minimum_lease_margin_seconds": 120,
               "stages": {stage: {"samples": 4, "p95_ms": 10} for stage in adapter.NATIVE_STAGES}}
    path = tmp_path / "previous.json"
    path.write_text(json.dumps(receipt))
    options.update(workers=8, tasks=1000, previous_receipt=str(path))
    adapter._check_options(options)
    receipt["stages"]["evidence_append"]["p95_ms"] = 16
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="Previous wave"):
        adapter._check_options(options)


def test_aggregate_admission_preserves_host_capacity_reserve(options, monkeypatch):
    monkeypatch.setattr(adapter.os, "sched_getaffinity", lambda _pid: set(range(36)))
    monkeypatch.setattr(Path, "read_text", lambda _path: "MemAvailable: 1048576 kB\n")
    options.update(workers=8)
    with pytest.raises(ValueError, match="capacity reserve"):
        adapter._resource_budget(options)
