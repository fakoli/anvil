# Contributing

## Test workflows on Windows

Run these commands from the repository root. They work in PowerShell and keep
the `bin` project environment explicit.

For the complete suite, use pytest-xdist across the available workers:

```powershell
uv run --project bin pytest -n auto
```

For the Git-heavy fixture contract changed by the Windows performance work:

```powershell
uv run --project bin pytest tests/test_git_ops.py tests/test_reconciliation.py -n 16 -q
```

The complete suite runs in parallel locally and in Linux CI. Windows CI uses
separate focused contracts instead of repeating the complete suite. No
maintained workflow requires or schedules a full serial run; the only broad
serial result cited here is the revision-bound historical comparison below.

For a faster local feedback loop that skips tests marked `slow`:

```powershell
uv run --project bin pytest -n auto -m "not slow"
```

The fast command is only a local test selection. It does not reduce the
project's required test coverage or replace the complete parallel suite when a
change needs broad verification.

For changes limited to the Windows sample source-binding repair, run its direct
four-test contract:

```powershell
uv run --project bin pytest tests/test_cli.py::TestSampleSourceBindingContract -q
```

This focused contract is one recurring Windows CI gate. Windows CI also runs
the 167-node Git fixture contract at 16 workers and the timing-harness mechanics
tests as separate steps with isolated temporary roots; it does not run the
optional timed measurement. Changes to lifecycle, Git, replay, schema, or
platform behavior still require the complete suite.

For the fixed native-Windows parallel regression measurement of the Git-heavy
fixture contract, start from a clean, committed worktree, choose a public-safe
host label, and leave the output path absent:

```powershell
$env:ANVIL_TIMING_HOST_LABEL = "windows-test-host"
pwsh -NoProfile -File scripts/measure-windows-pytest.ps1 -Warmups 1 -Samples 3 -Workers 16 -TimeoutSeconds 120 -Output artifacts/windows-pytest-timing.json
```

The harness collects and executes only `tests/test_git_ops.py` and
`tests/test_reconciliation.py` (currently 167 tests), records one parallel
warmup plus three 16-worker samples, and keeps raw logs in a Git-ignored
directory. It never runs the suite serially. The initial affected-slice
regression budget is a 35-second median; this is a same-host ceiling, not a
project-wide speedup claim.

The harness does not require an elevated shell. Windows may make individual
Defender exclusion groups unavailable to a standard user; the artifact records
each group's availability while still requiring power, Defender status, and
every available exclusion digest to remain stable throughout the run.

The repository also has one-time full-suite evidence at commit
`21af013fd29c7b6c6c988e1175ed08cdee09fa46`. From the repository root,
`uv run --project bin pytest tests -q` completed with 4,951 passed, 16 skipped,
and 3 deselected in 1,982.05 seconds; `uv run --project bin pytest tests -n auto
-q` completed with 4,951 passed and 16 skipped in 238.89 seconds (about 8.3x
faster). That historical broad comparison supports the parallel default; it is
not a recurring benchmark protocol or evidence for the narrower fixture budget
by itself. Required parallel CI supplies broad verification, so
contributors do not need to repeat the full suite locally for every change.
