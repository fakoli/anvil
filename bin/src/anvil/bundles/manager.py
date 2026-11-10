"""Atomic coordinator claim, progress, and completion services for bundles."""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

from anvil.actors import ActorIdentityError, canonicalize_new_actor
from anvil.bundles.eligibility import analyze_bundle_graph
from anvil.claims.conflicts import canonical_claim_file_scope
from anvil.clock import Clock
from anvil.context.packets import (
    BundleMemberPacketContext,
    BundleWorkPacket,
    relevant_assumptions,
    render_bundle_packet,
)
from anvil.naming import session_discriminator
from anvil.planning.inference import BundlePlanningError
from anvil.review.gates import evaluate_claims, evidence_complete
from anvil.state.backend import Backend, BackendError
from anvil.state.models import (
    BundleClaim,
    BundleStatus,
    ClaimGitMetadata,
    EventDraft,
    ExecutionBundle,
    Task,
    TaskStatus,
)
from anvil.state.schema import SCHEMA_VERSION
from anvil.verification_profiles import ProfileError, require_profile_current


class BundleError(Exception):
    """A coordinator bundle operation failed its gate."""


class BundleActorMismatch(BundleError):
    """Exact coordinator mismatch whose identifiers require structured output."""

    def __init__(self, *, bundle_id: str, owner: str, actual: str) -> None:
        self.bundle_id = bundle_id
        self.owner = owner
        self.actual = actual
        super().__init__(
            f"Bundle '{bundle_id}' belongs to a different persisted coordinator."
        )


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
        git_metadata: ClaimGitMetadata | None = None,
        pre_log_check: Callable[[], None] | None = None,
        expected_scope_digest: str | None = None,
    ) -> BundleClaimResult:
        from anvil.roots.registry import (
            RootSetError,
            RootSetRegistry,
            assert_unscoped_claim_allowed,
            ordinary_claim_coordinator_held,
        )

        try:
            if self._project_root is None:
                assert_unscoped_claim_allowed()
            elif not ordinary_claim_coordinator_held():
                # Keep the owner journal locked through the append.  A caller
                # which already holds State must establish this context first.
                with RootSetRegistry().ordinary_claim_coordinator(self._project_root):
                    return self.claim(
                        bundle_id, branch=branch, worktree_path=worktree_path,
                        git_metadata=git_metadata, pre_log_check=pre_log_check,
                        expected_scope_digest=expected_scope_digest,
                    )
            else:
                pass
        except RootSetError as exc:
            raise BundleError(f"{exc.code}: {exc}") from exc
        lock_factory = getattr(self._backend, "claim_operation_lock", None)
        with lock_factory() if lock_factory is not None else nullcontext():
            return self._claim_locked(
                bundle_id, branch=branch, worktree_path=worktree_path,
                git_metadata=git_metadata, pre_log_check=pre_log_check,
                expected_scope_digest=expected_scope_digest,
            )

    def _claim_locked(
        self, bundle_id: str, *, branch: str | None, worktree_path: str | None,
        git_metadata: ClaimGitMetadata | None, pre_log_check: Callable[[], None] | None,
        expected_scope_digest: str | None,
    ) -> BundleClaimResult:
        from anvil.roots.registry import live_claim_append_authorized

        if expected_scope_digest is not None:
            from anvil.bundles.execution import (
                BundleExecutionPlanError,
                bundle_execution_scope_digest,
            )

            caller_scope_check = pre_log_check

            def check_scope_before_log() -> None:
                try:
                    actual_digest = bundle_execution_scope_digest(self._backend, bundle_id)
                except BundleExecutionPlanError as exc:
                    raise BundleError(str(exc)) from exc
                if actual_digest != expected_scope_digest:
                    raise BundleError(
                        "bundle_scope_changed: refresh the execution plan before claiming."
                    )
                if caller_scope_check is not None:
                    caller_scope_check()

            check_scope_before_log()
            pre_log_check = check_scope_before_log
        bundle = self.preflight(bundle_id)
        tasks = [self._backend.get_task(task_id) for task_id in bundle.task_ids]
        if any(task is None for task in tasks):  # pragma: no cover - preflight
            raise BundleError("Bundle member disappeared after preflight.")
        typed_tasks = [task for task in tasks if task is not None]
        if expected_scope_digest is not None:
            member_ids = set(bundle.task_ids)
            if any(
                dependency in member_ids for task in typed_tasks for dependency in task.dependencies
            ):
                raise BundleError(
                    "bundle_internal_dependencies: scoped execution requires independent members."
                )

        if any(task.verification.profile is not None for task in typed_tasks):
            caller_check = pre_log_check

            def check_profiles_before_log() -> None:
                if caller_check is not None:
                    caller_check()
                if self._backend.get_bundle(bundle_id) != bundle:
                    raise BundleError("verification profile refused: binding_mismatch")
                for task in typed_tasks:
                    current = self._backend.get_task(task.id)
                    if current != task:
                        raise BundleError("verification profile refused: binding_mismatch")
                    self._require_profile_current(current)

            pre_log_check = check_profiles_before_log

        expected_files: list[str] = []
        for task in typed_tasks:
            for path in task.likely_files:
                if path not in expected_files:
                    expected_files.append(path)
        now = self._clock.now()
        claim_id = self._id("BC")
        from anvil.claims.progress_attestation import (
            ProgressAttestationError,
            capture_claim_progress_context,
            inspect_local_repository,
        )

        project = self._backend.get_project()
        if project is None:
            raise BundleError("Cannot capture bundle claim context: project not found.")
        try:
            repository_snapshot = inspect_local_repository(
                self._project_root, project_id=project.id,
            )
        except ProgressAttestationError as exc:
            if exc.code not in {"repository_unavailable", "git_unavailable"} or (
                git_metadata is not None
            ):
                raise BundleError(
                    f"Cannot capture bundle claim context ({exc.code}): {exc}"
                ) from exc
            repository_snapshot = None
        member_claims: list[dict[str, object]] = []
        for task in typed_tasks:
            member_id = self._id("C")
            generation = self._backend.next_claim_generation(task.id)
            member: dict[str, object] = {
                "id": member_id, "task_id": task.id, "generation": generation,
            }
            if repository_snapshot is None:
                member_claims.append(member)
                continue
            prd = self._backend.get_prd(task.prd_id)
            if prd is None:
                raise BundleError("Cannot capture bundle claim context: PRD not found.")
            try:
                context = capture_claim_progress_context(
                    self._project_root,
                    project_id=project.id,
                    claim_id=member_id,
                    claim_generation=generation,
                    task_id=task.id,
                    task_snapshot=task,
                    prd_id=prd.id,
                    prd_revision=prd.revision,
                    actor=self._actor,
                    claim_created_at=now,
                    expected_paths=task.likely_files,
                    claim_start_sha=(
                        git_metadata.claim_start_sha if git_metadata is not None else None
                    ),
                    repository_snapshot=repository_snapshot,
                )
            except ProgressAttestationError as exc:
                raise BundleError(
                    f"Cannot capture bundle claim context ({exc.code}): {exc}"
                ) from exc
            else:
                member["attestation_context"] = context.context_dict()
            member_claims.append(member)
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
                "git_metadata": (
                    git_metadata.model_dump(mode="json")
                    if git_metadata is not None
                    else None
                ),
                "session_id": session_discriminator(),
                "expected_files": expected_files,
                "member_claims": member_claims,
                "created_at": now.isoformat(),
                "lease_expires_at": (
                    now + datetime.timedelta(minutes=self._lease_minutes)
                ).isoformat(),
                "last_heartbeat_at": now.isoformat(),
                **({"expected_scope_digest": expected_scope_digest}
                   if expected_scope_digest is not None else {}),
            },
        )
        try:
            with live_claim_append_authorized():
                if pre_log_check is None:
                    self._backend.append(draft)
                else:
                    self._backend.append(draft, pre_log_check=pre_log_check)
        except BackendError as exc:
            raise BundleError(str(exc)) from exc
        claimed_bundle = self._backend.get_bundle(bundle_id)
        claim = self._backend.get_bundle_claim(bundle_id)
        if claimed_bundle is None or claim is None:  # pragma: no cover - invariant
            raise BundleError("Bundle claim committed without a readable projection.")
        return BundleClaimResult(bundle=claimed_bundle, claim=claim)

    def preflight(self, bundle_id: str) -> ExecutionBundle:
        """Read-only claimability check used before any Git side effect."""
        try:
            self._actor = canonicalize_new_actor(self._actor)
        except ActorIdentityError as exc:
            raise BundleError(f"Invalid coordinator identity: {exc}") from exc
        bundle = self._backend.get_bundle(bundle_id)
        if bundle is None:
            raise BundleError(f"Bundle '{bundle_id}' not found.")
        if bundle.coordinator != self._actor:
            raise BundleActorMismatch(
                bundle_id=bundle_id,
                owner=bundle.coordinator,
                actual=self._actor,
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
            self._require_profile_current(task)
            tasks.append(task)
        members = set(bundle.task_ids)
        graph = analyze_bundle_graph(
            bundle.task_ids,
            {task.id: list(task.dependencies) for task in tasks},
        )
        if len(bundle.task_ids) > bundle.throughput_budget.max_tasks:
            raise BundleError(
                f"Bundle has {len(bundle.task_ids)} members, exceeding max_tasks "
                f"{bundle.throughput_budget.max_tasks}."
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
        bundle_groups = {group for task in tasks for group in task.conflict_groups}
        try:
            expected_set = canonical_claim_file_scope(expected_files)
            if not expected_set and not bundle_groups:
                # No cross-task scope can overlap. Read custody only for members,
                # including expired active claims, instead of hydrating the fleet.
                active_claims = [
                    claim for task_id in bundle.task_ids
                    for claim in self._backend.task_action_context(task_id)[0]
                    if claim.status.value == "active"
                ]
            else:
                active_claims = self._backend.list_active_claims()
            for active in active_claims:
                file_overlap = expected_set and expected_set.intersection(
                    canonical_claim_file_scope(active.expected_files)
                )
                active_task = (
                    self._backend.get_task(active.task_id) if bundle_groups else None
                )
                if (
                    active.task_id in members
                    or file_overlap
                    or (
                        active_task is not None
                        and bundle_groups.intersection(active_task.conflict_groups)
                    )
                ):
                    raise BundleError(
                        f"Bundle conflicts with active claims: ['{active.id}']."
                    )
        except BundlePlanningError as exc:
            raise BundleError(f"Invalid claim file scope: {exc}") from exc
        return bundle

    def _require_profile_current(self, task: Task) -> None:
        if task.verification.profile is None:
            return
        if self._project_root is None:
            raise BundleError("verification profile refused: invalid_path")
        try:
            require_profile_current(task.verification, self._project_root)
        except ProfileError as exc:
            raise BundleError(str(exc)) from None

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
        prd = self._backend.get_prd(bundle.prd_id)
        if prd is None:
            raise BundleError(f"Bundle '{bundle_id}' references missing PRD '{bundle.prd_id}'.")
        requirements = {
            requirement.id: requirement
            for requirement in self._backend.list_requirements(prd_id=bundle.prd_id)
        }
        bundle_claim = self._backend.get_bundle_claim(bundle_id)
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
                    assumptions=relevant_assumptions(prd, feature),
                    member_claim=(
                        self._backend.get_claim(bundle_claim.member_claim_ids[task_id])
                        if bundle_claim is not None else None
                    ),
                )
            )
        return render_bundle_packet(
            bundle, contexts, bundle_claim=bundle_claim
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
                        "schema_version": SCHEMA_VERSION,
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
