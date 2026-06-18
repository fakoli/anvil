"""Tests for anvil.sync — Phase 8 Wave 1 SyncProvider abstraction.

Coverage:
- ``ExternalRef``, ``ExternalTask``, ``ProviderHealth`` Pydantic models —
  validation, serialization, ``extra="forbid"`` enforcement, UTC discipline.
- ``SyncProvider`` Protocol — structural compliance of both
  ``RecordedSyncProvider`` and a tiny in-test concrete impl.
- :mod:`anvil.sync.registry` — register / get / list, duplicate
  registration raises, unknown lookup raises.
- ``RecordedSyncProvider`` — hit returns canned value, miss raises with key
  prefix, replay produces identical output, key is stable across calls.
- ``RecordedSyncProvider.record_key`` is SHA256 (not Python's salted
  ``hash``), independent of kwarg ordering at the call site, distinct
  across method names, and stable across processes.

Pattern mirrors ``tests/test_llm.py`` (Phase 7); no live network, no real
provider, no SDKs — every test runs in-process.
"""

from __future__ import annotations

import datetime
import subprocess
import sys

import pytest
from pydantic import ValidationError

from anvil.state.models import Task, TaskPriority, TaskStatus
from anvil.sync import (
    PROVIDER_REGISTRY,
    ExternalRef,
    ExternalTask,
    ProviderHealth,
    RecordedSyncProvider,
    SyncProvider,
    SyncProviderError,
    get_sync_provider,
    list_sync_providers,
    register_sync_provider,
)
from anvil.sync.errors import (
    AuthenticationFailed,
    ProviderUnavailable,
    RateLimitExceeded,
    SyncConflict,
)

UTC = datetime.UTC


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime.datetime:
    return datetime.datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)


def _make_task(task_id: str = "T001") -> Task:
    return Task(
        id=task_id,
        feature_id="F001",
        title="Sample task",
        description="Task body",
        status=TaskStatus.proposed,
        priority=TaskPriority.medium,
        created_at=_now(),
        updated_at=_now(),
    )


def _make_external_task(external_id: str = "42") -> ExternalTask:
    return ExternalTask(
        external_id=external_id,
        title="Remote title",
        body="Remote body",
        status_label="open",
        url=f"https://github.com/example/repo/issues/{external_id}",
        last_modified=_now(),
        provider_metadata={
            "labels": ["bug", "status:in-progress"],
            "assignees": ["octocat"],
        },
    )


