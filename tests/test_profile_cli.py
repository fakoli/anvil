"""Actual CLI profile targets, Git custody, and compensation."""

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from anvil.claims.manager import ClaimError
from anvil.cli import app
from anvil.cli._helpers import _open_backend
from anvil.state.backend import EventRejected
from anvil.state.sqlite import SqliteBackend
from tests.test_profile_planning_cli import _approve, _invoke, _project, runner


def _git(root, *args):
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _prepared(tmp_path, monkeypatch, *, bundle=False, workspace=False, profiled=True):
    root, state, source = _project(
        tmp_path, monkeypatch, "default", workspace=workspace, profiled=profiled,
    )
    config = state / "config.yaml"
    config.write_text(config.read_text().replace("events_storage: git", "events_storage: local"))
    source.write_text(source.read_text().replace(
        "**Feature:** F001", "**Feature:** F001\n**Likely files:** src/feature.txt\n"
        "**Acceptance criteria:**\n- Verification succeeds.",
    ))
    _git(root, "add", "anvil-verification.toml", "verify.py")
    _git(root, "commit", "-m", "Commit verification sources")
    _invoke(root, ["prd", "parse", "--json"])
    _approve(root, "default")
    _invoke(root, ["plan", "--no-llm", "--json"])
    _invoke(root, ["score", "--json"])
    _invoke(root, ["review", "tasks", "--json"])
    if bundle:
        _invoke(root, [
            "bundle", "create", "B1", "T001", "--prd", "default",
            "--coordinator", "author", "--actor", "author", "--json",
        ])
    return root, state, source


def _claim_args(bundle, *extra):
    return [
        "claim", "B1" if bundle else "T001", *(["--bundle"] if bundle else []),
        "--actor", "author", "--json", *extra,
    ]


def _no_claim(state, root, before):
    assert (state / "events.jsonl").read_bytes() == before
    backend = _open_backend(state, project_root=root)
    try:
        assert backend.list_active_claims() == []
        assert backend.get_task("T001").status.value == "ready"
    finally:
        backend.close()


def _git_identity(root):
    return tuple(_git(root, *args) for args in [
        ("show-ref",), ("worktree", "list", "--porcelain"),
        ("rev-parse", "HEAD"), ("status", "--porcelain"),
    ])


