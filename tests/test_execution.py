"""Disposable native Run admission; synthetic adapter, real claims and proofs."""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from anvil.cli._helpers import _resolve_state_dir, prd_source_path
from anvil.clock import SystemClock
from anvil.config import register_execution_adapter
from anvil.execution import (
    ExecutionError,
    RunOptions,
    confirm_run,
    digest,
    journal_root,
    native_cli,
    preview_run,
    read_receipt,
    scope_digest,
)
from anvil.state.sqlite import SqliteBackend
from tests.test_bundle_state import _event, _seed

# A real executable contract double. It edits returned bytes, never fabricates
# native proof or native state. The Run worker must run and submit real checks.
_ADAPTER = r"""
import hashlib, io, json, pathlib, subprocess, sys, tarfile, uuid

TOOLS = {"read", "write", "edit", "grep", "find", "ls"}
def sha(raw): return hashlib.sha256(raw).hexdigest()
def validate_task(task):
    assert task['version'] == 2
    assert task['kind'] == 'implementation'
    if len(task['task_id']) > 128 or len(task['instructions'].encode()) > 32000:
        raise ValueError('Synthetic adapter task exceeds the actual v2 bounds')
    return task

def profile_settings(path): return {"model":"synthetic/model", "sandbox_name":"synthetic-owned", "sandbox_id":"synthetic-sandbox", "thinking":"low", **json.loads(pathlib.Path(path).read_text())}
def snapshot(root): return {str(p.relative_to(root)):p.read_bytes() for p in root.rglob("*") if p.is_file()}
def frozen_source(task):
    root, revision = task['repository'], task['revision']
    raw = subprocess.check_output(['git', '-C', root, 'archive', revision])
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        if any(not item.isfile() and not item.isdir() for item in archive):
            raise ValueError('Synthetic adapter refuses nonregular source members')
        files = {item.name: archive.extractfile(item).read() for item in archive if item.isfile()}
    return files, {'kind':'git','commit':revision,'files':{p:sha(b) for p,b in files.items()},'modes':{p:420 for p in files}}
def verify_return(results, task, files, run_id):
    worker = json.loads((results/'worker-receipt.json').read_text())
    assert worker['run_id'] == run_id and worker['task_id'] == task['task_id']
    assert worker['artifacts'] == {str(p.relative_to(results)):sha(p.read_bytes()) for p in results.rglob('*') if p.is_file() and p.name != 'worker-receipt.json'}
    return worker

def owner_receipt(profile):
    value={'sandbox_before':{'id':profile['sandbox_id'],'main_process_instance_id':'before','phase':'Stopped'},'sandbox_running':{'id':profile['sandbox_id'],'main_process_instance_id':'owned-main','phase':'Ready'},'sandbox_after':{'id':profile['sandbox_id'],'main_process_instance_id':'owned-main','phase':'Stopped'}}
    case=profile.get('bad_owner')
    if case=='missing': return {}
    if case=='foreign': value['sandbox_after']['id']='foreign-sandbox'
    if case=='active': value['sandbox_after']['phase']='Ready'
    if case=='main_mismatch': value['sandbox_after']['main_process_instance_id']='foreign-main'
    if case=='missing_main':
        value['sandbox_running'].pop('main_process_instance_id'); value['sandbox_after'].pop('main_process_instance_id')
    if case=='protected': value['protected_unchanged']=False
    if case=='cleanup': value['cleanup_complete']=False
    return value

def configuration_for(profile):
    catalog=[{'provider':'synthetic','id':'model'}]
    if profile.get('mixed_catalog'): catalog += [{'provider':'synthetic','id':'m'*256},{'provider':'p'*129,'id':'other'}]
    return {'available_models':catalog,'effective':{'provider':'synthetic','model':'m'*256 if profile.get('invalid_selected') else 'model','thinking':'low'},'supported_thinking_levels':['off','low']}

def main():
    metadata = sys.argv[1] == 'capabilities'
    task = {'version':2,'task_id':'capabilities','kind':'validation','instructions':'metadata','writable_paths':[],'checks':[],'run_options':{}} if metadata else json.loads(pathlib.Path(sys.argv[2]).read_text())
    output = pathlib.Path(sys.argv[sys.argv.index('--output')+1]); output.mkdir()
    profile = profile_settings(sys.argv[sys.argv.index('--profile')+1])
    if profile.get('wait_file'):
        import time
        while not pathlib.Path(profile['wait_file']).exists(): time.sleep(.02)
    files, source = ({}, {'kind':'empty-workspace','files':{},'modes':{}}) if metadata else frozen_source(task)
    run_id = uuid.uuid4().hex
    native = {**task,'run_id':run_id,'source':source,'operation':'run','max_tool_calls':40,'max_turns':32,'allowed_tools':sorted(TOOLS),'runtime_defaults':{'model':profile['model'],'thinking':profile.get('thinking','high')}}
    if metadata: native['operation']='capabilities'
    if profile.get('wrong_options'): native['run_options']={'thinking':'max'}
    with tarfile.open(output/'inputs.tar','x') as archive:
        raw = json.dumps(native).encode(); item=tarfile.TarInfo('task.json'); item.size=len(raw); archive.addfile(item,io.BytesIO(raw))
    results=output/'results'; results.mkdir()
    if metadata:
        configuration={**configuration_for(profile),'prompt_sent':False}
        worker={'run_id':run_id,'task_id':'capabilities','configuration':configuration,'artifacts':{}}
        (results/'worker-receipt.json').write_text(json.dumps(worker))
        receipt={'version':2,'task_id':'capabilities','run_id':run_id,'input_sha256':sha((output/'inputs.tar').read_bytes()),'worker':worker,'capabilities_valid':True,'cleanup_complete':True,'protected_unchanged':True,'artifacts_verified':True,'acceptance':'not_granted','outcome':'capabilities'}
        receipt.update(owner_receipt(profile))
        (output/'receipt.json').write_text(json.dumps(receipt)); return
    (results/'changed-files').mkdir()
    name=task['writable_paths'][0]; target=results/'changed-files'/name; target.parent.mkdir(parents=True,exist_ok=True)
    target.write_bytes(profile.get('returned_content', 'implemented\n').encode())
    changes=[{'path':name,'before_sha256':sha(files[name]) if name in files else None,'after_sha256':sha(target.read_bytes())}]
    if profile.get('wrong_preimage'): changes[0]['before_sha256']='0'*64
    (results/'changes.json').write_text(json.dumps(changes))
    configuration={**configuration_for(profile),'session_file':None}
    (results/'rpc-configuration.json').write_text(json.dumps(configuration))
    worker={'run_id':run_id,'task_id':task['task_id'],'checks':[],'configuration':configuration,'artifacts':{str(p.relative_to(results)):sha(p.read_bytes()) for p in results.rglob('*') if p.is_file()}}
    (results/'worker-receipt.json').write_text(json.dumps(worker))
    receipt={'version':2,'task_id':task['task_id'],'run_id':run_id,'source':source,'input_sha256':sha((output/'inputs.tar').read_bytes()),'worker':worker,'delivery_valid':True,'cleanup_complete':True,'protected_unchanged':True,'artifacts_verified':True,'acceptance':'not_granted','outcome':'returned'}
    receipt.update(owner_receipt(profile))
    (output/'receipt.json').write_text(json.dumps(receipt))
if __name__ == '__main__': main()
"""


