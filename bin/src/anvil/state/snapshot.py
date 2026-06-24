"""Canonical-state snapshot — the semantic definition of "the state the system
cares about", as a deterministic, JSON-serialisable structure.

This module exposes a single pure function, :func:`serialize_state`, used by the
SL-1 replay-equivalence test: it snapshots a normally-built backend and a
replayed backend, then byte-compares the two via ``json.dumps(..., sort_keys=True)``.

Design contract
---------------
- **Read-only.** ``serialize_state`` calls only the backend's read API. It never
  writes, never reads the clock, and never touches the filesystem beyond the
  backend's own queries. It reaches through the Backend protocol, never into
  SQLite directly.
- **Total.** Every canonical collection is covered: project, prd, features,
  tasks, claims (ALL of them — active, released, stale, force_released),
  reviews, evidence, requirements, and sync mappings.

  Intentionally excluded tables: ``decisions`` and ``conflict_groups`` exist in
  the schema but are never written by any current handler — they are always empty
  and cannot diverge between a normal run and a replay. If a writer is added for
  either table, this snapshot MUST be extended at the same time or
  replay-equivalence will silently stop covering that table.

- **Deterministic.** Each collection is sorted by a stable key before
  serialisation, and every model is dumped via pydantic ``model_dump(mode="json")``
  so datetimes/enums serialise to stable strings. The result satisfies:

      json.dumps(serialize_state(b), sort_keys=True)

  being byte-identical across repeated calls on the same backend. ``sort_keys``
  handles object-key ordering; this module's job is collection-element ordering.

Output shape (the contract downstream fixtures/tests depend on)
---------------------------------------------------------------
``serialize_state`` returns a ``dict`` with exactly these top-level keys::

    {
      "project":       <object> | None,     # single Project, or None
      "prd":           <object> | None,     # single PRD, or None
      "features":      [<object>, ...],      # sorted by id
      "tasks":         [<object>, ...],      # sorted by id
      "claims":        [<object>, ...],      # ALL claims, sorted by id
      "reviews":       [<object>, ...],      # sorted by id
      "evidence":      [<object>, ...],      # sorted by id
      "requirements":  [<object>, ...],      # sorted by id
      "sync_mappings": [<object>, ...],      # sorted by (task_id, external_system)
    }

Each ``<object>`` is the corresponding model's ``model_dump(mode="json")``.
``project`` and ``prd`` are singletons (the backend exposes ``get_project`` /
``get_prd`` returning one-or-None), so they are emitted as a single object or
``None`` rather than a list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from anvil.state.backend import Backend


def serialize_state(backend: Backend) -> dict[str, Any]:
    """Return a deterministic, JSON-serialisable snapshot of canonical state.

    Pure read over the backend's read API — see module docstring for the
    full contract and the exact output shape.

    Parameters
    ----------
    backend:
        Any object satisfying the read side of the Backend protocol. Only
        the ``get_project``, ``get_prd``, ``list_features``, ``list_tasks``,
        ``list_claims``, ``list_reviews``, ``list_evidence``,
        ``list_requirements``, and ``list_sync_mappings`` methods are used.

    Returns
    -------
    dict
        A structure for which ``json.dumps(result, sort_keys=True)`` is
        byte-identical across repeated calls on an unchanged backend.
    """
    project = backend.get_project()
    # T021 audit (get_prd no-arg): default-only-correct. The snapshot's ``prd``
    # singleton mirrors the on-disk shape the replay-equivalence oracle compares,
    # which resolves the ``is_default = 1`` PRD — exactly what no-arg get_prd()
    # returns. Per-PRD partitions surface through ``tasks``/``features``/
    # ``requirements`` (each carries its prd_id), so multi-PRD state is not lost
    # here; only the legacy singleton field stays pinned to the default PRD.
    prd = backend.get_prd()

    return {
        # Singletons: one-or-None, emitted directly (no list wrapper).
        "project": project.model_dump(mode="json") if project is not None else None,
        "prd": prd.model_dump(mode="json") if prd is not None else None,
        # Collections: each sorted by a stable key so element order is
        # deterministic regardless of the order the backend returned rows.
        # The backend already sorts by id ASC, but we re-sort here so the
        # snapshot's determinism does not silently depend on backend ordering.
        "features": [
            f.model_dump(mode="json")
            for f in sorted(backend.list_features(), key=lambda f: f.id)
        ],
        "tasks": [
            t.model_dump(mode="json")
            for t in sorted(backend.list_tasks(), key=lambda t: t.id)
        ],
        # list_claims() returns ALL claims (active, released, stale,
        # force_released) — NOT list_active_claims(). The replay-equivalence
        # test depends on this so terminal claim states are part of the
        # compared snapshot.
        "claims": [
            c.model_dump(mode="json")
            for c in sorted(backend.list_claims(), key=lambda c: c.id)
        ],
        "reviews": [
            r.model_dump(mode="json")
            for r in sorted(backend.list_reviews(), key=lambda r: r.id)
        ],
        "evidence": [
            e.model_dump(mode="json")
            for e in sorted(backend.list_evidence(), key=lambda e: e.id)
        ],
        # requirements are written by prd.parsed (destructive replace).
        # Sorted by id for determinism — id is the stable natural key.
        "requirements": [
            rq.model_dump(mode="json")
            for rq in sorted(backend.list_requirements(), key=lambda rq: rq.id)
        ],
        # SyncMapping has no single-column id; its natural key is the
        # (task_id, external_system) pair (matching the backend's own ORDER BY).
        # The sort key (task_id, external_system) is total because that pair is
        # unique per the table's UNIQUE constraint.
        #
        # T028: a prd-kind (milestone) mapping carries ``task_id=None`` (it is
        # owned by a PRD, not a task). Comparing ``None`` against a ``str``
        # task_id raises TypeError, so coerce a null task_id to ``""`` for
        # ordering only — empty string sorts ahead of any real task_id and the
        # dumped row is unchanged (the sort key never reaches the snapshot).
        "sync_mappings": [
            m.model_dump(mode="json")
            for m in sorted(
                backend.list_sync_mappings(),
                key=lambda m: (m.task_id or "", m.external_system),
            )
        ],
    }
