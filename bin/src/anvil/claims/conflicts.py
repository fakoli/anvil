"""Portable file identity for claim conflict comparisons, without filesystem I/O."""

from collections.abc import Iterable

from anvil.planning.inference import _canonical_project_path


def canonical_claim_file_scope(paths: Iterable[str]) -> set[str]:
    """Compare legacy and canonical reservations using execution-plan identity.

    Keep the authored spellings in events and proof bindings. Comparisons alone
    normalize relative spellings and conservatively fold case, as bundle wave
    planning does. Invalid reservations raise BundlePlanningError; callers must
    refuse the candidate rather than discard an uncomparable active lock.
    """
    return {_canonical_project_path(path).casefold() for path in paths}
