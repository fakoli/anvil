"""Atomic coordinator claim, progress, and completion services for bundles."""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from anvil.bundles.eligibility import analyze_bundle_graph
from anvil.clock import Clock
from anvil.context.packets import (
    BundleMemberPacketContext,
    BundleWorkPacket,
    render_bundle_packet,
)
from anvil.naming import session_discriminator
from anvil.review.gates import evaluate_claims, evidence_complete
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
        bundle = self.preflight(bundle_id)
        tasks = [self._backend.get_task(task_id) for task_id in bundle.task_ids]
        if any(task is None for task in tasks):  # pragma: no cover - preflight
            raise BundleError("Bundle member disappeared after preflight.")
        typed_tasks = [task for task in tasks if task is not None]

        expected_files: list[str] = []
        for task in typed_tasks:
            for path in task.likely_files:
                if path not in expected_files:
                    expected_files.append(path)
        now = self._clock.now()
        claim_id = self._id("BC")
        member_claims = [
            {"id": self._id("C"), "task_id": task.id} for task in typed_tasks
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

    def preflight(self, bundle_id: str) -> ExecutionBundle:
        """Read-only claimability check used before any Git side effect."""
        bundle = self._backend.get_bundle(bundle_id)
        if bundle is None:
            raise BundleError(f"Bundle '{bundle_id}' not found.")
        if bundle.coordinator != self._actor:
            raise BundleError(
                f"Bundle '{bundle_id}' coordinator is '{bundle.coordinator}', "
                f"not '{self._actor}'."
            )
        if bundle.status is not BundleStatus.planned:
            raise BundleError(
                f"Bundle '{bundle_id}' status is '{bundle.status.value}', expected planned."
            )
        existing_claim = self._backend.get_bundle_claim(bundle_id)
        if existing_claim is not None and existing_claim.status.value == "active":
            raise BundleError(f"Bundle '{bundle_id}' already has a coordinator claim.")
        tasks = []
        for task_id in bundle.task_ids:
            task = self._backend.get_task(task_id)
            if task is None:
                raise BundleError(f"Bundle member task '{task_id}' not found.")
            tasks.append(task)
        members = set(bundle.task_ids)
        graph = analyze_bundle_graph(
            bundle.task_ids,
            {task.id: list(task.dependencies) for task in tasks},
        )
        if graph.dependency_cycle:
            raise BundleError(
                "Bundle member dependency cycle: "
                + " -> ".join(graph.dependency_cycle)
                + "."
            )
        if graph.critical_path_depth > bundle.throughput_budget.max_serial_stages:
            raise BundleError(
                f"Bundle critical path {graph.critical_path_depth} exceeds "
                "max_serial_stages "
                f"{bundle.throughput_budget.max_serial_stages}."
            )
        for task in tasks:
            if task.status is not TaskStatus.ready:
                raise BundleError(f"Bundle member '{task.id}' is not ready.")
            for dependency_id in task.dependencies:
                if dependency_id in members:
                    continue
                dependency = self._backend.get_task(dependency_id)
                if dependency is None or dependency.status is not TaskStatus.done:
                    raise BundleError(
                        "Bundle external dependencies are not done: "
                        f"['{dependency_id}']."
                    )
        expected_files: list[str] = []
        for task in tasks:
            for path in task.likely_files:
                if path not in expected_files:
                    expected_files.append(path)
        expected_set = set(expected_files)
        bundle_groups = {group for task in tasks for group in task.conflict_groups}
        for active in self._backend.list_active_claims():
            active_task = self._backend.get_task(active.task_id)
            if (
                active.task_id in members
                or expected_set.intersection(active.expected_files)
                or (
                    active_task is not None
                    and bundle_groups.intersection(active_task.conflict_groups)
                )
            ):
                raise BundleError(
                    f"Bundle conflicts with active claims: ['{active.id}']."
                )
        return bundle

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
        bundle_claim = self._backend.get_bundle_claim(bundle_id)
        if bundle_claim is None:
            raise BundleError(f"Bundle '{bundle_id}' has no coordinator claim.")
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
                expected_claim = bundle_claim.member_claim_ids.get(task_id)
                if evidence.claim_id != expected_claim:
                    reasons.append(
                        "evidence is not bound to the current bundle member claim"
                    )
                complete, missing = evidence_complete(task, evidence)
                if not complete:
                    reasons.extend(f"evidence missing: {item}" for item in missing)
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

    def renew(self, bundle_id: str) -> BundleClaim:
        claim = self._backend.get_bundle_claim(bundle_id)
        if claim is None:
            raise BundleError(f"Bundle '{bundle_id}' has no coordinator claim.")
        now = self._clock.now()
        expires = now + datetime.timedelta(minutes=self._lease_minutes)
        try:
            self._backend.append(
                EventDraft(
                    timestamp=now,
                    actor=self._actor,
                    action="bundle.claim_renewed",
                    target_kind="bundle",
                    target_id=bundle_id,
                    payload_json={
                        "bundle_claim_id": claim.id,
                        "bundle_id": bundle_id,
                        "renewed_by": self._actor,
                        "lease_expires_at": expires.isoformat(),
                        "last_heartbeat_at": now.isoformat(),
                    },
                )
            )
        except BackendError as exc:
            raise BundleError(str(exc)) from exc
        renewed = self._backend.get_bundle_claim(bundle_id)
        if renewed is None:  # pragma: no cover
            raise BundleError("Renewed bundle claim disappeared.")
        return renewed

    def release(
        self,
        bundle_id: str,
        *,
        force: bool = False,
        reason: str | None = None,
    ) -> None:
        claim = self._backend.get_bundle_claim(bundle_id)
        if claim is None:
            raise BundleError(f"Bundle '{bundle_id}' has no coordinator claim.")
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
                        "release_reason": reason,
                        "force": force,
                    },
                )
            )
        except BackendError as exc:
            raise BundleError(str(exc)) from exc

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
            dependencies = []
            for dependency_id in task.dependencies:
                dependency = self._backend.get_task(dependency_id)
                if dependency is None:
                    raise BundleError(
                        f"Task '{task.id}' references missing dependency "
                        f"'{dependency_id}'."
                    )
                dependencies.append(dependency)
            missing_requirements = [
                requirement_id
                for requirement_id in feature.requirements
                if requirement_id not in requirements
            ]
            if missing_requirements:
                raise BundleError(
                    f"Feature '{feature.id}' references missing requirements: "
                    f"{missing_requirements}."
                )
            contexts.append(
                BundleMemberPacketContext(
                    task=task,
                    feature=feature,
                    requirements=[
                        requirements[requirement_id]
                        for requirement_id in feature.requirements
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
        bundle_claim = self._backend.get_bundle_claim(bundle_id)
        if bundle_claim is None:  # pragma: no cover - guarded by readiness
            raise BundleError(f"Bundle '{bundle_id}' has no coordinator claim.")
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
                        "bundle_claim_id": bundle_claim.id,
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
