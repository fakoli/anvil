"""Drift-prevention tests for anvil's roster docs (skills, agents, MCP tools,
hooks) and a path-hygiene sweep across ``docs/``.

Each of these docs is a hand-maintained index over a directory or source file
that changes independently of the doc — a new skill directory, a new
``@mcp.tool``-registered function, or a new ``hooks.json`` entry can land
without anyone updating the corresponding reference doc. These tests catch
that drift the same way ``test_version_sync.py`` catches version drift and
``test_agents_md.py`` catches MCP-tool drift in ``AGENTS.md``: enumerate the
live source of truth, assert every entry is named in the doc, and (where the
doc states a headline count) assert the count matches too.

Layout note: this file lives at ``<repo-root>/tests/test_docs_rosters.py`` so
``parents[1]`` is the repo root (matching ``test_version_sync.py`` /
``test_agents_md.py`` / ``test_install_manifests.py``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _docs() -> Path:
    return _repo_root() / "docs"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- 1. Skills roster ------------------------------------------------------


def _skill_names() -> list[str]:
    """Every ``skills/*/SKILL.md`` directory name (the skill's frontmatter
    ``name:``, which anvil convention keeps equal to the directory name)."""
    skills_dir = _repo_root() / "skills"
    return sorted(
        p.parent.name for p in skills_dir.glob("*/SKILL.md")
    )


def test_every_skill_listed_in_skills_reference() -> None:
    """Every skill under skills/*/SKILL.md must be indexed in
    docs/skills-reference.md (matched via its ``Source: skills/<name>/SKILL.md``
    pointer, which is stable regardless of the prose heading's casing)."""
    doc_path = _docs() / "skills-reference.md"
    text = _read(doc_path)
    missing = [
        name
        for name in _skill_names()
        if f"skills/{name}/SKILL.md" not in text
    ]
    assert not missing, (
        f"docs/skills-reference.md is missing an entry (no "
        f"'skills/<name>/SKILL.md' source pointer found) for: {missing}. "
        "Add a section with 'Source: skills/<name>/SKILL.md' for each."
    )


def test_skills_reference_headline_count_matches_directory() -> None:
    """The '> anvil ships N skills' headline must equal the actual skill count."""
    doc_path = _docs() / "skills-reference.md"
    text = _read(doc_path)
    match = re.search(r"ships (\d+) skills?", text)
    assert match, (
        "docs/skills-reference.md is missing its '> anvil ships N skills' "
        "headline sentence — cannot verify the count."
    )
    headline_count = int(match.group(1))
    actual_count = len(_skill_names())
    assert headline_count == actual_count, (
        f"docs/skills-reference.md says 'anvil ships {headline_count} skills' "
        f"but skills/*/SKILL.md contains {actual_count}. Update the headline."
    )


# --- 2. Agents roster -------------------------------------------------------


def _agent_frontmatter() -> dict[str, dict[str, str]]:
    """Map agent name -> {'model': ..., 'color': ...} parsed from each
    agents/<name>.md frontmatter block."""
    result: dict[str, dict[str, str]] = {}
    for path in sorted((_repo_root() / "agents").glob("*.md")):
        text = _read(path)
        fm_match = re.search(r"^---\n(.*?)\n---\n", text, re.S)
        assert fm_match, f"{path} has no '---' frontmatter block"
        fm = fm_match.group(1)
        name_match = re.search(r"^name:\s*(\S+)", fm, re.M)
        model_match = re.search(r"^model:\s*(\S+)", fm, re.M)
        assert name_match, f"{path} frontmatter missing 'name:'"
        assert model_match, f"{path} frontmatter missing 'model:'"
        result[name_match.group(1)] = {"model": model_match.group(1)}
    return result


def _agent_doc_sections() -> dict[str, str]:
    """Map agent name -> its '### <name>' section body in
    docs/agents-reference.md (text up to the next '### ' heading)."""
    text = _read(_docs() / "agents-reference.md")
    sections: dict[str, str] = {}
    for match in re.finditer(r"^### (\S+)\n(.*?)(?=^### |\Z)", text, re.S | re.M):
        sections[match.group(1)] = match.group(2)
    return sections


def test_every_agent_listed_in_agents_reference() -> None:
    """Every agents/*.md agent must have a '### <name>' section in
    docs/agents-reference.md."""
    agents = _agent_frontmatter()
    sections = _agent_doc_sections()
    missing = [name for name in agents if name not in sections]
    assert not missing, (
        f"docs/agents-reference.md has no '### <name>' section for: {missing}. "
        "Add a per-agent reference section (see docs/agents-reference.md's "
        "existing '### planner' etc. sections for the shape)."
    )


def test_agent_model_in_doc_matches_frontmatter() -> None:
    """The 'model: X' shown in each agent's doc section must match the
    'model:' frontmatter value in agents/<name>.md — catches a doc that
    still says 'opus' after the agent was retargeted to 'haiku' (or vice
    versa)."""
    agents = _agent_frontmatter()
    sections = _agent_doc_sections()
    mismatches = []
    for name, fm in agents.items():
        section = sections.get(name)
        if section is None:
            continue  # already reported by test_every_agent_listed_in_agents_reference
        doc_model_match = re.search(r"model:\s*`?(\w+)", section)
        if doc_model_match is None:
            mismatches.append(f"{name}: doc section names no 'model: ...' value")
            continue
        doc_model = doc_model_match.group(1)
        fm_model = fm["model"]
        if doc_model != fm_model:
            mismatches.append(
                f"{name}: docs/agents-reference.md says model={doc_model!r}, "
                f"agents/{name}.md frontmatter says model={fm_model!r}"
            )
    assert not mismatches, "Agent model drift between doc and frontmatter:\n" + "\n".join(
        mismatches
    )