@pytest.fixture
def execution_project(git_repo, tmp_path, monkeypatch):
    if sys.platform != "linux":
        pytest.skip("Owned Run adapter requires Linux descendant custody")
    monkeypatch.delenv("ANVIL_STATE_LAYOUT", raising=False)
    state = _resolve_state_dir(git_repo)
    state.mkdir(parents=True)
    events = state / "events.jsonl"
    events.touch()
    backend = SqliteBackend(
        db_path=str(state / "state.db"),
        events_path=str(events),
        clock=SystemClock(),
        project_root=str(git_repo),
    )
    backend.initialize()
    _seed(backend)
    source = prd_source_path(state, "release")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(backend.get_prd("release").source_bytes)
    command = shlex.join(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import os; assert os.environ['ANVIL_ROOT'] == os.getcwd(); assert Path('README.md').read_bytes() == b'implemented\\n'; print('native-proof-ok')",
        ]
    )
    task = backend.get_task("release:T001")
    payload = {
        **task.model_dump(mode="json"),
        "prd_id": task.prd_id,
        "likely_files": ["README.md"],
        "verification": {
            "commands": [command],
            "required_proofs": [
                {
                    "kind": "command",
                    "command": command,
                    "passing_exit_codes": [0],
                    "label": "real",
                }
            ],
        },
    }
    backend.append(_event("task.created", payload, target_kind="task", target_id=task.id))
    plugin = tmp_path / "adapter"
    script = plugin / "scripts" / "coordinate.py"
    script.parent.mkdir(parents=True)
    script.write_text(_ADAPTER)
    extension = plugin / "extensions" / "coordination.js"
    extension.parent.mkdir()
    extension.write_text("// synthetic adapter\n")
    profile = tmp_path / "profile.json"
    profile.write_text("{}")
    for path in (script, extension, profile):
        path.chmod(0o600)
    system_python = shutil.which("python3", path=os.defpath) or sys.executable
    package = plugin / "package.json"
    package.write_text('{"name":"codex-pi-coordination","version":"0.2.0"}')
    package.chmod(0o600)
    registration = register_execution_adapter(script, Path(system_python), profile)
    global_config = tmp_path / "global" / "config.yaml"
    global_config.parent.mkdir(mode=0o700)
    global_config.write_text(
        yaml.safe_dump({"execution_adapter": dataclasses.asdict(registration)})
    )
    global_config.chmod(0o600)
    monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(global_config))
    try:
        yield backend, git_repo, state, registration
    finally:
        backend.close()


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_unsupported_platform_refuses_before_state(tmp_path, monkeypatch, platform):
    import anvil.execution as execution

    with monkeypatch.context() as patch:
        patch.setattr(execution.sys, "platform", platform)
        with pytest.raises(ExecutionError, match="requires Linux"):
            preview_run(tmp_path, "missing", "synthetic-owner", RunOptions())
    assert not list(tmp_path.iterdir())


