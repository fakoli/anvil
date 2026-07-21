from pathlib import Path

from benchmarks.harness import runner


def test_remove_trial_directory_retries_transient_permission_error(
    monkeypatch,
) -> None:
    attempts = 0

    def flaky_rmtree(path: Path) -> None:
        nonlocal attempts
        attempts += 1
        assert path == Path("trial")
        if attempts < 3:
            raise PermissionError("handle still closing")

    monkeypatch.setattr(runner.shutil, "rmtree", flaky_rmtree)

    runner._remove_trial_directory(
        Path("trial"),
        max_attempts=3,
        retry_delay_seconds=0,
    )

    assert attempts == 3


def test_configure_utf8_stdout_reconfigures_legacy_stream(monkeypatch) -> None:
    calls: list[dict[str, str]] = []

    class LegacyStream:
        def reconfigure(self, **kwargs: str) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(runner.sys, "stdout", LegacyStream())

    runner._configure_utf8_stdout()

    assert calls == [{"encoding": "utf-8", "errors": "replace"}]
