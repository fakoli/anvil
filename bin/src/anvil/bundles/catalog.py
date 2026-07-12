"""Shared bundle creation and read contract for CLI and MCP."""

from __future__ import annotations

from anvil.clock import Clock
from anvil.state.backend import Backend, BackendError
from anvil.state.models import (
    BundleReviewPolicy,
    BundleThroughputBudget,
    EventDraft,
    ExecutionBundle,
)


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
                        "prd_id": prd_id,
                        "task_ids": task_ids,
                        "coordinator": coordinator,
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
