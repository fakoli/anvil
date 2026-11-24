"""MkDocs build hooks for the docs site (registered in ``mkdocs.yml`` › ``hooks``).

Search scope
------------
``docs/`` also holds maintainer material that is kept for provenance: archived
plans, market research, backlogs, specs and postmortems. Those pages are still
published, but they should not outrank the user guides when a newcomer searches
the site for "install". This hook sets the search plugin's per-page metadata
from the page path:

* excluded from the index: superseded or internal reference material;
* demoted: contributor planning and design records;
* promoted: the how-to guides, the FAQ and the glossary.

A page that sets ``search:`` in its own front matter keeps what it set.
"""

from __future__ import annotations

from fnmatch import fnmatch
from typing import Any

_EXCLUDED = (
    "archive/*",
    "research/*",
    "_positioning.md",
    "backlog/*.prd.md",
)
_DEMOTED = (
    "plans/*",
    "specs/*",
    "decisions/*",
    "design/*",
    "postmortems/*",
    "findings/*",
    "backlog/*",
    "tech-debt-backlog.md",
    "critic-false-pass-baseline.md",
    "live-tests.md",
)
_PROMOTED = (
    "how-to/*",
    "faq.md",
    "glossary.md",
)

_DEMOTED_BOOST = 0.2
_PROMOTED_BOOST = 2


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch(path, pattern) for pattern in patterns)


def search_metadata(path: str) -> dict[str, Any]:
    """Return the ``search`` metadata implied by a docs-relative page path."""
    if _matches(path, _EXCLUDED):
        return {"exclude": True}
    if _matches(path, _DEMOTED):
        return {"boost": _DEMOTED_BOOST}
    if _matches(path, _PROMOTED):
        return {"boost": _PROMOTED_BOOST}
    return {}


def on_page_markdown(markdown: str, *, page: Any, **_: Any) -> str:
    """Apply the path-based search metadata without overriding front matter."""
    implied = search_metadata(page.file.src_uri)
    if implied:
        explicit = page.meta.get("search")
        merged = dict(implied)
        if isinstance(explicit, dict):
            merged.update(explicit)
        page.meta["search"] = merged
    return markdown
