"""Shared bundle creation and read contract for CLI and MCP."""

from __future__ import annotations

import unicodedata

from anvil.actors import canonicalize_new_actor
from anvil.clock import Clock
from anvil.state.backend import Backend, BackendError
from anvil.state.models import (
    BundleReviewPolicy,
    BundleThroughputBudget,
    EventDraft,
    ExecutionBundle,
)
from anvil.state.schema import SCHEMA_VERSION


class BundleCatalogError(Exception):
    """A bundle catalog request was refused."""


class BundleCatalog:
    def __init__(self, backend: Backend, clock: Clock, *, actor: str) -> None:
        self._backend = backend
        self._clock = clock
        self._actor = actor

    def create(
        self,
        bundle_id: str,
        *,
        prd_id: str,
        task_ids: list[str],
        coordinator: str,
        review_policy: BundleReviewPolicy | None = None,
        throughput_budget: BundleThroughputBudget | None = None,
    ) -> ExecutionBundle:
        coordinator_input: str | None = None
        existing = {claim.claimed_by for claim in self._backend.list_claims()}
        existing.update(bundle.coordinator for bundle in self._backend.list_bundles())
        if coordinator not in existing:
            try:
                raw_coordinator = coordinator
                coordinator = canonicalize_new_actor(coordinator)
                if coordinator != raw_coordinator:
                    coordinator_input = raw_coordinator
            except ValueError as exc:
                raise BundleCatalogError(f"Invalid coordinator identity: {exc}") from exc
            for persisted in existing:
                try:
                    collision = unicodedata.normalize("NFC", persisted) == coordinator
                except UnicodeError:
                    collision = False
                if collision:
                    raise BundleCatalogError(
                        "Coordinator identity collides with an existing owner after "
                        "NFC normalization."
                    )
        now = self._clock.now()
        try:
            self._backend.append(
                EventDraft(
                    timestamp=now,
                    actor=self._actor,
                    action="bundle.created",
                    target_kind="bundle",
                    target_id=bundle_id,
                    payload_json={
                        "id": bundle_id,
                        "schema_version": SCHEMA_VERSION,
                        "prd_id": prd_id,
                        "task_ids": task_ids,
                        "coordinator": coordinator,
                        **(
                            {"coordinator_input": coordinator_input}
                            if coordinator_input is not None
                            else {}
                        ),
                        "status": "planned",
                        "review_policy": (review_policy or BundleReviewPolicy()).model_dump(
                            mode="json"
                        ),
                        "throughput_budget": (
                            throughput_budget or BundleThroughputBudget()
                        ).model_dump(mode="json"),
                        "created_at": now.isoformat(),
                        "updated_at": now.isoformat(),
                    },
                )
            )
        except BackendError as exc:
            raise BundleCatalogError(str(exc)) from exc
        bundle = self._backend.get_bundle(bundle_id)
        if bundle is None:  # pragma: no cover - append invariant
            raise BundleCatalogError("Bundle creation did not project.")
        return bundle

    def get(self, bundle_id: str) -> ExecutionBundle:
        bundle = self._backend.get_bundle(bundle_id)
        if bundle is None:
            raise BundleCatalogError(f"Bundle '{bundle_id}' not found.")
        return bundle

    def list(self, *, prd_id: str | None = None) -> list[ExecutionBundle]:
        return self._backend.list_bundles(prd_id=prd_id)