@pytest.mark.parametrize("bundle", [False, True])
@pytest.mark.parametrize("existing_branch", [False, True])
@pytest.mark.parametrize("write_error", [
    sqlite3.OperationalError, EventRejected, "audit_allocation",
])
def test_postlog_claim_failure_retains_recoverable_git(
    tmp_path, monkeypatch, bundle, existing_branch, write_error,
):
    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=bundle)
    branch = "existing-target" if existing_branch else "new-target"
    if existing_branch:
        _git(root, "branch", branch)
    target = root.parent / ("wt-b1" if bundle else "wt-t001")
    before = (state / "events.jsonl").read_bytes()
    action = "bundle.claimed" if bundle else "claim.created"
    writer = "_write_bundle_claimed" if bundle else "_write_claim_created"
    prepared = []
    audit_failures = []
    dumps = json.dumps

    def fail_audit_allocation(value, *args, **kwargs):
        if isinstance(value, dict) and value.get("kind") == "write_failed_after_log":
            audit_failures.append(value["event_id"])
            raise MemoryError("injected audit serialization allocation failure")
        return dumps(value, *args, **kwargs)

    def fail_after_log(backend, conn, payload, event):
        logged = (state / "events.jsonl").read_bytes()[len(before):]
        assert action in logged.decode()
        assert event.id in logged.decode()
        assert payload.git_metadata.target_path == str(target)
        assert target.is_dir()
        prepared.append((_git_identity(target), (target / "verify.py").read_bytes()))
        error_type = EventRejected if write_error == "audit_allocation" else write_error
        raise error_type("injected post-log projection failure")

    with monkeypatch.context() as patch:
        patch.setattr(SqliteBackend, writer, fail_after_log)
        if write_error == "audit_allocation":
            patch.setattr(json, "dumps", fail_audit_allocation)
        result = runner.invoke(app, [
            *_claim_args(bundle, "--worktree", "--branch", branch), "--cwd", str(root),
        ], catch_exceptions=True)

    assert result.exit_code == 1
    if write_error == "audit_allocation":
        assert isinstance(result.exception, MemoryError)
        assert isinstance(result.exception.__context__, EventRejected)
        assert len(audit_failures) == 1
    else:
        assert "log line remains" in result.output
    appended = (state / "events.jsonl").read_bytes()[len(before):]
    events = [json.loads(line) for line in appended.splitlines()]
    assert len(events) == 1 and events[0]["action"] == action
    if audit_failures:
        assert audit_failures == [events[0]["id"]]
    assert target.is_dir()
    assert prepared == [(_git_identity(target), (target / "verify.py").read_bytes())]
    assert _git(target, "symbolic-ref", "--short", "HEAD") == branch
    assert _git(root, "rev-parse", branch) == _git(target, "rev-parse", "HEAD")
    backend = _open_backend(state, project_root=root)
    try:
        claims = backend.list_active_claims()
        assert len(claims) == 1
        assert claims[0].worktree_path == str(target)
        assert claims[0].branch == branch
        assert claims[0].status.value == "active"
        assert backend.get_task("T001").status.value == "claimed"
        if bundle:
            bundle_claim = backend.get_bundle_claim("B1")
            assert bundle_claim.status.value == "active"
            assert bundle_claim.id == claims[0].bundle_claim_id
            assert bundle_claim.worktree_path == str(target)
            assert bundle_claim.branch == branch
    finally:
        backend.close()


@pytest.mark.parametrize("bundle", [False, True])
@pytest.mark.parametrize("failure", [
    "unknown", "unknown_cause", "os_context", "native_wrapper_unknown",
    "native_wrapper_context_only", "interruption", "interruption_context",
    "profile", "context_cycle",
])
def test_postcallback_claim_failure_respects_publication_certainty(
    tmp_path, monkeypatch, bundle, failure,
):
    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=bundle)
    target = root.parent / ("wt-b1" if bundle else "wt-t001")
    before = (state / "events.jsonl").read_bytes()
    identity = _git_identity(root)
    check = SqliteBackend._check_live_profiles
    observed = []

    def fail_after_callback(backend, conn, action, payload, prepared):
        check(backend, conn, action, payload, prepared)
        if action not in {"claim.created", "bundle.claimed"}:
            return
        observed.append(action)
        assert target.is_dir()
        if failure == "profile":
            source = target / "verify.py"
            original = source.read_bytes()
            try:
                source.write_text("assert False\n")
                check(backend, conn, action, payload, prepared)
                pytest.fail("native final profile guard did not reject drift")
            finally:
                source.write_bytes(original)
        if failure.startswith("interruption"):
            interrupt = KeyboardInterrupt("injected post-callback interruption")
            if failure == "interruption_context":
                interrupt.__context__ = EventRejected("earlier prepublication refusal")
            raise interrupt
        error = RuntimeError("injected post-callback uncertainty")
        if failure == "context_cycle":
            refusal = EventRejected("injected prepublication refusal")
            error.__context__ = refusal
            refusal.__context__ = error
        elif failure == "unknown_cause":
            error.__cause__ = EventRejected("earlier prepublication refusal")
        elif failure == "os_context":
            error = OSError("injected post-callback uncertainty")
            error.__context__ = EventRejected("earlier prepublication refusal")
        elif failure == "native_wrapper_unknown":
            error.__cause__ = EventRejected("earlier prepublication refusal")
            raise ClaimError("injected post-callback uncertainty") from error
        elif failure == "native_wrapper_context_only":
            error = ClaimError("injected post-callback uncertainty")
            error.__context__ = EventRejected("earlier prepublication refusal")
        raise error

    monkeypatch.setattr(SqliteBackend, "_check_live_profiles", fail_after_callback)
    result = runner.invoke(app, [
        *_claim_args(bundle, "--worktree", "--branch", "new-target"), "--cwd", str(root),
    ], catch_exceptions=True)

    assert result.exit_code != 0
    assert len(observed) == 1
    _no_claim(state, root, before)
    if failure == "profile":
        assert not target.exists()
        assert _git_identity(root) == identity
    else:
        assert target.is_dir()
        assert _git(target, "symbolic-ref", "--short", "HEAD") == "new-target"
        assert _git(root, "rev-parse", "new-target") == _git(target, "rev-parse", "HEAD")
    if failure == "profile":
        assert "verification profile refused" in result.output
    elif failure.startswith("interruption"):
        assert result.exit_code == 130
    else:
        assert "injected post-callback uncertainty" in f"{result.output}{result.exception}"


