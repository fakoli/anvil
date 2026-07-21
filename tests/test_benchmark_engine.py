from pathlib import Path

from benchmarks.harness.engine import _venv_anvil_candidate


def test_venv_anvil_candidate_uses_windows_console_script() -> None:
    assert _venv_anvil_candidate(Path("project"), os_name="nt") == (
        Path("project") / ".venv" / "Scripts" / "anvil.exe"
    )


def test_venv_anvil_candidate_uses_posix_console_script() -> None:
    assert _venv_anvil_candidate(Path("project"), os_name="posix") == (
        Path("project") / ".venv" / "bin" / "anvil"
    )
