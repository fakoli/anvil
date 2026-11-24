"""Drift-prevention tests for the docs site's landing page.

``docs/index.md`` is hand-written HTML, so MkDocs does not validate its links,
and it quotes real CLI output that the terminal replay also plays from
``docs/demo/*.json``. The page it replaced went stale in exactly these ways
(tool counts that disagreed with each other, links to renamed pages), so these
tests pin the page to what exists:

* every relative link resolves to a docs source file, and every ``#anchor``
  to an id on the page;
* the page carries no version numbers (they go stale; reference pages own them);
* every line of output quoted in a ``data-recorded`` sample appears verbatim in
  the recorded session, and every quoted command was really run;
* the recorded session is well formed and leaks no machine-specific path;
* the search-scope hook classifies paths as documented.

Layout note: this file lives at ``<repo-root>/tests/``, so ``parents[1]`` is
the repo root (matching ``test_docs_rosters.py``).
"""

from __future__ import annotations

import html
import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _docs() -> Path:
    return _repo_root() / "docs"


def _landing() -> str:
    return (_docs() / "index.md").read_text(encoding="utf-8")


def _recorded(name: str) -> list[dict]:
    return json.loads((_docs() / "demo" / name).read_text(encoding="utf-8"))


def _source_for(url_path: str) -> Path | None:
    """Map a built-site URL path (``how-to/x/``) back to its docs source."""
    docs = _docs()
    clean = url_path.strip("/")
    if not clean:
        return docs / "index.md"
    direct = docs / clean
    if direct.is_file():
        return direct
    for candidate in (docs / f"{clean}.md", direct / "index.md", direct / "README.md"):
        if candidate.is_file():
            return candidate
    return None


# --- 1. Links ---------------------------------------------------------------


def test_landing_relative_links_resolve_to_docs_sources():
    page = _landing()
    refs = set(re.findall(r'\s(?:href|src|data-src|data-fallback-href)="([^"]+)"', page))
    relative = {r for r in refs if not re.match(r"(?:https?:|mailto:|#)", r)}
    assert relative, "expected the landing page to link into the docs"
    missing = sorted(r for r in relative if _source_for(r.split("#")[0]) is None)
    assert not missing, f"landing page links to pages that do not exist: {missing}"


def test_landing_anchor_links_match_ids_on_the_page():
    page = _landing()
    ids = set(re.findall(r'\sid="([^"]+)"', page))
    anchors = set(re.findall(r'\shref="#([^"]+)"', page))
    assert anchors <= ids, f"anchor links without a target: {sorted(anchors - ids)}"


# --- 2. No numbers that go stale ---------------------------------------------


def test_landing_states_no_version_number():
    # Strip the recorded samples: event ids and proof file names are data.
    prose = re.sub(r"<pre>.*?</pre>", "", _landing(), flags=re.S)
    found = re.findall(r"\bv?\d+\.\d+\.\d+\b", prose)
    assert not found, f"version numbers on the landing page go stale: {found}"


# --- 3. Quoted output is the recorded output ---------------------------------


def _samples() -> list[str]:
    blocks = re.findall(
        r'<figure class="sample" data-recorded>.*?<pre>(.*?)</pre>', _landing(), flags=re.S
    )
    return [html.unescape(re.sub(r"<[^>]+>", "", block)) for block in blocks]


def test_landing_samples_quote_the_recording_verbatim():
    steps = _recorded("session.json") + _recorded("strict-gate.json")
    commands = {step["cmd"] for step in steps}
    output_lines = {line.strip() for step in steps for line in step["output"].splitlines()}

    samples = _samples()
    assert samples, "expected data-recorded samples on the landing page"
    for sample in samples:
        for line in filter(None, (raw.strip() for raw in sample.splitlines())):
            if line.startswith("$ "):
                assert line[2:] in commands, f"quoted command was never recorded: {line}"
            else:
                assert line in output_lines, f"quoted output is not in the recording: {line}"


# --- 4. The recording itself --------------------------------------------------


def test_recorded_session_is_well_formed_and_portable():
    session = _recorded("session.json")
    strict = _recorded("strict-gate.json")
    for step in session + strict:
        assert {"act", "label", "comment", "cmd", "exit", "output"} <= step.keys()
        assert isinstance(step["exit"], int)
        for leaked in ("/home/", "/Users/", "/tmp/", "\x1b["):
            assert leaked not in step["output"], f"machine-specific text in {step['label']!r}"

    # The story the page tells: a second claim and a no-evidence approval are
    # refused, and the strict gate refuses approval without the proof.
    refused = [step for step in session if step["exit"] != 0]
    assert len(refused) >= 2
    assert any("cannot be claimed" in step["output"] for step in refused)
    assert strict[-1]["exit"] != 0 and "REFUSED" in strict[-1]["output"]
    assert any("replay integrity verified" in step["output"] for step in session)


# --- 5. Search scope hook -----------------------------------------------------


def _hooks():
    path = _repo_root() / "scripts" / "mkdocs_hooks.py"
    spec = importlib.util.spec_from_file_location("anvil_mkdocs_hooks", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_search_scope_classifies_paths():
    hooks = _hooks()
    assert hooks.search_metadata("archive/phase-9-backlog.md") == {"exclude": True}
    assert hooks.search_metadata("_positioning.md") == {"exclude": True}
    assert hooks.search_metadata("backlog/anvil-backlog.prd.md") == {"exclude": True}
    assert hooks.search_metadata("specs/jev-integration/README.md")["boost"] < 1
    assert hooks.search_metadata("how-to/getting-started.md")["boost"] > 1
    assert hooks.search_metadata("cli-reference.md") == {}


def test_search_scope_keeps_explicit_front_matter():
    hooks = _hooks()
    page = SimpleNamespace(
        file=SimpleNamespace(src_uri="archive/README.md"), meta={"search": {"exclude": False}}
    )
    assert hooks.on_page_markdown("body", page=page, config=None, files=None) == "body"
    assert page.meta["search"] == {"exclude": False}

    plain = SimpleNamespace(file=SimpleNamespace(src_uri="how-to/web-interface.md"), meta={})
    hooks.on_page_markdown("body", page=plain, config=None, files=None)
    assert plain.meta["search"]["boost"] > 1