@pytest.mark.parametrize("bundle", [False, True])
def test_existing_branch_shared_profile_refusal_restores_owned_checkout(
    tmp_path, monkeypatch, bundle,
):
    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=bundle)
    caller = _git(root, "symbolic-ref", "--short", "HEAD")
    runner = (root / "verify.py").read_bytes()
    _git(root, "checkout", "-b", "existing-target")
    (root / "verify.py").write_text("assert False\n")
    _git(root, "add", "verify.py")
    _git(root, "commit", "-m", "Change target profile")
    _git(root, "checkout", caller)
    before = (state / "events.jsonl").read_bytes()
    identity = _git_identity(root)

    result = _invoke(root, _claim_args(
        bundle, "--shared-tree", "--branch", "existing-target",
    ), expected=1)

    assert "verification profile refused" in result.output
    _no_claim(state, root, before)
    assert _git_identity(root) == identity
    assert (root / "verify.py").read_bytes() == runner


@pytest.mark.parametrize("bundle", [False, True])
def test_existing_branch_new_target_append_refusal_removes_only_owned_worktree(
    tmp_path, monkeypatch, bundle,
):
    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=bundle)
    _git(root, "branch", "existing-target")
    target = root.parent / ("wt-b1" if bundle else "wt-t001")
    assert not target.exists()
    before = (state / "events.jsonl").read_bytes()
    identity = _git_identity(root)
    append = SqliteBackend.append

    def refuse(backend, draft, **kwargs):
        if draft.action in {"claim.created", "bundle.claimed"}:
            assert target.is_dir()
            raise EventRejected("injected native append refusal")
        return append(backend, draft, **kwargs)

    monkeypatch.setattr(SqliteBackend, "append", refuse)
    result = _invoke(root, _claim_args(
        bundle, "--worktree", "--branch", "existing-target",
    ), expected=1)

    assert "injected native append refusal" in result.output
    _no_claim(state, root, before)
    assert not target.exists()
    assert _git_identity(root) == identity


