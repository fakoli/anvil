"""Permanent guard against command/flag drift in agent-facing docs.

Skills and AGENTS.md tell agents exactly which `anvil <cmd> [--flags]` to run.
Three drift bugs keep recurring on these surfaces:

  - dead "Phase N pending" tables claiming a command does not exist after it ships,
  - fabricated flags (e.g. `anvil submit --evidence`, which is not a real flag), and
  - the `anvil review prd` vs `anvil prd review` subcommand swap.

This module builds the real CLI surface by importing the Typer app and walking the
underlying Click tree (no `--help` scraping, which is slow and wrap/locale fragile),
then asserts every `anvil ...` invocation cited in `skills/*/SKILL.md` + `AGENTS.md`
names a real command path and only real flags. It COMPLEMENTS
test_layout_assumptions.py (in-repo `.anvil/` paths + hook firing) and
test_skill_init_detection.py; it does not duplicate them.

Pure import + file read: no network, subprocess, or temp files. CI-safe and fast.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import click
import typer

from anvil.cli import app

# --- Source files ------------------------------------------------------------

_REPO = Path(__file__).resolve().parents[1]
_SKILLS = _REPO / "skills"
_AGENTS = _REPO / "AGENTS.md"
DOCS = sorted(_SKILLS.glob("*/SKILL.md")) + [_AGENTS]

# Citations that look like commands but are intentionally not real invocations.
# Each is documented (file:line + reason) in the design spec's allowlist.
ALLOWLIST_PHANTOM = {"decision", "start-prd"}  # honestly marked not-yet-shipped
ALLOWLIST_OUTPUT = {"for"}  # terminal-output example inside a fence, not a command
ALLOWLIST = ALLOWLIST_PHANTOM | ALLOWLIST_OUTPUT

# A command/subcommand token shape: lowercase, may contain hyphens.
_NAME = re.compile(r"[a-z][a-z-]+")
# Split a shell line at the first pipe/operator that is OUTSIDE quotes.
_SHELL_OP = re.compile(r"\s(?:\|\||\||&&|&|;)\s")


# --- Build the real CLI surface (once, at module load) -----------------------


def _build_surface():  # type: ignore[no-untyped-def]
    """Walk the Click command tree under the Typer app.

    Returns:
        top_commands: set of top-level command names.
        group_paths:  set of path tuples that are Click Groups.
        group_subs:   dict[group-path-tuple] -> set of subcommand names.
        leaf_flags:   dict[path-tuple] -> set of valid --flags for that leaf.
        root_flags:   set of root-level --flags.
    """
    root_cmd = typer.main.get_command(app)
    top_commands: set[str] = set()
    group_paths: set[tuple[str, ...]] = set()
    group_subs: dict[tuple[str, ...], set[str]] = {}
    leaf_flags: dict[tuple[str, ...], set[str]] = {}

    def _opt_flags(node: click.Command) -> set[str]:
        flags: set[str] = set()
        for p in node.params:
            if isinstance(p, click.Option):
                # primaries (--json, -q) + secondaries (--no-strict off-switch)
                for opt in list(p.opts) + list(p.secondary_opts):
                    if opt.startswith("--"):
                        flags.add(opt)
        return flags

    def _recurse(node: click.Command, path: list[str]) -> None:
        if isinstance(node, click.Group):
            if path:
                group_paths.add(tuple(path))
                group_subs[tuple(path)] = set(node.commands.keys())
            for name, sub in node.commands.items():
                _recurse(sub, path + [name])
        else:
            leaf_flags[tuple(path)] = _opt_flags(node)
            if len(path) == 1:
                top_commands.add(path[0])

    for name, sub in root_cmd.commands.items():
        # Every top-level name is a valid command, whether a leaf or a group.
        top_commands.add(name)
        _recurse(sub, [name])

    root_flags = _opt_flags(root_cmd)
    return top_commands, group_paths, group_subs, leaf_flags, root_flags


TOP_COMMANDS, GROUP_PATHS, GROUP_SUBS, LEAF_FLAGS, ROOT_FLAGS = _build_surface()


def _group_union(group_path: tuple[str, ...]) -> set[str]:
    """Union of flags across every leaf under a group path.

    A group invoked with flags but no recognized subcommand (e.g. `anvil sync
    --fix`) carries its subcommands' flags, so validate against their union.
    """
    union: set[str] = set()
    for path, flags in LEAF_FLAGS.items():
        if len(path) > len(group_path) and path[: len(group_path)] == group_path:
            union |= flags
    return union


# --- Parse agent-facing docs for `anvil ...` citations -----------------------


def _code_segments(text: str):
    """Yield (lineno, segment) for code-context segments only: fenced-block lines
    and inline spans.

    Prose ("anvil coordinates work", "anvil knows without") is never a command,
    so we never look at raw prose lines outside code contexts. The lineno lets
    findings cite the exact source line.
    """
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), 1):
        if re.match(r"^\s*```", line):
            in_fence = not in_fence
            continue
        if in_fence:
            yield lineno, line
        else:
            for span in re.findall(r"`([^`\n]+)`", line):
                yield lineno, span


def _invocations(segment: str):
    """Yield token lists for each `anvil ...` invocation in a code segment."""
    for m in re.finditer(r"\banvil[ \t]+", segment):
        sub = segment[m.start():]
        # cut at the first out-of-quote shell pipe/operator
        sub = _SHELL_OP.split(sub)[0]
        sub = sub.rstrip().rstrip("\\").strip()
        if not sub:
            continue
        try:
            tokens = shlex.split(sub, posix=True)
        except ValueError:
            # unbalanced quotes: fall back, drop quote-bearing tokens
            tokens = [t for t in sub.split() if "'" not in t and '"' not in t]
        if tokens and tokens[0] == "anvil":
            yield tokens


def _check(tokens: list[str]) -> list[str]:
    """Validate one tokenized `anvil ...` invocation; return finding strings."""
    rest = list(tokens[1:])  # drop "anvil"
    if not rest:
        return []  # bare "anvil"
    cmd = rest[0]
    if not _NAME.fullmatch(cmd):
        return []  # placeholder (<cmd>) or root flag (--version): not the bug class
    if cmd in ALLOWLIST:
        return []
    if cmd not in TOP_COMMANDS:
        return ["unknown command: anvil " + cmd]

    path = [cmd]
    rest = rest[1:]
    if (cmd,) in GROUP_PATHS:
        subs = GROUP_SUBS[(cmd,)]
        if rest and rest[0] in subs:
            path.append(rest.pop(0))
            valid = LEAF_FLAGS[tuple(path)]
        elif rest and _NAME.fullmatch(rest[0]):
            return [
                "unknown subcommand: anvil "
                + cmd
                + " "
                + rest[0]
                + " (valid: "
                + ",".join(sorted(subs))
                + ")"
            ]
        else:
            valid = _group_union((cmd,))
    else:
        valid = LEAF_FLAGS[(cmd,)]
    valid = valid | {"--help"}

    findings: list[str] = []
    for t in rest:
        if t.startswith("[") or not t.startswith("--") or len(t) <= 2:
            continue  # positional / optional-syntax placeholder / value
        base = t.split("=", 1)[0]
        for fl in base.split("/"):  # expand slash-shorthand --add/--remove
            if fl.startswith("--") and fl not in valid:
                findings.append("unknown flag " + fl + " for: anvil " + " ".join(path))
    return findings


def _scan(kinds: tuple[str, ...]) -> list[str]:
    """Collect findings of the given kinds across every doc/segment/invocation."""
    out: list[str] = []
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        rel = doc.relative_to(_REPO)
        lines = text.splitlines()
        for lineno, seg in _code_segments(text):
            for tokens in _invocations(seg):
                for finding in _check(tokens):
                    if finding.startswith(kinds):
                        src = lines[lineno - 1].strip()
                        out.append(f"{rel}:{lineno}: {finding}\n    -> {src}")
    return out


# --- Tests -------------------------------------------------------------------


def test_surface_loaded() -> None:
    """Guard against a silently empty/vacuous surface (broken introspection)."""
    assert len(TOP_COMMANDS) > 20, f"only {len(TOP_COMMANDS)} top commands found"
    assert ("submit",) in LEAF_FLAGS, "submit leaf missing from surface"
    assert "--evidence" not in LEAF_FLAGS[("submit",)], (
        "submit unexpectedly has --evidence; the documented fabricated flag is real?"
    )
    assert "--version" in ROOT_FLAGS, "root --version flag missing from surface"


def test_validator_catches_known_bugs() -> None:
    """Durable proof the guard bites: synthetic bad cases flag, good cases pass."""
    # Fabricated flag.
    assert _check(["anvil", "submit", "T1", "--evidence", "x"]), (
        "validator failed to flag the fabricated --evidence flag"
    )
    # Synthetic bogus command + bogus flag.
    assert _check(["anvil", "totally-not-a-command"]), (
        "validator failed to flag a bogus command"
    )
    assert _check(["anvil", "submit", "T1", "--not-a-real-flag"]), (
        "validator failed to flag a bogus flag"
    )
    # review/prd swap -> unknown subcommand.
    swap = _check(["anvil", "review", "prd", "approve"])
    assert swap and "unknown subcommand" in swap[0], (
        "validator failed to flag the review-prd vs prd-review swap"
    )
    # Correct usages report nothing.
    assert _check(["anvil", "prd", "review", "--approve"]) == []
    assert (
        _check(["anvil", "submit", "T1", "--commands", "x", "--files-changed", "y"])
        == []
    )
    # Group + flag with no subcommand validates against the union.
    assert _check(["anvil", "sync", "--fix"]) == []
    # Quoted value must not leak inner flags.
    assert (
        _check(["anvil", "submit", "T012", "--commands", "pytest -x"]) == []
    ), "shlex value leaked an inner flag"


def test_phantom_commands_are_not_real() -> None:
    """Self-policing guard for the dead-table direction: if an allowlisted phantom
    actually ships, the docs that call it pending are now stale; fail loudly so we
    fix the line and drop the allowlist entry."""
    for name in ALLOWLIST_PHANTOM:
        assert name not in TOP_COMMANDS, (
            "anvil "
            + name
            + " now ships but docs still call it pending / not-a-CLI-command; "
            "fix the stale line and remove it from ALLOWLIST_PHANTOM."
        )


def test_every_cited_command_exists() -> None:
    """Every `anvil <cmd>` / `anvil <group> <sub>` cited in skills + AGENTS.md
    must name a real command path."""
    findings = _scan(("unknown command", "unknown subcommand"))
    assert not findings, (
        "Agent-facing docs cite anvil commands that do not exist (command/flag "
        "drift). Fix the citation to a real command path:\n  "
        + "\n  ".join(findings)
    )


def test_every_cited_flag_exists() -> None:
    """Every `--flag` cited on an `anvil` invocation in skills + AGENTS.md must be
    a real flag for that command (catches fabricated flags like submit --evidence)."""
    findings = _scan(("unknown flag",))
    assert not findings, (
        "Agent-facing docs cite anvil flags that do not exist for the command "
        "(fabricated-flag drift). Fix the citation to a real flag:\n  "
        + "\n  ".join(findings)
    )
