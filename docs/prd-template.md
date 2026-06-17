# PRD Template

The `.fakoli-state/prd.md` file is the authoritative source of truth for every project
that uses fakoli-state. It describes what the project must do, why, and how to verify it.
The parser reads it deterministically — no LLM required — and writes the results into
`state.db` as `Requirement`, `Feature`, and `Task` rows.

**Location**: `.fakoli-state/prd.md` inside your project (created by `fakoli-state init`).

**Hard rule**: structure matters. The parser rejects the file with a `ParseError` if any
required section is missing or malformed. Edit `prd.md`, then run `fakoli-state prd parse`
to refresh state.

**Reference**: the canonical data model and CLI command set are defined in
[`docs/specs/2026-05-24-fakoli-state-v0.md`](specs/2026-05-24-fakoli-state-v0.md).

---

## Quick-Start Example

Copy this block into `.fakoli-state/prd.md` and edit it for your project. Every required
and optional section is shown with realistic content. Delete optional sections you do not
need; do not delete required ones.

```markdown
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
**Dependencies:** T001, T002

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
```

---

## Required Sections

The parser rejects the PRD with a `ParseError` if any of these sections is absent. The
parse fails cleanly and existing state in `state.db` is preserved — no silent fallback.

### `# Project: <Project Name>` — H1

The first line of the file. Sets the project display name stored on the `Project` entity.

**Format**: `# Project: ` followed by a non-empty name string.

**Parser behavior**: if this heading is absent or the name is empty, parse fails with
`ParseError("missing required section: Project title")`.

---

### `## Summary`

A single paragraph describing what the project does and who it is for. The parser stores
this verbatim in `PRD.summary`.

**Format**: one or more sentences of prose. No subsections, no bullet lists.

**Parser behavior**: if the section is absent or the body is empty after stripping
whitespace, parse fails with `ParseError("missing required section: Summary")`.

---

### `## Goals`

A bulleted list of at least one item. Stored in `PRD.goals` as a list of strings with
the leading `- ` stripped.

**Format**:

```markdown
## Goals

- First goal statement.
- Second goal statement.
```

**Parser behavior**: if the section is absent, parse fails. If the section is present but
the list is empty, parse fails with `ParseError("missing required section: Goals (must have at least one item)")`.

---

### `## Requirements`

A bulleted list of requirements. Each item may carry an explicit ID in `RNNN:` format or
omit it — the parser assigns IDs in document order when they are absent.

**Format with explicit IDs** (recommended — stable on edits):

```markdown
## Requirements

- R001: The system does X.
- R002: The system does Y when Z.
```

**Format without IDs** (parser auto-assigns R001, R002, …):

```markdown
## Requirements

- The system does X.
- The system does Y when Z.
```

IDs must be zero-padded to three digits: `R001`, `R002`, ..., `R099`, `R100`.

**Parser behavior**: if the section is absent, parse fails. Each item becomes a
`Requirement` entity with `prd_section = "Requirements"`. If explicit IDs conflict
(duplicate `RNNN` in the same file), parse fails with a `ParseError` naming the
conflicting ID.

---

## Optional Sections

If these sections are absent, the parser defaults to empty lists and continues. No
`ParseError` is raised for a missing optional section.

### `## Non-Goals`

A bulleted list of explicitly out-of-scope items. Stored in `PRD.non_goals`. Communicates
boundaries to the planner agent and to reviewers.

```markdown
## Non-Goals

- Round-trip conversion from YAML back to JSON.
- Support for JSON5 comment extensions.
```

---

### `## Acceptance Criteria`

A bulleted list of project-level acceptance criteria (distinct from per-task acceptance
criteria). Stored in `PRD.acceptance_criteria`. Used by the `prd review` gate.

```markdown
## Acceptance Criteria

- Running `tool input.json` produces a valid `input.yaml` in the same directory.
- Invalid JSON input exits 1 with a message naming the file.
```

---

### `## Risks`

