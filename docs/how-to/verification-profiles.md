# Frozen repository verification profiles

The core profile API resolves repository-owned verification instructions without
executing commands or installing a runner. Planning, canonical PRD selection,
claim/evidence integration, reviewer isolation and runner resource enforcement
are separate integration work; this API alone does not implement those gates.

Use `anvil-verification.toml` at the explicit repository root:

```toml
schema_version = 1

[profiles.full]
source_files = ["tools/verify.py"]

[profiles.full.platforms.linux]
commands = ["python tools/verify.py full"]
preflight_commands = ["python --version"]

[profiles.full.platforms.darwin]
commands = ["python tools/verify.py full"]

[profiles.full.platforms.windows]
commands = ["py tools/verify.py full"]
```

The repository must supply `tools/verify.py`. A disposable fixture can contain:

```python
import sys
assert sys.argv[1] == "full"
```

This small fixture demonstrates source binding only; real repositories own
meaningful checks, runtime prerequisites, timeouts and process cleanup.

A `VerificationProfileReference(name, platform, source_sha256)` pins the exact
manifest bytes with lowercase SHA-256. Platform selection is explicit: `linux`,
`darwin`, or `windows`; the resolver never substitutes the current host platform.
Changes to that reference belong in the canonical PRD revision. The binding
contains the reference, sorted immutable `(path, SHA-256)` source pairs (JSON
arrays), literal commands, preflight commands and a recomputed contract digest.
The digest uses canonical JSON under `anvil.verification-profile.v1\0`.

Core Python entrypoints in `anvil.verification_profiles`:

- `resolve_profile(project_root, reference)` returns an immutable binding.
- `materialize_verification(verification, project_root)` returns verification
  with additive literal commands and exact zero-exit command proofs.
- `require_profile_current(verification, project_root)` refuses missing,
  changed or unmaterialized bindings and returns `None` on success.

Pass an absolute repository root, never a HOME state directory or implicit
working directory. Existing explicit requirements remain in place. Preflight
commands are retained separately and cannot replace verification command proofs.
No-profile verification retains its original serialization. Existing bindings
are compared with fresh resolution and cannot be silently updated.

Schema 1 rejects unknown fields, duplicate TOML keys, invalid types, unsupported
platforms and duplicate entries. It allows 1–64 profiles with 1–64-character
ASCII names (`A–Z`, `a–z`, digits, dots, underscores and hyphens; an alphanumeric
first character), 1–32 unique source files, 1–16 commands per platform and 0–16
preflight commands. Commands are nonblank single lines of at most 1,024 UTF-8
bytes. Portable relative source paths are at most 1,024 UTF-8 bytes. The manifest
and each source are limited to 1 MiB; declared source bytes total at most 16 MiB.
Empty source files are valid. Absolute paths, traversal, symlinks/reparse points
in any component, nonregular files, read failures and observed identity/byte
changes are refused with value-safe `ProfileError.code` diagnostics.

`source_files` must explicitly cover runner entrypoints and any dependencies
whose bytes need protection. Hashing declared files cannot discover or prove
undeclared imports, tools, environment state or runtime dependencies. Resolution
checks a bounded filesystem snapshot; it does not lock files for future command
execution. The owner must revalidate at its mutation and execution boundaries.
Windows uses held handles that deny write/delete sharing during each read;
POSIX uses descriptor-relative no-follow traversal and identity checks.
