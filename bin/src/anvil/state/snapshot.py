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
- **Total.** Every canonical collection is covered: project, prds, features,
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
      "prds":          [<object>, ...],      # ALL PRDs, sorted by id
      "features":      [<object>, ...],      # sorted by id
      "tasks":         [<object>, ...],      # sorted by id
      "claims":        [<object>, ...],      # ALL claims, sorted by id
      "reviews":       [<object>, ...],      # sorted by id
      "evidence":      [<object>, ...],      # sorted by id
      "requirements":  [<object>, ...],      # FULL lineage; sorted by (prd_id, rev_introduced, id)
      "sync_mappings": [<object>, ...],      # sorted by (task_id, external_system)
    }

Each ``<object>`` is the corresponding model's ``model_dump(mode="json")``.

The ``prds`` collection (T024) replaces the legacy singleton ``prd`` key: a
multi-PRD DB carries one entry per PRD, sorted by ``id``. A single-PRD DB (every
pre-v7 / default-only shape) emits exactly one entry — the default PRD. Each
entry additionally carries its identity/revision stamps (``id`` / ``revision``),
which are ``exclude=True`` on the model — surfaced explicitly so the oracle
compares per-PRD partition identity and the revision counter, not just the
mutable scalar fields.

The ``requirements`` objects additionally carry the partition id (``prd_id``) and
the lineage stamps (``revision_introduced`` / ``revision_superseded``), all
``exclude=True`` on the model — the oracle compares them explicitly so the
per-PRD partition and superseded-row lineage are part of the replay-equivalence
check. They are sorted by ``(prd_id, revision_introduced, id)`` so a multi-PRD /
multi-revision DB has a total, deterministic order: a missed sort key would let
replay silently diverge on a DB whose ids interleave across PRDs/revisions.

``project`` is the sole remaining singleton (the backend exposes ``get_project``
returning one-or-None), emitted as a single object or ``None``.
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
        the ``get_project``, ``list_prds``, ``list_features``, ``list_tasks``,
        ``list_claims``, ``list_reviews``, ``list_evidence``,
        ``list_requirements``, ``list_sync_mappings``, and ``list_bundles``
        methods are used.

    Returns
    -------
    dict
        A structure for which ``json.dumps(result, sort_keys=True)`` is
        byte-identical across repeated calls on an unchanged backend.
    """
    project = backend.get_project()

    state = {
        # Singleton: one-or-None, emitted directly (no list wrapper).
        "project": project.model_dump(mode="json") if project is not None else None,
        # T024: ALL PRDs, sorted by id, replacing the legacy singleton ``prd``.
        # ``id`` and ``revision`` are exclude=True on the PRD model (so a v6 prds
        # row that predates the columns still constructs and existing event
        # payloads stay byte-identical), but they are the per-PRD partition
        # identity and the revision counter — the oracle MUST compare them or a
        # mis-partitioned PRD or a mis-bumped revision would diverge invisibly
        # between the live and replayed DBs. So they are surfaced explicitly here,
        # exactly as the requirements lineage stamps are below. A single-PRD DB
        # emits exactly one entry (the default PRD); the legacy no-arg get_prd()
        # singleton is subsumed by this sorted list.
        "prds": [
            {
                **p.model_dump(mode="json"),
                # id/revision AND the identity/config fields are all Field(exclude=True)
                # on the PRD model, so model_dump() drops them. Re-surface ALL of them
                # so the replay-equivalence oracle + doctor compare them: is_default is
                # the ux_prds_default invariant carrier; title/target_* are written
                # straight from the event payload — a replay that corrupted any of them
                # would otherwise diverge invisibly between the live and replayed DBs.
                "id": p.id,
                "revision": p.revision,
                "is_default": p.is_default,
                "title": p.title,
                "target_version": p.target_version,
                "target_tag": p.target_tag,
            }
            for p in sorted(backend.list_prds(), key=lambda p: p.id)
        ],
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
        # requirements: the FULL lineage (live + superseded), not just the live
        # set. prd.parsed writes the live rows; prd.revised supersedes rows in
        # place (NEVER DELETE) and adds new ones. The partition id (prd_id) and
        # the lineage stamps (revision_introduced / revision_superseded) are
        # surfaced explicitly — all three are exclude=True on the model so
        # model_dump() drops them, but the replay-equivalence oracle MUST compare
        # them or a mis-partitioned / mis-stamped / dropped superseded row would
        # diverge invisibly between the live and replayed DBs.
        #
        # T024: sorted by (prd_id, revision_introduced, id). The id alone is NOT
        # a safe total order on a multi-PRD / multi-revision DB — requirement ids
        # can interleave across PRDs and across revisions, so a id-only sort lets
        # replay silently diverge if two backends assign overlapping ids to
        # different partitions. The (prd_id, revision_introduced, id) tuple is
        # total because id is unique under the single-column PK, so the trailing
        # id breaks any tie deterministically.
        "requirements": [
            {
                **rq.model_dump(mode="json"),
                "prd_id": rq.prd_id,
                "revision_introduced": rq.revision_introduced,
                "revision_superseded": rq.revision_superseded,
            }
            for rq in sorted(
                backend.list_requirements(include_superseded=True),
                key=lambda rq: (rq.prd_id, rq.revision_introduced, rq.id),
            )
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

    # Bundle state is a new canonical collection, but legacy no-bundle
    # snapshots are a committed byte contract. Omit the key while empty (the
    # same additive/omit-when-empty discipline used by Task.claims) and include
    # the full sorted collection as soon as any bundle exists.
    list_bundles = getattr(backend, "list_bundles", None)
    bundles = list_bundles() if list_bundles is not None else []
    if bundles:
        state["bundles"] = [
            bundle.model_dump(mode="json")
            for bundle in sorted(bundles, key=lambda bundle: bundle.id)
        ]
    return state