A bulleted list of known risks. Stored in `PRD.risks`. Informs the planner's scoring
decisions and surfaces in the `prd review` checklist.

```markdown
## Risks

- PyYAML may not preserve key order on Python < 3.7; pin Python ≥ 3.8.
- Large files (>100 MB) may exhaust memory; document the limit.
```

---

### `## Open Questions`

A bulleted list of unresolved decisions. Stored in `PRD.open_questions`. Presence of
items here does not block parsing or approval — they are informational.

```markdown
## Open Questions

- Should stdin be supported as an input source?
- Is an --in-place flag worth adding in v1?
```

---

### `## Features`

Defines logical groupings of tasks. Each feature is an H3 heading followed by a
description and a `**Requirements:**` field.

**Feature heading format**:

```markdown
### F001: <Feature Title>
```

IDs must be zero-padded to three digits: `F001`, `F002`, etc. Each feature produces a
`Feature` entity. The `**Requirements:**` field is a comma-separated list of requirement
IDs with no extra formatting.

**Full feature block**:

```markdown
## Features

### F001: Single-file conversion

Converts one JSON file to YAML. Covers the basic happy path.

**Requirements:** R001, R002, R003
```

**Parser behavior if absent**: no `Feature` entities are created. Tasks in the `## Tasks`
section still require a `**Feature:**` field if any features exist; if the Features
section is absent, the `**Feature:**` field in tasks is optional (but still recorded if
present).

**Parser behavior on ID conflicts**: duplicate `FNNN` in the same file produces a
`ParseError` naming the conflicting ID.

---

### `## Tasks`

Defines the concrete units of work. Each task is an H3 heading followed by a set of
structured fields and an optional free-form description paragraph.

**Task heading format**:

```markdown
### T001: <Task Title>
```

IDs must be zero-padded to three digits: `T001`, `T002`, etc. Subtask IDs (`T001.1`,
`T001.2`) are created by `fakoli-state expand`, not by the user directly in `prd.md`.

**Task fields** (all optional; order within the block does not matter):

| Field | Format | Default |
|---|---|---|
| `**Feature:**` | `F001` (bare ID) | empty |
| `**Priority:**` | `low`, `medium`, `high`, or `critical` | `medium` |
| `**Type:**` | `feature`, `bugfix`, `refactor`, or `modify` | `feature` |
| `**Likely files:**` | comma-separated relative paths | empty list |
| `**Dependencies:**` | comma-separated TaskIDs (e.g. `T001, T002`) | empty list |
| `**Acceptance criteria:**` | bulleted list on subsequent lines | empty list |
| `**Verification:**` | bulleted list of shell commands, each wrapped in backticks | empty list |

A free-form description paragraph may appear after the fields. It is stored in
`Task.description`.

**`**Dependencies:**` (v1.16.0)** is for SEMANTIC dependencies — Task B truly cannot
function until Task A is done. Examples: T002 tests `HttpTransport` in 2-process mode
→ T002 depends on T001 (the task that implements `HttpTransport`); T015 migrates data
to the new schema → T015 depends on T010 (the task that adds the schema). It is NOT
for "tasks I share files with" — file overlap is detected automatically as conflict
groups. The `fakoli-state claim` command warns (but does not refuse) when claiming a
task whose dependencies aren't yet `done`; pass `--force` to silence the warning if
you're intentionally working a stacked-PR workflow.

**Full task block**:

```markdown
### T001: Implement argument parsing

**Feature:** F001
**Priority:** high
**Likely files:** src/tool/cli.py, src/tool/__main__.py

Parse positional arguments. Derive the output path by swapping `.json` for `.yaml`.

**Acceptance criteria:**

- `parse_args(["sample.json"])` returns a list of `(input, output)` pairs.
- A non-`.json` filename raises `ValueError` containing the filename.

**Verification:**

- `pytest tests/test_cli.py -v`
- `python -m tool --help`
```

**Parser behavior if absent**: no `Task` entities are created. `fakoli-state plan` can
generate tasks from requirements after parsing, but `## Tasks` is the direct way to
provide hand-authored tasks.

