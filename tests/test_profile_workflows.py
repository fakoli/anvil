"""Workflow profile selection refuses before ephemeral state creation."""

from pathlib import Path

import pytest

from anvil.state.models import Verification
from anvil.state.snapshot import serialize_state
from anvil.verification_profiles import ProfileError
from anvil.workflows.tasks import WORKFLOW_FEATURE_ID, create_workflow_task
from tests.test_profile_claims import _profile
from tests.test_verification_profiles import reference
from tests.test_workflow_tasks import _setup_approved_prd


@pytest.mark.parametrize("frozen", [False, True])
def test_profile_creation_refuses_before_sentinel_or_task(
    backend, frozen_clock, tmp_path, frozen,
):
    _setup_approved_prd(backend)
    verification = _profile(tmp_path) if frozen else Verification(profile=reference(tmp_path))
    before = serialize_state(backend)
    log_before = Path(backend._events_path).read_bytes()
    with pytest.raises(ProfileError, match="identity_unavailable"):
        create_workflow_task(
            backend, title="Unsupported profile", description="No prepared target",
            actor="runner", clock=frozen_clock, verification=verification,
        )
    assert serialize_state(backend) == before
    assert Path(backend._events_path).read_bytes() == log_before
    assert backend.get_feature(WORKFLOW_FEATURE_ID) is None


def test_legacy_workflow_creation_does_not_read_profiles(backend, frozen_clock, monkeypatch):
    _setup_approved_prd(backend)

    def forbidden(*args, **kwargs):
        pytest.fail("legacy workflow accessed a profile")

    monkeypatch.setattr("anvil.verification_profiles.require_profile_current", forbidden)
    task_id = create_workflow_task(
        backend, title="Legacy task", description="Existing runner contract", actor="runner",
        clock=frozen_clock, verification=Verification(commands=["python -m unittest"]),
    )
    task = backend.get_task(task_id)
    assert task.status.value == "ready"
    assert task.verification.commands == ["python -m unittest"]
    assert backend.get_feature(WORKFLOW_FEATURE_ID) is not None
