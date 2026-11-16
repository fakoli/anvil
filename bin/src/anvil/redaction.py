"""Best-effort secret redaction for text anvil persists on the operator's behalf.

The evidence-capture hook stores command lines and output excerpts that later
reach ``events.jsonl`` at submit. Those strings routinely carry credentials
(``API_KEY=... pytest``, a bearer token echoed by a failing HTTP test, a
database URL with a password). :func:`redact_secrets` replaces the secret
*value* with :data:`REDACTED` and keeps the surrounding text, so the record
stays readable.

Contract:

* **Identity on no match** — text with nothing to redact is returned
  unchanged (the same ``str`` value), so records without secrets are
  byte-identical to what was written before redaction existed.
* **Idempotent** — redacting already-redacted text changes nothing.
* **Linear-ish and bounded** — every quantifier is bounded and every pattern
  is anchored on a boundary, so attacker-influenced command output cannot
  drive catastrophic backtracking.
* **Best effort** — this is pattern matching, not a guarantee. A secret in a
  shape not listed here is stored as-is.
"""

from __future__ import annotations

import re

__all__ = ["REDACTED", "redact_secrets"]

REDACTED = "[REDACTED]"

# Largest secret value a single pattern consumes. Longer runs are simply
# redacted up to this many characters; the bound exists for predictable cost.
_MAX_VALUE = 4096

_KEYWORD = r"(?:api[_-]?key|apikey|token|secret|password|passwd|credential)"

# A value: double-quoted, single-quoted, or a bare run up to a shell/markup
# delimiter. Quotes are consumed with the value.
_VALUE = (
    rf'(?:"[^"\n]{{0,{_MAX_VALUE}}}"'
    rf"|'[^'\n]{{0,{_MAX_VALUE}}}'"
    rf"|[^\s\"',;&|)<>]{{1,{_MAX_VALUE}}})"
)

# ``Authorization: Bearer <value>`` (also ``Basic`` / ``Token``), in a header
# line, a curl ``-H`` argument, or a JSON/dict rendering of one.
_AUTH_HEADER = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])"
    r"(authorization[\"']?\s{0,8}[:=]\s{0,8}[\"']?(?:bearer|basic|token)\s{1,8})"
    rf"([^\s\"']{{1,{_MAX_VALUE}}})"
)

# A bare ``Bearer <value>`` with no header name in front of it.
_BEARER = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])(bearer\s{1,8})"
    rf"(?!\[REDACTED\])([A-Za-z0-9._~+/=-]{{16,{_MAX_VALUE}}})"
)

# ``scheme://user:password@host`` — keep the scheme and user, drop the password.
_URL_USERINFO = re.compile(
    r"(?<![A-Za-z0-9+.-])([A-Za-z][A-Za-z0-9+.-]{0,31}://[^\s/:@]{0,256}:)"
    r"(?!\[REDACTED\]@)([^\s/@]{1,1024})(?=@)"
)

# ``NAME=value`` / ``NAME: value`` where NAME names a credential. ``=`` may be
# surrounded by blanks; ``:`` must be followed by a blank, so a pytest node
# id (``tests/test_token.py::test_x``) and a URL port never match. ``==`` is a
# comparison, not an assignment. The match may only start at the beginning of
# a name run, which keeps the scan linear on long dotted or dashed input.
_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])"
    rf"([A-Za-z0-9_.-]{{0,128}}{_KEYWORD}[A-Za-z0-9_.-]{{0,64}}[\"']?)"
    r"([ \t]{0,4}=(?!=)[ \t]{0,4}|:[ \t]{1,4})"
    rf"(?!\[REDACTED\])({_VALUE})"
)

# ``--token value`` / ``--api-key value`` / ``-password value``. The ``=value``
# spelling is an assignment and is handled above.
_FLAG = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])"
    rf"(--?(?:[a-z0-9]{{1,32}}[-_]){{0,4}}{_KEYWORD}s?(?:[-_][a-z0-9]{{1,32}}){{0,2}})"
    r"([ \t]{1,4})"
    rf"(?!-|\[REDACTED\])({_VALUE})"
)

# Provider token shapes that are secrets wherever they appear.
_TOKEN_SHAPES = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"sk-[A-Za-z0-9_-]{20,512}"
    r"|gh[pousr]_[A-Za-z0-9]{20,255}"
    r"|github_pat_[A-Za-z0-9_]{20,255}"
    r"|glpat-[A-Za-z0-9_-]{20,255}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,255}"
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r"|eyJ[A-Za-z0-9_-]{8,2048}\.eyJ[A-Za-z0-9_-]{8,4096}\.[A-Za-z0-9_-]{8,2048}"
    r")(?![A-Za-z0-9_-])"
)

_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----")


def _redact_private_key_blocks(text: str) -> str:
    """Replace PEM private-key blocks. Linear: each character is scanned once.

    A block whose END marker is missing (typically cut off by the excerpt
    limit) is redacted to the end of the text — never left half-visible.
    """
    if "PRIVATE KEY-----" not in text:
        return text
    parts: list[str] = []
    position = 0
    while True:
        begin = _PEM_BEGIN.search(text, position)
        if begin is None:
            parts.append(text[position:])
            break
        parts.append(text[position : begin.start()])
        parts.append(REDACTED)
        end = _PEM_END.search(text, begin.end())
        if end is None:
            break
        position = end.end()
    return "".join(parts)


def redact_secrets(text: str) -> str:
    """Return ``text`` with recognizable secret values replaced by ``[REDACTED]``.

    Covers PEM private-key blocks, ``Authorization`` / ``Bearer`` credentials,
    URL passwords, credential-named assignments and command-line flags, and
    well-known provider token shapes. Text with no match is returned unchanged.
    """
    if not text:
        return text
    text = _redact_private_key_blocks(text)
    text = _AUTH_HEADER.sub(lambda m: m.group(1) + REDACTED, text)
    text = _BEARER.sub(lambda m: m.group(1) + REDACTED, text)
    text = _URL_USERINFO.sub(lambda m: m.group(1) + REDACTED, text)
    text = _ASSIGNMENT.sub(lambda m: m.group(1) + m.group(2) + REDACTED, text)
    text = _FLAG.sub(lambda m: m.group(1) + m.group(2) + REDACTED, text)
    return _TOKEN_SHAPES.sub(REDACTED, text)