**Parser behavior on ID conflicts**: duplicate `TNNN` in the same file produces a
`ParseError` naming the conflicting ID.

---

## ID Conventions

IDs follow a consistent three-digit zero-padded format across all entity types:

| Entity | Format | Examples |
|---|---|---|
| Requirement | `R` + 3 digits | `R001`, `R012`, `R100` |
| Feature | `F` + 3 digits | `F001`, `F002` |
| Task | `T` + 3 digits | `T001`, `T015` |
| Subtask | `T` + 3 digits + `.` + integer | `T001.1`, `T001.2` |

**Provide IDs explicitly.** When IDs are omitted, the parser assigns them in document
order. If you later insert a new item before an existing one, auto-assigned IDs shift —
breaking cross-references and the event log's stable mapping to database rows. Explicit
IDs are stable across edits.

**Cross-references use bare IDs without backticks.** In `**Requirements:**` and
`**Feature:**` fields, write `R001, R002` — not `` `R001` `` or `[R001]`. The parser
looks for the bare ID pattern.

**Subtask IDs are generated, not authored.** The `## Tasks` section should only contain
root task IDs (`T001`, `T002`, ...). Run `fakoli-state expand T001` to break a task
into subtasks; the planner writes `T001.1`, `T001.2`, etc. into state. These do not
appear in `prd.md`.

---

## Parser Behavior at a Glance

**Preprocessing**: HTML comments (`<!-- ... -->`) are stripped before any section
matching. Trailing whitespace and extra blank lines are ignored.

**Re-parse replaces, not merges**: running `fakoli-state prd parse` a second time
replaces all `Requirement`, `Feature`, and `Task` entities in `state.db` completely.
There is no merge. Edit `prd.md` and re-run the command to refresh state. Tasks in
`in_progress` or `claimed` status are also replaced — coordinate with active agents
before re-parsing a live project.

**Missing required sections**: the parse fails immediately with a `ParseError` that names
the missing section. The existing `state.db` content is preserved untouched.

**Missing optional sections**: silently default to empty lists. The parse continues.

**Duplicate IDs**: a `ParseError` is raised naming the first conflicting ID. Existing
state is preserved.

**ID auto-assignment**: when a requirement bullet has no `RNNN:` prefix, the parser
assigns the next available ID in document order. The assigned ID is recorded in
`state.db`; the source `prd.md` is not rewritten. Consider providing explicit IDs to
avoid drift.

**Verification field**: each `- ` item under `**Verification:**` is stored as a shell
command string in `Task.verification.commands`. Backticks around the command are stripped
by the parser — write `` - `pytest tests/` `` or `- pytest tests/` ; both are accepted.

---

## After Parsing

Once `fakoli-state prd parse` succeeds, the PRD status is `draft`. From there:

1. **Review and approve the PRD**: `fakoli-state prd review --approve` transitions the
   PRD from `draft` → `reviewed` → `approved`. The claims manager enforces this gate —
   no task can be claimed while the PRD is in `draft` or `reviewed` status.

2. **Generate and promote tasks**: `fakoli-state plan` promotes requirements and features
   into `proposed` tasks. `fakoli-state score` populates the six-dimension scores
   (complexity, parallelizability, context load, blast radius, review risk, agent
   suitability). `fakoli-state expand T001` breaks tasks with `complexity ≥ 4` into
   subtasks. `fakoli-state review tasks` promotes drafted tasks to `reviewed` and then
   `ready`.

3. **Claim and work**: only tasks in `ready` status can be claimed. Run
   `fakoli-state next` to find the highest-priority claimable task, then
   `fakoli-state claim T001` to acquire an exclusive lease. The claim auto-creates an
   `agent/t001-<slug>` branch. Evidence submitted via `fakoli-state submit` releases the
   claim automatically.

The full workflow is described in the spec at
[`docs/specs/2026-05-24-fakoli-state-v0.md`](specs/2026-05-24-fakoli-state-v0.md)
under "Data Flows".
