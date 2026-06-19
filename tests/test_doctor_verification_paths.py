"""B30 / T007 — anvil doctor flags verification command paths that don't resolve.

A verification command like `pytest tests/foo.py` run from a project root where
tests live at `../tests` resolves to a non-existent path and would be silently
skipped by CI. The doctor check catches this for ready tasks.
"""

from __future__ import annotations

from pathlib import Path

from anvil.cli.doctor import (
    _check_verification_paths,
    _extract_verification_paths,
)
from anvil.state.models import Verification
from anvil.workflows.tasks import create_workflow_task


def test_extract_paths_only_returns_slashed_concrete_paths():
    assert _extract_verification_paths("uv run pytest tests/foo.py -q") == ["tests/foo.py"]
    # pytest node id: matched up to the extension
    assert _extract_verification_paths("pytest tests/a.py::test_b") == ["tests/a.py"]
    # globs are skipped (not a concrete path)
    assert _extract_verification_paths("pytest tests/*.py") == []
    # bare filename (no slash) is not flagged
    assert _extract_verification_paths("pytest foo.py") == []
    # flags/markers ignored
    assert _extract_verification_paths("pytest -k foo --maxfail=1") == []


def test_resolving_paths_produce_ok(backend, frozen_clock, tmp_path: Path):  # type: ignore[no-untyped-def]
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "real.py").write_text("", encoding="utf-8")
    create_workflow_task(
        backend, title="t", description="d", actor="r", clock=frozen_clock,
        verification=Verification(commands=["uv run pytest tests/real.py -q"]),
    )
    finding = _check_verification_paths(backend, tmp_path)
    assert finding.severity == "ok"


def test_nonresolving_path_is_flagged(backend, frozen_clock, tmp_path: Path):  # type: ignore[no-untyped-def]
    tid = create_workflow_task(
        backend, title="t", description="d", actor="r", clock=frozen_clock,
        verification=Verification(commands=["uv run pytest tests/missing.py -q"]),
    )
    finding = _check_verification_paths(backend, tmp_path)
    assert finding.severity == "warning"
    assert tid in finding.message
    assert finding.detail["offenders"][0]["path"] == "tests/missing.py"


def test_parent_dir_exists_is_accepted(backend, frozen_clock, tmp_path: Path):  # type: ignore[no-untyped-def]
    # a not-yet-created output file under a real dir should not be flagged
    (tmp_path / "out").mkdir()
    create_workflow_task(
        backend, title="t", description="d", actor="r", clock=frozen_clock,
        verification=Verification(commands=["cat out/result.json"]),
    )
    finding = _check_verification_paths(backend, tmp_path)
    assert finding.severity == "ok"
