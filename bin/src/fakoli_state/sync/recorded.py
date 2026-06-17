"""Deterministic :class:`SyncProvider` test double (Phase 8 Wave 1).

Mirrors :class:`fakoli_state.planning.llm.RecordedLLMProvider`: tests build
a ``{key: canned_value}`` dict, inject the provider into the code under
test, and assert on the resulting behaviour. On a key miss the provider
raises :class:`fakoli_state.sync.errors.SyncProviderError` so accidental
"this test secretly called the real GitHub" failures are loud, not silent.

Key design
----------
The lookup key is

    sha256(method + ":" + json.dumps(args, sort_keys=True, default=...))

Choices behind that shape:

* **SHA256, not Python's built-in ``hash()``.** ``hash(str)`` is salted
  per-process (PEP 456) so a recording built in one process would miss in
  another. SHA256 is stable across processes, machines, Python versions —
  tests can pre-compute keys as fixtures and they keep working.
* **JSON serialization for args.** Pydantic models become JSON via
  ``model_dump(mode="json")``; ``None`` survives as ``null``; strings are
  strings. The serializer is deterministic given ``sort_keys=True``.
* **``method`` separator with ``:``.** Two different method names cannot
  collide because the method name has no ``:`` (Python identifier rules).
* **No method-arg "shape" beyond the kwargs dict.** All Protocol methods
  are kw-only; we always hash the kwargs dict. Positional args would
  require ordering rules; we sidestep that by mirroring the Protocol's
  kw-only discipline.

Public surface
--------------
- :class:`RecordedSyncProvider` — the test double.
- :meth:`RecordedSyncProvider.record_key` — classmethod for pre-computing
  fixture keys deterministically.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from fakoli_state.sync.errors import SyncProviderError
from fakoli_state.sync.provider import ExternalRef, ExternalTask, ProviderHealth

if TYPE_CHECKING:
    from fakoli_state.state.models import Task

__all__ = ["RecordedSyncProvider"]


# The five Protocol methods. Centralised so the key function and the
# dispatch methods agree on names verbatim.
_METHOD_PUSH = "push_task"
_METHOD_FETCH = "fetch_task"
_METHOD_LIST = "list_tasks"
_METHOD_DELETE = "delete_task"
_METHOD_HEALTH = "health_check"


def _json_default(value: Any) -> Any:
    """JSON encoder for non-native types that may appear in args.

    * Pydantic models → ``model_dump(mode="json")`` (recursive, sorted
      where the model defines its own ordering — Pydantic preserves
      declaration order, which is the canonical form for a given class).
    * ``datetime`` → ISO 8601 string (Pydantic does this internally for
      ``mode="json"``; we mirror it for raw datetime args, of which the
      Protocol currently has none but future kw-only additions may).
    * Anything else → ``TypeError`` (deliberate; recording keys must be
      built from json-serialisable args, and unknown types should fail
      loudly so the test author fixes the recording, not silently hashes
      a ``repr()`` that drifts across Python versions).
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    raise TypeError(
        f"RecordedSyncProvider.record_key: unsupported argument type "
        f"{type(value).__name__}; only Pydantic models, str/int/float/bool/None, "
        f"list/dict of the above, and datetime are accepted."
    )