# ---------------------------------------------------------------------------
# Registry isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Snapshot + restore PROVIDER_REGISTRY around every test.

    The registry is module-global; without isolation, tests that register
    providers would bleed into each other (and into other test files
    once Task 4 lands GitHubIssuesProvider with its own self-registration).
    """
    snapshot = dict(PROVIDER_REGISTRY)
    PROVIDER_REGISTRY.clear()
    yield
    PROVIDER_REGISTRY.clear()
    PROVIDER_REGISTRY.update(snapshot)


# ---------------------------------------------------------------------------
# ExternalRef — Pydantic model
# ---------------------------------------------------------------------------


class TestExternalRef:
    def test_constructs_with_all_fields(self) -> None:
        ref = ExternalRef(
            provider_id="github-issues",
            external_id="42",
            url="https://github.com/example/repo/issues/42",
        )
        assert ref.provider_id == "github-issues"
        assert ref.external_id == "42"
        assert ref.url == "https://github.com/example/repo/issues/42"

    def test_url_defaults_to_none(self) -> None:
        ref = ExternalRef(provider_id="recorded", external_id="X")
        assert ref.url is None

    def test_rejects_empty_provider_id(self) -> None:
        with pytest.raises(ValidationError):
            ExternalRef(provider_id="", external_id="42")

    def test_rejects_empty_external_id(self) -> None:
        with pytest.raises(ValidationError):
            ExternalRef(provider_id="github-issues", external_id="")

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            ExternalRef(
                provider_id="github-issues",
                external_id="42",
                surprise="rejected",  # type: ignore[call-arg]
            )

    def test_round_trips_via_model_dump(self) -> None:
        ref = ExternalRef(
            provider_id="github-issues",
            external_id="42",
            url="https://example.com",
        )
        rebuilt = ExternalRef(**ref.model_dump())
        assert rebuilt == ref


# ---------------------------------------------------------------------------
# ExternalTask — Pydantic model
# ---------------------------------------------------------------------------


class TestExternalTask:
    def test_constructs_with_all_fields(self) -> None:
        et = _make_external_task()
        assert et.external_id == "42"
        assert et.title == "Remote title"
        assert et.body == "Remote body"
        assert et.status_label == "open"
        assert et.provider_metadata == {
            "labels": ["bug", "status:in-progress"],
            "assignees": ["octocat"],
        }
        assert et.url == "https://github.com/example/repo/issues/42"
        assert et.last_modified == _now()

    def test_optional_fields_default(self) -> None:
        et = ExternalTask(
            external_id="X",
            title="",
            body="",
            last_modified=_now(),
        )
        assert et.status_label is None
        assert et.provider_metadata == {}
        assert et.url is None

    def test_labels_assignees_rejected_as_top_level_fields(self) -> None:
        """Provider-specific fields must go in provider_metadata, not on the model itself."""
        with pytest.raises(ValidationError):
            ExternalTask(
                external_id="X",
                title="t",
                body="b",
                last_modified=_now(),
                labels=["bug"],  # type: ignore[call-arg]
            )
        with pytest.raises(ValidationError):
            ExternalTask(
                external_id="X",
                title="t",
                body="b",
                last_modified=_now(),
                assignees=["octocat"],  # type: ignore[call-arg]
            )

    def test_rejects_naive_last_modified(self) -> None:
        with pytest.raises(ValidationError, match="must be timezone-aware"):
            ExternalTask(
                external_id="X",
                title="t",
                body="b",
                last_modified=datetime.datetime(2026, 5, 25, 12, 0, 0),
            )

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            ExternalTask(
                external_id="X",
                title="t",
                body="b",
                last_modified=_now(),
                surprise="rejected",  # type: ignore[call-arg]
            )

    def test_rejects_empty_external_id(self) -> None:
        with pytest.raises(ValidationError):
            ExternalTask(
                external_id="",
                title="t",
                body="b",
                last_modified=_now(),
            )

    def test_round_trips_via_model_dump(self) -> None:
        et = _make_external_task()
        rebuilt = ExternalTask(**et.model_dump())
        assert rebuilt == et


# ---------------------------------------------------------------------------
# ProviderHealth — Pydantic model
# ---------------------------------------------------------------------------


class TestProviderHealth:
    def test_constructs_healthy(self) -> None:
        h = ProviderHealth(
            available=True,
            auth_configured=True,
            last_check_at=_now(),
            error=None,
        )
        assert h.available is True
        assert h.auth_configured is True
        assert h.error is None

    def test_constructs_unhealthy(self) -> None:
        h = ProviderHealth(
            available=False,
            auth_configured=False,
            last_check_at=_now(),
            error="DNS lookup failed",
        )
        assert h.available is False
        assert h.error == "DNS lookup failed"

    def test_error_defaults_to_none(self) -> None:
        h = ProviderHealth(
            available=True,
            auth_configured=True,
            last_check_at=_now(),
        )
        assert h.error is None

    def test_rejects_naive_last_check_at(self) -> None:
        with pytest.raises(ValidationError, match="must be timezone-aware"):
            ProviderHealth(
                available=True,
                auth_configured=True,
                last_check_at=datetime.datetime(2026, 5, 25, 12, 0, 0),
            )

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            ProviderHealth(
                available=True,
                auth_configured=True,
                last_check_at=_now(),
                surprise="rejected",  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# SyncProvider Protocol — structural typing
# ---------------------------------------------------------------------------


class _ToyProvider:
    """In-test concrete provider that satisfies the SyncProvider Protocol.

    Exists so we can confirm Protocol compliance without dragging in the
    Task 4 GitHubIssuesProvider (which doesn't exist yet at this wave).
    """

    provider_id = "toy"
    display_name = "Toy Provider"

    def push_task(self, *, task: Task, mapping: ExternalRef | None) -> ExternalRef:
        return ExternalRef(provider_id=self.provider_id, external_id=task.id)

    def fetch_task(self, *, external_id: str) -> ExternalTask | None:
        return None

    def list_tasks(self) -> list[ExternalTask]:
        return []

    def delete_task(self, *, external_id: str) -> None:
        return None

    def health_check(self) -> ProviderHealth:
        return ProviderHealth(
            available=True, auth_configured=True, last_check_at=_now()
        )


class TestSyncProviderProtocol:
    def test_recorded_provider_satisfies_protocol(self) -> None:
        rec = RecordedSyncProvider()
        # Attribute presence — Protocol is not @runtime_checkable.
        assert hasattr(rec, "provider_id")
        assert hasattr(rec, "display_name")
        assert callable(rec.push_task)
        assert callable(rec.fetch_task)
        assert callable(rec.list_tasks)
        assert callable(rec.delete_task)
        assert callable(rec.health_check)
        # Static-type contract: assignable to a SyncProvider-typed variable.
        provider: SyncProvider = rec
        assert provider is rec

    def test_toy_provider_satisfies_protocol(self) -> None:
        toy = _ToyProvider()
        provider: SyncProvider = toy
        assert provider is toy
        assert provider.provider_id == "toy"


# ---------------------------------------------------------------------------
# Registry — register / get / list
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_register_then_get(self) -> None:
        register_sync_provider("toy", _ToyProvider)
        assert get_sync_provider("toy") is _ToyProvider

    def test_register_appears_in_list(self) -> None:
        register_sync_provider("toy", _ToyProvider)
        assert "toy" in list_sync_providers()

    def test_list_is_sorted(self) -> None:
        register_sync_provider("zeta", _ToyProvider)
        register_sync_provider("alpha", _ToyProvider)
        register_sync_provider("mu", _ToyProvider)
        assert list_sync_providers() == ["alpha", "mu", "zeta"]

    def test_list_empty_when_no_registration(self) -> None:
        assert list_sync_providers() == []

    def test_register_duplicate_raises(self) -> None:
        register_sync_provider("toy", _ToyProvider)
        with pytest.raises(ValueError, match="already registered"):
            register_sync_provider("toy", _ToyProvider)

    def test_register_empty_id_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            register_sync_provider("", _ToyProvider)

    def test_get_unknown_raises(self) -> None:
        with pytest.raises(KeyError, match="no sync provider registered"):
            get_sync_provider("nonexistent")

    def test_get_unknown_error_lists_available(self) -> None:
        register_sync_provider("toy", _ToyProvider)
        with pytest.raises(KeyError, match="toy"):
            get_sync_provider("nope")

    def test_get_unknown_with_empty_registry_says_none(self) -> None:
        with pytest.raises(KeyError, match=r"\(none\)"):
            get_sync_provider("nope")

    def test_registry_dict_directly_accessible(self) -> None:
        """PROVIDER_REGISTRY is exposed for introspection."""
        register_sync_provider("toy", _ToyProvider)
        assert PROVIDER_REGISTRY["toy"] is _ToyProvider


# ---------------------------------------------------------------------------
# RecordedSyncProvider — hit / miss / replay
# ---------------------------------------------------------------------------


class TestRecordedSyncProviderHits:
    def test_fetch_task_returns_canned(self) -> None:
        canned = _make_external_task("42")
        key = RecordedSyncProvider.record_key("fetch_task", external_id="42")
        prov = RecordedSyncProvider(recordings={key: canned})

        got = prov.fetch_task(external_id="42")
        assert got == canned

    def test_fetch_task_canned_none(self) -> None:
        """None is a legitimate canned response (deletion tombstone)."""
        key = RecordedSyncProvider.record_key("fetch_task", external_id="ghost")
        prov = RecordedSyncProvider(recordings={key: None})

        assert prov.fetch_task(external_id="ghost") is None

    def test_list_tasks_returns_canned(self) -> None:
        canned = [_make_external_task("1"), _make_external_task("2")]
        key = RecordedSyncProvider.record_key("list_tasks")
        prov = RecordedSyncProvider(recordings={key: canned})

        assert prov.list_tasks() == canned

    def test_push_task_returns_canned_ref(self) -> None:
        task = _make_task("T001")
        canned_ref = ExternalRef(provider_id="recorded", external_id="42")
        key = RecordedSyncProvider.record_key("push_task", task=task, mapping=None)
        prov = RecordedSyncProvider(recordings={key: canned_ref})

        got = prov.push_task(task=task, mapping=None)
        assert got == canned_ref

    def test_push_task_distinguishes_create_vs_update(self) -> None:
        """A new push (mapping=None) and an update (mapping=ref) hash to different keys."""
        task = _make_task("T001")
        existing = ExternalRef(provider_id="recorded", external_id="42")
        new_ref = ExternalRef(provider_id="recorded", external_id="42")
        updated_ref = ExternalRef(
            provider_id="recorded", external_id="42", url="https://example.com/42"
        )

        create_key = RecordedSyncProvider.record_key(
            "push_task", task=task, mapping=None
        )
        update_key = RecordedSyncProvider.record_key(
            "push_task", task=task, mapping=existing
        )
        assert create_key != update_key

        prov = RecordedSyncProvider(
            recordings={create_key: new_ref, update_key: updated_ref}
        )
        assert prov.push_task(task=task, mapping=None) == new_ref
        assert prov.push_task(task=task, mapping=existing) == updated_ref

    def test_delete_task_returns_none(self) -> None:
        key = RecordedSyncProvider.record_key("delete_task", external_id="42")
        prov = RecordedSyncProvider(recordings={key: None})

        assert prov.delete_task(external_id="42") is None

    def test_health_check_returns_canned(self) -> None:
        canned = ProviderHealth(
            available=True, auth_configured=True, last_check_at=_now()
        )
        key = RecordedSyncProvider.record_key("health_check")
        prov = RecordedSyncProvider(recordings={key: canned})

        assert prov.health_check() == canned

    def test_constructor_copies_recordings(self) -> None:
        """Mutating the source dict after construction must not leak in."""
        key = RecordedSyncProvider.record_key("fetch_task", external_id="X")
        canned = _make_external_task("X")
        src = {key: canned}

        prov = RecordedSyncProvider(recordings=src)
        src.clear()

        assert prov.fetch_task(external_id="X") == canned

    def test_provider_id_and_display_name_overridable(self) -> None:
        prov = RecordedSyncProvider(
            provider_id="github-issues",
            display_name="GitHub Issues",
        )
        assert prov.provider_id == "github-issues"
        assert prov.display_name == "GitHub Issues"

    def test_replay_is_idempotent(self) -> None:
        """Calling the same recorded method twice returns the same value."""
        canned = _make_external_task("42")
        key = RecordedSyncProvider.record_key("fetch_task", external_id="42")
        prov = RecordedSyncProvider(recordings={key: canned})

        first = prov.fetch_task(external_id="42")
        second = prov.fetch_task(external_id="42")
        third = prov.fetch_task(external_id="42")
        assert first == second == third == canned


class TestRecordedSyncProviderMisses:
    def test_miss_raises_sync_provider_error(self) -> None:
        prov = RecordedSyncProvider()
        with pytest.raises(SyncProviderError, match="no recording for fetch_task"):
            prov.fetch_task(external_id="anything")

    def test_miss_error_includes_key_prefix(self) -> None:
        prov = RecordedSyncProvider()
        expected_prefix = RecordedSyncProvider.record_key(
            "fetch_task", external_id="X"
        )[:8]
        with pytest.raises(SyncProviderError) as exc_info:
            prov.fetch_task(external_id="X")
        assert expected_prefix in str(exc_info.value)

    def test_miss_on_list_tasks(self) -> None:
        prov = RecordedSyncProvider()
        with pytest.raises(SyncProviderError, match="no recording for list_tasks"):
            prov.list_tasks()

    def test_miss_on_health_check(self) -> None:
        prov = RecordedSyncProvider()
        with pytest.raises(SyncProviderError, match="no recording for health_check"):
            prov.health_check()

    def test_miss_on_push_task(self) -> None:
        prov = RecordedSyncProvider()
        with pytest.raises(SyncProviderError, match="no recording for push_task"):
            prov.push_task(task=_make_task(), mapping=None)

    def test_miss_on_delete_task(self) -> None:
        prov = RecordedSyncProvider()
        with pytest.raises(SyncProviderError, match="no recording for delete_task"):
            prov.delete_task(external_id="42")

    def test_wrong_type_recording_raises(self) -> None:
        """A recording of the wrong type for the method raises SyncProviderError."""
        key = RecordedSyncProvider.record_key("fetch_task", external_id="X")
        prov = RecordedSyncProvider(recordings={key: "not an ExternalTask"})
        with pytest.raises(SyncProviderError, match="must be an ExternalTask"):
            prov.fetch_task(external_id="X")


# ---------------------------------------------------------------------------
# RecordedSyncProvider.record_key — stability & determinism
# ---------------------------------------------------------------------------


class TestRecordKey:
    def test_same_inputs_same_key(self) -> None:
        k1 = RecordedSyncProvider.record_key("fetch_task", external_id="42")
        k2 = RecordedSyncProvider.record_key("fetch_task", external_id="42")
        assert k1 == k2

    def test_different_method_different_key(self) -> None:
        k1 = RecordedSyncProvider.record_key("fetch_task", external_id="42")
        k2 = RecordedSyncProvider.record_key("delete_task", external_id="42")
        assert k1 != k2

    def test_different_args_different_key(self) -> None:
        k1 = RecordedSyncProvider.record_key("fetch_task", external_id="42")
        k2 = RecordedSyncProvider.record_key("fetch_task", external_id="43")
        assert k1 != k2

    def test_kwarg_order_does_not_matter(self) -> None:
        """sort_keys=True means call-site kwarg order is irrelevant."""
        task = _make_task("T001")
        ref = ExternalRef(provider_id="r", external_id="42")
        k1 = RecordedSyncProvider.record_key("push_task", task=task, mapping=ref)
        k2 = RecordedSyncProvider.record_key("push_task", mapping=ref, task=task)
        assert k1 == k2

    def test_key_is_hex_sha256(self) -> None:
        k = RecordedSyncProvider.record_key("list_tasks")
        assert len(k) == 64
        assert all(c in "0123456789abcdef" for c in k)

    def test_no_args_method_has_stable_key(self) -> None:
        k1 = RecordedSyncProvider.record_key("list_tasks")
        k2 = RecordedSyncProvider.record_key("list_tasks")
        assert k1 == k2
        assert len(k1) == 64

    def test_pydantic_model_args_serialize(self) -> None:
        """Task / ExternalRef args round-trip through json via model_dump."""
        task = _make_task("T001")
        # Should not raise: pydantic models are handled by _json_default.
        k = RecordedSyncProvider.record_key("push_task", task=task, mapping=None)
        assert len(k) == 64

    def test_none_arg_serialises(self) -> None:
        k = RecordedSyncProvider.record_key("push_task", task=_make_task(), mapping=None)
        assert len(k) == 64

    def test_unsupported_arg_type_raises(self) -> None:
        """A non-JSON-serialisable, non-pydantic, non-datetime arg fails loudly."""

        class _Opaque:
            pass

        with pytest.raises(TypeError, match="unsupported argument type"):
            RecordedSyncProvider.record_key("fetch_task", external_id=_Opaque())

    def test_key_stable_across_processes(self) -> None:
        """SHA256, not Python's salted hash().

        Spawn a fresh Python interpreter (PYTHONHASHSEED is randomised by
        default on every run) and recompute the key for a fixed input. If
        we were using Python's built-in ``hash()``, the digests would
        differ across runs — they must NOT, because tests pre-compute
        fixture keys.
        """
        # Use the in-process key as the expected value.
        expected = RecordedSyncProvider.record_key(
            "fetch_task", external_id="stability-probe"
        )

        # Reuse the same Python interpreter, but spawn a child with a
        # different PYTHONHASHSEED to confirm independence from Python's
        # built-in salted hash().
        code = (
            "from anvil.sync import RecordedSyncProvider; "
            "print(RecordedSyncProvider.record_key("
            "'fetch_task', external_id='stability-probe'))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
            env={"PYTHONHASHSEED": "12345"},
        )
        child_key = result.stdout.strip()
        assert child_key == expected


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class TestErrorHierarchy:
    def test_subclasses_inherit_from_base(self) -> None:
        """Every error type catches as SyncProviderError."""
        assert issubclass(SyncConflict, SyncProviderError)
        assert issubclass(ProviderUnavailable, SyncProviderError)
        assert issubclass(AuthenticationFailed, SyncProviderError)
        assert issubclass(RateLimitExceeded, SyncProviderError)

    def test_chained_exception_preserves_cause(self) -> None:
        """raise X from exc must round-trip the cause."""
        original = RuntimeError("upstream boom")
        try:
            try:
                raise original
            except RuntimeError as exc:
                raise SyncProviderError("wrapped") from exc
        except SyncProviderError as wrapped:
            assert wrapped.__cause__ is original