# --- 3. MCP tool roster ------------------------------------------------------


def _mcp_tool_names() -> list[str]:
    """Every function registered with ``@mcp.tool`` (with or without a
    ``(tags={...})`` argument) in mcp_server.py, parsed with a regex rather
    than importing the module — cheaper, and avoids pulling in the full
    anvil/FastMCP import graph just to enumerate names."""
    path = _repo_root() / "bin" / "src" / "anvil" / "mcp_server.py"
    text = _read(path)
    pattern = re.compile(
        r"@mcp\.tool(?:\([^)]*\))?\s*\n\s*(?:async\s+)?def\s+(\w+)"
    )
    names = pattern.findall(text)
    assert names, "regex found no @mcp.tool-registered functions — pattern drift?"
    return sorted(set(names))


def test_every_mcp_tool_listed_in_mcp_doc() -> None:
    """Every @mcp.tool-registered function name must appear in docs/mcp.md
    (as its '### `tool_name`' reference heading or otherwise)."""
    doc_path = _docs() / "mcp.md"
    text = _read(doc_path)
    missing = [name for name in _mcp_tool_names() if name not in text]
    assert not missing, (
        f"docs/mcp.md is missing a reference entry for these registered MCP "
        f"tools: {missing}. Add a '### `{{tool_name}}`' section."
    )


def test_mcp_doc_tool_count_matches_registrations() -> None:
    """Registered/default/planning MCP counts must match the live source split."""
    doc_path = _docs() / "mcp.md"
    text = _read(doc_path)
    actual_count = len(_mcp_tool_names())
    source = _read(_repo_root() / "bin" / "src" / "anvil" / "mcp_server.py")
    planning_count = len(
        re.findall(r"@mcp\.tool\(tags=\{PLANNING_TAG\}\)", source)
    )
    execution_count = actual_count - planning_count

    registered = re.search(r"server has (\d+)\s+registered tools", text)
    execution = re.search(r"exposes only the \*\*(\d+) execution", text)
    planning = re.search(r"other \*\*(\d+) planning tools", text)
    assert registered and execution and planning, "MCP count headlines are missing"
    assert int(registered.group(1)) == actual_count
    assert int(execution.group(1)) == execution_count
    assert int(planning.group(1)) == planning_count


def test_bundle_workflow_guide_covers_required_recovery_paths() -> None:
    text = _read(_docs() / "how-to" / "coordinating-a-bundle.md").lower()
    required_topics = {
        "coordinator-only workflow",
        "bounded delegation",
        "delegate stalls",
        "replan_required",
        "checkpoint",
        "supersession",
        "reconciliation",
        "adopting existing tasks without losing history",
        "model-neutral comparison protocol",
    }
    missing = sorted(topic for topic in required_topics if topic not in text)
    assert not missing, f"bundle workflow guide is missing topics: {missing}"


# --- 4. Hooks roster ---------------------------------------------------------


def _hook_dispatch_names() -> list[str]:
    """Every distinct 'anvil hook dispatch <name>' subcommand referenced in
    hooks/hooks.json (heartbeat is wired twice — PostToolUse on two matchers —
    so this dedupes)."""
    path = _repo_root() / "hooks" / "hooks.json"
    data = json.loads(_read(path))
    names: set[str] = set()
    for event_specs in data["hooks"].values():
        for event_spec in event_specs:
            for hook in event_spec["hooks"]:
                match = re.search(r"dispatch (\S+)", hook["command"])
                assert match, f"hook command has no 'dispatch <name>': {hook['command']!r}"
                names.add(match.group(1))
    assert names, "hooks/hooks.json has no dispatch-style hook commands"
    return sorted(names)


def test_every_hook_listed_in_hooks_reference() -> None:
    """Every 'anvil hook dispatch <name>' entry in hooks/hooks.json must be
    named in docs/hooks-reference.md (e.g. as '<name>.sh' in its per-hook
    section)."""
    doc_path = _docs() / "hooks-reference.md"
    text = _read(doc_path)
    missing = [name for name in _hook_dispatch_names() if name not in text]
    assert not missing, (
        f"docs/hooks-reference.md does not mention these hooks/hooks.json "
        f"dispatch targets: {missing}. Add a '### <name>.sh' section."
    )


# --- 5. Path hygiene ----------------------------------------------------


def test_no_local_user_paths_under_docs() -> None:
    """No doc under docs/ may leak a contributor's local '/Users/...'
    filesystem path — a copy-pasted absolute path from a local machine
    (CLAUDE.md: "no ... internal-only paths in commits"). This is a public
    repo, so historical snapshots get no carve-out here: exposure is
    exposure regardless of the doc's age.
    """
    offenders = []
    for path in sorted(_docs().rglob("*.md")):
        text = _read(path)
        if "/Users/" in text:
            offenders.append(str(path.relative_to(_repo_root())))
    assert not offenders, (
        f"Found '/Users/' (a local, machine-specific path) in: {offenders}. "
        "Replace with a repo-relative path or a generic placeholder."
    )
