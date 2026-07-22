# Contributing

## Test workflows on Windows

Run these commands from the repository root. They work in PowerShell and keep
the `bin` project environment explicit.

For the complete suite in the default serial mode:

```powershell
uv run --project bin pytest
```

For an opt-in parallel local run that uses all available workers:

```powershell
uv run --project bin pytest -n auto
```

Parallel execution is an optional contributor workflow. It is not the
mandatory CI default; keep the serial command as the reference result until
stability evidence supports changing that policy.

For a faster local feedback loop that skips tests marked `slow`:

```powershell
uv run --project bin pytest -m "not slow"
```

The fast command is only a local test selection. It does not reduce the
project's required test coverage or replace a complete serial run before a
change is merged.
