"""Unit tests for ``anvil.naming.safe_path_component`` (#105 / #108.1).

A namespaced task id (``prd:T001``) must be reducible to a single path
component that is safe as both a Windows/NTFS filename and a git refname. The
core failure mode is the ``:`` — on NTFS it opens an alternate data stream, so
``packets/prd:T001.md`` silently corrupts; in git it is an illegal refname
character.
"""

from __future__ import annotations

import pytest

from anvil.naming import safe_path_component

# The exact hostile set the sanitizer must eliminate: Windows-reserved plus a
# couple of C0 control characters.
_RESERVED = '<>:"/\\|?*'
_CONTROL = "\x00\x07\x1f"


def test_bare_id_is_unchanged() -> None:
    # Non-namespaced ids are already path-safe and must round-trip untouched,
    # so existing packets/branches keep their names (backward compatibility).
    assert safe_path_component("T001") == "T001"
    assert safe_path_component("T014") == "T014"


def test_subtask_dot_is_preserved() -> None:
    # '.' is legal in filenames and refs; expansion ids like T001.2 must survive.
    assert safe_path_component("T001.2") == "T001.2"


def test_namespaced_colon_becomes_dash() -> None:
    assert safe_path_component("advise-and-defer:T005") == "advise-and-defer-T005"
    assert safe_path_component("v0.4.0:T001") == "v0.4.0-T001"


def test_colon_never_survives() -> None:
    assert ":" not in safe_path_component("harness-router:T001")


@pytest.mark.parametrize("raw", [
    'a<b>c:d"e/f\\g|h?i*j',
    "prd:T001",
    "weird\x00id\x1f:T009",
    "a::b:::c",
])
def test_no_reserved_or_control_char_survives(raw: str) -> None:
    out = safe_path_component(raw)
    assert not (set(out) & set(_RESERVED)), out
    assert not (set(out) & set(_CONTROL)), out


def test_runs_collapse_to_single_dash() -> None:
    # A run of unsafe chars becomes ONE '-', not many.
    assert safe_path_component("a:::b") == "a-b"
    assert safe_path_component('x:/\\y') == "x-y"


def test_idempotent() -> None:
    for raw in ["T001", "advise-and-defer:T005", 'a<b>:c', "a::b"]:
        once = safe_path_component(raw)
        assert safe_path_component(once) == once


def test_result_is_a_single_path_component() -> None:
    # No path separators may remain — the result must be one component.
    out = safe_path_component("some/nested:T001")
    assert "/" not in out and "\\" not in out
