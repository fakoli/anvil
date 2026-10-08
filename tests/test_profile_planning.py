"""Pure profile selectors and explicit-root, frozen planning contracts."""
import builtins
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from anvil.planning._plan_helpers import build_prd_revision_draft
from anvil.planning.prd_persistence import PrdRevisionError, build_prd_persistence_plan
from anvil.planning.template import parse_prd
from anvil.state.models import EventDraft
from anvil.state.sqlite import SqliteBackend
from anvil.verification_profiles import MANIFEST

_MANIFEST = '''schema_version = 1
[profiles.full]
source_files = ["verify.py"]
[profiles.full.platforms.linux]
commands = ["python verify.py full"]
'''


def _markdown(field="", *, second=""):
    return f'''# Project: Profiles
## Summary
Frozen verification.
## Goals
- Verify changes.
## Non-goals
- Run commands during planning.
## Requirements
- R001: The task is verified.
## Features
### F001: Verification
**Requirements:** R001
## Tasks
### T001: Verify
**Feature:** F001
**Verification:** pytest -q
{field}
{second}
'''


def _source(markdown):
    data = markdown.encode()
    return SimpleNamespace(
        markdown=markdown, source_bytes=data, source_sha256=hashlib.sha256(data).hexdigest(),
        source_size_bytes=len(data), source_encoding="utf-8",
    )


@pytest.fixture
def repository(tmp_path):
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    (root / MANIFEST).write_text(_MANIFEST)
    (root / "verify.py").write_text("print('verified')\n")
    return root


def _field(root, *, name="full"):
    digest = hashlib.sha256((root / MANIFEST).read_bytes()).hexdigest()
    return f"**Verification profile:** {name} linux {digest}"


def _build(backend, clock, markdown, root=None):
    parsed = parse_prd(markdown, clock=clock)
    plan = build_prd_persistence_plan(
        backend, parsed, _source(markdown), project_id="project", is_default=True,
        actor="test", clock=clock, project_root=root,
    )
    return parsed, plan


def _event(clock, action, kind, target, payload):
    return EventDraft(
        timestamp=clock.now(), actor="test", action=action, target_kind=kind,
        target_id=target, payload_json=payload,
    )


def _persist(backend, clock, parsed, plan, *, tasks=True):
    if backend.get_project() is None:
        backend.append(_event(clock, "project.created", "project", "project", {
            "id": "project", "name": "Profiles", "description": "",
            "created_at": clock.now().isoformat(), "updated_at": clock.now().isoformat(),
        }))
    if plan.draft:
        backend.append(plan.draft)
    if tasks:
        for kind, items in (("feature", parsed.features), ("task", parsed.tasks)):
            for item in items:
                backend.append(_event(clock, f"{kind}.created", kind, item.id, {
                    **item.model_dump(mode="json"), "prd_id": item.prd_id,
                }))


def _forbid(*args, **kwargs):
    raise AssertionError("unexpected filesystem access")


def test_parser_is_pure_and_legacy_contract_is_exact(monkeypatch, frozen_clock):
    field = "**Verification profile:** full linux " + "a" * 64
    with monkeypatch.context() as patch:
        for owner, name in ((builtins, "open"), (os, "open"), (Path, "open"),
                            (Path, "stat"), (Path, "read_bytes"), (Path, "read_text")):
            patch.setattr(owner, name, _forbid)
        parsed = parse_prd(_markdown(field), clock=frozen_clock)
        legacy = parse_prd(_markdown(), clock=frozen_clock)
    assert not parsed.errors
    assert parsed.tasks[0].verification.profile.name == "full"
    assert parsed.tasks[0].verification.profile_binding is None
    assert legacy.tasks[0].verification.model_dump_json() == (
        '{"commands":["pytest -q"],"manual_steps":[],"required_evidence":[],"required_proofs":'
        '[{"kind":"command","command":"pytest -q","passing_exit_codes":[0],'
        '"link_contains":null,"label":"`pytest -q` exits 0","claim":null}]}'
    )


