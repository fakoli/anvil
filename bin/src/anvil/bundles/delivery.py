"""Delivery checkpoints, reconciliation, and history-preserving supersession."""

from __future__ import annotations

from anvil.clock import Clock
from anvil.state.backend import Backend, BackendError
from anvil.state.models import (
    TERMINAL_BUNDLE_STATUSES,
    BundleCheckpoint,
    BundleStatus,
    EventDraft,
)
from anvil.state.schema import SCHEMA_VERSION


class BundleDeliveryError(Exception):
    """A bundle delivery mutation was refused."""


class BundleDeliveryManager:
    def __init__(self, backend: Backend, clock: Clock, *, actor: str) -> None:
        self._backend = backend
        self._clock = clock
        self._actor = actor

    def checkpoint(
        self,
        bundle_id: str,
        *,
        commit_sha: str | None = None,
        pr_url: str | None = None,
    ) -> BundleCheckpoint:
        bundle = self._backend.get_bundle(bundle_id)
        if bundle is None:
            raise BundleDeliveryError(f"Bundle '{bundle_id}' not found.")
        if self._actor != bundle.coordinator:
            raise BundleDeliveryError("Only the bundle coordinator may checkpoint.")
        now = self._clock.now()
        checkpoint = BundleCheckpoint(
            commit_sha=commit_sha,
            pr_url=pr_url,
            recorded_at=now,
            recorded_by=self._actor,
        )
        try:
            self._backend.append(
                EventDraft(
                    timestamp=now,
                    actor=self._actor,
                    action="bundle.checkpoint_recorded",
                    target_kind="bundle",
                    target_id=bundle_id,
                    payload_json={
                        "bundle_id": bundle_id,
                        "creation_event_id": bundle.creation_event_id,
                        "checkpoint": checkpoint.model_dump(mode="json"),
                    },
                )
            )
        except BackendError as exc:
            raise BundleDeliveryError(str(exc)) from exc
        stored = self._backend.get_bundle(bundle_id)
        if stored is None or stored.checkpoint is None:  # pragma: no cover
            raise BundleDeliveryError("Recorded checkpoint did not project.")
        return stored.checkpoint

    def reconcile(
        self,
        bundle_id: str,
        *,
        commit_sha: str | None = None,
        pr_url: str | None = None,
        merged: bool = False,
    ) -> None:
        self.checkpoint(bundle_id, commit_sha=commit_sha, pr_url=pr_url)
        bundle = self._backend.get_bundle(bundle_id)
        assert bundle is not None  # checkpoint already proved this
        if bundle.status is BundleStatus.reviewed_unintegrated:
            self._transition(bundle_id, BundleStatus.integrated, "delivery reconciled")
            bundle = self._backend.get_bundle(bundle_id)
            assert bundle is not None
        if merged and bundle.status is BundleStatus.integrated:
            self._transition(bundle_id, BundleStatus.merged, "merged delivery reconciled")
        if merged:
            bundle = self._backend.get_bundle(bundle_id)
            assert bundle is not None
            if bundle.status in {BundleStatus.merged, BundleStatus.completed}:
                self._release_legacy_terminal_claim(bundle_id)

    def supersede(self, bundle_id: str, *, replacement_bundle_id: str) -> None:
        bundle = self._backend.get_bundle(bundle_id)
        if bundle is None:
            raise BundleDeliveryError(f"Bundle '{bundle_id}' not found.")
        now = self._clock.now()
        try:
            self._backend.append(
                EventDraft(
                    timestamp=now,
                    actor=self._actor,
                    action="bundle.superseded",
                    target_kind="bundle",
                    target_id=bundle_id,
                    payload_json={
                        "bundle_id": bundle_id,
                        "creation_event_id": bundle.creation_event_id,
                        "replacement_bundle_id": replacement_bundle_id,
                        "superseded_by_actor": self._actor,
                        "superseded_at": now.isoformat(),
                    },
                )
            )
        except BackendError as exc:
            raise BundleDeliveryError(str(exc)) from exc

    def _release_legacy_terminal_claim(self, bundle_id: str) -> None:
        """Repair a pre-fix terminal-plus-active-claim projection once.

        Fresh terminal transitions release their claim inside the
        ``bundle.status_changed`` projector.  This separate event is only for
        databases that already projected the v0.5.0 inconsistent state before
        that atomic behavior existed.
        """
        claim = self._backend.get_bundle_claim(bundle_id)
        if claim is None or claim.status.value != "active":
            return
        now = self._clock.now()
        try:
            self._backend.append(
                EventDraft(
                    timestamp=now,
                    actor=self._actor,
                    action="bundle.claim_released",
                    target_kind="bundle",
                    target_id=bundle_id,
                    payload_json={
                        "bundle_claim_id": claim.id,
                        "bundle_id": bundle_id,
                        "released_by": self._actor,
                        "release_reason": "terminal reconciliation repair",
                        "force": False,
                    },
                )
            )
        except BackendError as exc:
            raise BundleDeliveryError(str(exc)) from exc

    def _transition(
        self, bundle_id: str, target: BundleStatus, reason: str
    ) -> None:
        bundle = self._backend.get_bundle(bundle_id)
        assert bundle is not None
        claim = self._backend.get_bundle_claim(bundle_id)
        now = self._clock.now()
        try:
            self._backend.append(
                EventDraft(
                    timestamp=now,
                    actor=self._actor,
                    action="bundle.status_changed",
                    target_kind="bundle",
                    target_id=bundle_id,
                    payload_json={
                        "bundle_id": bundle_id,
                        "schema_version": SCHEMA_VERSION,
                        "creation_event_id": bundle.creation_event_id,
                        "bundle_claim_id": (
                            claim.id
                            if claim is not None and claim.status.value == "active"
                            else None
                        ),
                        "release_claim": target in TERMINAL_BUNDLE_STATUSES,
                        "from": bundle.status.value,
                        "to": target.value,
                        "changed_at": now.isoformat(),
                        "reason": reason,
                    },
                )
            )
        except BackendError as exc:
            raise BundleDeliveryError(str(exc)) from exc
