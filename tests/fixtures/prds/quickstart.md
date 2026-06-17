# Project: JSON-to-YAML Converter

## Summary

A small CLI tool that reads one or more JSON files and writes equivalent YAML files.
Targets developers who need to convert configuration files or API fixtures between formats
without installing a full-featured transformation pipeline.

## Goals

- Convert a single JSON file to YAML with one command.
- Accept multiple input files and write each to a matching `.yaml` output path.
- Exit non-zero and print a descriptive message when the input is not valid JSON.
- Preserve key order so diffs are readable.

## Non-Goals

- Round-trip YAML back to JSON (out of scope for v1).
- Support JSON5 or JSONC comment extensions.
- Provide a library API; CLI only in v1.

## Requirements

- R001: The CLI accepts one or more file paths as positional arguments.
- R002: Each input file is parsed as UTF-8 JSON.
- R003: The output file path is derived by replacing the `.json` extension with `.yaml`.
- R004: If the output file already exists, the tool refuses unless `--overwrite` is passed.
- R005: Invalid JSON input exits with code 1 and prints the filename and parse error.
- R006: The tool preserves insertion order for JSON object keys in the YAML output.

## Acceptance Criteria

- Running `jy2yaml sample.json` produces `sample.yaml` with valid YAML content.
- Running `jy2yaml a.json b.json` produces `a.yaml` and `b.yaml` in a single invocation.
- Running `jy2yaml bad.json` exits 1 and prints a message containing the filename.
- Running `jy2yaml existing.yaml` without `--overwrite` exits 1 without overwriting.

## Risks

- PyYAML's default dumper may not preserve key order on Python < 3.7; pin Python ≥ 3.8.
- Very large JSON files (>100 MB) may exhaust memory; document the size limit for v1.

## Open Questions

- Should we support stdin as an input source (`-` as filename)?
- Is a `--in-place` flag (rename original to `.json.bak`) worth adding in v1?

## Features

### F001: Single-file conversion

Converts one JSON file to YAML. Covers the basic happy path.

**Requirements:** R001, R002, R003, R006

### F002: Multi-file batch conversion

Accepts multiple positional arguments and converts each in sequence.

**Requirements:** R001, R002, R003, R004

### F003: Error handling

Validates inputs and produces actionable error messages on failure.

**Requirements:** R005, R004

## Tasks

### T001: Implement argument parsing and file-path resolution

**Feature:** F001
**Priority:** high
**Likely files:** src/jy2yaml/cli.py, src/jy2yaml/__main__.py

Parse positional arguments using `argparse`. Resolve each input path to an absolute path.
Derive the output path by swapping the `.json` extension for `.yaml`. Raise `ValueError`
with the input filename when the extension is not `.json`.

**Acceptance criteria:**

- `cli.parse_args(["sample.json"])` returns a list of `(input_path, output_path)` pairs.
- A non-`.json` filename raises `ValueError` containing the filename.
- Absolute and relative paths both resolve correctly.

**Verification:**

- `pytest tests/test_cli.py::test_parse_args -v`
- `python -m jy2yaml --help`

### T002: Implement JSON-to-YAML conversion core

**Feature:** F001
**Priority:** high
**Likely files:** src/jy2yaml/convert.py

Read the input file as UTF-8, parse with `json.loads`, dump with `yaml.dump` using
`default_flow_style=False` and `sort_keys=False`. Return the YAML string. Do not write
to disk — the caller owns the file write.

**Acceptance criteria:**

- `convert('{"b": 2, "a": 1}')` returns a YAML string with `b:` before `a:`.
- `convert('not json')` raises `json.JSONDecodeError`.
- Output round-trips: `json.loads(json.dumps(original)) == yaml.safe_load(convert(json.dumps(original)))`.

**Verification:**

- `pytest tests/test_convert.py -v`
- `python -c "from jy2yaml.convert import convert; print(convert('{\"x\": 1}'))"`

### T003: Wire CLI to conversion core and handle --overwrite

**Feature:** F002
**Priority:** medium
**Likely files:** src/jy2yaml/cli.py, src/jy2yaml/__main__.py

Call `convert()` for each `(input, output)` pair. Write output only when the output file
does not exist or `--overwrite` was passed. Exit 1 with a descriptive message on any
error. Exit 0 after all files are converted.

**Acceptance criteria:**

- `jy2yaml sample.json` writes `sample.yaml` and exits 0.
- `jy2yaml sample.json` (output exists, no flag) exits 1 without overwriting.
- `jy2yaml sample.json --overwrite` (output exists) overwrites and exits 0.
- `jy2yaml a.json b.json` converts both files in a single invocation.

**Verification:**

- `pytest tests/test_integration.py -v`
- `python -m jy2yaml tests/fixtures/simple.json && cat tests/fixtures/simple.yaml`

### T004: Error handling and exit codes

**Feature:** F003
**Priority:** medium
**Likely files:** src/jy2yaml/cli.py

Catch `json.JSONDecodeError` and `FileNotFoundError` per input file. Print a message to
stderr in the format `error: <filename>: <reason>`. Exit 1 after processing all files
(even if some succeed) when any file fails.

**Acceptance criteria:**

- `jy2yaml bad.json` prints a message containing `bad.json` to stderr and exits 1.
- `jy2yaml missing.json` prints a message containing `missing.json` and exits 1.
- `jy2yaml good.json bad.json` converts `good.json`, prints an error for `bad.json`, exits 1.

**Verification:**

- `pytest tests/test_errors.py -v`
- `python -m jy2yaml tests/fixtures/invalid.json; echo "exit: $?"`