@pytest.mark.parametrize("bundle", [False, True])
@pytest.mark.parametrize("change", [
    "shared_dirty", "shared_commit", "shared_checkout", "shared_original_ref",
    "isolated_dirty", "isolated_commit", "isolated_checkout", "isolated_replacement",
])
def test_existing_branch_compensation_preserves_intervening_git_changes(
    tmp_path, monkeypatch, bundle, change,
):
    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=bundle)
    caller = _git(root, "symbolic-ref", "--short", "HEAD")
    _git(root, "checkout", "-b", "foreign-target")
    _git(root, "commit", "--allow-empty", "-m", "Independent commit")
    foreign_sha = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", caller)
    _git(root, "branch", "existing-target")
    isolated = change.startswith("isolated_")
    target = root.parent / ("wt-b1" if bundle else "wt-t001") if isolated else root
    relocated = target.with_name(target.name + "-foreign")
    before = (state / "events.jsonl").read_bytes()
    append = SqliteBackend.append
    observed = []

    def refuse(backend, draft, **kwargs):
        if draft.action in {"claim.created", "bundle.claimed"}:
            assert _git(target, "symbolic-ref", "--short", "HEAD") == "existing-target"
            if change.endswith("_dirty"):
                (target / "foreign.txt").write_bytes(b"preserve concurrent work\n")
            elif change.endswith("_commit"):
                head_path = Path(_git(target, "rev-parse", "--absolute-git-dir")) / "HEAD"
                identity = head_path.stat()
                _git(target, "commit", "--allow-empty", "-m", "Concurrent commit")
                after = head_path.stat()
                assert (identity.st_dev, identity.st_ino, identity.st_ctime_ns) == (
                    after.st_dev, after.st_ino, after.st_ctime_ns,
                )
            elif change.endswith("_checkout"):
                _git(target, "checkout", "--no-guess", "foreign-target")
            elif change.endswith("_original_ref"):
                _git(root, "update-ref", f"refs/heads/{caller}", foreign_sha)
            else:
                original_admin = (target / ".git").read_bytes()
                _git(root, "worktree", "move", str(target), str(relocated))
                _git(root, "worktree", "add", "--detach", str(target), "existing-target")
                assert (target / ".git").read_bytes() != original_admin
            observed.append((_git_identity(root), _git_identity(target)))
            raise EventRejected("injected native append refusal")
        return append(backend, draft, **kwargs)

    monkeypatch.setattr(SqliteBackend, "append", refuse)
    result = _invoke(root, _claim_args(
        bundle, "--worktree" if isolated else "--shared-tree", "--branch", "existing-target",
    ), expected=1)

    assert "injected native append refusal" in result.output
    _no_claim(state, root, before)
    assert len(observed) == 1 and target.is_dir()
    assert (_git_identity(root), _git_identity(target)) == observed[0]
    assert _git(root, "rev-parse", "--verify", "refs/heads/existing-target")
    if change.endswith("_dirty"):
        assert (target / "foreign.txt").read_bytes() == b"preserve concurrent work\n"
    if change.endswith("_replacement"):
        assert relocated.is_dir()


@pytest.mark.parametrize("bundle", [False, True])
@pytest.mark.parametrize("workspace", [False, True])
@pytest.mark.parametrize("canonical_drift", [False, True])
def test_profile_claim_prepares_actual_isolated_target(
    tmp_path, monkeypatch, bundle, workspace, canonical_drift,
):
    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=bundle, workspace=workspace)
    runner_bytes = (root / "verify.py").read_bytes()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.setenv("ANVIL_ROOT", str(unrelated))
    monkeypatch.chdir(unrelated)
    if canonical_drift:
        (root / "verify.py").write_text("uncommitted canonical drift\n")
    result = _invoke(root, _claim_args(bundle, "--worktree"))
    claim = json.loads(result.output)["data"]["claim"]
    target = Path(claim["worktree_path"])
    assert target != root and (target / "verify.py").read_bytes() == runner_bytes
    assert claim["git_metadata"]["target_path"] == str(target)
    backend = _open_backend(state, project_root=root)
    try:
        assert len(backend.list_active_claims()) == 1
    finally:
        backend.close()


@pytest.mark.parametrize("linked", [False, True])
def test_dedicated_profile_from_nested_caller_uses_actual_target(
    tmp_path, monkeypatch, linked,
):
    import anvil.cli.bundle as bundle_cli

    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=True, workspace=True)
    target = root
    if linked:
        target = tmp_path / "linked"
        _git(root, "worktree", "add", "-b", "linked", str(target))
    caller = target / "nested"
    caller.mkdir()
    monkeypatch.setenv("ANVIL_ROOT", str(tmp_path / "unrelated"))
    before = _git_identity(root), _git_identity(target)
    opened_roots = []
    open_backend = bundle_cli._open_backend

    def observe_backend(*args, **kwargs):
        opened_roots.append(kwargs["project_root"])
        return open_backend(*args, **kwargs)

    monkeypatch.setattr(bundle_cli, "_open_backend", observe_backend)
    result = _invoke(caller, [
        "bundle", "claim", "B1", "--shared-tree", "--actor", "author", "--json",
    ])
    claim = json.loads(result.output)["data"]["claim"]
    assert opened_roots == [caller]
    assert claim["git_metadata"]["target_path"] == str(target)
    assert claim["git_metadata"]["canonical_root"] == str(root)
    assert claim["branch"] == _git(target, "branch", "--show-current")
    assert claim["worktree_path"] is None
    assert (_git_identity(root), _git_identity(target)) == before
    backend = _open_backend(state, project_root=caller)
    try:
        members = backend.list_active_claims()
        assert len(members) == 1 and members[0].bundle_claim_id == claim["id"]
        assert backend.get_task("T001").status.value == "claimed"
    finally:
        backend.close()


