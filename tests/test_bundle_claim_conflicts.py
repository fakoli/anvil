"""Bundle claim conflict selection preserves native admission and replay outcomes."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from anvil.state.backend import EventRejected
from anvil.state.models import Event
from anvil.state.snapshot import serialize_state
from tests.test_bundle_state import (
    _backend,
    _bundle_claim_payload,
    _claim_payload,
    _create_bundle,
    _event,
    _seed,
)


@pytest.mark.parametrize(
    "incoming_files,incoming_groups,active_files,active_groups,conflicts",
    [
        ([], [], [], [], False),
        ([], [], ["src/owned.py"], ["owned"], False),
        (["src/shared.py"], [], ["src/shared.py"], [], True),
        ([], ["shared"], [], ["shared"], True),
        (["src/one.py"], ["shared"], ["src/two.py"], ["shared"], True),
        (["src/shared.py"], ["one"], ["src/shared.py"], ["two"], True),
        (["src/one.py"], ["one"], ["src/two.py"], ["two"], False),
        (["./src/Shared.py"], [], ["src/shared.py"], [], True),
    ],
    ids=["empty", "empty-with-scoped-neighbor", "files", "groups", "mixed-group", "mixed-file", "distinct", "canonical-alias"],
)
@pytest.mark.parametrize("external_prd,external_actor", [("release", "worker"), ("other", "codex-main")])
@pytest.mark.parametrize("release_neighbor", [False, True])
def test_bundle_conflict_selection_preserves_live_and_replay(
    tmp_path: Path,
    incoming_files,
    incoming_groups,
    active_files,
    active_groups,
    conflicts,
    external_prd,
    external_actor,
    release_neighbor,
):
    backend = _backend(tmp_path)
    try:
        _seed(backend, second_prd=True)
        template = backend.get_task("release:T001").model_dump(mode="json")
        target_task = "release:T003"
        external_task = f"{external_prd}:T004"
        for task_id, files, groups in (
            (target_task, incoming_files, incoming_groups),
            (external_task, ["src/current-task-file.py"], active_groups),
        ):
            prd_id = task_id.split(":", 1)[0]
            backend.append(_event(
                "task.created",
                {**template, "id": task_id, "title": task_id, "prd_id": prd_id,
                 "feature_id": f"{prd_id}:F001", "likely_files": files, "conflict_groups": groups},
                target_kind="task", target_id=task_id,
            ))
        _create_bundle(backend, task_ids=[target_task])
        active_claim = {
            **_claim_payload("C-OTHER", external_task),
            "expected_files": active_files,
            "claimed_by": external_actor,
        }
        backend.append(_event(
            "claim.created", active_claim, target_kind="claim", target_id="C-OTHER",
            actor=external_actor,
        ))
        if release_neighbor:
            backend.append(_event(
                "claim.released", {
                    "claim_id": "C-OTHER", "released_by": external_actor,
                    "release_reason": "Neighbor finished its reservation",
                }, target_kind="claim", target_id="C-OTHER", actor=external_actor,
            ))
        payload = {**_bundle_claim_payload(backend), "expected_files": incoming_files}
        draft = _event("bundle.claimed", payload, target_kind="bundle", target_id="B001", actor="codex-main")
        events = tmp_path / "events.jsonl"
        before = events.read_bytes()
        if conflicts and not release_neighbor:
            with pytest.raises(EventRejected, match="conflicts with active claims"):
                backend.append(draft)
            assert events.read_bytes() == before
            assert backend.get_bundle_claim("B001") is None
            assert backend.get_task(target_task).status.value == "ready"
            # A forged losing history must also be refused by the reducer.
            event = Event(id=f"E{len(before.splitlines()) + 1:06d}", **draft.model_dump())
            with events.open("a") as handle:
                handle.write(event.model_dump_json() + "\n")
            backend.replay_from_empty(str(events))
            assert backend.get_bundle_claim("B001") is None
            assert backend.get_task(target_task).status.value == "ready"
            assert backend.get_claim("BC001-1") is None
        else:
            backend.append(draft)
            snapshot = serialize_state(backend)
            backend.replay_from_empty(str(events))
            assert serialize_state(backend) == snapshot
            assert backend.get_bundle_claim("B001") is not None
        assert backend.get_claim("C-OTHER") is not None
        assert backend.get_claim("C-OTHER").status.value == (
            "released" if release_neighbor else "active"
        )
        assert json.loads(events.read_text().splitlines()[0])["action"] == "project.created"
    finally:
        backend.close()


@pytest.mark.parametrize("first_bundle", ["B001", "B002"])
@pytest.mark.parametrize("files,groups", [([], []), (["src/shared.py"], []), ([], ["shared"])])
def test_competing_bundle_histories_keep_first_conflicting_claim(
    tmp_path, first_bundle, files, groups,
):
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        for task in backend.list_tasks():
            backend.append(_event(
                "task.created", {
                    **task.model_dump(mode="json"), "prd_id": task.prd_id,
                    "likely_files": files, "conflict_groups": groups,
                }, target_kind="task", target_id=task.id,
            ))
        _create_bundle(backend, "B001", ["release:T001"])
        _create_bundle(backend, "B002", ["release:T002"])
        drafts = {}
        for bundle_id, task_id in (("B001", "release:T001"), ("B002", "release:T002")):
            payload = {
                **_bundle_claim_payload(backend), "id": f"claim-{bundle_id}",
                "bundle_id": bundle_id,
                "creation_event_id": backend.get_bundle(bundle_id).creation_event_id,
                "expected_files": files,
                "member_claims": [{"id": f"member-{bundle_id}", "task_id": task_id}],
            }
            drafts[bundle_id] = _event(
                "bundle.claimed", payload, target_kind="bundle", target_id=bundle_id,
                actor="codex-main",
            )
        second_bundle = "B002" if first_bundle == "B001" else "B001"
        backend.append(drafts[first_bundle])
        events = tmp_path / "events.jsonl"
        before = events.read_bytes()
        if files or groups:
            with pytest.raises(EventRejected, match="conflicts with active claims"):
                backend.append(drafts[second_bundle])
            assert events.read_bytes() == before
            loser = Event(
                id=f"E{len(before.splitlines()) + 1:06d}",
                **drafts[second_bundle].model_dump(),
            )
            with events.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(loser.model_dump_json() + "\n")
        else:
            backend.append(drafts[second_bundle])
        snapshot = serialize_state(backend)
        backend.replay_from_empty(str(events))
        assert backend.get_bundle_claim(first_bundle) is not None
        if files or groups:
            assert backend.get_bundle_claim(second_bundle) is None
            assert backend.get_claim(f"member-{second_bundle}") is None
            assert backend.get_bundle(second_bundle).status.value == "planned"
        else:
            assert backend.get_bundle_claim(second_bundle) is not None
            assert serialize_state(backend) == snapshot
    finally:
        backend.close()
