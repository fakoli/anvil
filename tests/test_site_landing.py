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
* maintainer working material is excluded from the build, the nav lists only
  published pages, and no published page links into excluded material (MkDocs
  reports such a link at info level only, so ``--strict`` does not catch it);
* the public roadmap stays one screen, with no tracker ids, dates or versions;
* the search-scope hook classifies paths as documented.

Layout note: this file lives at ``<repo-root>/tests/``, so ``parents[1]`` is
the repo root (matching ``test_docs_rosters.py``).
"""

from __future__ import annotations

import html
import importlib.util
import json
import posixpath
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


# --- 5. What the site publishes -------------------------------------------------


def _mkdocs_yml() -> str:
    return (_repo_root() / "mkdocs.yml").read_text(encoding="utf-8")


def _excluded_patterns() -> list[str]:
    block = re.search(r"^exclude_docs: \|\n((?:  \S.*\n)+)", _mkdocs_yml(), flags=re.M)
    assert block, "mkdocs.yml must keep its exclude_docs block"
    patterns = [line.strip() for line in block.group(1).splitlines()]
    # Only the two anchored forms are used (``/dir/`` and ``/file``), which is
    # what lets ``_is_excluded`` stay a prefix/equality check.
    assert all(p.startswith("/") and "*" not in p for p in patterns), patterns
    return patterns


def _is_excluded(src_uri: str) -> bool:
    path = "/" + src_uri
    return any(
        path.startswith(pattern) if pattern.endswith("/") else path == pattern
        for pattern in _excluded_patterns()
    )


def _published_pages() -> list[Path]:
    docs = _docs()
    return sorted(
        page for page in docs.rglob("*.md") if not _is_excluded(page.relative_to(docs).as_posix())
    )


def test_maintainer_material_is_excluded_from_the_build():
    for internal in (
        "archive/README.md",
        "backlog/anvil-backlog.md",
        "backlog/strategic-backlog.md",
        "findings/2026-07-05-openclaw-weak-runner.md",
        "plans/bake-off.md",
        "postmortems/2026-06-22-skill-init-loop.md",
        "research/competitor-issue-analysis.md",
        "specs/2026-05-24-anvil-v0.md",
        "_positioning.md",
        "tech-debt-backlog.md",
    ):
        assert _is_excluded(internal), f"{internal} would be published"
    for public in (
        "index.md",
        "how-to/getting-started.md",
        "cli-reference.md",
        "design.md",
    ):
        assert not _is_excluded(public), f"{public} would be dropped from the site"
    # A pattern that matches nothing is a typo or a leftover.
    stale = [p for p in _excluded_patterns() if not (_docs() / p.strip("/")).exists()]
    assert not stale, f"exclude_docs patterns that match nothing: {stale}"


def test_nav_lists_only_published_pages():
    nav = _mkdocs_yml().split("\nnav:\n", 1)[1]
    entries = re.findall(r":\s+(\S+\.md)\s*$", nav, flags=re.M)
    assert entries, "expected nav entries in mkdocs.yml"
    missing = sorted(e for e in entries if not (_docs() / e).is_file())
    assert not missing, f"nav entries without a source file: {missing}"
    hidden = sorted(e for e in entries if _is_excluded(e))
    assert not hidden, f"nav entries that are excluded from the build: {hidden}"


def test_published_pages_do_not_link_into_excluded_material():
    docs = _docs()
    broken: list[str] = []
    for page in _published_pages():
        src_uri = page.relative_to(docs).as_posix()
        text = re.sub(r"^```.*?^```", "", page.read_text(encoding="utf-8"), flags=re.S | re.M)
        for target in re.findall(r"\]\(([^)\s]+)", text):
            if re.match(r"(?:[a-z][a-z0-9+.-]*:|#)", target):
                continue
            resolved = posixpath.normpath(
                posixpath.join(posixpath.dirname(src_uri), target.split("#")[0])
            )
            if not resolved.startswith("..") and _is_excluded(resolved):
                broken.append(f"{src_uri} -> {target}")

    # The landing page links with built-site URLs in raw HTML.
    for ref in re.findall(r'\shref="([^"]+)"', _landing()):
        if re.match(r"(?:https?:|mailto:|#)", ref):
            continue
        source = _source_for(ref.split("#")[0])
        if source is not None and _is_excluded(source.relative_to(docs).as_posix()):
            broken.append(f"index.md -> {ref}")

    assert not broken, (
        "published pages link to material that is excluded from the build "
        f"(use an absolute GitHub URL instead): {broken}"
    )


def test_public_roadmap_is_one_screen_without_tracker_detail():
    # The item-level tracker lives in docs/backlog/roadmap.md and is not
    # published. The public page states outcomes only, so it cannot go stale
    # the way a dated, id-keyed tracker does.
    assert _is_excluded("backlog/roadmap.md")
    roadmap = (_docs() / "roadmap.md").read_text(encoding="utf-8")
    assert len(roadmap.splitlines()) <= 60, "the public roadmap should fit one screen"
    for section in ("## Now", "## Next", "## Later", "## Not planned"):
        assert section in roadmap, f"public roadmap is missing {section!r}"
    tracker_detail = re.findall(
        r"\b(?:P\d+B?-[A-Z0-9-]+|S[LW]?-?\d+|WF-\d+|B\d{2}|Phase \d+|Wave \d+|20\d\d-\d\d-\d\d)\b"
        r"|\bv?\d+\.\d+\.\d+\b|#\d+",
        roadmap,
    )
    assert not tracker_detail, (
        f"tracker ids, dates or versions on the public roadmap: {tracker_detail}"
    )


# --- 6. Search scope hook -----------------------------------------------------


def _hooks():
    path = _repo_root() / "scripts" / "mkdocs_hooks.py"
    spec = importlib.util.spec_from_file_location("anvil_mkdocs_hooks", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_search_scope_classifies_paths():
    hooks = _hooks()
    assert hooks.search_metadata("design/ledger-latency-validation.md")["boost"] < 1
    assert hooks.search_metadata("decisions/2026-07-19-behavioral-readiness.md")["boost"] < 1
    assert hooks.search_metadata("how-to/getting-started.md")["boost"] > 1
    assert hooks.search_metadata("faq.md")["boost"] > 1
    assert hooks.search_metadata("design.md") == {}
    assert hooks.search_metadata("cli-reference.md") == {}


def test_search_scope_keeps_explicit_front_matter():
    hooks = _hooks()
    page = SimpleNamespace(
        file=SimpleNamespace(src_uri="design/ledger-latency-validation.md"),
        meta={"search": {"boost": 1}},
    )
    assert hooks.on_page_markdown("body", page=page, config=None, files=None) == "body"
    assert page.meta["search"] == {"boost": 1}

    plain = SimpleNamespace(file=SimpleNamespace(src_uri="how-to/web-interface.md"), meta={})
    hooks.on_page_markdown("body", page=plain, config=None, files=None)
    assert plain.meta["search"]["boost"] > 1
