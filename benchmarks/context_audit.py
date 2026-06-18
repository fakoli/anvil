#!/usr/bin/env python3
"""
context_audit.py — Measure the ALWAYS-ON context/token footprint of anvil.

"Measure before you market." anvil positions itself as context-frugal /
structurally immune to context bloat. This script MEASURES that honestly.

It counts the tokens that sit in an agent's context the moment anvil is
installed, BEFORE any skill is invoked or any tool is called:

  ALWAYS-ON (paid on every turn, every agent):
    - Agent descriptions     6 agent files' frontmatter `description`
                             (the "when to use" + <example> blocks live in the
                             agent registry / system prompt always-on)
    - Skill descriptions     8 skills' frontmatter `description:` ONLY
                             (skill BODIES load on demand — counted separately)
    - MCP tool schemas       22 MCP tools (name + description + JSON input
                             schema) as serialized over the wire by FastMCP
    - Hook injections        SessionStart hook stdout injected into context
    - Commands               slash-command frontmatter (none ship today)

  ON-DEMAND (progressive disclosure — NOT always-on):
    - Skill bodies           loaded only when the skill fires
    - Agent bodies           loaded only when the subagent is dispatched

Token counting uses tiktoken (cl100k_base) when available, else a chars/4
fallback. Run with:

    uv run --with tiktoken python benchmarks/context_audit.py

or plain `python3 benchmarks/context_audit.py` (chars/4 fallback).

The report is deterministic and re-runnable. Numbers in CONTEXT_AUDIT.md MUST
match what this script prints.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = PLUGIN_ROOT / "agents"
SKILLS_DIR = PLUGIN_ROOT / "skills"
HOOKS_DIR = PLUGIN_ROOT / "hooks"
COMMANDS_DIR = PLUGIN_ROOT / "commands"
BIN_DIR = PLUGIN_ROOT / "bin"


# ---------------------------------------------------------------------------
# Tokenizer (tiktoken if present, chars/4 fallback)
# ---------------------------------------------------------------------------
def _make_counter():
    try:
        import tiktoken  # type: ignore

        enc = tiktoken.get_encoding("cl100k_base")
        return ("tiktoken/cl100k_base", lambda s: len(enc.encode(s)))
    except Exception:
        # Deterministic chars/4 fallback (ceil division).
        return ("chars/4 (fallback)", lambda s: (len(s) + 3) // 4)


TOKENIZER_NAME, count_tokens = _make_counter()


# ---------------------------------------------------------------------------
# Frontmatter parsing (no PyYAML dependency)
# ---------------------------------------------------------------------------
def split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_block, body). Frontmatter is between the first two
    `---` lines. If absent, frontmatter is "" and body is the whole text."""
    if not text.startswith("---"):
        return "", text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not m:
        return "", text
    return m.group(1), m.group(2)


def extract_description(frontmatter: str) -> str:
    """Extract the `description` field value from a YAML frontmatter block.

    Handles three shapes that appear in anvil:
      1. Single-line:   description: some text
      2. Block scalar:  description: >    (folded, indented continuation lines)
                        description: |    (literal)
      3. Quoted single-line.

    Returns the raw description text (what the model actually reads). For block
    scalars we keep the indented body verbatim minus the indentation, which is
    the faithful representation of what lands in the registry/system prompt.
    """
    lines = frontmatter.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^description:\s*(.*)$", line)
        if not m:
            continue
        rest = m.group(1).strip()
        # Block scalar indicator?
        if rest in (">", "|", ">-", "|-", ">+", "|+"):
            block_lines: list[str] = []
            j = i + 1
            # Determine indentation from first non-blank continuation line.
            base_indent = None
            while j < len(lines):
                ln = lines[j]
                if ln.strip() == "":
                    block_lines.append("")
                    j += 1
                    continue
                indent = len(ln) - len(ln.lstrip())
                if base_indent is None:
                    base_indent = indent
                if indent < base_indent and ln.strip() != "":
                    # A dedented, non-blank line that is a new top-level key.
                    if re.match(r"^[A-Za-z_][\w-]*:", ln):
                        break
                block_lines.append(ln[base_indent:] if len(ln) >= base_indent else ln.lstrip())
                j += 1
            # Strip trailing blank lines.
            while block_lines and block_lines[-1] == "":
                block_lines.pop()
            return "\n".join(block_lines)
        # Inline value (possibly quoted).
        if (rest.startswith('"') and rest.endswith('"')) or (
            rest.startswith("'") and rest.endswith("'")
        ):
            rest = rest[1:-1]
        return rest
    return ""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Item:
    label: str
    tokens: int
    chars: int


@dataclass
class Category:
    name: str
    always_on: bool
    items: list[Item] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return sum(i.tokens for i in self.items)

    @property
    def chars(self) -> int:
        return sum(i.chars for i in self.items)


def measure(label: str, text: str) -> Item:
    return Item(label=label, tokens=count_tokens(text), chars=len(text))


