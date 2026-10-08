"""Bounded import and prospective accepted-attempt invalidation regressions."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from anvil.cli import app
from anvil.cli.packet_apply import CommandProofImportOverflow, _read_command_proofs
from anvil.naming import task_claim_buffer_path
from anvil.state.backend import EventRejected, TransactionAborted
from anvil.state.models import HookCommandAttribution, hook_command_semantic_digest
from anvil.state.payloads import AcceptedAttemptInvalidation
from anvil.state.snapshot import serialize_state
from test_sqlite import (
    _T0, _make_applied_payload, _make_backend, _make_claim_payload,
    _make_evidence_payload, _make_event, _make_task_payload,
    _setup_claimable_task_and_claim,
)


def _buffer(state_dir: Path, count: int, *, failed: bool = False) -> Path:
    path = task_claim_buffer_path(state_dir / '.evidence-buffer', 'C001')
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    attribution = HookCommandAttribution(
        project_id='proj-1', claim_id='C001', generation=1, claimed_by='agent-alpha',
        task_id='T001', task_revision='a' * 64, prd_id='default', prd_revision=1,
    )
    records = []
    for i in range(count):
        command, exit_code = f'check {i}', 1 if failed and i == 0 else 0
        records.append(json.dumps({
            'claim_id': 'C001', 'attribution': attribution.model_dump(mode='json'),
            'timestamp': _T0.isoformat(), 'command': command, 'exit_code': exit_code,
            'output_sha256': 'b' * 64, 'semantic_digest': hook_command_semantic_digest(
                attribution=attribution, command=command, exit_code=exit_code,
                output_sha256='b' * 64, captured_at=_T0,
            ),
        }))
    path.write_text('\n'.join(records) + '\n')
    return path


def test_bounded_import_eof_and_failed_capture_preservation(tmp_path):
    path = _buffer(tmp_path, 16, failed=True)
    original = path.read_bytes()
    proofs = _read_command_proofs(tmp_path, 'C001')
    assert len(proofs) == 16 and proofs[0].exit_code == 1
    path.write_bytes(original + b'bad legacy record\n')
    assert len(_read_command_proofs(tmp_path, 'C001')) == 16
    path = _buffer(tmp_path, 17, failed=True)
    original = path.read_bytes()
    with pytest.raises(CommandProofImportOverflow, match='record limit'):
        _read_command_proofs(tmp_path, 'C001')
    assert path.read_bytes() == original


def test_bounded_import_byte_cap_and_exact_eof(tmp_path, monkeypatch):
    path = _buffer(tmp_path, 1)
    import anvil.cli.packet_apply as module
    monkeypatch.setattr(module, 'MAX_CLAIM_COMMAND_PROOF_BATCH_BYTES', path.stat().st_size)
    assert len(_read_command_proofs(tmp_path, 'C001')) == 1
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(CommandProofImportOverflow, match='byte limit'):
        _read_command_proofs(tmp_path, 'C001')


@pytest.fixture
def accepted(tmp_path, monkeypatch):
    # Keep every owner registry and test projection isolated from real state.
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    backend = _make_backend(tmp_path)
    _setup_claimable_task_and_claim(backend)
    backend.append(_make_event('evidence.submitted', _make_evidence_payload(),
                               target_kind='task', target_id='T001'))
    backend.append(_make_event('task.applied', _make_applied_payload(),
                               target_kind='task', target_id='T001'))
    yield backend
    backend.close()


def _reference(backend):
    binding = backend.acceptance_invalidation_binding('T001')
    return AcceptedAttemptInvalidation(
        accepted_event_id=binding['accepted_event_id'], binding_digest=binding['binding_digest'],
        decision_id='independent-gap-review-1', reason='Command import overflow; source PASS remains historical.',
        evidence_gap_reference='review/command-gap.json', evidence_gap_sha256='c' * 64,
        evidence_gap_reviewed_by='gap-reviewer', confirmed=True,
    )


def _invalidate(backend, reference):
    return backend.invalidate_task_acceptance(
        task_id='T001', reviewer=reference.evidence_gap_reviewed_by,
        reference=reference, timestamp=_T0 + dt.timedelta(minutes=20),
    )


def test_invalidation_preserves_history_replays_and_retry_cannot_reopen(accepted, tmp_path):
    backend = accepted
    reference = _reference(backend)
    before = (tmp_path / 'events.jsonl').read_bytes()
    original_evidence = backend.get_latest_evidence('T001')
    event = _invalidate(backend, reference)
    assert event is not None and backend.get_task('T001').status.value == 'drafted'
    assert (tmp_path / 'events.jsonl').read_bytes().startswith(before)
    assert backend.get_latest_evidence('T001') == original_evidence
    assert backend.list_reviews()[-1].counts_toward_accept_rate is True
    assert _invalidate(backend, reference) is None
    for old, new in [('drafted', 'reviewed'), ('reviewed', 'ready')]:
        backend.append(_make_event('task.status_changed', {'task_id': 'T001', 'from': old, 'to': new},
                                   target_kind='task', target_id='T001'))
    backend.append(_make_event('claim.created', _make_claim_payload(claim_id='C002', generation=2),
                               target_kind='claim', target_id='C002'))
    backend.append(_make_event('evidence.submitted', _make_evidence_payload(claim_id='C002', evidence_id='EV002'),
                               target_kind='task', target_id='T001'))
    backend.append(_make_event('task.applied', _make_applied_payload(review_attempt_id='EV002'),
                               target_kind='task', target_id='T001'))
    assert _invalidate(backend, reference) is None
    assert backend.get_task('T001').status.value == 'done'
    assert backend.get_claim('C002').generation == 2
    from anvil.state.sqlite import SqliteBackend
    rebuilt = SqliteBackend(db_path=str(tmp_path / 'rebuilt.db'), events_path=str(tmp_path / 'events.jsonl'))
    rebuilt.initialize()
    rebuilt.replay_from_empty(str(tmp_path / 'events.jsonl'))
    try:
        assert serialize_state(rebuilt) == serialize_state(backend)
    finally:
        rebuilt.close()


@pytest.mark.parametrize('field,value', [('accepted_event_id', 'E999999'), ('binding_digest', 'f' * 64),
                                        ('evidence_gap_reviewed_by', 'agent-alpha')])
def test_invalidation_exact_cas_and_actor_refuse_without_append(accepted, tmp_path, field, value):
    reference = _reference(accepted).model_copy(update={field: value})
    before = (tmp_path / 'events.jsonl').read_bytes()
    with pytest.raises(EventRejected):
        _invalidate(accepted, reference)
    assert (tmp_path / 'events.jsonl').read_bytes() == before
    assert accepted.get_task('T001').status.value == 'done'


def test_invalidation_refuses_changed_task_and_ordinary_done_reject(accepted, tmp_path):
    reference = _reference(accepted)
    accepted.append(_make_event('task.created', _make_task_payload(task_id='T002'),
                                target_kind='task', target_id='T002'))
    from test_sqlite import _make_rejected_applied_payload
    with pytest.raises(EventRejected, match='status-drift'):
        accepted.append(_make_event('task.applied', _make_rejected_applied_payload(accepted),
                                    target_kind='task', target_id='T001'))
    scores = accepted.get_task('T001').scores.model_dump(mode='json')
    scores['review_risk'] = 5
    accepted.append(_make_event('task.scored', {'task_id': 'T001', 'scores': scores},
                                target_kind='task', target_id='T001'))
    with pytest.raises(EventRejected, match='binding changed'):
        _invalidate(accepted, reference)


@pytest.mark.parametrize('status', ['claimed', 'in_progress', 'needs_review', 'accepted', 'done'])
def test_invalidation_refuses_transitive_consumer(accepted, status):
    for task_id, dependency in [('T002', 'T001'), ('T003', 'T002')]:
        payload = _make_task_payload(task_id=task_id)
        payload['dependencies'] = [dependency]
        payload['status'] = status if task_id == 'T003' else 'ready'
        accepted.append(_make_event('task.created', payload, target_kind='task', target_id=task_id))
    with pytest.raises(EventRejected, match='dependent consumers.*T003'):
        _invalidate(accepted, _reference(accepted))


def test_invalidation_conflicting_retry_refuses(accepted):
    reference = _reference(accepted)
    _invalidate(accepted, reference)
    with pytest.raises(EventRejected, match='conflicting'):
        _invalidate(accepted, reference.model_copy(update={'decision_id': 'other'}))


def test_cli_preview_and_explicit_invalidation(accepted, tmp_path, monkeypatch):
    import anvil.cli.packet_apply as module
    monkeypatch.setattr(module, '_resolve_state_dir', lambda cwd: tmp_path)
    monkeypatch.setattr(module, '_require_state_dir', lambda *a, **k: None)
    monkeypatch.setattr(module, '_open_backend', lambda state: accepted)
    monkeypatch.setattr(accepted, 'close', lambda: None)
    result = CliRunner().invoke(app, ['apply', 'T001', '--invalidation-preview', '--json'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['data']['claim_id'] == 'C001'
    path = tmp_path / 'invalidation.json'
    path.write_text(_reference(accepted).model_dump_json())
    result = CliRunner().invoke(app, ['apply', 'T001', '--invalidate-accepted', str(path), '--json'])
    assert result.exit_code == 1 and accepted.get_task('T001').status.value == 'done'
    result = CliRunner().invoke(app, ['apply', 'T001', '--invalidate-accepted', str(path),
                                     '--reviewer', 'gap-reviewer', '--json'])
    assert result.exit_code == 0, result.output
    assert accepted.get_task('T001').status.value == 'drafted'
