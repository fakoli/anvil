"""Bounded, independently authored adversarial review flow for bundles."""

from __future__ import annotations

import uuid

from anvil.clock import Clock
from anvil.review.gates import BundleReviewGate, evaluate_bundle_reviews
from anvil.state.backend import Backend, BackendError
from anvil.state.models import BundleStatus, EventDraft, ReviewDecision


class BundleReviewError(Exception):
    """A bundle review mutation or gate transition was refused."""


class BundleReviewManager:
    """Record one-reviewer verdicts and apply the coordinator-owned gate."""

    def __init__(self, backend: Backend, clock: Clock, *, actor: str) -> None:
        self._backend = backend
        self._clock = clock
        self._actor = actor

    def gate(self, bundle_id: str) -> BundleReviewGate:
        bundle = self._backend.get_bundle(bundle_id)
        if bundle is None:
            raise BundleReviewError(f"Bundle '{bundle_id}' not found.")
        return evaluate_bundle_reviews(
            bundle.review_policy,
            self._backend.list_bundle_reviews(bundle_id),
            coordinator=bundle.coordinator,
        )

    def record(
        self,
        bundle_id: str,
        *,
        review_round: int,
        angle: str,
        decision: ReviewDecision,
        notes: str | None = None,
    ) -> BundleReviewGate:
        bundle = self._backend.get_bundle(bundle_id)
        if bundle is None:
            raise BundleReviewError(f"Bundle '{bundle_id}' not found.")
        now = self._clock.now()
        try:
            self._backend.append(
                EventDraft(
                    timestamp=now,
                    actor=self._actor,
                    action="bundle.review_recorded",
                    target_kind="bundle",
                    target_id=bundle_id,
                    payload_json={
                        "id": f"BR{uuid.uuid4().hex[:8].upper()}",
                        "bundle_id": bundle_id,
                        "creation_event_id": bundle.creation_event_id,
                        "review_round": review_round,
                        "angle": angle,
                        "reviewed_by": self._actor,
                        "decision": decision.value,
                        "notes": notes,
                        "created_at": now.isoformat(),
                    },
                )
            )
        except BackendError as exc:
            raise BundleReviewError(str(exc)) from exc
        return self.gate(bundle_id)

    def finalize(self, bundle_id: str) -> BundleReviewGate:
        bundle = self._backend.get_bundle(bundle_id)
        if bundle is None:
            raise BundleReviewError(f"Bundle '{bundle_id}' not found.")
        if self._actor != bundle.coordinator:
            raise BundleReviewError("Only the bundle coordinator may apply the gate.")
        claim = self._backend.get_bundle_claim(bundle_id)
        now = self._clock.now()
        if (
            claim is None
            or claim.status.value != "active"
            or claim.lease_expires_at < now
        ):
            raise BundleReviewError(
                "An active coordinator claim is required to apply the review gate."
            )
        gate = self.gate(bundle_id)
        if gate.passed:
            target = BundleStatus.reviewed_unintegrated
            reason = "independent adversarial review quorum passed"
        elif gate.replan_required:
            target = BundleStatus.replan_required
            reason = "blocking review findings exhausted the re-review budget"
        else:
            raise BundleReviewError(
                "Bundle review gate remains incomplete: "
                f"missing_angles={gate.missing_angles}, "
                f"missing_reviewers={gate.missing_reviewers}, "
                f"blocking_findings={gate.blocking_findings}."
            )
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
                        "creation_event_id": bundle.creation_event_id,
                        "bundle_claim_id": claim.id,
                        "from": bundle.status.value,
                        "to": target.value,
                        "changed_at": now.isoformat(),
                        "reason": reason,
                    },
                )
            )
        except BackendError as exc:
            raise BundleReviewError(str(exc)) from exc
        return gate