# ---------------------------------------------------------------------------
# Category collectors
# ---------------------------------------------------------------------------
def collect_agent_descriptions() -> Category:
    """ALWAYS-ON: the full `description` of each agent (incl. <example> blocks)
    sits in the agent registry / system prompt for every turn."""
    cat = Category("Agent descriptions (registry)", always_on=True)
    for path in sorted(AGENTS_DIR.glob("*.md")):
        fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        desc = extract_description(fm)
        cat.items.append(measure(path.stem, desc))
    return cat


def collect_agent_bodies() -> Category:
    """ON-DEMAND: the agent system prompt (body after frontmatter) loads only
    when the subagent is actually dispatched."""
    cat = Category("Agent bodies (on dispatch)", always_on=False)
    for path in sorted(AGENTS_DIR.glob("*.md")):
        _, body = split_frontmatter(path.read_text(encoding="utf-8"))
        cat.items.append(measure(path.stem, body))
    return cat


def collect_skill_descriptions() -> Category:
    """ALWAYS-ON: only the skill `description` is registered up front; the body
    loads via progressive disclosure when the skill fires."""
    cat = Category("Skill descriptions (registry)", always_on=True)
    for path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        desc = extract_description(fm)
        cat.items.append(measure(path.parent.name, desc))
    return cat


def collect_skill_bodies() -> Category:
    """ON-DEMAND: skill body (everything after frontmatter) loads only when the
    skill is invoked."""
    cat = Category("Skill bodies (on invocation)", always_on=False)
    for path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        _, body = split_frontmatter(path.read_text(encoding="utf-8"))
        cat.items.append(measure(path.parent.name, body))
    return cat