@pytest.mark.parametrize("value", [
    "", "full", "full linux", "full linux " + "a" * 64 + " extra",
    "full unknown " + "a" * 64, "full linux " + "A" * 64,
    "full linux " + "a" * 63, "x" * 65 + " linux " + "a" * 64,
    "../full linux " + "a" * 64, "\x1bsecret linux " + "a" * 64,
    pytest.param("x" * 100000, id="oversized"),
])
def test_malformed_profile_errors_are_bounded(value, frozen_clock):
    parsed = parse_prd(_markdown("**Verification profile:** " + value), clock=frozen_clock)
    assert [error.message for error in parsed.errors] == [
        "Invalid **Verification profile:**; expected NAME PLATFORM MANIFEST_SHA256."
    ]
    assert parsed.tasks[0].verification.profile is None
    assert parsed.errors[0].section == "verification_profile"


def test_duplicate_profile_even_after_empty_field_is_an_error(frozen_clock):
    parsed = parse_prd(_markdown(
        "**Verification profile:**\n**Verification profile:** full linux " + "a" * 64,
    ), clock=frozen_clock)
    assert len(parsed.errors) == 2
    assert parsed.errors[1].message == "Duplicate **Verification profile:** field."

    field = "**Verification profile:** full linux " + "a" * 64
    duplicate = parse_prd(_markdown(field + "\n" + field), clock=frozen_clock)
    assert [error.message for error in duplicate.errors] == [
        "Duplicate **Verification profile:** field."
    ]
    assert duplicate.errors[0].section == "verification_profile"


@pytest.mark.parametrize("field", [
    "**Verification profile:**",
    "**Verification profile:** invalid",
    "**Verification profile:**\n**Verification profile:** full linux " + "a" * 64,
    "\n".join(["**Verification profile:** full linux " + "a" * 64] * 2),
])
def test_invalid_profile_cannot_downgrade_to_literal_verification(
    backend, state_dir, frozen_clock, monkeypatch, field,
):
    markdown = _markdown(field)
    parsed = parse_prd(markdown, clock=frozen_clock)
    assert parsed.tasks[0].verification.commands == ["pytest -q"]
    before = [task.model_dump_json() for task in parsed.tasks]
    events_before = (state_dir / "events.jsonl").read_bytes()
    monkeypatch.setattr("anvil.planning.prd_persistence.materialize_verification", _forbid)
    for root in (None, state_dir):
        with pytest.raises(PrdRevisionError, match="^invalid verification profile declaration$"):
            build_prd_persistence_plan(
                backend, parsed, _source(markdown), project_id="project", is_default=True,
                actor="test", clock=frozen_clock, project_root=root,
            )
    assert [task.model_dump_json() for task in parsed.tasks] == before
    assert (state_dir / "events.jsonl").read_bytes() == events_before
    assert backend.get_prd() is None
    assert backend.get_task("T001") is None


def test_legacy_parser_warning_remains_nonfatal(backend, frozen_clock):
    markdown = _markdown().replace("**Feature:** F001", "**Feature:** F099")
    parsed, plan = _build(backend, frozen_clock, markdown)
    assert parsed.errors and all(error.section == "tasks" for error in parsed.errors)
    assert plan.action == "parsed"


def test_whitespace_and_reference_limits(frozen_clock):
    parsed = parse_prd(_markdown(
        "**Verification profile:** " + "x" * 64 + "\tdarwin  " + "a" * 64,
    ), clock=frozen_clock)
    assert not parsed.errors
    assert parsed.tasks[0].verification.profile.platform == "darwin"


def test_explicit_root_and_literal_profile_merge(backend, frozen_clock, repository):
    markdown = _markdown(_field(repository))
    with pytest.raises(PrdRevisionError, match="explicit project root"):
        _build(backend, frozen_clock, markdown)
    parsed, plan = _build(backend, frozen_clock, markdown, repository)
    verification = parsed.tasks[0].verification
    assert plan.action == "parsed"
    assert backend.get_prd() is None
    assert verification.commands == ["pytest -q", "python verify.py full"]
    assert [proof.command for proof in verification.required_proofs] == verification.commands
    assert all(proof.passing_exit_codes == [0] for proof in verification.required_proofs)
    assert verification.profile_binding is not None