@pytest.mark.parametrize("workspace", [False, True])
@pytest.mark.parametrize("profiled", [False, True])
def test_dedicated_bundle_uses_actual_shared_identity_without_git_mutation(
    tmp_path, monkeypatch, workspace, profiled,
):
    root, state, _ = _prepared(
        tmp_path, monkeypatch, bundle=True, workspace=workspace, profiled=profiled,
    )
    monkeypatch.setenv("ANVIL_ROOT", str(tmp_path / "unrelated"))
    before = _git_identity(root)
    result = _invoke(root, [
        "bundle", "claim", "B1", "--shared-tree", "--actor", "author", "--json",
    ])
    claim = json.loads(result.output)["data"]["claim"]
    assert _git_identity(root) == before
    assert claim["worktree_path"] is None
    if profiled:
        assert claim["branch"] == _git(root, "branch", "--show-current")
        assert claim["git_metadata"]["claim_start_sha"] == _git(root, "rev-parse", "HEAD")
        assert claim["git_metadata"]["target_path"] == str(root)
    else:
        assert claim.get("git_metadata") is None and claim["branch"] is None


@pytest.mark.parametrize("bundle", [False, True])
@pytest.mark.parametrize("change", ["missing", "runner", "manifest"])
def test_preexisting_target_profile_refusal_preserves_git_and_state(
    tmp_path, monkeypatch, bundle, change,
):
    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=bundle)
    target = root.parent / ("wt-b1" if bundle else "wt-t001")
    _git(root, "worktree", "add", "-b", "existing", str(target))
    if change == "missing":
        (target / "verify.py").unlink()
    else:
        (target / ("verify.py" if change == "runner" else "anvil-verification.toml")).write_text(
            "foreign target bytes\n",
        )
    _git(target, "add", "-u")
    _git(target, "commit", "-m", "Target drift")
    before = (state / "events.jsonl").read_bytes()
    identity = _git_identity(root), _git_identity(target)
    target_files = {p.name: p.read_bytes() for p in target.iterdir() if p.is_file()}
    result = _invoke(root, _claim_args(bundle, "--worktree", "--branch", "existing"), expected=1)
    assert "verification profile refused" in result.output
    _no_claim(state, root, before)
    assert (_git_identity(root), _git_identity(target)) == identity
    assert {p.name: p.read_bytes() for p in target.iterdir() if p.is_file()} == target_files


@pytest.mark.parametrize("bundle", [False, True])
def test_fresh_target_cannot_borrow_uncommitted_canonical_runner(tmp_path, monkeypatch, bundle):
    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=bundle)
    original = (root / "verify.py").read_bytes()
    _git(root, "rm", "verify.py")
    _git(root, "commit", "-m", "Baseline without runner")
    (root / "verify.py").write_bytes(original)
    before = (state / "events.jsonl").read_bytes()
    identity = _git_identity(root)
    result = _invoke(root, _claim_args(bundle, "--worktree"), expected=1)
    assert "verification profile refused" in result.output
    _no_claim(state, root, before)
    assert _git_identity(root) == identity
    assert (root / "verify.py").read_bytes() == original
    assert not (root.parent / ("wt-b1" if bundle else "wt-t001")).exists()