def _mcp_tool_wire_schemas() -> list[dict] | None:
    """Return the list of MCP tool wire schemas exactly as FastMCP serializes
    them to the client (name + description + inputSchema). This is the
    ground-truth always-on payload Claude Code receives.

    Strategy: shell out to the plugin's own venv via `uv run` so we get the
    real FastMCP serialization. Returns None if the runtime is unavailable
    (e.g. uv not installed) so the caller can fall back to a static estimate.
    """
    helper = (
        "import json,asyncio\n"
        "from anvil.mcp_server import mcp\n"
        "async def m():\n"
        "    tools = await mcp.list_tools()\n"
        "    out=[]\n"
        "    for t in tools:\n"
        "        w=t.to_mcp_tool().model_dump(exclude_none=True, mode='json')\n"
        "        out.append({'name':w['name'],"
        "'description':w.get('description',''),"
        "'inputSchema':w.get('inputSchema',{})})\n"
        "    print(json.dumps(out))\n"
        "asyncio.run(m())\n"
    )
    try:
        proc = subprocess.run(
            ["uv", "run", "--quiet", "--project", str(BIN_DIR), "python", "-c", helper],
            capture_output=True,
            text=True,
            cwd=str(BIN_DIR),
            timeout=300,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    # stdout may carry sync logs before the JSON line; take the last JSON array.
    for line in reversed(proc.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def collect_mcp_schemas() -> tuple[Category, str]:
    """ALWAYS-ON: the 22 MCP tool schemas (name + description + input schema)
    are injected into context whenever the MCP server is connected.

    Each tool is measured as the COMPACT JSON wire form (no whitespace) — this
    is what the model actually pays for. We tokenize the per-tool JSON.
    """
    cat = Category("MCP tool schemas (22 tools)", always_on=True)
    schemas = _mcp_tool_wire_schemas()
    if schemas is None:
        note = (
            "MCP runtime unavailable (uv/venv not reachable). MCP schemas "
            "NOT measured this run — install uv and run from the plugin to "
            "include them. Reported MCP subtotal = 0."
        )
        return cat, note
    for s in schemas:
        wire = json.dumps(s, separators=(",", ":"), ensure_ascii=False)
        cat.items.append(measure(s["name"], wire))
    note = f"Measured live from FastMCP ({len(schemas)} tools)."
    return cat, note


def collect_hook_injection() -> tuple[Category, str]:
    """ALWAYS-ON: SessionStart hook stdout is injected into context at the
    start of every session. PreToolUse/PostToolUse hooks only emit on tool
    use (not always-on) and their stdout is transient, so they are noted but
    not counted as always-on baseline.

    We measure the SessionStart hook's actual stdout by running it in a
    NON-initialized context (the common install-day case) and, if possible,
    note the initialized-case shape. The not-initialized line is the
    deterministic always-on baseline.
    """
    cat = Category("Hook injection (SessionStart)", always_on=True)
    script = HOOKS_DIR / "detect-state.sh"
    note_lines: list[str] = []
    injected = ""
    if script.exists():
        try:
            # Run in a scratch dir with no .anvil so output is the
            # deterministic "not initialized" line. CLAUDE_PLUGIN_ROOT points
            # at the plugin so the script can locate its bin.
            import tempfile

            with tempfile.TemporaryDirectory() as td:
                proc = subprocess.run(
                    ["bash", str(script)],
                    capture_output=True,
                    text=True,
                    cwd=td,
                    env={
                        "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
                        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                    },
                    timeout=10,
                )
            injected = proc.stdout
            note_lines.append("SessionStart stdout measured (uninitialized project).")
        except Exception as exc:  # pragma: no cover
            note_lines.append(f"SessionStart hook run failed: {exc}; counted as 0.")
    else:
        note_lines.append("detect-state.sh missing; counted as 0.")
    cat.items.append(measure("SessionStart: detect-state.sh stdout", injected))
    note_lines.append(
        "PreToolUse/PostToolUse hook stdout is transient (fires on tool use, "
        "not always-on) and is excluded from the always-on baseline."
    )
    return cat, " ".join(note_lines)


def collect_commands() -> Category:
    """ALWAYS-ON: slash-command frontmatter descriptions, if any commands ship."""
    cat = Category("Command descriptions", always_on=True)
    if COMMANDS_DIR.is_dir():
        for path in sorted(COMMANDS_DIR.rglob("*.md")):
            fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
            desc = extract_description(fm)
            cat.items.append(measure(path.stem, desc))
    return cat


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def hr(char: str = "=", width: int = 72) -> str:
    return char * width


def fmt_row(label: str, tokens: int, width: int = 46) -> str:
    return f"  {label:<{width}.{width}} {tokens:>8,d} tok"


def main() -> int:
    always: list[Category] = []
    ondemand: list[Category] = []
    notes: list[str] = []

    always.append(collect_agent_descriptions())
    always.append(collect_skill_descriptions())
    mcp_cat, mcp_note = collect_mcp_schemas()
    always.append(mcp_cat)
    notes.append(f"MCP: {mcp_note}")
    hook_cat, hook_note = collect_hook_injection()
    always.append(hook_cat)
    notes.append(f"Hooks: {hook_note}")
    always.append(collect_commands())

    ondemand.append(collect_skill_bodies())
    ondemand.append(collect_agent_bodies())

    print(hr())
    print("  anvil — ALWAYS-ON CONTEXT FOOTPRINT AUDIT")
    print(hr())
    print(f"  Tokenizer: {TOKENIZER_NAME}")
    print(f"  Plugin root: {PLUGIN_ROOT}")
    print(hr())
    print()

    # ---- Always-on breakdown ----
    print("ALWAYS-ON (paid on every turn, before any skill/tool is used)")
    print(hr("-"))
    always_total = 0
    for cat in always:
        n = len(cat.items)
        print(f"\n{cat.name}  [{n} item{'s' if n != 1 else ''}]  ->  {cat.tokens:,} tok")
        for it in cat.items:
            print(fmt_row(it.label, it.tokens))
        always_total += cat.tokens
    print()
    print(hr("-"))
    print(f"  ALWAYS-ON SUBTOTALS:")
    for cat in always:
        pct = (cat.tokens / always_total * 100) if always_total else 0.0
        print(f"    {cat.name:<40.40} {cat.tokens:>8,d} tok  ({pct:4.1f}%)")
    print(hr("-"))
    print(f"  ALWAYS-ON GRAND TOTAL:{'':<19}{always_total:>8,d} tok")
    print(hr("-"))

    # ---- On-demand breakdown ----
    print()
    print("ON-DEMAND (progressive disclosure — NOT in baseline context)")
    print(hr("-"))
    for cat in ondemand:
        print(f"\n{cat.name}  [{len(cat.items)} items]  ->  {cat.tokens:,} tok")
        for it in cat.items:
            print(fmt_row(it.label, it.tokens))
    ondemand_total = sum(c.tokens for c in ondemand)
    print()
    print(hr("-"))
    print(f"  ON-DEMAND TOTAL (sum of all bodies):{'':<5}{ondemand_total:>8,d} tok")
    print(f"  (Worst case if EVERY body loaded at once; real usage loads 1-2.)")
    print(hr("-"))

    # ---- Top contributors (always-on only) ----
    print()
    print("TOP 10 ALWAYS-ON CONTRIBUTORS (single items)")
    print(hr("-"))
    all_always_items: list[tuple[str, str, int]] = []
    for cat in always:
        for it in cat.items:
            all_always_items.append((cat.name, it.label, it.tokens))
    all_always_items.sort(key=lambda r: -r[2])
    for i, (catname, label, tok) in enumerate(all_always_items[:10], 1):
        share = (tok / always_total * 100) if always_total else 0.0
        print(f"  {i:>2}. {tok:>6,d} tok ({share:4.1f}%)  {label}  [{catname.split(' (')[0]}]")
    print(hr("-"))

    # ---- Notes ----
    print()
    print("NOTES")
    print(hr("-"))
    for nt in notes:
        # wrap-ish
        print(f"  - {nt}")
    print(hr())

    # Machine-readable tail for cross-checking the written report.
    summary = {
        "tokenizer": TOKENIZER_NAME,
        "always_on_total": always_total,
        "always_on_by_category": {c.name: c.tokens for c in always},
        "ondemand_total": ondemand_total,
        "ondemand_by_category": {c.name: c.tokens for c in ondemand},
        "top_contributors": [
            {"label": l, "category": c.split(" (")[0], "tokens": t}
            for c, l, t in all_always_items[:10]
        ],
    }
    print()
    print("JSON_SUMMARY " + json.dumps(summary, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
