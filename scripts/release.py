#!/usr/bin/env python3
"""anvil release helper — bump the version in lockstep across every pinned file.

A version bump touches four core files (enforced by tests/test_version_sync.py),
five per-harness packaging manifests (tests/test_install_manifests.py), the
CHANGELOG, and the user-facing version/schema examples in the docs — and it is
easy to miss one by hand. This script does all of it from one command, so a
release is `python3 scripts/release.py minor` instead of a grep-and-edit hunt.

Harness-agnostic by design: stdlib only, no `uv`/venv needed, so it runs the
same under Claude Code, Codex, or a plain shell — `python3 scripts/release.py …`.

Usage:
    python3 scripts/release.py <major|minor|patch|X.Y.Z> [options]

Options:
    --date YYYY-MM-DD   release date for the CHANGELOG (default: today)
    --dry-run           print every planned edit; change nothing
    --no-verify         skip the post-bump `uv run pytest` sync-test check
    -h, --help          this help

What it edits (relative to the repo root):
    core (version-synced):
        .claude-plugin/plugin.json
        bin/pyproject.toml
        bin/src/anvil/__init__.py
        bin/uv.lock
    packaging manifests (version-locked to anvil.__version__):
        packaging/codex/.codex-plugin/plugin.json
        packaging/codex/.agents/plugins/marketplace.json
        packaging/gemini/gemini-extension.json
        packaging/openclaw/plugin/openclaw.plugin.json
        packaging/openclaw/plugin/package.json
    CHANGELOG.md         promote the [Unreleased] block to [X.Y.Z] - <date>
    user-facing docs     README badge + "Beta" lines; the `anvil X.Y.Z (schema N)`
                         and current-version examples in getting-started,
                         cli-reference, and architecture — the schema number is
                         re-synced to state/schema.py's SCHEMA_VERSION.

Historical snapshots (docs/archive/**, benchmarks/RESULTS.md, older CHANGELOG
entries) are deliberately left untouched.
"""

from __future__ import annotations

import datetime
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

MANIFESTS = [
    "packaging/codex/.codex-plugin/plugin.json",
    "packaging/codex/.agents/plugins/marketplace.json",
    "packaging/gemini/gemini-extension.json",
    "packaging/openclaw/plugin/openclaw.plugin.json",
    "packaging/openclaw/plugin/package.json",
]

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


class ReleaseError(RuntimeError):
    pass


def _read(rel: str) -> str:
    p = REPO / rel
    if not p.exists():
        raise ReleaseError(f"expected file is missing: {rel}")
    return p.read_text(encoding="utf-8")


def current_version() -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', _read("bin/src/anvil/__init__.py"))
    if not m:
        raise ReleaseError("could not find __version__ in bin/src/anvil/__init__.py")
    return m.group(1)


def schema_version() -> int:
    m = re.search(r"SCHEMA_VERSION\s*:\s*int\s*=\s*(\d+)", _read("bin/src/anvil/state/schema.py"))
    if not m:
        raise ReleaseError("could not find SCHEMA_VERSION in state/schema.py")
    return int(m.group(1))


def next_version(current: str, spec: str) -> str:
    if _SEMVER.match(spec):
        return spec
    major, minor, patch = (int(x) for x in current.split("."))
    if spec == "major":
        return f"{major + 1}.0.0"
    if spec == "minor":
        return f"{major}.{minor + 1}.0"
    if spec == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ReleaseError(f"invalid version spec {spec!r} — use major|minor|patch|X.Y.Z")


class Editor:
    """Collects planned edits so --dry-run can preview before anything is written."""

    def __init__(self, dry_run: bool) -> None:
        self.dry_run = dry_run
        self.changes: list[str] = []
        self.skipped: list[str] = []

    def sub(
        self, rel: str, pattern: str, repl: str, *,
        required: bool = True, count: int = 0,
    ) -> None:
        text = _read(rel)
        new, n = re.subn(pattern, repl, text, count=count, flags=re.MULTILINE)
        if n == 0:
            msg = f"{rel}: pattern not found: {pattern}"
            if required:
                raise ReleaseError(msg + " — refusing to release with a missed file")
            self.skipped.append(msg)
            return
        if new == text:
            self.skipped.append(f"{rel}: already up to date ({pattern})")
            return
        self.changes.append(f"{rel}: {n} edit(s) [{pattern}]")
        if not self.dry_run:
            (REPO / rel).write_text(new, encoding="utf-8")

    def insert(self, rel: str, anchor: str, block: str) -> None:
        text = _read(rel)
        if block.strip() in text:
            self.skipped.append(f"{rel}: changelog section already present")
            return
        if anchor not in text:
            raise ReleaseError(f"{rel}: changelog anchor {anchor!r} not found")
        new = text.replace(anchor, anchor + block, 1)
        self.changes.append(f"{rel}: inserted changelog section")
        if not self.dry_run:
            (REPO / rel).write_text(new, encoding="utf-8")