@pytest.mark.parametrize("bundle", [False, True])
@pytest.mark.parametrize("failure", ["append", "canonical"])
@pytest.mark.parametrize("existing", [False, True])
def test_prepared_target_compensates_native_append_and_late_source_refusal(
    tmp_path, monkeypatch, bundle, failure, existing,
):
    root, state, source = _prepared(tmp_path, monkeypatch, bundle=bundle)
    extra = []
    if existing:
        target = root.parent / ("wt-b1" if bundle else "wt-t001")
        _git(root, "worktree", "add", "-b", "existing", str(target))
        extra = ["--branch", "existing"]
    before = (state / "events.jsonl").read_bytes()
    identity = _git_identity(root)
    original_append = SqliteBackend.append
    observed = []

    def interrupted(backend, draft, **kwargs):
        if draft.action in {"claim.created", "bundle.claimed"}:
            target = Path(draft.payload_json["git_metadata"]["target_path"])
            assert target.is_dir() and (target / "verify.py").is_file()
            assert backend.list_active_claims() == []
            observed.append(target)
            if failure == "append":
                raise EventRejected("injected native append refusal")
            callback = kwargs["pre_log_check"]

            def late_source_change():
                source.write_text(source.read_text() + "\nChanged after preparation.\n")
                callback()

            kwargs["pre_log_check"] = late_source_change
        return original_append(backend, draft, **kwargs)

    monkeypatch.setattr(SqliteBackend, "append", interrupted)
    result = _invoke(root, _claim_args(bundle, "--worktree", *extra), expected=1)
    assert "injected native append refusal" in result.output if failure == "append" else (
        "prd_source_unapproved" in result.output
    )
    assert len(observed) == 1 and observed[0].exists() == existing
    if existing:
        assert (observed[0] / "verify.py").read_bytes() == (root / "verify.py").read_bytes()
    _no_claim(state, root, before)
    assert _git_identity(root) == identity


@pytest.mark.parametrize("bundle", [False, True])
@pytest.mark.parametrize("profiled", [False, True])
def test_git_preparation_failure_keeps_profile_state_empty_and_legacy_order(
    tmp_path, monkeypatch, bundle, profiled,
):
    import anvil.git_ops as git_ops

    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=bundle, profiled=profiled)
    before = (state / "events.jsonl").read_bytes()
    identity = _git_identity(root)
    observed = []

    def fail_preparation(*args, **kwargs):
        observed.append(sum(
            json.loads(line)["action"] in {"claim.created", "bundle.claimed"}
            for line in (state / "events.jsonl").read_bytes()[len(before):].splitlines()
        ))
        raise git_ops.ClaimPlanError("injected_prepare", "injected Git preparation refusal")

    monkeypatch.setattr(git_ops, "apply_claim_plan", fail_preparation)
    result = _invoke(root, _claim_args(bundle, "--worktree"), expected=1)
    assert "injected_prepare" in result.output
    assert observed == ([0] if profiled else [1])
    assert _git_identity(root) == identity
    if profiled:
        _no_claim(state, root, before)
    else:
        actions = [json.loads(line)["action"] for line in (
            (state / "events.jsonl").read_bytes()[len(before):].splitlines()
        )]
        assert actions == (["bundle.claimed", "bundle.claim_released"] if bundle else [
            "claim.created", "claim.released",
        ])
        backend = _open_backend(state, project_root=root)
        try:
            assert backend.list_active_claims() == []
        finally:
            backend.close()


@pytest.mark.parametrize("bundle", [False, True])
def test_foreign_target_sentinel_is_not_removed(tmp_path, monkeypatch, bundle):
    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=bundle)
    target = root.parent / ("wt-b1" if bundle else "wt-t001")
    target.mkdir()
    sentinel = target / "foreign"
    sentinel.write_bytes(b"must survive")
    before = (state / "events.jsonl").read_bytes()
    identity = _git_identity(root)
    result = _invoke(root, _claim_args(bundle, "--worktree"), expected=1)
    assert "target_path_occupied" in result.output
    assert sentinel.read_bytes() == b"must survive"
    assert _git_identity(root) == identity
    _no_claim(state, root, before)


