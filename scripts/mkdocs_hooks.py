"""MkDocs build hooks for the docs site (registered in ``mkdocs.yml`` › ``hooks``).

Search scope
------------
Maintainer working material (backlogs, plans, specs, research, archives) is
kept out of the site altogether by ``exclude_docs`` in ``mkdocs.yml``, so it
never reaches the search index. What this hook ranks is the material that *is*
published: a newcomer who searches the site for "install" should meet the user
guides before a design record. It sets the search plugin's per-page metadata
from the page path:

* demoted: design records and performance notes written for contributors;
* promoted: the how-to guides, the FAQ and the glossary.

A page that sets ``search:`` in its own front matter keeps what it set.
"""

from __future__ import annotations

from fnmatch import fnmatch
from typing import Any

_DEMOTED = (
    "decisions/*",
    "design/*",
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