def run(spec: str, date: str, dry_run: bool, verify: bool) -> int:
    cur = current_version()
    new = next_version(cur, spec)
    schema = schema_version()
    if new == cur:
        print(f"release: already at {cur} — nothing to do")
        return 0

    print(f"release: {cur} -> {new}  (schema {schema}, date {date})"
          + ("  [DRY RUN]" if dry_run else ""))

    ed = Editor(dry_run)
    old_q = re.escape(cur)

    # --- core + manifests: the version string on its declaring line ---
    ed.sub(".claude-plugin/plugin.json", rf'("version":\s*"){old_q}(")', rf"\g<1>{new}\g<2>")
    ed.sub("bin/pyproject.toml", rf'(^version\s*=\s*"){old_q}(")', rf"\g<1>{new}\g<2>", count=1)
    ed.sub("bin/src/anvil/__init__.py", rf'(__version__\s*=\s*"){old_q}(")', rf"\g<1>{new}\g<2>")
    ed.sub(
        "bin/uv.lock",
        rf'(\[\[package\]\]\nname = "anvil-state"\nversion = "){old_q}(")',
        rf"\g<1>{new}\g<2>",
        count=1,
    )
    for rel in MANIFESTS:
        ed.sub(rel, rf'("version":\s*"){old_q}(")', rf"\g<1>{new}\g<2>")

    # --- CHANGELOG: promote [Unreleased] to the new version ---
    ed.insert("CHANGELOG.md", "## [Unreleased]", f"\n\n## [{new}] - {date}")

    # --- user-facing docs (not test-enforced; easy to miss by hand) ---
    ed.sub("README.md", rf"(badge/version-){old_q}(-)", rf"\g<1>{new}\g<2>", required=False)
    ed.sub("README.md", rf"(Beta — v){old_q}\b", rf"\g<1>{new}", required=False)
    ed.sub("README.md", rf"(Beta \(v){old_q}(\))", rf"\g<1>{new}\g<2>", required=False)
    # `anvil X.Y.Z (schema N)` examples — bump BOTH the version and the schema
    # number so the docs match `anvil --version` (the exact drift this fixes).
    for rel in ("docs/how-to/getting-started.md", "docs/cli-reference.md"):
        ed.sub(
            rel, rf"anvil {old_q} \(schema \d+\)",
            f"anvil {new} (schema {schema})", required=False,
        )
    ed.sub("docs/architecture.md", rf"(\*\*v){old_q}(\*\*)", rf"\g<1>{new}\g<2>", required=False)

    # --- report ---
    print("\nchanges:")
    for c in ed.changes:
        print(f"  edit {c}")
    if ed.skipped:
        print("\nskipped (no-op / not applicable):")
        for s in ed.skipped:
            print(f"  · {s}")

    if dry_run:
        print("\nDRY RUN — nothing written. Re-run without --dry-run to apply.")
        return 0

    if verify:
        print("\nverifying version sync (uv run pytest) ...")
        try:
            r = subprocess.run(
                ["uv", "run", "pytest", "../tests/test_version_sync.py",
                 "../tests/test_install_manifests.py", "-q"],
                cwd=REPO / "bin", capture_output=True, text=True, timeout=300,
            )
            tail = (r.stdout or r.stderr).strip().splitlines()[-1:] or ["(no output)"]
            print(f"  {tail[0]}")
            if r.returncode != 0:
                print("release: sync tests FAILED — review the edits above", file=sys.stderr)
                return 1
        except FileNotFoundError:
            print("  uv not found — skipping verification (run the sync tests manually)")
        except subprocess.TimeoutExpired:
            print("  verification timed out — run the sync tests manually", file=sys.stderr)

    print(f"\nrelease: v{new} staged. Next: review the diff, commit, tag v{new}, "
          "and push per the repo's release steps.")
    return 0


def main(argv: list[str]) -> int:
    args = list(argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if args else 2
    spec = args.pop(0)
    date = datetime.date.today().isoformat()
    dry_run = False
    verify = True
    while args:
        a = args.pop(0)
        if a == "--dry-run":
            dry_run = True
        elif a == "--no-verify":
            verify = False
        elif a == "--date":
            if not args:
                print("release: --date needs a value", file=sys.stderr)
                return 2
            date = args.pop(0)
        else:
            print(f"release: unknown option {a!r}", file=sys.stderr)
            return 2
    try:
        return run(spec, date, dry_run, verify)
    except ReleaseError as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
