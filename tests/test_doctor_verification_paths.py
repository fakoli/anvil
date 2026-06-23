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


def test_cd_prefix_resolves_dotdot_path_ok(backend, frozen_clock, tmp_path: Path):  # type: ignore[no-untyped-def]
    # The canonical `cd bin && uv run pytest ../tests/x.py` form: the token is
    # bin-relative, so it must resolve against <root>/bin, landing on <root>/tests.
    # Without cwd-tracking the doctor wrongly flagged this correct command.
    (tmp_path / "bin").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "real.py").write_text("", encoding="utf-8")
    create_workflow_task(
        backend, title="t", description="d", actor="r", clock=frozen_clock,
        verification=Verification(
            commands=["cd bin && uv run pytest -q ../tests/real.py"]
        ),
    )
    finding = _check_verification_paths(backend, tmp_path)
    assert finding.severity == "ok"


def test_cd_prefix_bare_path_is_flagged(backend, frozen_clock, tmp_path: Path):  # type: ignore[no-untyped-def]
    # Under `cd bin`, a bare `tests/x.py` is bin-relative (<root>/bin/tests/x.py),
    # which does not exist — the "passes by hand, never runs in CI" footgun the
    # check exists to catch. (Before the cwd fix this was inverted: the doctor
    # green-lit this broken form and flagged the correct ../tests one above.)
    (tmp_path / "bin").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "real.py").write_text("", encoding="utf-8")
    tid = create_workflow_task(
        backend, title="t", description="d", actor="r", clock=frozen_clock,
        verification=Verification(
            commands=["cd bin && uv run pytest -q tests/real.py"]
        ),
    )
    finding = _check_verification_paths(backend, tmp_path)
    assert finding.severity == "warning"
    assert tid in finding.message
    assert finding.detail["offenders"][0]["path"] == "tests/real.py"


def test_cd_prefix_resets_per_statement_no_false_positive(backend, frozen_clock, tmp_path: Path):  # type: ignore[no-untyped-def]
    # A multi-statement command that re-cd's in each ';' statement
    # (cd bin && A; cd bin && B) must resolve B's ../tests path from bin/, not
    # accumulate to bin/bin. Regression guard for the per-statement cwd reset.
    (tmp_path / "bin").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "real.py").write_text("", encoding="utf-8")
    create_workflow_task(
        backend, title="t", description="d", actor="r", clock=frozen_clock,
        verification=Verification(
            commands=[
                "cd bin && uv run pytest -q -k foo; "
                "cd bin && uv run pytest -q ../tests/real.py"
            ]
        ),
    )
    finding = _check_verification_paths(backend, tmp_path)
    assert finding.severity == "ok"
