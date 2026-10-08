"""Actual CLI profile targets, Git custody, and compensation."""

import json
import subprocess
from pathlib import Path

import pytest

from anvil.cli._helpers import _open_backend
from anvil.state.backend import EventRejected
from anvil.state.sqlite import SqliteBackend
from tests.test_profile_planning_cli import _approve, _invoke, _project


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
