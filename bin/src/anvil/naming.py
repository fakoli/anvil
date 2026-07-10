"""Filesystem- and git-ref-safe rendering of task ids used as path components.

A task id like ``advise-and-defer:T005`` is a valid anvil id but a hostile
*path* component. On Windows/NTFS the ``:`` is an alternate-data-stream
separator, so ``packets/advise-and-defer:T005.md`` silently writes the content
into an ADS attached to a 0-byte file named ``advise-and-defer`` — unreadable by
its logical name (#105). The same ``:`` is illegal in a git refname (#108.1).

This is the ONE place that maps a task id to a safe single path component. It is
a leaf module (a sibling of ``clock`` and ``signing``) so every subsystem that
turns an id into a file or branch name can import it *downward* with no cycle —
which is why it does not live in ``cli._helpers`` (``sync`` and ``git_ops`` must
not depend upward on ``cli``). Adopted so far at the packet/proof filename sites
(``cli.packet_apply``) and the reconciliation read-back (``sync``); the git
branch and worktree name sites (``git_ops.branch`` / ``git_ops.worktree``) move
onto it in the follow-up that closes #108.1.
"""

from __future__ import annotations

import os
import re

# Env vars that identify ONE agent loop/session (shared across that loop's
# subprocesses, distinct between sibling loops). Lives here — the leaf naming
# module — so both cli._helpers (actor naming) and claims.manager (the
# same-actor/different-session fail-fast) import it downward with no cycle.
_SESSION_ENV_VARS = ("ANVIL_SESSION_ID", "CLAUDE_CODE_SESSION_ID")


def session_discriminator() -> str | None:
    """The per-loop session id, FULL length, or None when no session env is
    set. Identity comparisons (the same-actor/different-session fail-fast)
    must use the full value: truncation is lossy, and a user-pinned
    ANVIL_SESSION_ID with a shared prefix (loop-prod-1 / loop-prod-2, or any
    timestamp-ordered id) would collide and silently defeat the guard.
    Display/actor-suffix call sites slice for readability themselves."""
    for var in _SESSION_ENV_VARS:
        value = os.environ.get(var)
        if value and value.strip():
            return value.strip()
    return None


# The Windows-reserved filename set ``<>:"/\|?*`` plus C0 control characters.
# Every one is illegal in a Windows filename; ``:`` additionally opens an NTFS
# alternate data stream, and several are illegal in a git refname. Collapsing
# any run of them to a single ``-`` yields one component that is safe as BOTH a
# packet filename and a branch-name segment (resolved Decision on #105).
_UNSAFE_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def safe_path_component(task_id: str) -> str:
    """Return ``task_id`` reduced to a single filesystem/git-ref-safe component.

    Every run of Windows-reserved / control characters is replaced with a single
    ``-``. A bare id like ``T001`` is already path-safe and is returned
    unchanged, so existing packets and branches keep their names — the transform
    is backward-compatible and idempotent (its own output has no unsafe chars).

    >>> safe_path_component("T001")
    'T001'
    >>> safe_path_component("advise-and-defer:T005")
    'advise-and-defer-T005'
    """
    return _UNSAFE_PATH_CHARS.sub("-", task_id)
