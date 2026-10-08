"""PRD content events freeze profiles before task rows exist."""
import hashlib
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from anvil.planning.prd_persistence import material_content_sha256
from anvil.state.backend import EventRejected
from anvil.state.hashing import domain_separated_sha256
from anvil.state.models import PRD, EventDraft
from anvil.state.payloads import PrdParsedPayload, PrdRevisedPayload
from anvil.state.sqlite import SqliteBackend
from anvil.verification_profiles import resolve_profile
from tests.test_verification_profiles import reference


@pytest.fixture
def repository(tmp_path):
    root = tmp_path.resolve()
    (root / "tools").mkdir()
    (root / "tools/verify.py").write_text("assert True\n")
    return root


def _binding(root):
    return resolve_profile(root, reference(root)).model_dump(mode="json")


def _source(revision=1):
    text = "# Project: Profiles\n\n## Summary\nProfiles\n" + "\n" * (revision - 1)
    return {
        "source_text": text, "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "source_size_bytes": len(text.encode()), "source_encoding": "utf-8",
        "source_revision": revision, "provenance_state": "available", "content_available": True,
        "material_sha256": material_content_sha256(SimpleNamespace(markdown=text), "Profiles"),
    }


def _append(backend, clock, action, target, payload, kind="prd"):
    return backend.append(EventDraft(
        timestamp=clock.now(), actor="test", action=action, target_kind=kind,
        target_id=target, payload_json=payload,
    ))


@pytest.mark.parametrize("bindings", [None, {}])
def test_legacy_absence_and_explicit_empty_are_distinct(bindings):
    payload = PrdParsedPayload(project_id="project", **_source(), profile_bindings=bindings)
    assert payload.profile_bindings == bindings
    assert "profile_bindings" not in PRD(profile_bindings=bindings).model_dump()
    assert "profile_bindings" not in dict(PRD(profile_bindings=bindings))
    assert PrdParsedPayload(project_id="project").profile_bindings is None


@pytest.mark.parametrize("bad_key", ["", "T001/private", "wrong:T001\n", "T1", "../T001"])
def test_invalid_keys_refuse(bad_key, repository):
    with pytest.raises(ValidationError):
        PrdParsedPayload(project_id="project", **_source(), profile_bindings={bad_key: _binding(repository)})


def test_bounded_strict_bindings(repository):
    binding = _binding(repository)
    assert len(PrdParsedPayload(project_id="project", **_source(), profile_bindings={
        f"T{i:03d}": binding for i in range(128)
    }).profile_bindings) == 128
    large = {key: value for key, value in binding.items() if key != "contract_sha256"}
    large["commands"] = ["a" * 1024, "b" * 1024]
    large["contract_sha256"] = domain_separated_sha256(b"anvil.verification-profile.v1\0", large)
    with pytest.raises(ValidationError, match="byte_limit_exceeded"):
        PrdParsedPayload(project_id="project", **_source(), profile_bindings={
            f"T{i:03d}": large for i in range(128)
        })
    for model, extra in ((PrdParsedPayload, {}), (PrdRevisedPayload, {"revision": 1})):
        with pytest.raises(ValidationError):
            model(project_id="project", **_source(), **extra, profile_bindings={
                "T001": {**binding, "contract_sha256": "0" * 64},
            })
        with pytest.raises(ValidationError):
            model(project_id="project", **_source(), **extra, profile_bindings={
                f"T{i:03d}": binding for i in range(129)
            })
        with pytest.raises(ValidationError):
            model(project_id="project", profile_bindings={"T001": binding}, **extra)


def test_parse_revision_exact_event_and_replay(backend, state_dir, frozen_clock, repository):
    binding = _binding(repository)
    _append(backend, frozen_clock, "project.created", "project", {
        "id": "project", "name": "Profiles", "description": "",
        "created_at": frozen_clock.now().isoformat(), "updated_at": frozen_clock.now().isoformat(),
    }, "project")
    _append(backend, frozen_clock, "prd.parsed", "project", {
        "project_id": "project", "title": "Profiles", "expected_absent": True,
        **_source(), "profile_bindings": {"T001": binding},
    })
    prd = backend.get_prd()
    assert prd.profile_bindings["T001"].model_dump(mode="json") == binding
    assert backend.get_task("T001") is None
    assert "profile_bindings" not in prd.model_dump() and "profile_bindings" not in dict(prd)
    initial_log = (state_dir / "events.jsonl").read_bytes()
    (repository / "tools/verify.py").write_text("assert False\n")
    for bindings, reason in ((None, "must be explicit"), ({"T001": _binding(repository)}, "cannot rebind")):
        with pytest.raises(EventRejected, match=reason):
            _append(backend, frozen_clock, "prd.revised", "default", {
                "project_id": "project", "prd_id": "default", "title": "Profiles",
                "revision": 2, "is_default": True, **_source(2), "profile_bindings": bindings,
            })
        assert (state_dir / "events.jsonl").read_bytes() == initial_log
        assert backend.get_prd().profile_bindings["T001"].model_dump(mode="json") == binding
    _append(backend, frozen_clock, "prd.parsed", "project", {
        "project_id": "project", "prd_id": "other", "is_default": False,
        "title": "Profiles", "expected_absent": True, **_source(),
        "profile_bindings": {"other:T001": binding},
    })
    assert backend.get_prd().profile_bindings["T001"].model_dump(mode="json") == binding
    assert backend.get_prd("other").profile_bindings["other:T001"].model_dump(mode="json") == binding
    _append(backend, frozen_clock, "prd.revised", "default", {
        "project_id": "project", "prd_id": "default", "title": "Profiles",
        "revision": 2, "is_default": True, **_source(2), "profile_bindings": {},
    })
    assert backend.get_prd().profile_bindings == {}
    (repository / "tools/verify.py").unlink()
    replay_path = state_dir / "replay.jsonl"
    replay_path.touch()
    replay = SqliteBackend(db_path=str(state_dir / "replay.db"), events_path=str(replay_path), clock=frozen_clock)
    try:
        replay.initialize()
        replay.replay_from_empty(str(state_dir / "events.jsonl"))
        assert replay.get_prd().profile_bindings == {}
        assert replay.get_prd("other").profile_bindings["other:T001"].model_dump(mode="json") == binding
    finally:
        replay.close()
