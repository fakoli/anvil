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


def test_receipt_is_invisible_until_complete_and_excludes_other_writers(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import anvil.execution as execution

    path = tmp_path / "result.json"
    entered, release = threading.Event(), threading.Event()
    serialize = execution.json_bytes

    def pause_serialization(value):
        entered.set()
        assert release.wait(5)
        return serialize(value)

    monkeypatch.setattr(execution, "json_bytes", pause_serialization)
    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(execution.write_receipt, path, {"complete": True})
        try:
            assert entered.wait(5)
            assert not path.exists()
            assert execution.receipt_attempt_exists(path)
            with pytest.raises(FileExistsError):
                execution.write_receipt(path, {"complete": False})
        finally:
            release.set()
        writer.result(timeout=5)
    assert read_receipt(path) == {"complete": True}
    assert not execution.pending_receipt_path(path).exists()
    with pytest.raises(FileExistsError):
        execution.write_receipt(path, {"complete": False})
    assert read_receipt(path) == {"complete": True}
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("failure", ["serialization", "directory_fsync", "file_fsync", "publication"])
def test_failed_receipt_attempt_is_retained_and_cannot_retry(tmp_path, monkeypatch, failure):
    import stat

    import anvil.execution as execution

    path = tmp_path / "result.json"

    def fail(*_args, **_kwargs):
        raise OSError("injected receipt failure")

    fsync = execution.os.fsync

    def fail_file_sync(descriptor):
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            fail()
        return fsync(descriptor)

    with monkeypatch.context() as patch:
        if failure == "serialization":
            patch.setattr(execution, "json_bytes", fail)
        elif failure == "directory_fsync":
            patch.setattr(execution.os, "fsync", fail)
        elif failure == "file_fsync":
            patch.setattr(execution.os, "fsync", fail_file_sync)
        else:
            patch.setattr(execution.os, "link", fail)
        with pytest.raises(OSError, match="injected receipt failure"):
            execution.write_receipt(path, {"complete": True})
    pending = execution.pending_receipt_path(path)
    assert pending.is_file() and not path.exists()
    retained = pending.read_bytes()
    with pytest.raises(FileExistsError):
        execution.write_receipt(path, {"retry": True})
    assert pending.read_bytes() == retained and not path.exists()


def test_receipt_publication_collision_does_not_poison_reusable_lock(tmp_path, monkeypatch):
    import anvil.execution as execution

    path = tmp_path / "slot.lock"
    link = execution.os.link

    def completed_other_writer(source, destination):
        destination.write_text('{"winner": "other"}')
        return link(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(execution.os, "link", completed_other_writer)
        with pytest.raises(FileExistsError):
            execution.write_receipt(path, {"winner": "this"})
    assert read_receipt(path) == {"winner": "other"}
    assert not execution.pending_receipt_path(path).exists()
    path.unlink()
    execution.write_receipt(path, {"next": True})
    assert read_receipt(path) == {"next": True}


@pytest.mark.parametrize("marker", ["consumed", "slot"])
def test_pending_receipt_never_reports_unconsumed_preview(execution_project, marker):
    import anvil.execution as execution

    _, repo, _, _ = execution_project
    preview = preview_run(repo, "release:T001", "agent", RunOptions())
    root = journal_root(repo)
    path = (root / "consumed" / (preview["preview_id"] + ".json") if marker == "consumed"
            else root / "slots" / (digest(b"release:T001") + ".json"))
    execution.private_directory(path.parent)
    execution.pending_receipt_path(path).touch(mode=0o600)
    inspected = execution.inspect_execution_preview(repo, preview["preview_id"], "agent")
    assert inspected["state"] == "unknown" and inspected["run_id"] is None
    if marker == "consumed":
        with pytest.raises(ExecutionError, match="bounded regular execution receipt"):
            execution.require_unconsumed_preview(root, preview["preview_id"])
    else:
        with pytest.raises(ExecutionError, match="bounded regular execution receipt"):
            execution.reserve_slot(root, path, {
                "run_id": "a" * 32, "task_id": "release:T001",
                "preview_id": preview["preview_id"],
            }, consume_preview=True)
        assert not execution.receipt_attempt_exists(root / "consumed" / (preview["preview_id"] + ".json"))

# A real executable contract double. It edits returned bytes, never fabricates
# native proof or native state. The Run worker must run and submit real checks.
_ADAPTER = r"""
import contextlib, hashlib, io, json, pathlib, re, subprocess, sys, tarfile, uuid

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

def private_control_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()

def saved_descriptor(task):
    session = task['session']
    if not isinstance(session, dict) or set(session) != {'conversation_id','directory','file','prior','prior_sha256'}:
        raise ValueError('Invalid synthetic saved descriptor')
    conversation_id = session['conversation_id']
    if not isinstance(conversation_id, str) or re.fullmatch('[0-9a-f]{32}', conversation_id) is None:
        raise ValueError('Invalid conversation identity')
    directory = pathlib.Path(task['output_directory']).parent.parent/'conversations'/conversation_id
    if session['directory'] != str(directory) or session['file'] != str(directory/'session.jsonl') or task['workspace'] != str(directory/'workspace'):
        raise ValueError('Saved workspace/file differs')
    prior = session['prior']
    if task['operation'] == 'run':
        if conversation_id != task['run_id'] or prior is not None or session['prior_sha256'] is not None:
            raise ValueError('Invalid initial saved lineage')
    elif task['operation'] == 'continue':
        if not isinstance(prior, dict) or prior.get('conversation_id') != conversation_id or prior.get('run_id') == task['run_id'] or re.fullmatch('[0-9a-f]{32}', prior.get('run_id','')) is None or session['prior_sha256'] != sha(private_control_bytes(prior)):
            raise ValueError('Invalid prior saved lineage')
    else: raise ValueError('Unsupported saved operation')
    return session

def validate_native_task(task):
    if task['version'] == 2: return task
    keys={'version','run_id','task_id','kind','instructions','workspace','output_directory','writable_paths','allowed_tools','max_tool_calls','max_turns','source','checks','operation','run_options','runtime_defaults'}
    if task['version'] != 3: raise ValueError('Invalid native version')
    if task['operation'] == 'capabilities_selected':
        if set(task) != keys|{'selected_model'} or task['writable_paths'] or task['checks'] or task['run_options']:
            raise ValueError('Invalid selected metadata scope')
        pair=task['selected_model']
        if not isinstance(pair,dict) or set(pair) != {'provider','model'} or any(not isinstance(x,str) or not x or re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}',x) is None for x in pair.values()):
            raise ValueError('Invalid selected model pair')
    else:
        if set(task) != keys|{'session'}: raise ValueError('Invalid saved task fields')
        saved_descriptor(task)
    return task

def run_task(task, profile, output, timeout, *, save_session=False, previous_directory=None, selected_model=None):
    raise RuntimeError('Synthetic fixture uses its executable entrypoint')

@contextlib.contextmanager
def exclusive_owner(profile, lock_root):
    yield

def verify_conversation(results, task, worker):
    descriptor=saved_descriptor(task)
    checkpoint_raw=(results/'conversation.json').read_bytes()
    checkpoint=json.loads(checkpoint_raw)
    if checkpoint_raw != private_control_bytes(checkpoint): raise ValueError('Noncanonical checkpoint')
    prior=descriptor['prior']
    conversation=worker['conversation']
    if checkpoint['conversation_id'] != descriptor['conversation_id'] or checkpoint['run_id'] != task['run_id'] or checkpoint['task_id'] != task['task_id'] or checkpoint['task_sha256'] != sha(json.dumps(task).encode()) or checkpoint['source_commit'] != task['source']['commit']:
        raise ValueError('Checkpoint task binding changed')
    if checkpoint['prior_run_id'] != (prior['run_id'] if prior else None) or checkpoint['prior_checkpoint_sha256'] != descriptor['prior_sha256'] or conversation['checkpoint_sha256'] != sha(checkpoint_raw) or conversation['session_after'] != checkpoint['session'] or conversation['continuable'] is not True:
        raise ValueError('Checkpoint prior binding changed')
    if conversation['session_before'] != (prior['session'] if prior else None) or conversation['prior_prefix_sha256'] != (prior['session']['sha256'] if prior else sha(b'')):
        raise ValueError('Session prefix changed')
    return checkpoint

def continuation_checkpoint(previous_directory, profile):
    receipt=json.loads((previous_directory/'receipt.json').read_text())
    with tarfile.open(previous_directory/'inputs.tar') as archive:
        task=json.loads(archive.extractfile('task.json').read())
    validate_native_task(task)
    worker=verify_return(previous_directory/'results',task,{},receipt['run_id'])
    if worker != receipt['worker']: raise ValueError('Prior worker mismatch')
    return verify_conversation(previous_directory/'results',task,worker)

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
    saved = not metadata and ('--save-session' in sys.argv or sys.argv[1] == 'continue')
    selected = metadata and '--provider' in sys.argv
    prior = None
    if saved:
        if sys.argv[1] == 'continue':
            previous=pathlib.Path(sys.argv[sys.argv.index('--from')+1])
            prior=continuation_checkpoint(previous, profile)
        conversation_id=prior['conversation_id'] if prior else run_id
        conversation_root=pathlib.Path('/synthetic/conversations')/conversation_id
        native={key:value for key,value in native.items() if key not in {'repository','revision','wall_seconds'}}
        native.update(version=3,operation='continue' if prior else 'run',workspace=str(conversation_root/'workspace'),output_directory='/synthetic/'+run_id+'/results',session={'conversation_id':conversation_id,'directory':str(conversation_root),'file':str(conversation_root/'session.jsonl'),'prior':prior,'prior_sha256':sha(private_control_bytes(prior)) if prior else None})
        validate_native_task(native)
    if selected:
        native.update(version=3,operation='capabilities_selected',workspace='/synthetic/metadata',output_directory='/synthetic/'+run_id+'/results',selected_model={'provider':sys.argv[sys.argv.index('--provider')+1],'model':sys.argv[sys.argv.index('--model')+1]})
        validate_native_task(native)
    if profile.get('wrong_options'): native['run_options']={'thinking':'max'}
    with tarfile.open(output/'inputs.tar','x') as archive:
        raw = json.dumps(native).encode(); item=tarfile.TarInfo('task.json'); item.size=len(raw); archive.addfile(item,io.BytesIO(raw))
    results=output/'results'; results.mkdir()
    if metadata:
        configuration={**configuration_for(profile),'prompt_sent':False}
        if selected:
            configuration['effective']={**native['selected_model'],'thinking':'low'}
            configuration['available_models']=[{'provider':native['selected_model']['provider'],'id':native['selected_model']['model']}]
        worker={'run_id':run_id,'task_id':'capabilities','configuration':configuration,'artifacts':{}}
        (results/'worker-receipt.json').write_text(json.dumps(worker))
        receipt={'version':native['version'],'task_id':'capabilities','run_id':run_id,'input_sha256':sha((output/'inputs.tar').read_bytes()),'worker':worker,'capabilities_valid':True,'cleanup_complete':True,'protected_unchanged':True,'artifacts_verified':True,'acceptance':'not_granted','outcome':'capabilities'}
        receipt.update(owner_receipt(profile))
        (output/'receipt.json').write_text(json.dumps(receipt)); return
    (results/'changed-files').mkdir()
    name=task['writable_paths'][0]; target=results/'changed-files'/name; target.parent.mkdir(parents=True,exist_ok=True)
    target.write_bytes(profile.get('returned_content', 'continued\n' if prior else 'implemented\n').encode())
    changes=[{'path':name,'before_sha256':sha(files[name]) if name in files else None,'after_sha256':sha(target.read_bytes())}]
    if profile.get('wrong_preimage'): changes[0]['before_sha256']='0'*64
    (results/'changes.json').write_text(json.dumps(changes))
    configuration={**configuration_for(profile),'session_file':None}
    if saved:
        configuration.update(session_file=native['session']['file'],session_id=prior['session']['id'] if prior else str(uuid.uuid4()))
    (results/'rpc-configuration.json').write_text(json.dumps(configuration))
    worker={'run_id':run_id,'task_id':task['task_id'],'checks':[],'configuration':configuration,'artifacts':{str(p.relative_to(results)):sha(p.read_bytes()) for p in results.rglob('*') if p.is_file()}}
    if saved:
        session={'id':configuration['session_id'],'bytes':prior['session']['bytes']+64 if prior else 128,'sha256':sha((run_id+str(prior)).encode()),'header_sha256':prior['session']['header_sha256'] if prior else sha(configuration['session_id'].encode())}
        terminal={**files,name:target.read_bytes()}
        workspace={'files':{path:{'sha256':sha(value),'mode':420} for path,value in terminal.items()},'directories':{'.':493}}
        checkpoint={'version':1,'conversation_id':native['session']['conversation_id'],'run_id':run_id,'task_id':task['task_id'],'task_sha256':sha(json.dumps(native).encode()),'source_commit':source['commit'],'prior_run_id':prior['run_id'] if prior else None,'prior_checkpoint_sha256':native['session']['prior_sha256'],'session':session,'workspace':workspace}
        checkpoint_raw=private_control_bytes(checkpoint)
        (results/'conversation.json').write_bytes(checkpoint_raw)
        worker['conversation']={'checkpoint_sha256':sha(checkpoint_raw),'session_before':prior['session'] if prior else None,'session_after':session,'prior_prefix_sha256':prior['session']['sha256'] if prior else sha(b''),'continuable':True}
        worker['extension']={'session':{'id':configuration['session_id'],'file':configuration['session_file'],'cwd':native['workspace'],'version':3}}
        worker['artifacts']['conversation.json']=sha(checkpoint_raw)
    (results/'worker-receipt.json').write_text(json.dumps(worker))
    receipt={'version':native['version'],'task_id':task['task_id'],'run_id':run_id,'source':source,'input_sha256':sha((output/'inputs.tar').read_bytes()),'worker':worker,'delivery_valid':True,'cleanup_complete':True,'protected_unchanged':True,'artifacts_verified':True,'acceptance':'not_granted','outcome':'returned'}
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


@pytest.fixture
def saved_execution_project(execution_project):
    backend, repo, state, registration = execution_project
    saved = register_execution_adapter(
        Path(registration.script), Path(registration.interpreter), Path(registration.profile),
        saved_session_continue=True,
    )
    Path(os.environ["ANVIL_GLOBAL_CONFIG"]).write_text(
        yaml.safe_dump({"execution_adapter": dataclasses.asdict(saved)})
    )
    return backend, repo, state, saved


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


def _completed_saved_run(project):
    from anvil.execution import git
    from tests.test_execution_runner import _wait

    _, repo, _, _ = project
    preview = _preview(project)
    admitted = confirm_run(repo, "release:T001", "synthetic-owner", preview["preview_id"])
    directory = journal_root(repo) / "runs" / admitted["run_id"]
    result = _wait(directory)
    assert result["state"] == "needs_review", result
    checkout = Path(admitted["cwd"])
    git(checkout, "add", "README.md")
    git(checkout, "commit", "-qm", "Retain first synthetic edit")
    git(repo, "merge", "--ff-only", git(checkout, "rev-parse", "HEAD").decode().strip())
    return preview, admitted, directory


def test_saved_same_task_requires_request_changes_readiness_and_new_claim(saved_execution_project):
    from anvil.execution import confirm_continue, preview_continue
    from tests.test_execution_runner import _wait

    backend, repo, state, _ = saved_execution_project
    _, prior, directory = _completed_saved_run(saved_execution_project)
    before = (state / "events.jsonl").read_bytes()
    with pytest.raises(ExecutionError, match="ready task"):
        preview_continue(repo, prior["run_id"], "synthetic-owner", RunOptions())
    assert (state / "events.jsonl").read_bytes() == before
    rejected = json.loads(native_cli(
        repo, "apply", "release:T001", "--reject", "--reason", "Add the requested follow-up edit",
        "--reviewer", "synthetic-reviewer", "--json",
    ))
    assert rejected["ok"] is True and backend.get_task("release:T001").status.value == "drafted"
    with pytest.raises(ExecutionError, match="ready task"):
        preview_continue(repo, prior["run_id"], "synthetic-owner", RunOptions())
    task = backend.get_task("release:T001")
    command = shlex.join([sys.executable, "-I", "-c",
                         "from pathlib import Path; assert Path('README.md').read_bytes() == b'continued\\n'; print('rework-proof')"])
    backend.append(_event("task.created", {
        **task.model_dump(mode="json"), "prd_id": task.prd_id,
        "verification": {"commands": [command], "required_proofs": [
            {"kind": "command", "command": command, "passing_exit_codes": [0], "label": "rework"}]},
    }, target_kind="task", target_id=task.id))
    native_cli(repo, "review", "tasks", "--prd", "release", "--json")
    assert backend.get_task(task.id).status.value == "ready"
    preview = preview_continue(repo, prior["run_id"], "synthetic-owner", RunOptions())
    assert "Add the requested follow-up edit" in preview["assignment"]["instructions"]
    assert preview["continuation_material"]["review"]["binding_status"] == "exact"
    next_run = confirm_continue(repo, prior["run_id"], "synthetic-owner", preview["preview_id"])
    returned = _wait(journal_root(repo) / "runs" / next_run["run_id"])
    assert returned["state"] == "needs_review", returned
    from anvil.execution import inspect_execution_preview

    # Simulate losing the confirm response: inspect its exact saved route,
    # then retrying confirm must never create a third dispatch or native claim.
    dispatch_count = len(list(journal_root(repo).glob("runs/*/dispatch.json")))
    retained_events = (state / "events.jsonl").read_bytes()
    inspected = inspect_execution_preview(repo, preview["preview_id"], "synthetic-owner")
    assert inspected["state"] == "consumed" and inspected["run_id"] == next_run["run_id"]
    assert inspected["operation"] == "continue" and inspected["prior_run_id"] == prior["run_id"]
    with pytest.raises(ExecutionError) as duplicate:
        confirm_continue(repo, prior["run_id"], "synthetic-owner", preview["preview_id"])
    assert duplicate.value.code == "preview_consumed"
    assert len(list(journal_root(repo).glob("runs/*/dispatch.json"))) == dispatch_count == 2
    assert (state / "events.jsonl").read_bytes() == retained_events
    claim = backend.get_claim(next_run["claim_id"])
    assert claim.generation == 2 and claim.id != prior["claim_id"]
    evidence = backend.get_latest_evidence(task.id)
    assert evidence.proofs[0].evidence_core.claim_id == claim.id
    assert evidence.proofs[0].evidence_core.generation == 2
    assert read_receipt(directory / "result.json")["state"] == "needs_review"
    applied = [json.loads(line) for line in (state / "events.jsonl").read_bytes().splitlines()
               if json.loads(line)["action"] == "task.applied"]
    assert len(applied) == 1 and rejected["data"]["decision"] == "rejected"


def test_saved_prior_tampering_and_metadata_cas_refuse_before_claim(saved_execution_project):
    from anvil.execution import preview_continue, refresh_execution_capabilities

    backend, repo, state, registration = saved_execution_project
    followup_task = backend.get_task("release:T002")
    backend.append(_event("task.created", {
        **followup_task.model_dump(mode="json"), "prd_id": followup_task.prd_id,
        "likely_files": ["README.md"],
        "verification": backend.get_task("release:T001").verification.model_dump(mode="json"),
    }, target_kind="task", target_id=followup_task.id))
    _, prior, directory = _completed_saved_run(saved_execution_project)
    before = (state / "events.jsonl").read_bytes()
    for name, mutate in (
        ("result.json", lambda value: value.update(local_children_stopped=False)),
        ("adapter-result.json", lambda value: value["saved_conversation"].update(checkpoint_sha256="0"*64)),
        ("submitted.json", lambda value: value["data"].update(evidence_id="EVFOREIGN")),
        ("context.json", lambda value: value["claim"].update(claimed_by="foreign")),
    ):
        path = directory / name
        original = path.read_bytes()
        changed = json.loads(original)
        mutate(changed)
        path.write_text(json.dumps(changed))
        try:
            with pytest.raises(ExecutionError) as refused:
                preview_continue(repo, prior["run_id"], "synthetic-owner", RunOptions(), task_id="release:T002")
            assert refused.value.code == {
                "result.json": "unsettled_run", "adapter-result.json": "result_mismatch",
                "submitted.json": "unsettled_run", "context.json": "continuation_lineage",
            }[name]
        finally:
            path.write_bytes(original)
        assert (state / "events.jsonl").read_bytes() == before
        assert not backend.list_active_claims()
    with pytest.raises(ExecutionError, match="registration changed"):
        refresh_execution_capabilities(repo, expected_registration_sha256="0"*64)
    assert (state / "events.jsonl").read_bytes() == before
    assert registration.registration["contract_version"] == 3
    from anvil.execution import confirm_continue

    preview = preview_continue(repo, prior["run_id"], "synthetic-owner", RunOptions(), task_id="release:T002")
    path = directory / "result.json"
    original = path.read_bytes()
    changed = json.loads(original)
    changed["local_children_stopped"] = False
    path.write_text(json.dumps(changed))
    try:
        with pytest.raises(ExecutionError, match="reconciliation"):
            confirm_continue(repo, prior["run_id"], "synthetic-owner", preview["preview_id"])
    finally:
        path.write_bytes(original)
    assert (state / "events.jsonl").read_bytes() == before
    assert not backend.list_active_claims()
    assert not (journal_root(repo) / "consumed" / (preview["preview_id"] + ".json")).exists()
    from anvil.task_actions import transition_task

    task = backend.get_task("release:T001")
    transition_task(backend, task.id, "close", "synthetic-reviewer",
                    expected_status=task.status.value, expected_updated_at=task.updated_at,
                    resolution="obsolete", reason="Separate explicit follow-up owns remaining work")
    followup = preview_continue(repo, prior["run_id"], "synthetic-owner", RunOptions(), task_id="release:T002")
    assert followup["task_id"] == "release:T002"
    assert backend.get_task(task.id).status.value == "closed"
    assert not backend.list_active_claims()


def test_preview_inspection_malformed_receipts_are_read_only(execution_project, monkeypatch):
    from anvil.execution import inspect_execution_preview, json_bytes

    backend, repo, state, _ = execution_project
    preview = _preview(execution_project)
    root = journal_root(repo)
    preview_path = root / "previews" / (preview["preview_id"] + ".json")
    preview_raw = preview_path.read_bytes()
    events = (state / "events.jsonl").read_bytes()

    def forbid_dispatch(*args, **kwargs):
        pytest.fail("Inspection must not reserve or dispatch work")

    monkeypatch.setattr("anvil.execution.reserve_slot", forbid_dispatch)
    monkeypatch.setattr("anvil.execution.native_cli", forbid_dispatch)
    for value in (
        [], None, 1,
        {key: value for key, value in preview.items() if key != "task_id"},
        {**preview, "task_id": None}, {**preview, "task_id": []},
        {**preview, "operation": {}}, {**preview, "operation": []},
        {**preview, "preview_id": "f" * 32},
        {**preview, "version": True}, {**preview, "claim": {}},
    ):
        preview_path.write_bytes(json_bytes(value))
        with pytest.raises(ExecutionError, match="preview object"):
            inspect_execution_preview(repo, preview["preview_id"], "synthetic-owner")
        assert preview_path.read_bytes() == json_bytes(value)
    preview_path.write_bytes(preview_raw)
    slot = root / "slots" / (digest(preview["task_id"].encode()) + ".json")
    slot.parent.mkdir(mode=0o700, exist_ok=True)
    valid_slot = {"run_id": "a" * 32, "task_id": preview["task_id"],
                  "preview_id": preview["preview_id"]}
    for value, expected in [
        *[(value, "unknown") for value in ([], None, 1, "slot", {},
                                           {"preview_id": preview["preview_id"]})],
        ({**valid_slot, "run_id": None}, "unknown"),
        ({**valid_slot, "preview_id": []}, "unknown"),
        ({**valid_slot, "task_id": "release:T002"}, "unknown"),
        (valid_slot, "unknown"),
        ({**valid_slot, "preview_id": "b" * 32}, "unconsumed"),
    ]:
        slot.write_bytes(json_bytes(value))
        retained = {str(p.relative_to(root)): p.read_bytes()
                    for p in root.rglob("*") if p.is_file()}
        inspected = inspect_execution_preview(repo, preview["preview_id"], "synthetic-owner")
        assert inspected["state"] == expected and inspected["run_id"] is None
        assert {str(p.relative_to(root)): p.read_bytes()
                for p in root.rglob("*") if p.is_file()} == retained
    assert (state / "events.jsonl").read_bytes() == events
    assert not backend.list_active_claims()
    assert not list(root.glob("runs/*/dispatch.json"))


def test_preview_route_reader_validates_fields_and_preserves_legacy_actor(tmp_path):
    from anvil.execution import json_bytes, read_execution_preview
    from tests.test_sqlite import _make_claim_payload

    preview_id = "a" * 32
    path = tmp_path / "previews" / (preview_id + ".json")
    path.parent.mkdir(mode=0o700)
    preview = {"version": 1, "preview_id": preview_id, "task_id": "release:T001",
               "project_id": "synthetic", "actor": "synthetic-owner",
               "cwd": str(tmp_path.resolve()), "claim": None}
    # Legacy Run previews omit operation. Reading must preserve the exact bytes.
    path.write_bytes(json_bytes(preview))
    assert read_execution_preview(tmp_path, preview_id) == preview
    assert path.read_bytes() == json_bytes(preview)
    invalid = [{key: value for key, value in preview.items() if key != missing}
               for missing in preview]
    for field, maximum in (("task_id", 255), ("project_id", 255), ("actor", 4096),
                           ("cwd", 4096), ("preview_id", 32)):
        invalid.extend({**preview, field: value}
                       for value in (None, [], {}, 1, "", "x" * (maximum + 1)))
    invalid.extend({**preview, "version": value} for value in (None, True, 2, "1"))
    invalid.extend({**preview, "operation": value} for value in (None, [], {}, 1, "other"))
    invalid.extend({**preview, "claim": value} for value in ([], {}, 1, "claim"))
    invalid.extend(({**preview, "preview_id": "b" * 32}, {**preview, "cwd": "relative"},
                    {**preview, "continuation": {}}))
    continuation = {
        "prior_run_id": "b" * 32, "prior_adapter_run_id": "c" * 32,
        "conversation_id": "d" * 32, "official_session_id": "synthetic-session",
        **dict.fromkeys(("prior_request_file_sha256", "prior_result_file_sha256",
                         "prior_adapter_result_file_sha256", "prior_adapter_receipt_sha256",
                         "checkpoint_sha256", "session_sha256"), "e" * 64),
    }
    continued = {**preview, "operation": "continue", "continuation": continuation}
    path.write_bytes(json_bytes(continued))
    assert read_execution_preview(tmp_path, preview_id) == continued
    invalid.extend({**continued, "continuation": value} for value in (None, [], {}, 1))
    for key in ("prior_run_id", "prior_adapter_run_id", "conversation_id"):
        invalid.append({**continued, "continuation": {**continuation, key: []}})
    # Existing claims retain their exact historical actor spelling, even when
    # it exceeds the current new-actor limit, and use resolved checkout identity.
    historical_actor = "legacy-" + "x" * 130
    claim = {**_make_claim_payload(task_id=preview["task_id"], actor=historical_actor),
             "worktree_path": str(tmp_path / "unused" / "..")}
    claimed = {**preview, "actor": historical_actor, "claim": claim}
    path.write_bytes(json_bytes(claimed))
    assert read_execution_preview(tmp_path, preview_id) == claimed
    for field, value in (("task_id", "release:T002"), ("claimed_by", "foreign"),
                         ("worktree_path", str(tmp_path / "foreign"))):
        invalid.append({**claimed, "claim": {**claim, field: value}})
    for value in invalid:
        path.write_bytes(json_bytes(value))
        with pytest.raises(ExecutionError) as failure:
            read_execution_preview(tmp_path, preview_id)
        assert failure.value.code == "preview_mismatch"
        assert path.read_bytes() == json_bytes(value)


def test_preview_validity_dates_refuse_confirmation_but_allow_inspection(execution_project, monkeypatch):
    from anvil.execution import confirm_continue, inspect_execution_preview, json_bytes

    backend, repo, state, _ = execution_project
    preview = _preview(execution_project)
    root = journal_root(repo)
    path = root / "previews" / (preview["preview_id"] + ".json")
    events = (state / "events.jsonl").read_bytes()

    def forbid_dispatch(*args, **kwargs):
        pytest.fail("Malformed preview must not reserve or dispatch work")

    monkeypatch.setattr("anvil.execution.reserve_slot", forbid_dispatch)
    monkeypatch.setattr("anvil.execution.native_cli", forbid_dispatch)
    for field in ("created_at", "expires_at"):
        invalid = [{key: value for key, value in preview.items() if key != field}]
        invalid.extend({**preview, field: value}
                       for value in (None, [], {}, 1, "invalid", "2026-01-01T00:00:00"))
        for value in invalid:
            path.write_bytes(json_bytes(value))
            assert inspect_execution_preview(repo, preview["preview_id"], "synthetic-owner")["state"] == "unconsumed"
            with pytest.raises(ExecutionError) as failure:
                confirm_run(repo, preview["task_id"], "synthetic-owner", preview["preview_id"])
            assert failure.value.code == "preview_mismatch"
            assert path.read_bytes() == json_bytes(value)
    for value in ({**preview, "preview_id": "f" * 32}, {**preview, "claim": {}},
                  {**preview, "version": True}):
        path.write_bytes(json_bytes(value))
        for confirm in (
            lambda: confirm_run(repo, preview["task_id"], "synthetic-owner", preview["preview_id"]),
            lambda: confirm_continue(repo, "b" * 32, "synthetic-owner", preview["preview_id"]),
        ):
            with pytest.raises(ExecutionError) as failure:
                confirm()
            assert failure.value.code == "preview_mismatch"
    assert (state / "events.jsonl").read_bytes() == events
    assert not backend.list_active_claims()
    assert not list(root.glob("runs/*/dispatch.json"))


def test_saved_preview_inspection_is_exact_and_read_only_after_expiry(saved_execution_project, monkeypatch):
    from anvil.execution import inspect_execution_preview
    from tests.test_execution_runner import _wait

    backend, repo, state, _ = saved_execution_project
    preview = _preview(saved_execution_project)
    before = (state / "events.jsonl").read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr("anvil.execution.now", lambda: datetime.fromisoformat(preview["expires_at"]) + timedelta(seconds=1))
        inspected = inspect_execution_preview(repo, preview["preview_id"], "synthetic-owner")
        assert inspected["state"] == "unconsumed" and inspected["run_id"] is None
    assert (state / "events.jsonl").read_bytes() == before
    with pytest.raises(ExecutionError, match="actor"):
        inspect_execution_preview(repo, preview["preview_id"], "foreign")
    admitted = confirm_run(repo, "release:T001", "synthetic-owner", preview["preview_id"])
    directory = journal_root(repo) / "runs" / admitted["run_id"]
    result = _wait(directory)
    assert result["state"] == "needs_review", result
    before = (state / "events.jsonl").read_bytes()
    inspected = inspect_execution_preview(repo, preview["preview_id"], "synthetic-owner")
    assert inspected["state"] == "consumed" and inspected["run_id"] == admitted["run_id"]
    from anvil.execution import json_bytes

    names = ("request.json", "context.json", "dispatch.json")
    retained = {name: (directory / name).read_bytes() for name in names}
    for field in ("dispatch", "actor", "task"):
        request, context, dispatch_data = (json.loads(retained[name]) for name in names)
        if field == "dispatch":
            request["dispatch_id"] = context["run_id"] = "f" * 32
        elif field == "actor":
            request["binding"]["actor"] = context["actor"] = "foreign-owner"
        else:
            request["binding"]["task_id"] = context["task_id"] = "release:T002"
        request["request_sha256"] = digest(json_bytes({key: value for key, value in request.items() if key != "request_sha256"}))
        dispatch_data.update(request_sha256=request["request_sha256"],
                             request_file_sha256=digest(json_bytes(request)),
                             context_sha256=digest(json_bytes(context)))
        try:
            for name, value in zip(names, (request, context, dispatch_data), strict=True):
                (directory / name).write_bytes(json_bytes(value))
            assert inspect_execution_preview(repo, preview["preview_id"], "synthetic-owner")["state"] == "unknown"
        finally:
            for name, raw in retained.items():
                (directory / name).write_bytes(raw)
    consumed = journal_root(repo) / "consumed" / (preview["preview_id"] + ".json")
    original_consumed = consumed.read_bytes()
    consumed.unlink()
    try:
        assert inspect_execution_preview(repo, preview["preview_id"], "synthetic-owner")["state"] == "unknown"
    finally:
        consumed.write_bytes(original_consumed)
        consumed.chmod(0o600)
    assert len(list(journal_root(repo).glob("runs/*/dispatch.json"))) == 1
    dispatch = directory / "dispatch.json"
    original = dispatch.read_bytes()
    try:
        for value in ({}, [], None, 1):
            dispatch.write_bytes(json_bytes(value))
            assert inspect_execution_preview(repo, preview["preview_id"], "synthetic-owner")["state"] == "unknown"
            assert dispatch.read_bytes() == json_bytes(value)
    finally:
        dispatch.write_bytes(original)
    assert (state / "events.jsonl").read_bytes() == before
    assert not backend.list_active_claims()



def test_known_failed_saved_check_requires_explicit_release_before_continue(saved_execution_project):
    from anvil.execution import confirm_continue, git, preview_continue
    from tests.test_execution_runner import _wait

    backend, repo, state, _ = saved_execution_project
    task = backend.get_task("release:T001")
    failed_command = "exit 9"
    backend.append(_event("task.created", {
        **task.model_dump(mode="json"), "prd_id": task.prd_id,
        "verification": {"commands": [failed_command], "required_proofs": [
            {"kind": "command", "command": failed_command, "passing_exit_codes": [0], "label": "fails"}]},
    }, target_kind="task", target_id=task.id))
    preview = _preview(saved_execution_project)
    prior = confirm_run(repo, task.id, "synthetic-owner", preview["preview_id"])
    directory = journal_root(repo) / "runs" / prior["run_id"]
    failed = _wait(directory)
    assert failed["state"] == "failed_stopped" and failed["error_code"] == "command_failed"
    assert backend.get_latest_evidence(task.id) is None
    assert backend.get_claim(prior["claim_id"]).status.value == "active"
    checkout = Path(prior["cwd"])
    git(checkout, "add", "README.md")
    git(checkout, "commit", "-qm", "Retain inspected synthetic edit after failed check")
    git(repo, "merge", "--ff-only", git(checkout, "rev-parse", "HEAD").decode().strip())
    with pytest.raises(ExecutionError, match="isolated"):
        preview_continue(repo, prior["run_id"], "synthetic-owner", RunOptions())
    native_cli(repo, "release", prior["claim_id"], "--actor", "synthetic-owner",
               "--reason", "Explicitly re-admit inspected failed-check work", "--json")
    assert backend.get_task(task.id).status.value == "ready"
    task = backend.get_task(task.id)
    command = shlex.join([sys.executable, "-I", "-c",
                         "from pathlib import Path; assert Path('README.md').read_bytes() == b'continued\\n'; print('recovered-native-proof')"])
    backend.append(_event("task.created", {
        **task.model_dump(mode="json"), "prd_id": task.prd_id,
        "verification": {"commands": [command], "required_proofs": [
            {"kind": "command", "command": command, "passing_exit_codes": [0], "label": "recovered"}]},
    }, target_kind="task", target_id=task.id))
    outcome_path = directory / "result.json"
    original = outcome_path.read_bytes()
    outcome_path.write_text(json.dumps({**failed, "error_code": "source_changed"}))
    try:
        with pytest.raises(ExecutionError, match="Uncertain native failure"):
            preview_continue(repo, prior["run_id"], "synthetic-owner", RunOptions())
    finally:
        outcome_path.write_bytes(original)
    continued = preview_continue(repo, prior["run_id"], "synthetic-owner", RunOptions())
    assert continued["continuation_material"]["review"] is None
    next_run = confirm_continue(repo, prior["run_id"], "synthetic-owner", continued["preview_id"])
    assert _wait(journal_root(repo) / "runs" / next_run["run_id"])["state"] == "needs_review"
    assert backend.get_claim(next_run["claim_id"]).generation == 2
    proof = backend.get_latest_evidence(task.id).proofs[0].evidence_core
    assert proof.claim_id == next_run["claim_id"] and proof.generation == 2
    assert not any(json.loads(line)["action"] == "task.applied"
                   for line in (state / "events.jsonl").read_bytes().splitlines())