def test_no_profile_needs_no_root_or_files(backend, frozen_clock, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr("anvil.verification_profiles.resolve_profile", _forbid)
        parsed, plan = _build(backend, frozen_clock, _markdown())
    assert plan.action == "parsed"
    assert parsed.tasks[0].verification.profile is None


@pytest.mark.parametrize("failure", ["absent", "changed", "unreadable", "runner_absent"])
def test_sources_refuse_before_mutation(backend, frozen_clock, repository, monkeypatch, failure):
    markdown = _markdown(_field(repository))
    parsed = parse_prd(markdown, clock=frozen_clock)
    before = [task.model_dump_json() for task in parsed.tasks]
    if failure == "absent":
        (repository / MANIFEST).unlink()
    elif failure == "changed":
        (repository / MANIFEST).write_text(_MANIFEST + "\n")
    elif failure == "runner_absent":
        (repository / "verify.py").unlink()
    else:
        def denied(*args, **kwargs):
            raise PermissionError("private path must not escape")
        monkeypatch.setattr("anvil.verification_profiles.os.open", denied)
    with pytest.raises(PrdRevisionError, match="verification profile refused:") as error:
        build_prd_persistence_plan(
            backend, parsed, _source(markdown), project_id="project", is_default=True,
            actor="test", clock=frozen_clock, project_root=repository,
        )
    assert "private path" not in str(error.value)
    assert [task.model_dump_json() for task in parsed.tasks] == before
    assert backend.get_prd() is None


def test_multiple_tasks_materialize_atomically(backend, frozen_clock, repository):
    markdown = _markdown(_field(repository), second=(
        "### T002: Other\n**Feature:** F001\n" + _field(repository, name="missing")
    ))
    parsed = parse_prd(markdown, clock=frozen_clock)
    before = [task.model_dump_json() for task in parsed.tasks]
    with pytest.raises(PrdRevisionError, match="profile_missing"):
        build_prd_persistence_plan(
            backend, parsed, _source(markdown), project_id="project", is_default=True,
            actor="test", clock=frozen_clock, project_root=repository,
        )
    assert [task.model_dump_json() for task in parsed.tasks] == before


def test_same_source_reuses_only_current_frozen_binding(backend, frozen_clock, repository):
    markdown = _markdown(_field(repository))
    parsed, plan = _build(backend, frozen_clock, markdown, repository)
    _persist(backend, frozen_clock, parsed, plan)
    again, unchanged = _build(backend, frozen_clock, markdown, repository)
    assert unchanged.action == "unchanged"
    assert again.tasks[0].verification == parsed.tasks[0].verification
    (repository / "verify.py").write_text("print('different runner')\n")
    with pytest.raises(PrdRevisionError, match="binding_mismatch"):
        _build(backend, frozen_clock, markdown, repository)
    assert backend.get_task("T001").verification == parsed.tasks[0].verification
    assert backend.get_prd().revision == 1


def test_same_source_refuses_unbound_historical_profile(backend, frozen_clock, repository):
    markdown = _markdown(_field(repository))
    parsed, plan = _build(backend, frozen_clock, markdown, repository)
    parsed.tasks[0].verification = parse_prd(markdown).tasks[0].verification
    _persist(backend, frozen_clock, parsed, plan)
    with pytest.raises(PrdRevisionError, match="no frozen task binding"):
        _build(backend, frozen_clock, markdown, repository)


def test_first_plan_after_parse_materializes(backend, frozen_clock, repository):
    markdown = _markdown(_field(repository))
    parsed, plan = _build(backend, frozen_clock, markdown, repository)
    _persist(backend, frozen_clock, parsed, plan, tasks=False)
    first = parse_prd(markdown, clock=frozen_clock)
    with pytest.raises(PrdRevisionError, match="explicit project root"):
        build_prd_revision_draft(
            backend, first, _source(markdown), actor="test", clock=frozen_clock,
        )
    draft = build_prd_revision_draft(
        backend, first, _source(markdown), actor="test", clock=frozen_clock,
        project_root=repository,
    )
    assert draft is None
    assert first.tasks[0].verification == parsed.tasks[0].verification


def test_changed_canonical_reference_creates_new_contract(backend, frozen_clock, repository):
    markdown = _markdown(_field(repository))
    parsed, plan = _build(backend, frozen_clock, markdown, repository)
    _persist(backend, frozen_clock, parsed, plan)
    prd = backend.get_prd()
    backend.append(_event(frozen_clock, "prd.reviewed", "prd", "project", {
        "project_id": "project", "reviewer": "reviewer", "binding_version": 1,
        "expected_revision": prd.revision, "expected_status": "draft",
        "source_sha256": prd.source_sha256, "material_sha256": prd.material_sha256,
        "content_event_id": prd.content_event_id,
    }))
    assert backend.get_prd().status.value == "reviewed"
    (repository / MANIFEST).write_text(_MANIFEST.replace("verify.py full", "verify.py revised"))
    updated = _markdown(_field(repository))
    changed = parse_prd(updated, clock=frozen_clock)
    draft = build_prd_revision_draft(
        backend, changed, _source(updated), actor="test", clock=frozen_clock,
        project_root=repository,
    )
    assert draft.action == "prd.revised"
    assert draft.payload_json["revision"] == 2
    assert changed.tasks[0].verification.profile_binding != parsed.tasks[0].verification.profile_binding
    backend.append(draft)
    assert backend.get_prd().source_bytes == updated.encode()
    assert backend.get_prd().status.value == "draft"
    # A parse revision does not write tasks; subsequent planning must still
    # materialize the new reference against the old task's frozen contract.
    replanned, same = _build(backend, frozen_clock, updated, repository)
    assert same.action == "unchanged"
    assert replanned.tasks[0].verification == changed.tasks[0].verification


def test_runner_change_requires_changed_reference(backend, frozen_clock, repository):
    markdown = _markdown(_field(repository))
    parsed, plan = _build(backend, frozen_clock, markdown, repository)
    _persist(backend, frozen_clock, parsed, plan)
    (repository / "verify.py").write_text("print('revised runner')\n")
    with pytest.raises(PrdRevisionError, match="binding_mismatch"):
        _build(backend, frozen_clock, markdown + "\n", repository)
    (repository / MANIFEST).write_text(_MANIFEST + "\n# Runner revision two.\n")
    updated, revision = _build(backend, frozen_clock, _markdown(_field(repository)), repository)
    assert revision.action == "revised"
    assert updated.tasks[0].verification.profile_binding != parsed.tasks[0].verification.profile_binding
    assert updated.tasks[0].verification.profile != parsed.tasks[0].verification.profile


def test_persisted_contract_replays_without_profile_files(
    backend, state_dir, frozen_clock, repository, monkeypatch,
):
    parsed, plan = _build(backend, frozen_clock, _markdown(_field(repository)), repository)
    _persist(backend, frozen_clock, parsed, plan)
    expected = backend.get_task("T001").verification.model_dump_json()
    (repository / MANIFEST).unlink()
    (repository / "verify.py").unlink()
    monkeypatch.setattr("anvil.verification_profiles.resolve_profile", _forbid)
    events = state_dir / "replayed.jsonl"
    events.touch()
    replay = SqliteBackend(
        db_path=str(state_dir / "replayed.db"), events_path=str(events), clock=frozen_clock,
    )
    try:
        replay.initialize()
        replay.replay_from_empty(str(state_dir / "events.jsonl"))
        assert replay.get_task("T001").verification.model_dump_json() == expected
    finally:
        replay.close()