class RecordedSyncProvider:
    """Deterministic :class:`fakoli_state.sync.provider.SyncProvider` for tests.

    Construct with a dict of canned responses; the provider returns them
    on matching calls and raises :class:`SyncProviderError` on misses.

    Example
    -------
    >>> ref = ExternalRef(provider_id="recorded", external_id="42", url=None)
    >>> key = RecordedSyncProvider.record_key("fetch_task", external_id="42")
    >>> prov = RecordedSyncProvider(  # doctest: +SKIP
    ...     provider_id="recorded",
    ...     display_name="Recorded (test)",
    ...     recordings={key: ExternalTask(...)},
    ... )
    >>> prov.fetch_task(external_id="42")  # doctest: +SKIP
    ExternalTask(...)
    """

    # These satisfy the SyncProvider Protocol's class-level attributes.
    # Settable per-instance because the test double is generic — a single
    # class instantiated under several provider_ids in fixtures.
    provider_id: str
    display_name: str

    def __init__(
        self,
        *,
        provider_id: str = "recorded",
        display_name: str = "Recorded (test double)",
        recordings: dict[str, Any] | None = None,
    ) -> None:
        """Construct a RecordedSyncProvider.

        Parameters
        ----------
        provider_id:
            Identifier this instance reports as :attr:`SyncProvider.provider_id`.
            Defaults to ``"recorded"``; override to mimic a real provider id
            (e.g. ``"github_issues"``) in integration tests that exercise the
            registry dispatch.
        display_name:
            Human-facing name. Same defaulting rationale as ``provider_id``.
        recordings:
            Map from :meth:`record_key` output to the value the matching
            method call returns. The dict is copied to insulate the
            provider from caller-side mutation after construction.
        """
        self.provider_id = provider_id
        self.display_name = display_name
        # Copy to insulate the provider from caller-side mutation. Same
        # discipline as RecordedLLMProvider.
        self._recordings: dict[str, Any] = dict(recordings) if recordings else {}

    # ------------------------------------------------------------------
    # Key derivation
    # ------------------------------------------------------------------

    @classmethod
    def record_key(cls, method: str, **kwargs: Any) -> str:
        """Stable lookup key for ``(method, kwargs)``.

        Same hex digest in every process / interpreter / Python version
        for a given input. Tests pre-compute keys as fixtures and inject
        them into ``recordings={...}``.

        ``kwargs`` are serialized via :func:`json.dumps` with
        ``sort_keys=True``, so the order callers pass the keyword args in
        does NOT affect the key (which mirrors Python's own kw-call
        semantics — order is irrelevant on the call site).
        """
        # Serialize kwargs deterministically. sort_keys handles dict order;
        # _json_default handles Pydantic models + datetime.
        payload = json.dumps(kwargs, sort_keys=True, default=_json_default)
        h = hashlib.sha256()
        h.update(method.encode("utf-8"))
        # Separator: ``:`` cannot appear in a Python identifier, so
        # method-vs-payload boundary is unambiguous.
        h.update(b":")
        h.update(payload.encode("utf-8"))
        return h.hexdigest()

    def _lookup(self, method: str, **kwargs: Any) -> Any:
        """Look up the canned response for ``(method, kwargs)`` or raise.

        Centralised so every method below shares the same miss-error
        formatting. The hash prefix in the error makes it easy to grep
        recordings for the missing key when debugging.
        """
        key = self.record_key(method, **kwargs)
        if key not in self._recordings:
            raise SyncProviderError(
                f"no recording for {method}; key prefix {key[:8]}... "
                f"(have {len(self._recordings)} recording(s))"
            )
        return self._recordings[key]

    # ------------------------------------------------------------------
    # SyncProvider Protocol methods
    # ------------------------------------------------------------------

    def push_task(
        self,
        *,
        task: Task,
        mapping: ExternalRef | None,
    ) -> ExternalRef:
        """Return the canned :class:`ExternalRef` for this ``(task, mapping)``.

        Tests pre-compute the key via
        ``RecordedSyncProvider.record_key("push_task", task=task, mapping=mapping)``
        and stash the desired :class:`ExternalRef` under it.
        """
        result = self._lookup(_METHOD_PUSH, task=task, mapping=mapping)
        if not isinstance(result, ExternalRef):
            raise SyncProviderError(
                f"push_task recording must be an ExternalRef, "
                f"got {type(result).__name__}"
            )
        return result

    def fetch_task(self, *, external_id: str) -> ExternalTask | None:
        """Return the canned :class:`ExternalTask` (or ``None``) for ``external_id``."""
        result = self._lookup(_METHOD_FETCH, external_id=external_id)
        # None is a legitimate canned response (deletion tombstone).
        if result is not None and not isinstance(result, ExternalTask):
            raise SyncProviderError(
                f"fetch_task recording must be an ExternalTask or None, "
                f"got {type(result).__name__}"
            )
        return result

    def list_tasks(self) -> list[ExternalTask]:
        """Return the canned list of :class:`ExternalTask`."""
        result = self._lookup(_METHOD_LIST)
        if not isinstance(result, list) or not all(
            isinstance(t, ExternalTask) for t in result
        ):
            raise SyncProviderError(
                "list_tasks recording must be a list[ExternalTask]"
            )
        return result

    def delete_task(self, *, external_id: str) -> None:
        """Verify a recording exists for the call; return ``None``.

        Even though the method returns ``None``, we still require a
        recording — a test that never set one up almost certainly didn't
        intend the delete path to be exercised. The recorded value itself
        is ignored (callers conventionally use ``None``).
        """
        self._lookup(_METHOD_DELETE, external_id=external_id)
        return None

    def health_check(self) -> ProviderHealth:
        """Return the canned :class:`ProviderHealth` snapshot.

        Unlike a production provider, this raises on miss instead of
        synthesising a "broken" health response — tests that exercise the
        health surface MUST register a recording, so missing fixtures
        show up as test failures rather than silent "everything is down".
        """
        result = self._lookup(_METHOD_HEALTH)
        if not isinstance(result, ProviderHealth):
            raise SyncProviderError(
                f"health_check recording must be a ProviderHealth, "
                f"got {type(result).__name__}"
            )
        return result
