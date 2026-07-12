"""Atomic coordinator claim, progress, and completion services for bundles."""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from anvil.clock import Clock
from anvil.context.packets import (
    BundleMemberPacketContext,
    BundleWorkPacket,
    render_bundle_packet,
)
from anvil.naming import session_discriminator
from anvil.review.gates import evaluate_claims
from anvil.state.backend import Backend, BackendError
from anvil.state.models import (
    BundleClaim,
    BundleStatus,
    EventDraft,
    ExecutionBundle,
    TaskStatus,
)


class BundleError(Exception):
    """A coordinator bundle operation failed its gate."""


@dataclass(frozen=True)
class BundleClaimResult:
    bundle: ExecutionBundle
    claim: BundleClaim


@dataclass(frozen=True)
class BundleReadiness:
    bundle_id: str
    can_mark_implemented: bool
    unproven_members: dict[str, list[str]] = field(default_factory=dict)


class BundleManager:
    """Orchestrate one public coordinator claim over ordered member tasks."""

    def __init__(
        self,
        backend: Backend,
        clock: Clock,
        *,
        actor: str,
        project_root: Path,
        lease_minutes: float = 240,
    ) -> None:
        self._backend = backend
        self._clock = clock
        self._actor = actor
        self._project_root = project_root
        self._lease_minutes = lease_minutes

    @staticmethod
    def _id(prefix: str) -> str:
        return f"{prefix}{uuid.uuid4().hex[:8].upper()}"

    def claim(
        self,
        bundle_id: str,
        *,
        branch: str | None = None,
        worktree_path: str | None = None,
    ) -> BundleClaimResult:
        bundle = self._backend.get_bundle(bundle_id)
        if bundle is None:
            raise BundleError(f"Bundle '{bundle_id}' not found.")
        if bundle.coordinator != self._actor:
            raise BundleError(
                f"Bundle '{bundle_id}' coordinator is '{bundle.coordinator}', "
                f"not '{self._actor}'."
            )
        tasks = []
        for task_id in bundle.task_ids:
            task = self._backend.get_task(task_id)
            if task is None:
                raise BundleError(f"Bundle member task '{task_id}' not found.")
            tasks.append(task)
        expected_files: list[str] = []
        for task in tasks:
            for path in task.likely_files:
                if path not in expected_files:
                    expected_files.append(path)
        now = self._clock.now()
        claim_id = self._id("BC")
        member_claims = [
            {"id": self._id("C"), "task_id": task.id} for task in tasks
        ]
        draft = EventDraft(
            timestamp=now,
            actor=self._actor,
            action="bundle.claimed",
            target_kind="bundle",
            target_id=bundle_id,
            payload_json={
                "id": claim_id,
                "bundle_id": bundle_id,
                "creation_event_id": bundle.creation_event_id,
                "claimed_by": self._actor,
                "branch": branch,
                "worktree_path": worktree_path,
                "session_id": session_discriminator(),
                "expected_files": expected_files,
                "member_claims": member_claims,
                "created_at": now.isoformat(),
                "lease_expires_at": (
                    now + datetime.timedelta(minutes=self._lease_minutes)
                ).isoformat(),
                "last_heartbeat_at": now.isoformat(),
            },
        )
        try:
            self._backend.append(draft)
        except BackendError as exc:
            raise BundleError(str(exc)) from exc
        claimed_bundle = self._backend.get_bundle(bundle_id)
        claim = self._backend.get_bundle_claim(bundle_id)
        if claimed_bundle is None or claim is None:  # pragma: no cover - invariant
            raise BundleError("Bundle claim committed without a readable projection.")
        return BundleClaimResult(bundle=claimed_bundle, claim=claim)

    def note_progress(
        self,
        bundle_id: str,
        *,
        phase: str,
        detail: str | None = None,
        member_task_ids: list[str] | None = None,
    ) -> None:
        bundle = self._backend.get_bundle(bundle_id)
        claim = self._backend.get_bundle_claim(bundle_id)
        if bundle is None or claim is None:
            raise BundleError(f"Active claim for bundle '{bundle_id}' not found.")
        now = self._clock.now()
        try:
            self._backend.append(
                EventDraft(
                    timestamp=now,
                    actor=self._actor,
                    action="bundle.progress_noted",
                    target_kind="bundle",
                    target_id=bundle_id,
                    payload_json={
                        "bundle_id": bundle_id,
                        "creation_event_id": bundle.creation_event_id,
                        "bundle_claim_id": claim.id,
                        "actor": self._actor,
                        "phase": phase,
                        "detail": detail,
                        "member_task_ids": member_task_ids or [],
                        "noted_at": now.isoformat(),
                    },
                )
            )
        except BackendError as exc:
            raise BundleError(str(exc)) from exc

    def readiness(self, bundle_id: str) -> BundleReadiness:
        bundle = self._backend.get_bundle(bundle_id)
        if bundle is None:
            raise BundleError(f"Bundle '{bundle_id}' not found.")
        blockers: dict[str, list[str]] = {}
        for task_id in bundle.task_ids:
            task = self._backend.get_task(task_id)
            if task is None:
                blockers[task_id] = ["task missing"]
                continue
            evidence = self._backend.get_latest_evidence(task_id)
            reasons: list[str] = []
            if evidence is None:
                reasons.append("completion evidence missing")
            else:
                verdict = evaluate_claims(
                    task, evidence, project_root=self._project_root
                )
                reasons.extend(
                    f"claim {claim.claim or '(implicit)'}: {claim.verdict}"
                    for claim in verdict.enforceable_unproven
                )
            if task.status not in {
                TaskStatus.needs_review,
                TaskStatus.accepted,
                TaskStatus.done,
            }:
                reasons.append(f"task status is {task.status.value}")
            if reasons:
                blockers[task_id] = reasons
        return BundleReadiness(
            bundle_id=bundle_id,
            can_mark_implemented=not blockers,
            unproven_members=blockers,
        )

    def packet(self, bundle_id: str) -> BundleWorkPacket:
        bundle = self._backend.get_bundle(bundle_id)
        if bundle is None:
            raise BundleError(f"Bundle '{bundle_id}' not found.")
        requirements = {
            requirement.id: requirement
            for requirement in self._backend.list_requirements(prd_id=bundle.prd_id)
        }
        contexts: list[BundleMemberPacketContext] = []
        for task_id in bundle.task_ids:
            task = self._backend.get_task(task_id)
            if task is None:
                raise BundleError(f"Bundle member task '{task_id}' not found.")
            feature = self._backend.get_feature(task.feature_id)
            if feature is None:
                raise BundleError(f"Feature '{task.feature_id}' not found.")
            dependencies = [
                dependency
                for dependency_id in task.dependencies
                if (dependency := self._backend.get_task(dependency_id)) is not None
            ]
            contexts.append(
                BundleMemberPacketContext(
                    task=task,
                    feature=feature,
                    requirements=[
                        requirements[requirement_id]
                        for requirement_id in feature.requirements
                        if requirement_id in requirements
                    ],
                    dependencies=dependencies,
                )
            )
        return render_bundle_packet(
            bundle, contexts, bundle_claim=self._backend.get_bundle_claim(bundle_id)
        )

    def mark_implemented(self, bundle_id: str) -> BundleReadiness:
        readiness = self.readiness(bundle_id)
        if not readiness.can_mark_implemented:
            return readiness
        bundle = self._backend.get_bundle(bundle_id)
        if bundle is None:  # pragma: no cover - guarded by readiness
            raise BundleError(f"Bundle '{bundle_id}' not found.")
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
                        "creation_event_id": bundle.creation_event_id,
                        "from": bundle.status.value,
                        "to": BundleStatus.implemented_unreviewed.value,
                        "changed_at": now.isoformat(),
                        "reason": "all member completion evidence submitted",
                    },
                )
            )
        except BackendError as exc:
            raise BundleError(str(exc)) from exc
        return readiness