@pytest.mark.parametrize("case", ["detached", "nongit", "required", "linked"])
def test_dedicated_profile_refuses_unavailable_identity_or_isolation(
    tmp_path, monkeypatch, case,
):
    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=True, workspace=case == "linked")
    if case == "detached":
        _git(root, "checkout", "--detach")
    elif case == "nongit":
        (root / ".git").rename(root / "saved-git")
    elif case == "required":
        config = state / "config.yaml"
        config.write_text(config.read_text() + "\nworktree_isolation: require\n")
    else:
        linked = tmp_path / "linked"
        _git(root, "worktree", "add", "-b", "linked", str(linked))
        root = linked
    before = (state / "events.jsonl").read_bytes()
    identity = None if case == "nongit" else _git_identity(root)
    result = _invoke(root, ["bundle", "claim", "B1", "--actor", "author", "--json"], expected=1)
    expected = {
        "detached": "caller_head_unavailable", "nongit": "caller_head_unavailable",
        "required": "top-level", "linked": "linked_shared_tree_not_authorized",
    }
    assert expected[case] in result.output
    _no_claim(state, root, before)
    if identity is not None:
        assert _git_identity(root) == identity


@pytest.mark.parametrize("route", ["ordinary", "top_bundle", "dedicated"])
def test_profile_claim_keeps_registered_root_policy(tmp_path, monkeypatch, route):
    from anvil.roots.registry import RootSetRegistry

    bundle = route != "ordinary"
    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=bundle)
    RootSetRegistry().enroll(repository_id="fixture", path=str(root), origin="local:fixture")
    before = (state / "events.jsonl").read_bytes()
    identity = _git_identity(root)
    args = (["bundle", "claim", "B1", "--actor", "author", "--json"]
            if route == "dedicated" else _claim_args(bundle, "--worktree"))
    result = _invoke(root, args, expected=1)
    assert "root_set_registered" in result.output
    _no_claim(state, root, before)
    assert _git_identity(root) == identity


def test_dedicated_profile_accepts_explicit_linked_shared_target(tmp_path, monkeypatch):
    root, _, _ = _prepared(tmp_path, monkeypatch, bundle=True, workspace=True)
    linked = tmp_path / "linked"
    _git(root, "worktree", "add", "-b", "linked", str(linked))
    before = _git_identity(root), _git_identity(linked)
    result = _invoke(linked, [
        "bundle", "claim", "B1", "--shared-tree", "--actor", "author", "--json",
    ])
    claim = json.loads(result.output)["data"]["claim"]
    assert claim["branch"] == "linked"
    assert claim["git_metadata"]["target_path"] == str(linked)
    assert claim["git_metadata"]["canonical_root"] == str(root)
    assert (_git_identity(root), _git_identity(linked)) == before


def test_dedicated_profile_revalidates_actual_plan_before_append(tmp_path, monkeypatch):
    import anvil.git_ops as git_ops

    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=True)
    before = (state / "events.jsonl").read_bytes()
    original = git_ops.revalidate_claim_plan
    observed = []

    def advance_then_revalidate(plan, **kwargs):
        observed.append(plan)
        (root / "src/feature.txt").write_text("concurrent commit\n")
        _git(root, "add", "src/feature.txt")
        _git(root, "commit", "-m", "Advance caller after planning")
        return original(plan, **kwargs)

    monkeypatch.setattr(git_ops, "revalidate_claim_plan", advance_then_revalidate)
    result = _invoke(root, [
        "bundle", "claim", "B1", "--shared-tree", "--actor", "author", "--json",
    ], expected=1)
    assert "claim_plan_changed" in result.output
    assert len(observed) == 1
    assert observed[0].caller_head_sha != _git(root, "rev-parse", "HEAD")
    _no_claim(state, root, before)
