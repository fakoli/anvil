# Issue #196 Resolution Plan — Release/Skill Compatibility

**Issue:** [#196 — v0.6.0 PRD skill requires unreleased `prd source-name` command](https://github.com/fakoli/anvil/issues/196)

**Prepared:** 2026-08-03

**Priority:** Release blocker

**Recommended delivery:** one compatibility PR followed immediately by the `0.6.1` release

## Testable behaviors

The fix is complete only when all of these behaviors hold:

1. A plugin/skill labeled `0.6.1` can use every `anvil` command and flag it cites against the published `anvil-state 0.6.1` wheel.
2. `anvil prd source-name --prd CON` returns the CLI-selected portable relative name; skills never derive named-PRD filenames themselves.
3. If an older CLI lacks `prd source-name`, each affected skill fails before any PRD write or parse and gives a directional upgrade message. It must not fall back to a lower-case-only filename guess.
4. CI builds a wheel, installs it into an isolated environment, obtains the installed command manifest, and checks the release-tree skills against that installed artifact. Importing the source checkout is not sufficient evidence.
5. The artifact check refuses an empty or malformed CLI manifest. This is required because the published `0.6.0` `anvil describe --json` reports `cli.count: 0`.
6. The publish workflow refuses a release when the Git tag, Python package version, plugin manifests, installed `anvil --version`, or skill/CLI contract disagree.
7. A post-release source checkout identifies its commit/build provenance in `anvil --version` and `anvil describe --json`, so it cannot be mistaken for the immutable release artifact with the same base version.
8. A regression fixture proves that the current `source-name` citation fails against the published `0.6.0` manifest and passes against the candidate `0.6.1` wheel.

## Confirmed current state

| Evidence | Result |
|---|---|
| Published CLI | `anvil 0.6.0 (schema 16)` rejects `anvil prd source-name` with `No such command 'source-name'`. |
| Current `main` | `9fe02c0` is 29 commits after tag `v0.6.0`; its source CLI implements `prd source-name` and still prints `anvil 0.6.0 (schema 16)`. |
| Current plugin cache | The plugin directory labeled `0.6.0` is commit `9fe02c0`, so the same displayed version names a moving skill bundle and an older PyPI wheel. |
| Skill references | `skills/prd`, `start-prd`, `plan`, and `finish` require `prd source-name`. |
| Existing contract test | `tests/test_skill_cli_contract.py` passes because it imports the CLI from the same checkout as the skills. It cannot detect release-to-main skew. |
| Existing version test | `tests/test_version_sync.py` checks strings inside one checkout, not tag/artifact identity or compatibility with the published wheel. |
| Publish workflow | `.github/workflows/publish.yml` builds and publishes directly; it does not install the wheel or validate it against the shipped skills before publication. |
| Repository policy | `CLAUDE.md` intentionally keeps the version unchanged between publishes, while marketplace installs can fetch newer default-branch content. This is the policy gap that made two incompatible artifacts look aligned. |

The `source-name` implementation and its portable/Windows-reserved-name tests already exist on `main`; this issue does not require another path algorithm.

## Delivery decision

Ship `0.6.1` as the smallest release that makes the published CLI catch up to the already-distributed skill contract. Do not solve this by weakening portable-name safety or by teaching agents to construct `prds/<id>.md`.

At the same time, make command-contract changes release-coupled:

- A PR that adds or changes a skill-required CLI command/flag must bump the candidate version and pass the built-artifact compatibility job.
- Such a compatibility PR is published immediately after merge; unrelated implementation commits do not require a version bump.
- Development/source builds retain the base semantic version but add visible Git provenance. Release-tag wheels remain the plain stable version.
- A skill performs a direct capability check before its first use of a newly required command and fails safely with the installed and required versions when the capability is absent.

This keeps the normal patch-release policy while preventing an unreleased command contract from masquerading as the last stable artifact.

## Implementation plan

### 1. Add an artifact-driven skill/CLI contract checker

Refactor the parsing and command/flag validation logic from `tests/test_skill_cli_contract.py` into a reusable, stdlib-only checker, for example `scripts/check_skill_cli_contract.py`.

The checker must accept:

- a skills/document root (`skills/*/SKILL.md` plus `AGENTS.md`);
- a CLI manifest captured from a separately installed `anvil describe --json`;
- optional fixture manifests for negative regressions.

It must fail on unknown command paths, unknown flags, an empty command list, duplicate/malformed entries, or a manifest version that differs from the expected release version. Keep the existing source-tree unit tests as fast coverage of the parser itself.

Add `tests/test_release_artifact_contract.py` with at least these cases:

- the published `0.6.0` fixture rejects the `prd source-name` citation;
- the live/candidate manifest accepts it;
- an empty manifest is a hard failure;
- a fabricated flag remains a hard failure;
- Windows-reserved and uppercase PRD IDs remain covered by the existing CLI tests.

### 2. Gate pull requests and publishing on the built wheel

Add a packaging-compatibility job to `.github/workflows/ci.yml` on Ubuntu and Windows:

1. Build the wheel from `bin/`.
2. Create a clean temporary environment with no checkout path on `PYTHONPATH`.
3. Install only that wheel and its declared dependencies.
4. Capture `anvil --version` and `anvil describe --json` from the installed console script.
5. Run the reusable checker against the repository skills and `AGENTS.md`.

Extend `.github/workflows/publish.yml` so publication happens only after the same check. The publish job must also:

- check out the exact release tag (manual dispatch must require an explicit release ref);
- require `v<package-version>` to equal the tag;
- verify all plugin/packaging manifests match that version;
- build both sdist and wheel;
- install and smoke-test the wheel in isolation;
- reject an empty CLI manifest;
- run the skill/CLI contract check;
- publish only after every preflight passes.

Keep Trusted Publishing unchanged; this work strengthens the inputs to the existing publish step.

### 3. Make source and release identities distinguishable

Add bounded build provenance to the version surfaces:

- release-tag wheel: `anvil 0.6.1 (schema 16)`;
- source checkout after that tag: include the short commit and tag distance, for example `anvil 0.6.1+29.g9fe02c0 (schema 16)`;
- `anvil describe --json`: retain `engine_version` and add structured `build_kind`, `commit`, and `tag_distance` fields.

The implementation must be deterministic, tolerate a source archive without `.git`, never invoke Git on every normal command, and avoid leaking an absolute checkout path. Cache or generate the provenance once at build/import time and bound all subprocess output/failure handling.

Update `tests/test_version_sync.py` and release-helper coverage so:

- manifest/package base versions remain synchronized;
- a release-tag build is plain and exact;
- an ahead-of-tag checkout is visibly non-release;
- missing Git metadata produces an explicit `unknown`/archive build kind rather than falsely claiming an exact release.

### 4. Make the affected skills fail safe on old CLIs

Update `skills/prd/SKILL.md`, `skills/start-prd/SKILL.md`, `skills/plan/SKILL.md`, and `skills/finish/SKILL.md` to run a capability check before first use of `prd source-name`.

If unavailable, the skill must report:

- the executable actually invoked;
- its `anvil --version` output;
- that safe named-PRD path resolution requires `anvil-state >=0.6.1`;
- the normal upgrade command, `uv tool upgrade anvil-state`;
- that the user must restart/refresh the harness after the CLI/plugin upgrade.

It must not parse, write, infer a portable filename, or suggest deleting state. A temporary pinned bundled-CLI workaround may be shown only as an explicitly non-release recovery path.

### 5. Cut and qualify `0.6.1`

Use `scripts/release.py patch` so the package, plugin manifests, lockfile, changelog, and version-bearing docs move together. Amend the release helper and `CLAUDE.md` policy so any future skill-required CLI surface change cannot remain under the last stable identity.

Before creating the tag:

1. Run the targeted compatibility and version tests.
2. Build the candidate sdist/wheel and run the isolated artifact check on Windows and Linux.
3. Run repository Ruff, the full pytest suite, strict MkDocs build, and `git diff --check`.
4. Confirm GitHub CI is green and review the exact release diff.
5. Tag and publish `v0.6.1` only with the user's explicit release approval.
6. Install from PyPI in a fresh tool environment and repeat `--version`, `describe`, and `prd source-name --prd CON` smoke tests.
7. Refresh the marketplace/plugin, restart the harness, and rerun the originally blocked named-PRD workflow.

## Immediate unblock for the other session

Until `0.6.1` is published, the currently installed plugin bundle contains the required CLI at pinned commit `9fe02c0`:

```powershell
uv run --project 'C:\Users\sdoum\.codex\plugins\cache\anvil\anvil\0.6.0\bin' `
  python -m anvil.cli prd source-name --prd context-agentic-benchmarks
```

Observed result:

```text
prds/context-agentic-benchmarks.md
```

This is a temporary pinned-source workaround. It does not make the published PyPI `0.6.0` CLI compatible, and the plugin cache path must not be copied into product documentation.

## Coordination with issue #180

The active #180 claim is `autonomous-lifecycle-hardening:T001`, scoped to `state/sqlite.py`, `state/backend.py`, `test_schema_version.py`, and `test_sqlite.py`; it does not overlap the first #196 compatibility PR.

Later #180 tasks do overlap:

- `T002` may edit CLI version/schema diagnostics and `tests/test_cli.py`;
- `T003` may edit SessionStart skew reporting;
- `T005` owns upgrade-matrix documentation and `tests/test_install_manifests.py`.

Because #196 is blocking another build, merge the #196 compatibility/release PR before #180 claims `T002` or `T005`. Then rebase the #180 branch and let its broader schema-skew work consume the new version/provenance contract. Do not duplicate #180's state/schema error handling in #196, and do not edit its currently claimed files.

## Expected file scope

- `.github/workflows/ci.yml`
- `.github/workflows/publish.yml`
- `scripts/check_skill_cli_contract.py` (new)
- `scripts/release.py`
- `tests/test_release_artifact_contract.py` (new)
- `tests/test_skill_cli_contract.py`
- `tests/test_version_sync.py`
- `tests/test_release_helper.py`
- `bin/src/anvil/cli/__init__.py`
- `bin/src/anvil/cli/describe.py`
- `skills/{prd,start-prd,plan,finish}/SKILL.md`
- `CLAUDE.md`
- release-managed manifests, `CHANGELOG.md`, and version-bearing docs

No schema migration, state mutation, PRD filename-algorithm rewrite, or #180 lifecycle fix belongs in this issue.

## Verification commands

```powershell
uv run --project bin pytest `
  tests/test_skill_cli_contract.py `
  tests/test_release_artifact_contract.py `
  tests/test_version_sync.py `
  tests/test_release_helper.py -q

uv run --project bin pytest tests/test_cli.py `
  -k 'source_name or described_cli_surface or version' -q

Push-Location bin
uv run ruff check ..
Pop-Location
uv run --project bin pytest
uvx --with-requirements docs/requirements.txt mkdocs build --strict
git diff --check
```

The release-candidate artifact smoke must additionally run from isolated wheel installs on both Windows and Linux; source-tree test success alone is not acceptance evidence for #196.
