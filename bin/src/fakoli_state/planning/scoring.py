"""Rule-based six-dimension scoring engine — no LLM, no I/O.

Each dimension is scored 1-5 using pure heuristics derived from Task fields.
Optional LLM augmentation (Phase 7 Wave 2) is additive: if a provider is
supplied, an LLM-written one-paragraph trade-off summary is appended to the
``Score.explanation`` field.  The rule-based scores themselves are NEVER
modified by the LLM — augmentation is enrichment only.

Dimensions:
    complexity          — how hard is this task to implement?
    parallelizability   — can it run in parallel with other tasks?
    context_load        — how much context does the agent need to load?
    blast_radius        — how wide is the potential impact of a mistake?
    review_risk         — how carefully must this be reviewed?
    agent_suitability   — how suitable is this for a smaller/cheaper model?

All dimensions clamp to [1, 5].  score_all() returns new Task instances via
model_copy(update=...) so the caller's objects are never mutated.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import TYPE_CHECKING, NamedTuple

from fakoli_state.config import DEFAULT_AUTO_EXPAND_THRESHOLD
from fakoli_state.state.models import Score, Task

if TYPE_CHECKING:
    from fakoli_state.planning.llm import LLMProvider

__all__ = [
    "DEFAULT_RECURSION_DEPTH_CAP",
    "ExpansionCandidate",
    "RecursiveExpansionCandidate",
    "build_expansion_queue",
    "build_recursive_expansion_queue",
    "is_expanded",
    "score_task",
    "score_all",
    "suggested_subtask_count",
]

# ---------------------------------------------------------------------------
# LLM augmentation constants
# ---------------------------------------------------------------------------

_SCORE_EXPLAIN_SYSTEM_PROMPT = (
    "You are a senior engineer scoring a task on six dimensions. "
    "Given the rule-based scores and the task body, write 1-3 sentences "
    "explaining the trade-offs. Be concrete and terse — no marketing language."
)
_SCORE_EXPLAIN_MAX_TOKENS = 300

# ---------------------------------------------------------------------------
# Regex constants
# ---------------------------------------------------------------------------

# Files that touch shared infrastructure / config (blast_radius heuristic).
_SENSITIVE_FILE_RE = re.compile(
    r"(schema|migration|config\.|settings)", re.IGNORECASE
)

# Shared-infra path prefixes (blast_radius heuristic).
_SHARED_INFRA_PATH_RE = re.compile(r"\bsrc[\\/]", re.IGNORECASE)

# Public API surface files (blast_radius and review_risk heuristics).
_PUBLIC_API_FILE_RE = re.compile(
    r"(cli\.py|mcp_server\.py|__init__\.py)", re.IGNORECASE
)

# Complexity: keywords in description that indicate cross-cutting concern.
_COMPLEXITY_KEYWORDS_RE = re.compile(
    r"\b(refactor|redesign|migrate|migration|architecture)\b", re.IGNORECASE
)

# review_risk: security / auth / permission language in acceptance criteria.
_SECURITY_KEYWORDS_RE = re.compile(
    r"\b(security|auth|authentication|authoriz|permission)\b", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp(value: int, lo: int = 1, hi: int = 5) -> int:
    return max(lo, min(hi, value))


def _unique_dirs(files: list[str]) -> set[str]:
    """Return the set of parent directories for a list of file paths."""
    return {os.path.dirname(f) or "." for f in files}


# ---------------------------------------------------------------------------
# Per-dimension scorers — each returns (score, explanation_fragment)
# ---------------------------------------------------------------------------


class _Dim(NamedTuple):
    value: int
    explanation: str


def _score_complexity(task: Task) -> _Dim:
    base = 2
    reasons: list[str] = [f"base {base}"]
    files = task.likely_files
    nfiles = len(files)

    if nfiles >= 10:
        base = 4
        reasons = ["base 4 (>=10 files)"]
    elif nfiles >= 5:
        base = 4
        reasons = ["base 4 (>=5 files)"]

    word_count = len(task.description.split())
    if word_count >= 200:
        base += 1
        reasons.append("+1 (description >=200 words)")

    if _COMPLEXITY_KEYWORDS_RE.search(task.description):
        base += 1
        reasons.append("+1 (refactor/redesign/migrate/architecture keyword)")

    return _Dim(_clamp(base), f"complexity: {_clamp(base)} ({', '.join(reasons)})")


def _score_parallelizability(task: Task) -> _Dim:
    ndeps = len(task.dependencies)
    # Count how many conflict groups this task appears in (the field is a list
    # of group IDs, not cross-task membership — proxy: len(conflict_groups)).
    ngroups = len(task.conflict_groups)

    if ngroups >= 2:
        value = 1
        explanation = "parallelizability: 1 (in >=2 conflict groups)"
    elif ndeps == 0:
        value = 4
        explanation = "parallelizability: 4 (no dependencies)"
    elif ndeps <= 2:
        value = 3
        explanation = f"parallelizability: 3 ({ndeps} dependencies)"
    else:
        value = 2
        explanation = f"parallelizability: 2 (>=3 dependencies: {ndeps})"

    return _Dim(_clamp(value), explanation)


def _score_context_load(task: Task) -> _Dim:
    files = task.likely_files
    nfiles = len(files)

    if nfiles == 0:
        value = 5
        explanation = "context_load: 5 (0 known files — agent must discover)"
    elif nfiles == 1:
        value = 2
        explanation = "context_load: 2 (1 file)"
    else:
        ndirs = len(_unique_dirs(files))
        if ndirs > 1:
            value = 4
            explanation = (
                f"context_load: 4 (files span {ndirs} directories)"
            )
        else:
            value = 3
            explanation = (
                f"context_load: 3 ({nfiles} files in 1 directory)"
            )

    return _Dim(_clamp(value), explanation)


def _score_blast_radius(task: Task) -> _Dim:
    base = 2
    reasons: list[str] = [f"base {base}"]
    files = task.likely_files

    # Check for sensitive files first — this sets base to 5.
    has_sensitive = any(_SENSITIVE_FILE_RE.search(f) for f in files)
    if has_sensitive:
        base = 5
        reasons = ["base 5 (schema/migration/config/settings file)"]

    # Check shared infra paths.
    has_shared_infra = any(_SHARED_INFRA_PATH_RE.search(f) for f in files)
    if has_shared_infra and base < 5:
        base += 1
        reasons.append("+1 (src/ shared infra path)")

    # Check public API surface.
    has_public_api = any(_PUBLIC_API_FILE_RE.search(f) for f in files)
    if has_public_api:
        base += 1
        reasons.append("+1 (public API surface: cli.py / mcp_server.py / __init__.py)")

    return _Dim(_clamp(base), f"blast_radius: {_clamp(base)} ({', '.join(reasons)})")


def _score_review_risk(task: Task) -> _Dim:
    base = 2
    reasons: list[str] = [f"base {base}"]
    files = task.likely_files
    criteria_text = " ".join(task.acceptance_criteria)

    if _SECURITY_KEYWORDS_RE.search(criteria_text):
        base = 5
        reasons = ["base 5 (security/auth/permission in acceptance criteria)"]

    has_schema = any(_SENSITIVE_FILE_RE.search(f) for f in files)
    if has_schema:
        base += 1
        reasons.append("+1 (schema or migration file)")

    has_public_api = any(_PUBLIC_API_FILE_RE.search(f) for f in files)
    if has_public_api:
        base += 1
        reasons.append("+1 (CLI/MCP public surface)")

    return _Dim(_clamp(base), f"review_risk: {_clamp(base)} ({', '.join(reasons)})")


def _score_agent_suitability(complexity: int, blast_radius: int) -> _Dim:
    raw = 6 - complexity
    reasons: list[str] = [f"6 - complexity({complexity}) = {raw}"]
    if blast_radius >= 4:
        raw = min(raw, 2)
        reasons.append(f"capped at 2 (blast_radius={blast_radius} >= 4)")
    value = _clamp(raw)
    return _Dim(value, f"agent_suitability: {value} ({', '.join(reasons)})")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def score_task(
    task: Task,
    *,
    provider: LLMProvider | None = None,
) -> Score:
    """Compute a Score for task using rule-based heuristics.

    Pure function — does not mutate task.  Returns a fully-populated Score.

    When ``provider`` is supplied, the rule-based ``explanation`` is *augmented*
    with a 1-3 sentence trade-off summary written by the LLM.  The numeric
    scores themselves are never touched by the LLM (additive enrichment only).
    If the LLM call fails (``LLMProviderError``), a warning is written to
    stderr and the deterministic-only explanation is returned unchanged.

    Args:
        task: A Task (typically with scores=Score() / all None).
        provider: Optional LLM provider for explanation enrichment.

    Returns:
        A Score with all six dimensions populated and an explanation string.
    """
    complexity_dim = _score_complexity(task)
    parallelizability_dim = _score_parallelizability(task)
    context_load_dim = _score_context_load(task)
    blast_radius_dim = _score_blast_radius(task)
    review_risk_dim = _score_review_risk(task)
    agent_suitability_dim = _score_agent_suitability(
        complexity_dim.value, blast_radius_dim.value
    )

    rule_explanation = "\n".join([
        complexity_dim.explanation,
        parallelizability_dim.explanation,
        context_load_dim.explanation,
        blast_radius_dim.explanation,
        review_risk_dim.explanation,
        agent_suitability_dim.explanation,
    ])

    score = Score(
        complexity=complexity_dim.value,
        parallelizability=parallelizability_dim.value,
        context_load=context_load_dim.value,
        blast_radius=blast_radius_dim.value,
        review_risk=review_risk_dim.value,
        agent_suitability=agent_suitability_dim.value,
        explanation=rule_explanation,
    )

    if provider is not None:
        augmented = _augment_explanation(task, score, provider)
        if augmented is not None:
            score = score.model_copy(
                update={"explanation": rule_explanation + "\n\n" + augmented}
            )

    return score


def score_all(
    tasks: list[Task],
    *,
    provider: LLMProvider | None = None,
) -> list[Task]:
    """Score every task in the list, returning new Task instances.

    Args:
        tasks: A list of Task models (not mutated).
        provider: Optional LLM provider passed through to ``score_task``.

    Returns:
        A new list of Task instances with scores populated via model_copy.
    """
    return [
        task.model_copy(update={"scores": score_task(task, provider=provider)})
        for task in tasks
    ]


# ---------------------------------------------------------------------------
# Expansion queue (v1.21.0) — complexity score → auto-expansion loop
# ---------------------------------------------------------------------------

# Suggested-subtask envelope. Mirrors the expand engine's contract
# (``inference._EXPAND_MIN_SUBTASKS`` / ``_EXPAND_MAX_SUBTASKS``): the LLM is
# asked for 2-5 sub-tasks, so the deterministic suggestion never leaves that
# range either.
_SUGGESTED_SUBTASKS_MIN = 2
_SUGGESTED_SUBTASKS_MAX = 5


# v1.23.0 — recursive expand-to-threshold safety rails. The recursion walks
# the parent→child tree (``Task.parent_task_id``); ``DEFAULT_RECURSION_DEPTH_CAP``
# bounds how deep a single ``score`` run will surface descendants so the
# frontier always terminates even if a (malformed) cycle slips past the cycle
# guard. 3 was chosen to match the SKILL.md guidance ("repeated expansion of
# the same lineage is a sign the PRD block needs human restructuring"): two
# splits (parent → children → grandchildren) is the deepest auto-decomposition
# we trust without a human looking; a fourth level is almost always a scoring
# artifact, not real nested work.
DEFAULT_RECURSION_DEPTH_CAP = 3


class ExpansionCandidate(NamedTuple):
    """One task queued for sub-task expansion after scoring.

    Produced by :func:`build_expansion_queue` — *queue entries only*, never
    written to the backend by this module. The callers (CLI ``score``, MCP
    ``score_tasks``) render the queue; the LLM-side decomposition itself only
    happens when ``fakoli-state expand TASK_ID --use-llm`` runs.
    """

    task_id: str
    title: str
    complexity: int
    suggested_subtasks: int


class RecursiveExpansionCandidate(NamedTuple):
    """One task on the *recursive* expansion frontier (v1.23.0).

    Like :class:`ExpansionCandidate` but carries ``depth`` — the number of
    parent links between this task and the top-level root it descends from
    (0 = a top-level task with no parent; 1 = its child; …). The frontier
    only ever contains *leaf* tasks (a task that is not itself a container —
    see :func:`is_expanded`): once a task has been split into sub-tasks it is
    a container, not a unit of work, so it rolls up out of the queue and its
    children are evaluated instead. The depth cap and an explicit cycle guard
    guarantee the walk terminates regardless of the input shape.
    """

    task_id: str
    title: str
    complexity: int
    suggested_subtasks: int
    depth: int


def is_expanded(task: Task, all_tasks: list[Task]) -> bool:
    """Return True if *task* has at least one child task in *all_tasks*.

    A task with children was decomposed into sub-tasks, so it is now a
    *container* — a roll-up node — not an actionable unit of work. This is the
    parent roll-up model (v1.23.0, closing TM #250): the parent's stored
    complexity score is preserved untouched (the event log is immutable audit
    history, and the score still records how big the *unsplit* unit was), but
    every *actionable* view derived from the scores — the expansion queue, the
    CLI/MCP rendering — treats an expanded parent as a container and excludes
    it. An expanded parent is never re-queued for expansion (you do not expand
    something already expanded), which is exactly the "less useful as the main
    task isn't actionable" complaint this addresses.

    Pure — membership is computed purely from ``parent_task_id`` links; no I/O,
    no mutation.
    """
    return any(t.parent_task_id == task.id for t in all_tasks)


def suggested_subtask_count(complexity: int) -> int:
    """Return a deterministic suggested sub-task count for *complexity*.

    Heuristic: ``complexity - 1``, clamped to the expand engine's 2-5
    envelope — complexity 4 → 3 sub-tasks, complexity 5 → 4. A suggestion,
    not a contract: the LLM in ``expand --use-llm`` decides the final split
    within the same envelope.
    """
    return _clamp(
        complexity - 1, _SUGGESTED_SUBTASKS_MIN, _SUGGESTED_SUBTASKS_MAX
    )


def build_expansion_queue(
    tasks: list[Task],
    *,
    threshold: int = DEFAULT_AUTO_EXPAND_THRESHOLD,
) -> list[ExpansionCandidate]:
    """Return every scored task whose complexity is at/above *threshold*.

    Pure function — no I/O, no mutation. Tasks without a complexity score
    (not yet scored) are skipped; the queue only ever contains tasks the
    scoring engine has actually assessed.

    Args:
        tasks: Task models to filter (typically ``backend.list_tasks()``
            after a scoring run).
        threshold: Inclusive complexity cut-off, normally
            ``Config.auto_expand_threshold`` (default 4).

    Returns:
        Candidates sorted by complexity descending, then task id ascending —
        the most decomposition-worthy work first, deterministic throughout.

    An *expanded* parent (a task that already has children) is excluded even
    when its stored complexity is at/above the threshold: it is a container,
    not an actionable unit, so re-queuing it for expansion would be the TM-#250
    "main task isn't actionable" trap. See :func:`is_expanded`.
    """
    # Precompute the set of parent ids in one pass so the per-task container
    # check is O(1) — calling is_expanded() inside the loop would re-scan the
    # whole list each iteration (O(n^2)).
    expanded_ids = {t.parent_task_id for t in tasks if t.parent_task_id is not None}
    candidates: list[ExpansionCandidate] = []
    for task in tasks:
        complexity = task.scores.complexity
        if complexity is None or complexity < threshold:
            continue
        if task.id in expanded_ids:
            continue
        candidates.append(ExpansionCandidate(
            task_id=task.id,
            title=task.title,
            complexity=complexity,
            suggested_subtasks=suggested_subtask_count(complexity),
        ))
    return sorted(candidates, key=lambda c: (-c.complexity, c.task_id))


def _depth_of(
    task: Task, by_id: dict[str, Task], depth_cap: int
) -> int | None:
    """Return *task*'s depth in the parent tree, or None on a cycle/runaway.

    Depth 0 = a top-level task (no parent); 1 = its child; etc. Walks
    ``parent_task_id`` upward with a visited-set cycle guard and a hard
    iteration bound, so a malformed self-referential or mutually-referential
    parent chain returns None rather than looping forever. A dangling parent
    id (parent not present in *by_id*) terminates the walk at the current
    depth — the chain is treated as rooted where the data actually stops.
    """
    depth = 0
    seen = {task.id}
    current = task
    while current.parent_task_id is not None:
        if current.parent_task_id in seen:
            return None  # cycle
        parent = by_id.get(current.parent_task_id)
        if parent is None:
            break  # dangling parent — chain is effectively rooted here
        seen.add(parent.id)
        depth += 1
        current = parent
        if depth > depth_cap + 1:
            return None  # runaway guard (belt and suspenders past the cap)
    return depth


def build_recursive_expansion_queue(
    tasks: list[Task],
    *,
    threshold: int = DEFAULT_AUTO_EXPAND_THRESHOLD,
    depth_cap: int = DEFAULT_RECURSION_DEPTH_CAP,
) -> list[RecursiveExpansionCandidate]:
    """Return the recursive expansion frontier (v1.23.0).

    The frontier is every *leaf* task (one with no children of its own) whose
    complexity is at/above *threshold*, annotated with its tree ``depth``.
    Containers (already-expanded parents) roll up out of the queue; their
    children are evaluated in their place. A leaf deeper than *depth_cap* is
    dropped — repeated decomposition of one lineage is a sign the PRD block
    needs human restructuring, not another automatic split.

    Recursion happens across scoring runs, not inside one call: expand a leaf,
    re-score, and the next call surfaces any child that is still too big. This
    function reports the current frontier; the LLM still does the actual split
    (``expand --use-llm``). Pure, deterministic, and guaranteed to terminate
    (depth cap + cycle guard in :func:`_depth_of`).

    Returns candidates sorted by depth ascending (top-level decomposition
    first), then complexity descending, then task id.
    """
    by_id = {t.id: t for t in tasks}
    # One pass for the parent-id set (O(1) container check per task) and the
    # id→task map (O(1) parent lookups in _depth_of) — keeps the walk O(n).
    expanded_ids = {t.parent_task_id for t in tasks if t.parent_task_id is not None}
    candidates: list[RecursiveExpansionCandidate] = []
    for task in tasks:
        complexity = task.scores.complexity
        if complexity is None or complexity < threshold:
            continue
        if task.id in expanded_ids:
            continue  # container — its children are the actionable units
        depth = _depth_of(task, by_id, depth_cap)
        if depth is None or depth > depth_cap:
            continue  # too deep / malformed — needs a human, not auto-expansion
        candidates.append(RecursiveExpansionCandidate(
            task_id=task.id,
            title=task.title,
            complexity=complexity,
            suggested_subtasks=suggested_subtask_count(complexity),
            depth=depth,
        ))
    return sorted(candidates, key=lambda c: (c.depth, -c.complexity, c.task_id))


# ---------------------------------------------------------------------------
# LLM augmentation helper — local to keep dependency direction one-way.
# ---------------------------------------------------------------------------


def _augment_explanation(
    task: Task,
    score: Score,
    provider: LLMProvider,
) -> str | None:
    """Call the provider to produce a 1-3 sentence trade-off summary.

    Returns the LLM-written paragraph on success, or ``None`` if the call
    failed (a warning is printed to stderr in that case).  Never raises.
    """
    # Local import: keeps the optional LLM dep from leaking into the import
    # graph of callers that never set provider=.
    from fakoli_state.planning.llm import LLMProviderError

    user_payload = json.dumps(
        {
            "task_id": task.id,
            "title": task.title,
            "description": task.description,
            "likely_files": task.likely_files,
            "dependencies": task.dependencies,
            "scores": {
                "complexity": score.complexity,
                "parallelizability": score.parallelizability,
                "context_load": score.context_load,
                "blast_radius": score.blast_radius,
                "review_risk": score.review_risk,
                "agent_suitability": score.agent_suitability,
            },
        },
        sort_keys=True,
    )

    try:
        response = provider.generate(
            system=_SCORE_EXPLAIN_SYSTEM_PROMPT,
            user=user_payload,
            max_tokens=_SCORE_EXPLAIN_MAX_TOKENS,
        )
    except LLMProviderError as exc:
        print(
            f"warning: LLM augmentation of {task.id} score explanation failed "
            f"({exc}); falling back to rule-based explanation only.",
            file=sys.stderr,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — Phase 7 contract: LLM never aborts
        # Non-conforming custom provider raised something other than
        # LLMProviderError. Treat as fall-back to preserve the deterministic
        # baseline rather than abort the entire score batch.
        print(
            f"warning: LLM augmentation of {task.id} raised non-conforming "
            f"{type(exc).__name__}: {exc}; falling back to rule-based only.",
            file=sys.stderr,
        )
        return None

    text = response.text.strip()
    if not text:
        return None
    return text