def test_unavailable_linux_containment_refuses_before_claim(execution_project, monkeypatch):
    import anvil.execution_runner as runner

    backend, repo, state, _ = execution_project
    before = (state / "events.jsonl").read_bytes()
    monkeypatch.setattr(runner, "_CAPTURE_SUPERVISOR", "raise SystemExit(126)")
    with pytest.raises(ExecutionError, match="Owned command failed"):
        preview_run(repo, "release:T001", "synthetic-owner", RunOptions())
    assert not backend.list_active_claims()
    assert (state / "events.jsonl").read_bytes() == before
    assert not (state / "execution" / "slots").exists()


def test_denied_group_observation_refuses_before_state(tmp_path, monkeypatch):
    import anvil.execution_runner as runner

    def denied(*args):
        raise PermissionError("synthetic denied process-group observation")

    monkeypatch.setattr(runner.os, "killpg", denied)
    with pytest.raises(ExecutionError, match="observe owned process groups"):
        preview_run(tmp_path, "missing", "synthetic-owner", RunOptions())
    assert not list(tmp_path.iterdir())


def _preview(project, **kwargs):
    return preview_run(project[1], "release:T001", "synthetic-owner", RunOptions(), **kwargs)


@pytest.mark.parametrize("preflight_seconds", [0, 5])
def test_preview_does_not_claim_and_expiry_is_closed(
    execution_project, monkeypatch, preflight_seconds
):
    import anvil.execution as execution

    current_time = execution.now()
    preflight_assignment = execution.preflight_assignment
    monkeypatch.setattr(execution, "now", lambda: current_time)

    def advancing_preflight(preview):
        nonlocal current_time
        result = preflight_assignment(preview)
        current_time += timedelta(seconds=preflight_seconds)
        return result

    def forbid_reservation(*args, **kwargs):
        pytest.fail("Stale preview attempted execution reservation")

    monkeypatch.setattr(execution, "preflight_assignment", advancing_preflight)
    backend, repo, state, _ = execution_project
    before = (state / "events.jsonl").read_bytes()
    preview = _preview(execution_project)
    assert not backend.list_active_claims()
    assert (state / "events.jsonl").read_bytes() == before
    monkeypatch.setattr(execution, "reserve_slot", forbid_reservation)
    created_at = datetime.fromisoformat(preview["created_at"])
    for delta in (-timedelta(seconds=2), timedelta(seconds=301)):
        monkeypatch.setattr(execution, "now", lambda delta=delta: created_at + delta)
        with pytest.raises(ExecutionError, match="expired|backwards"):
            confirm_run(repo, "release:T001", "synthetic-owner", preview["preview_id"])
    assert not backend.list_active_claims()


@pytest.mark.parametrize(
    "options",
    [
        dict(model="one"),
        dict(provider="synthetic", model="m" * 256),
        dict(thinking="auto"),
        dict(thinking="dynamic"),
        dict(provider="https://bad", model="one"),
    ],
)
def test_invalid_options_refuse(options):
    with pytest.raises(ExecutionError):
        RunOptions(**options)


def test_scope_cas_and_dependency_refuse_before_claim(execution_project):
    backend, repo, state, _ = execution_project
    expected = scope_digest(backend, "release:T001")
    task = backend.get_task("release:T001")
    backend.append(
        _event(
            "task.created",
            {
                **task.model_dump(mode="json"),
                "prd_id": task.prd_id,
                "description": "changed",
            },
            target_kind="task",
            target_id=task.id,
        )
    )
    before = (state / "events.jsonl").read_bytes()
    with pytest.raises(ExecutionError):
        native_cli(
            repo,
            "claim",
            task.id,
            "--worktree",
            "--actor",
            "synthetic-owner",
            "--execution-scope",
            expected,
            "--json",
        )
    assert (state / "events.jsonl").read_bytes() == before
    backend.append(
        _event(
            "task.created",
            {
                **task.model_dump(mode="json"),
                "prd_id": task.prd_id,
                "dependencies": ["release:T002"],
            },
            target_kind="task",
            target_id=task.id,
        )
    )
    with pytest.raises(ExecutionError, match="dependency"):
        _preview(execution_project)


def test_scope_change_after_preview_retains_no_slot(execution_project):
    backend, repo, _, _ = execution_project
    preview = _preview(execution_project)
    task = backend.get_task("release:T001")
    backend.append(
        _event(
            "task.created",
            {
                **task.model_dump(mode="json"),
                "prd_id": task.prd_id,
                "likely_files": ["another.txt"],
            },
            target_kind="task",
            target_id=task.id,
        )
    )
    with pytest.raises(ExecutionError, match="scope changed"):
        confirm_run(repo, task.id, "synthetic-owner", preview["preview_id"])
    assert not (journal_root(repo) / "slots").exists()


def test_unknown_dispatch_keeps_slot_and_does_not_retry(execution_project, monkeypatch):
    import anvil.execution as execution

    preview = _preview(execution_project)
    original = execution.native_cli

    def interrupted(cwd, *arguments, **kwargs):
        if arguments[0] == "claim":
            raise OSError("simulated lost claim acknowledgement")
        return original(cwd, *arguments, **kwargs)

    monkeypatch.setattr(execution, "native_cli", interrupted)
    with pytest.raises(OSError, match="lost claim"):
        confirm_run(
            execution_project[1],
            "release:T001",
            "synthetic-owner",
            preview["preview_id"],
        )
    slots = list((journal_root(execution_project[1]) / "slots").glob("*.json"))
    assert len(slots) == 1
    run_id = read_receipt(slots[0])["run_id"]
    with pytest.raises(ExecutionError, match=run_id):
        confirm_run(
            execution_project[1],
            "release:T001",
            "synthetic-owner",
            preview["preview_id"],
        )


def test_scope_is_checked_again_at_native_publication(execution_project, monkeypatch):
    from typer.testing import CliRunner

    from anvil.claims.manager import ClaimManager
    from anvil.cli import app

    backend, repo, _, _ = execution_project
    expected = scope_digest(backend, "release:T001")
    original_claim = ClaimManager.claim

    def mutate_before_claim(manager, task_id, **kwargs):
        task = manager._backend.get_task(task_id)
        manager._backend.append(
            _event(
                "task.created",
                {
                    **task.model_dump(mode="json"),
                    "prd_id": task.prd_id,
                    "description": "changed inside native claim custody",
                },
                target_kind="task",
                target_id=task_id,
            )
        )
        return original_claim(manager, task_id, **kwargs)

    monkeypatch.setattr(ClaimManager, "claim", mutate_before_claim)
    result = CliRunner().invoke(
        app,
        [
            "claim",
            "release:T001",
            "--worktree",
            "--actor",
            "synthetic-owner",
            "--execution-scope",
            expected,
            "--cwd",
            str(repo),
            "--json",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "scope changed" in result.output.lower()
    assert not backend.list_active_claims()


def test_expired_claim_and_profile_refuse_before_dispatch(execution_project):
    from anvil.execution import live_claim, now

    backend, repo, _, _ = execution_project
    claimed = json.loads(
        native_cli(
            repo,
            "claim",
            "release:T001",
            "--actor",
            "synthetic-owner",
            "--worktree",
            "--json",
        )
    )["data"]["claim"]
    request = {
        "claim": claimed,
        "actor": "synthetic-owner",
        "task_id": "release:T001",
        "cwd": claimed["worktree_path"],
        "scope_sha256": scope_digest(backend, "release:T001"),
    }
    remaining = int((backend.get_claim(claimed["id"]).lease_expires_at - now()).total_seconds()) + 1
    with pytest.raises(ExecutionError, match="sufficient lease"):
        live_claim(backend, request, remaining=remaining)


def test_profile_and_unbounded_scope_refuse_before_launch(execution_project):
    from anvil.execution import task_contract
    from anvil.state.models import VerificationProfileReference

    backend = execution_project[0]
    task = backend.get_task("release:T001")
    profiled = task.model_copy(deep=True)
    profiled.verification.profile = VerificationProfileReference(
        name="test",
        platform="linux",
        source_sha256="a" * 64,
    )
    with pytest.raises(ExecutionError, match="verification profiles"):
        task_contract(profiled)
    for paths in (["src/**"], ["../outside"], ["README.md", "readme.md"], []):
        with pytest.raises(ExecutionError):
            task_contract(task.model_copy(update={"likely_files": paths}))


def test_source_cas_refuses_before_publishing_claim(execution_project):
    backend, repo, state, _ = execution_project
    before = (state / "events.jsonl").read_bytes()
    with pytest.raises(ExecutionError, match="Source revision changed"):
        native_cli(
            repo,
            "claim",
            "release:T001",
            "--worktree",
            "--actor",
            "synthetic-owner",
            "--execution-scope",
            scope_digest(backend, "release:T001"),
            "--execution-source",
            "0" * 40,
            "--json",
        )
    assert not backend.list_active_claims()
    assert (state / "events.jsonl").read_bytes() == before


def test_capability_view_is_sanitized_scoped_and_expires(execution_project, monkeypatch):
    import anvil.execution as execution

    backend, repo, state, registration = execution_project
    before = (state / "events.jsonl").read_bytes()
    assert execution.read_execution_capabilities(repo)["status"] == "unknown"
    configuration = {
        "available_models": [{"provider": "synthetic", "id": "one", "endpoint": "private"}],
        "effective": {"provider": "synthetic", "model": "one", "thinking": "low"},
        "supported_thinking_levels": ["off", "low"],
        "session_id": "private-session",
    }
    execution.save_execution_capabilities(journal_root(repo), configuration, registration, "a" * 64)
    fresh = execution.read_execution_capabilities(repo)
    assert fresh["status"] == "fresh"
    assert fresh["models"] == [{"provider": "synthetic", "id": "one"}]
    assert fresh["thinking"] == {
        "provider": "synthetic",
        "model": "one",
        "levels": ["off", "low"],
    }
    assert "private" not in json.dumps(fresh)
    observed = execution.now()
    monkeypatch.setattr(execution, "now", lambda: observed + timedelta(seconds=301))
    stale = execution.read_execution_capabilities(repo)
    assert stale["status"] == "stale" and stale["models"] == []
    assert "thinking" not in stale
    assert (state / "events.jsonl").read_bytes() == before
    assert not backend.list_active_claims()


def test_capability_profile_change_and_busy_owner_are_closed(execution_project):
    import anvil.execution as execution
    from anvil.config import global_config_path

    _, repo, _, registration = execution_project
    execution.save_execution_capabilities(
        journal_root(repo),
        {
            "available_models": [{"provider": "synthetic", "id": "one"}],
            "effective": {"provider": "synthetic", "model": "one", "thinking": "low"},
            "supported_thinking_levels": ["low"],
        },
        registration,
        "a" * 64,
    )
    Path(registration.profile).write_text('{"model":"changed"}')
    changed = dataclasses.asdict(registration)
    changed["registration"]["profile_sha256"] = digest(Path(registration.profile).read_bytes())
    global_config_path().write_text(yaml.safe_dump({"execution_adapter": changed}))
    assert execution.read_execution_capabilities(repo)["status"] == "unknown"
    root = journal_root(repo)
    unknown_id = "a" * 32
    execution.write_receipt(
        root / "slots" / "held.json", {"run_id": unknown_id, "task_id": "release:T001"}
    )
    with pytest.raises(ExecutionError, match="existing execution"):
        execution.refresh_execution_capabilities(repo)
    assert not (root / "runs").exists()


def test_admission_repair_normalizes_only_new_claim_actor(execution_project):
    backend, repo, state, _ = execution_project
    before = (state / "events.jsonl").read_bytes()
    preview = preview_run(repo, "release:T001", "cafe\u0301", RunOptions())
    assert preview["actor"] == "caf\u00e9"
    assert not backend.list_active_claims()
    assert (state / "events.jsonl").read_bytes() == before


def test_admission_repair_existing_owner_lookup_remains_exact(execution_project):
    backend, repo, state, _ = execution_project
    claimed = json.loads(native_cli(
        repo, "claim", "release:T001", "--actor", "caf\u00e9", "--worktree", "--json"
    ))["data"]["claim"]
    directory = Path(claimed["worktree_path"])
    before = (state / "events.jsonl").read_bytes()
    with pytest.raises(ExecutionError, match="exact owner"):
        preview_run(directory, "release:T001", "cafe\u0301", RunOptions())
    preview = preview_run(directory, "release:T001", "caf\u00e9", RunOptions())
    assert preview["claim"]["id"] == claimed["id"]
    assert backend.get_claim(claimed["id"]).claimed_by == "caf\u00e9"
    assert (state / "events.jsonl").read_bytes() == before


@pytest.mark.parametrize("confirming", [False, True])
def test_admission_repair_rechecks_material_after_packet_refresh(
    execution_project, monkeypatch, confirming
):
    import anvil.execution as execution

    backend, repo, state, _ = execution_project
    task = backend.get_task("release:T001")
    preview = _preview(execution_project) if confirming else None
    original = execution.native_cli

    def packet_then_status_change(cwd, *arguments, **kwargs):
        result = original(cwd, *arguments, **kwargs)
        if arguments[0] == "packet":
            backend.append(_event("task.status_changed", {
                "task_id": task.id, "from": "ready", "to": "drafted",
            }, target_kind="task", target_id=task.id))
        return result

    monkeypatch.setattr(execution, "native_cli", packet_then_status_change)
    with pytest.raises(ExecutionError, match="no longer ready"):
        if confirming:
            confirm_run(repo, task.id, "synthetic-owner", preview["preview_id"])
        else:
            _preview(execution_project)
    assert not backend.list_active_claims()
    assert not (state / "execution" / "slots").exists()
    assert not (state / "execution" / "consumed").exists()


def test_admission_repair_expiry_during_preflight_refuses_reservation(
    execution_project, monkeypatch
):
    import anvil.execution as execution

    backend, repo, state, _ = execution_project
    preview = _preview(execution_project)
    observed = execution.now()
    original = execution.preflight_assignment

    def preflight_then_expire(context):
        result = original(context)
        monkeypatch.setattr(execution, "now", lambda: observed + timedelta(seconds=301))
        return result

    monkeypatch.setattr(execution, "preflight_assignment", preflight_then_expire)
    with pytest.raises(ExecutionError, match="expired"):
        confirm_run(repo, "release:T001", "synthetic-owner", preview["preview_id"])
    assert not backend.list_active_claims()
    assert not (state / "execution" / "slots").exists()
    assert not (state / "execution" / "consumed").exists()


def test_admission_repair_local_layout_refuses_before_journal(execution_project, monkeypatch):
    backend, repo, state, _ = execution_project
    before = (state / "events.jsonl").read_bytes()
    monkeypatch.setenv("ANVIL_STATE_LAYOUT", "local")
    with pytest.raises(ExecutionError) as error:
        preview_run(repo, "release:T001", "synthetic-owner", RunOptions())
    assert error.value.code == "unsupported_local_isolated_mode"
    assert not (repo / ".anvil").exists()
    assert not backend.list_active_claims()
    assert (state / "events.jsonl").read_bytes() == before


@pytest.mark.parametrize("invalid", ["directory", "oversized_assignment", "source_symlink"])
def test_admission_repair_validates_actual_scope_before_reservation(execution_project, invalid):
    from anvil.execution import git

    backend, repo, state, _ = execution_project
    task = backend.get_task("release:T001")
    fields = {}
    if invalid == "directory":
        (repo / "directory").mkdir()
        (repo / "directory" / "file.txt").write_text("retained\n")
        fields["likely_files"] = ["directory"]
    elif invalid == "source_symlink":
        (repo / "source-link").symlink_to("README.md")
    else:
        fields["description"] = "x" * 33000
    if invalid != "oversized_assignment":
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "Synthetic invalid execution source")
    if fields:
        backend.append(_event("task.created", {
            **task.model_dump(mode="json"), "prd_id": task.prd_id, **fields,
        }, target_kind="task", target_id=task.id))
    before = (state / "events.jsonl").read_bytes()
    with pytest.raises(ExecutionError):
        preview_run(repo, task.id, "synthetic-owner", RunOptions())
    assert not backend.list_active_claims()
    assert (state / "events.jsonl").read_bytes() == before
    root = state / "execution"
    assert not (root / "slots").exists()
    assert not (root / "consumed").exists()
    assert not (root / "runs").exists()
    assert not list((root / "previews").glob("*.json"))


def test_admission_repair_checks_native_default_source_before_reservation(execution_project):
    from anvil.execution import git

    backend, repo, state, _ = execution_project
    git(repo, "checkout", "-qb", "synthetic-preview-branch")
    (repo / "separate.txt").write_text("must not silently disappear\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Preview source differs from default base")
    before = (state / "events.jsonl").read_bytes()
    with pytest.raises(ExecutionError, match="source|Source"):
        preview_run(repo, "release:T001", "synthetic-owner", RunOptions())
    with pytest.raises(ExecutionError):
        native_cli(
            repo, "claim", "release:T001", "--actor", "synthetic-owner", "--worktree",
            "--execution-scope", scope_digest(backend, "release:T001"),
            "--execution-source", git(repo, "rev-parse", "HEAD").decode().strip(), "--json",
        )
    assert not backend.list_active_claims()
    assert (state / "events.jsonl").read_bytes() == before
    assert not (state / "execution" / "slots").exists()


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_hidden_index_flags_refuse_admission_and_integrated_source(execution_project, flag):
    from anvil.execution import git
    from anvil.execution_runner import check_integrated_source

    backend, repo, state, _ = execution_project
    source = git(repo, "rev-parse", "HEAD").decode().strip()
    git(repo, "update-index", flag, "README.md")
    (repo / "README.md").write_bytes(b"hidden verification drift\n")
    assert git(repo, "status", "--porcelain") == b""
    events = (state / "events.jsonl").read_bytes()
    with pytest.raises(ExecutionError, match="index"):
        _preview(execution_project)
    with pytest.raises(ExecutionError, match="index"):
        check_integrated_source(repo, source, [], {})
    assert not backend.list_active_claims()
    assert (state / "events.jsonl").read_bytes() == events


def test_consumed_preview_racing_fresh_admission_preserves_new_slot(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from anvil.execution import private_directory, reserve_slot, write_receipt

    root = private_directory(tmp_path / "journal")
    old_preview, fresh_preview = "a" * 32, "b" * 32
    previous_run, next_run = "c" * 32, "d" * 32
    write_receipt(root / "consumed" / (old_preview + ".json"), {"run_id": previous_run})
    slot = root / "slots" / "task.json"
    original = {"run_id": next_run, "preview_id": fresh_preview, "task_id": "example:T001"}
    barrier = Barrier(3)

    def contender(preview):
        barrier.wait(timeout=5)
        try:
            reserve_slot(root, slot, {**original, "preview_id": preview}, consume_preview=True)
            return "admitted"
        except ExecutionError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=3) as executor:
        outcomes = list(executor.map(contender, [old_preview, old_preview, fresh_preview]))
    # A contender may see the short native admission lock; only the fresh
    # request can ever reserve. Retry only an explicitly unadmitted lock loser.
    if outcomes[2] == "admission_busy":
        reserve_slot(root, slot, original, consume_preview=True)
    else:
        assert outcomes[2] == "admitted"
    assert set(outcomes[:2]) <= {"admission_busy", "preview_consumed"}
    assert read_receipt(slot) == original
    assert read_receipt(root / "consumed" / (old_preview + ".json")) == {"run_id": previous_run}
    assert not (root / "retired").exists()
    assert not (root / "runs").exists()
