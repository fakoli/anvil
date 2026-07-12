"""Dependency, conflict-group, and (optional) sub-task inference.

Pure rule-based inference is the canonical baseline — no I/O, no LLM.  Phase 7
Wave 2 adds an optional ``expand_task`` entry point that uses an LLM provider
to propose 2-5 sub-tasks for *complex* tasks (complexity >= 4) when a provider
is supplied.  The deterministic engine on its own does not split tasks; that
responsibility lies with the author of prd.md (T001.1, T001.2 etc.).

Heuristics
----------
``infer_dependencies``:
    If Task A's ``likely_files`` is a *strict subset* of Task B's, A is added
    as a dependency of B (the broader change goes first; A specialises B).
    Conservative: only strict-subset edges are added — never speculative ones.

``infer_conflict_groups``:
    For each pair of tasks with *any* ``likely_files`` overlap that are NOT in a
    strict subset/superset relationship, they are grouped into a named
    ConflictGroup.  Group IDs follow the pattern ``CG-<sorted-task-ids>``.

``expand_task`` (LLM-only):
    With ``provider=`` and a task whose ``complexity >= 4``, asks the LLM for
    a JSON array of 2-5 sub-task proposals.  Returns ``[]`` for
    low-complexity tasks, when no provider is supplied, or when the LLM call
    or JSON parse fails (a warning is printed to stderr in the latter case).
"""

from __future__ import annotations

import json
import posixpath
import re
import sys
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, NamedTuple

from anvil.state.models import ConflictGroup, Task

if TYPE_CHECKING:
    from anvil.planning.llm import LLMProvider

__all__ = [
    "InferenceResult",
    "BundlePlanReport",
    "BundlePlanningError",
    "BundleProposal",
    "build_bundle_plan",
    "SubtaskProposal",
    "expand_task",
    "infer_all",
    "infer_conflict_groups",
    "infer_dependencies",
]


# ---------------------------------------------------------------------------
# Sub-task expansion (LLM-augmented; deterministic engine returns [])
# ---------------------------------------------------------------------------


class SubtaskProposal(NamedTuple):
    """A single LLM-proposed sub-task.

    Returned by :func:`expand_task` — *proposals only*, never written to the
    backend by this module.  The caller (CLI) decides what to do with them
    (typically: print for the human to paste into prd.md).
    """

    title: str
    description: str
    acceptance_criteria: list[str]
    likely_files: list[str]


_EXPAND_SYSTEM_PROMPT = (
    "You are decomposing a complex software task into 2-5 sub-tasks. "
    "Each sub-task should be independently claimable (no overlapping scope). "
    "Your entire response must be a single JSON array. Start your output "
    'with the literal character `[` and end with `]`. Each element is an '
    'object with keys: "title" (string, imperative), "description" '
    '(string), "acceptance_criteria" (array of strings, each independently '
    'verifiable), "likely_files" (array of file path strings). '
    "Do NOT wrap the array in markdown code fences. "
    "Do NOT include any prose before or after the array. "
    "Do NOT include explanatory commentary inside the array — only data."
)
_EXPAND_MAX_TOKENS = 2000
_EXPAND_COMPLEXITY_THRESHOLD = 4
_EXPAND_MIN_SUBTASKS = 2
_EXPAND_MAX_SUBTASKS = 5


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


class InferenceResult(NamedTuple):
    """Output of ``infer_all`` — always returned, never raised."""

    tasks: list[Task]
    conflict_groups: list[ConflictGroup]


@dataclass(frozen=True)
class BundleProposal:
    """One stable connected component proposed as a coordinator-owned bundle."""

    id: str
    task_ids: tuple[str, ...]
    serial_depth: int
    overlap_files: tuple[str, ...]
    review_angles: tuple[str, ...]
    expected_reviews: int
    expected_checkpoints: int = 1

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class BundlePlanReport:
    """Deterministic execution-cost report emitted before bundle execution."""

    task_count: int
    serial_depth: int
    overlap_pair_count: int
    overlap_files: tuple[str, ...]
    proposed_bundles: tuple[BundleProposal, ...]
    expected_review_count: int
    high_risk_policies: tuple[str, ...]
    expected_checkpoints: int
    max_tasks: int
    max_serial_stages: int
    limit_breaches: tuple[str, ...]
    acknowledgement_required: bool

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["proposed_bundles"] = [
            proposal.to_dict() for proposal in self.proposed_bundles
        ]
        return data


class BundlePlanningError(ValueError):
    """The proposed execution graph cannot be costed safely."""


def _canonical_project_path(path: str) -> str:
    candidate = path.strip().replace("\\", "/")
    if (
        not candidate
        or candidate.startswith("/")
        or re.match(r"^[A-Za-z]:/", candidate)
    ):
        raise BundlePlanningError(
            f"bundle planning requires a project-relative file path: {path!r}"
        )
    normalized = posixpath.normpath(candidate)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise BundlePlanningError(
            f"bundle planning file path escapes the project: {path!r}"
        )
    return normalized


def _serial_depth(tasks: list[Task]) -> int:
    by_id = {task.id: task for task in tasks}
    visiting: list[str] = []
    memo: dict[str, int] = {}

    def visit(task_id: str) -> int:
        if task_id in memo:
            return memo[task_id]
        if task_id in visiting:
            start = visiting.index(task_id)
            cycle = visiting[start:] + [task_id]
            raise BundlePlanningError(
                "bundle planning found a dependency cycle: " + " -> ".join(cycle)
            )
        visiting.append(task_id)
        depth = 1 + max(
            (visit(dep) for dep in by_id[task_id].dependencies if dep in by_id),
            default=0,
        )
        visiting.pop()
        memo[task_id] = depth
        return depth

    return max((visit(task_id) for task_id in sorted(by_id)), default=0)


def _risk_angles(tasks: list[Task]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    angles = {"correctness", "security", "integration"}
    policies: set[str] = set()
    joined_files = " ".join(path.lower() for task in tasks for path in task.likely_files)
    classifications = {
        "security": ("security", "auth", "crypto", "secret"),
        "privacy": ("privacy", "pii", "personal_data"),
        "topology": ("schema", "migration", "state/", "state\\"),
        "transport": ("http", "network", "sync/", "sync\\", "mcp"),
        "release": ("release", "packaging", "changelog"),
        "public-api": ("cli/", "cli\\", "api", "__init__.py"),
    }
    for angle, markers in classifications.items():
        if any(marker in joined_files for marker in markers):
            angles.add(angle)
            policies.add(angle)
    if any(
        (task.scores.blast_radius or 0) >= 4
        or (task.scores.review_risk or 0) >= 4
        for task in tasks
    ):
        angles.add("blast-radius")
        policies.add("high-score")
    return tuple(sorted(angles)), tuple(sorted(policies))


def build_bundle_plan(
    tasks: list[Task],
    *,
    max_tasks: int = 12,
    max_serial_stages: int = 6,
) -> BundlePlanReport:
    """Propose stable graph/file components and quantify execution overhead."""
    for name, value in (
        ("max_tasks", max_tasks),
        ("max_serial_stages", max_serial_stages),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 500:
            raise BundlePlanningError(f"{name} must be an integer in the range 1-500")
    ids = [task.id for task in tasks]
    duplicates = sorted({task_id for task_id in ids if ids.count(task_id) > 1})
    if duplicates:
        raise BundlePlanningError(f"bundle planning found duplicate task ids: {duplicates}")
    ordered_input = sorted(tasks, key=lambda task: task.id)
    canonical_files: dict[str, frozenset[str]] = {
        task.id: frozenset(_canonical_project_path(path) for path in task.likely_files)
        for task in ordered_input
    }
    display_path: dict[str, str] = {}
    for task in ordered_input:
        for path in task.likely_files:
            display_path.setdefault(_canonical_project_path(path), path)
    inferred_dependencies = {
        task.id: set(task.dependencies) for task in ordered_input
    }
    for left in ordered_input:
        for right in ordered_input:
            if (
                left.id != right.id
                and canonical_files[left.id]
                and canonical_files[left.id] < canonical_files[right.id]
                and left.id not in inferred_dependencies[right.id]
            ):
                inferred_dependencies[left.id].add(right.id)
    ordered = [
        task.model_copy(
            update={"dependencies": sorted(inferred_dependencies[task.id])}
        )
        for task in ordered_input
    ]
    task_ids = {task.id for task in ordered}
    missing_dependencies = sorted(
        {
            dependency
            for task in ordered
            for dependency in task.dependencies
            if dependency not in task_ids
        }
    )
    if missing_dependencies:
        raise BundlePlanningError(
            "bundle planning found missing dependency nodes: "
            f"{missing_dependencies}"
        )
    adjacency: dict[str, set[str]] = {task.id: set() for task in ordered}
    overlap_files: set[str] = set()
    overlap_pair_count = 0
    for task in ordered:
        for dependency in task.dependencies:
            if dependency in task_ids:
                adjacency[task.id].add(dependency)
                adjacency[dependency].add(task.id)
    for index, left in enumerate(ordered):
        left_files = set(canonical_files[left.id])
        for right in ordered[index + 1 :]:
            overlap = left_files & set(canonical_files[right.id])
            if not overlap:
                continue
            overlap_pair_count += 1
            overlap_files.update(overlap)
            adjacency[left.id].add(right.id)
            adjacency[right.id].add(left.id)

    components: list[tuple[str, ...]] = []
    remaining = set(task_ids)
    while remaining:
        seed = min(remaining)
        stack = [seed]
        component: set[str] = set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            stack.extend(sorted(adjacency[current] - component, reverse=True))
        remaining -= component
        components.append(tuple(sorted(component)))
    components.sort()

    by_id = {task.id: task for task in ordered}
    proposals: list[BundleProposal] = []
    high_risk: set[str] = set()
    for index, component in enumerate(components, start=1):
        members = [by_id[task_id] for task_id in component]
        member_files = [set(canonical_files[task.id]) for task in members]
        component_overlap = {
            path
            for left_index, left in enumerate(member_files)
            for right in member_files[left_index + 1 :]
            for path in left & right
        }
        risk_members = [
            task.model_copy(update={"likely_files": sorted(canonical_files[task.id])})
            for task in members
        ]
        angles, policies = _risk_angles(risk_members)
        high_risk.update(policies)
        proposals.append(
            BundleProposal(
                id=f"BP{index:03d}",
                task_ids=component,
                serial_depth=_serial_depth(members),
                overlap_files=tuple(
                    display_path[path] for path in sorted(component_overlap)
                ),
                review_angles=angles,
                expected_reviews=max(3, len(angles)),
            )
        )

    serial_depth = _serial_depth(ordered)
    breaches: list[str] = []
    if len(ordered) > max_tasks:
        breaches.append(f"task_count {len(ordered)} exceeds limit {max_tasks}")
    if serial_depth > max_serial_stages:
        breaches.append(
            f"serial_depth {serial_depth} exceeds limit {max_serial_stages}"
        )
    return BundlePlanReport(
        task_count=len(ordered),
        serial_depth=serial_depth,
        overlap_pair_count=overlap_pair_count,
        overlap_files=tuple(display_path[path] for path in sorted(overlap_files)),
        proposed_bundles=tuple(proposals),
        expected_review_count=sum(item.expected_reviews for item in proposals),
        high_risk_policies=tuple(sorted(high_risk)),
        expected_checkpoints=len(proposals),
        max_tasks=max_tasks,
        max_serial_stages=max_serial_stages,
        limit_breaches=tuple(breaches),
        acknowledgement_required=bool(breaches),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _files_set(task: Task) -> frozenset[str]:
    """Return the task's likely_files as a frozenset for set operations."""
    return frozenset(task.likely_files)


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def infer_dependencies(tasks: list[Task]) -> list[Task]:
    """Return a new Task list with ``.dependencies`` populated by subset heuristics.

    For each pair (A, B): if A.likely_files is a *strict* subset of B.likely_files,
    A is added to B.dependencies.  "B is a broader change; A specialises B, so B
    should be authored first."

    Pure — takes a Task list, returns a Task list.  Input tasks are never mutated;
    output tasks are produced via ``model_copy``.

    Args:
        tasks: List of Task models (likely_files populated from PRD parse).

    Returns:
        New list of Task instances with dependencies set from subset edges.
        Tasks with no inferred dependencies are returned unchanged.
    """
    if not tasks:
        return []

    # Build a map from task ID to its file set, then find all strict-subset edges.
    # An edge A → B means "A.files ⊂ B.files (strict)", so B depends on A.
    # Wait — task spec says: "if Task A's likely_files is a strict subset of
    # Task B's, A depends on B (because B is a broader change that A specialises;
    # the broader work usually goes first)."
    # So: A_files ⊂ B_files (strict) → A.dependencies.append(B.id)

    file_sets: dict[str, frozenset[str]] = {
        t.id: _files_set(t) for t in tasks
    }

    # Collect dependency edges: new_deps[task_id] = set of dependency IDs.
    new_deps: dict[str, set[str]] = {t.id: set(t.dependencies) for t in tasks}

    task_ids = [t.id for t in tasks]
    for id_a in task_ids:
        set_a = file_sets[id_a]
        if not set_a:
            # A task with no likely_files cannot be a subset of anything.
            continue
        for id_b in task_ids:
            if id_a == id_b:
                continue
            set_b = file_sets[id_b]
            # Strict subset: A ⊂ B means A ⊆ B and A ≠ B.
            if set_a < set_b:
                # A specialises B → A depends on B.
                new_deps[id_a].add(id_b)

    # Build the output list, replacing only tasks whose dependency set changed.
    updated: list[Task] = []
    for task in tasks:
        merged = sorted(new_deps[task.id])
        if merged != task.dependencies:
            updated.append(task.model_copy(update={"dependencies": merged}))
        else:
            updated.append(task)

    return updated


def infer_conflict_groups(
    tasks: list[Task],
) -> tuple[list[Task], list[ConflictGroup]]:
    """Return (tasks-with-conflict_groups-populated, ConflictGroup list).

    For each pair of tasks with ANY ``likely_files`` overlap that are NOT in a
    strict subset/superset relationship, group them together.  Groups are named
    ``CG-<sorted-task-ids>`` where the IDs are separated by ``-``.

    A task may appear in multiple conflict groups (one per pair that it is part
    of).  The ``Task.conflict_groups`` field records the IDs of all groups the
    task belongs to.

    Pure — takes a Task list, returns a new Task list and ConflictGroup list.

    Args:
        tasks: List of Task models (dependency inference should already be applied).

    Returns:
        Tuple of (updated Task list, list of ConflictGroup instances).
    """
    if not tasks:
        return [], []

    file_sets: dict[str, frozenset[str]] = {
        t.id: _files_set(t) for t in tasks
    }

    # Map task ID → set of conflict-group IDs it belongs to.
    task_conflict_groups: dict[str, set[str]] = {t.id: set() for t in tasks}
    conflict_groups: list[ConflictGroup] = []

    task_ids = [t.id for t in tasks]
    seen_pairs: set[frozenset[str]] = set()

    for idx_a in range(len(task_ids)):
        id_a = task_ids[idx_a]
        set_a = file_sets[id_a]
        if not set_a:
            continue
        for idx_b in range(idx_a + 1, len(task_ids)):
            id_b = task_ids[idx_b]
            pair = frozenset({id_a, id_b})
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            set_b = file_sets[id_b]
            if not set_b:
                continue

            overlap = set_a & set_b
            if not overlap:
                continue

            # If one is a strict subset of the other, skip — that's a dependency,
            # not a conflict.
            if set_a < set_b or set_b < set_a:
                continue

            # Partial overlap and neither is a subset: this is a conflict group.
            sorted_ids = sorted([id_a, id_b])
            cg_id = "CG-" + "-".join(sorted_ids)
            cg = ConflictGroup(
                id=cg_id,
                name=cg_id,
                task_ids=sorted_ids,
                reason=(
                    f"Tasks {id_a} and {id_b} share overlapping files: "
                    + ", ".join(sorted(overlap))
                ),
            )
            conflict_groups.append(cg)
            task_conflict_groups[id_a].add(cg_id)
            task_conflict_groups[id_b].add(cg_id)

    # Build updated task list.
    updated_tasks: list[Task] = []
    for task in tasks:
        new_cgs = sorted(task_conflict_groups[task.id])
        existing_cgs = sorted(task.conflict_groups)
        if new_cgs != existing_cgs:
            updated_tasks.append(
                task.model_copy(update={"conflict_groups": new_cgs})
            )
        else:
            updated_tasks.append(task)

    return updated_tasks, conflict_groups


def infer_all(tasks: list[Task]) -> InferenceResult:
    """Compose dependency and conflict inference into a single result.

    Apply in order: dependencies first, then conflict groups.  This ordering
    matters because ``infer_conflict_groups`` skips strict-subset pairs which
    are correctly classified as dependencies by ``infer_dependencies``.

    Pure — takes a Task list, returns an InferenceResult.  No I/O.

    Args:
        tasks: List of Task models to annotate.

    Returns:
        InferenceResult with the fully-annotated Task list and conflict groups.
    """
    tasks_with_deps = infer_dependencies(tasks)
    tasks_with_all, conflict_groups = infer_conflict_groups(tasks_with_deps)
    return InferenceResult(tasks=tasks_with_all, conflict_groups=conflict_groups)


# ---------------------------------------------------------------------------
# expand_task — LLM-augmented sub-task proposal (additive)
# ---------------------------------------------------------------------------


def expand_task(
    task: Task,
    *,
    provider: LLMProvider | None = None,
    threshold: int = _EXPAND_COMPLEXITY_THRESHOLD,
) -> list[SubtaskProposal]:
    """Propose 2-5 sub-tasks for a complex Task using an LLM.

    Deterministic baseline (provider=None or complexity < *threshold*):
    returns ``[]``.  The deterministic engine never proposes sub-tasks — that
    responsibility lies with the PRD author (manual subtask entries in prd.md).

    With ``provider=`` and ``task.scores.complexity >= threshold`` the
    provider is asked to return a JSON array of {title, description,
    acceptance_criteria, likely_files}.  On any failure (provider error, JSON
    parse error, schema mismatch) a warning is printed to stderr and ``[]``
    is returned — failures NEVER raise.

    Args:
        task: The Task to expand.  Must already be scored.
        provider: Optional LLM provider.
        threshold: Inclusive complexity cut-off below which the task is
            deemed simple enough to ship as-is.  Defaults to 4; callers with
            a loaded config pass ``Config.auto_expand_threshold`` (v1.21.0).

    Returns:
        A list of :class:`SubtaskProposal` (possibly empty).  Never raises.
    """
    if provider is None:
        return []

    complexity = task.scores.complexity
    if complexity is None or complexity < threshold:
        return []

    # Local import — keeps the optional LLM dep out of the main import graph.
    from anvil.planning.llm import LLMProviderError

    user_payload = json.dumps(
        {
            "task_id": task.id,
            "title": task.title,
            "description": task.description,
            "likely_files": task.likely_files,
            "acceptance_criteria": task.acceptance_criteria,
            "scores": {
                "complexity": task.scores.complexity,
                "parallelizability": task.scores.parallelizability,
                "context_load": task.scores.context_load,
                "blast_radius": task.scores.blast_radius,
                "review_risk": task.scores.review_risk,
                "agent_suitability": task.scores.agent_suitability,
            },
        },
        sort_keys=True,
    )

    try:
        response = provider.generate(
            system=_EXPAND_SYSTEM_PROMPT,
            user=user_payload,
            max_tokens=_EXPAND_MAX_TOKENS,
        )
    except LLMProviderError as exc:
        print(
            f"warning: LLM expansion of {task.id} failed ({exc}); "
            "no sub-task proposals produced.",
            file=sys.stderr,
        )
        return []
    except Exception as exc:  # noqa: BLE001 — Phase 7 contract: LLM never aborts
        # Non-conforming custom provider; preserve deterministic-empty result.
        print(
            f"warning: LLM expansion of {task.id} raised non-conforming "
            f"{type(exc).__name__}: {exc}; no sub-task proposals produced.",
            file=sys.stderr,
        )
        return []

    proposals = _parse_subtask_response(task.id, response.text)
    return proposals


_FENCE_RE = re.compile(
    # Matches the OPENING fence: ```json | ```jsonl | ``` plus any trailing
    # whitespace and a newline. Captures nothing — we just need the span to
    # drop it.
    r"^```(?:json|jsonl|JSON)?\s*\n",
    re.MULTILINE,
)


def _strip_markdown_fences(text: str) -> str:
    """Drop opening ```json fence + closing ``` fence if present.

    Tolerates both fenced (` ```json [...] ``` `) and unfenced output. The
    fence patterns are anchored: opening fence must start the (stripped)
    text; closing fence must end it. Mid-response fences are intentionally
    left alone — they could be part of nested code blocks inside a
    description field.
    """
    stripped = text.strip()
    m = _FENCE_RE.match(stripped)
    if m:
        stripped = stripped[m.end():]
        # Drop a trailing ``` (optionally surrounded by whitespace) if
        # present at end-of-string.
        if stripped.rstrip().endswith("```"):
            tail_idx = stripped.rstrip().rfind("```")
            stripped = stripped[:tail_idx].rstrip()
    return stripped


def _extract_first_json_array(text: str) -> str | None:
    """Return the substring spanning the first balanced JSON array, or None.

    Handles the case where the LLM prepends prose ("Here are 3 sub-tasks:
    [{...}, ...]") despite the prompt. Bracket-matching is simple but
    string-aware — quoted strings can contain ``[`` / ``]`` chars that
    should not affect depth. The matcher is NOT a full JSON parser; it
    returns the substring for ``json.loads`` to validate.
    """
    in_string = False
    escape = False
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "[":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "]":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    return text[start:i + 1]
    return None


def _parse_subtask_response(task_id: str, raw: str) -> list[SubtaskProposal]:
    """Parse the LLM's JSON-list response into ``SubtaskProposal``s.

    Tolerant of common LLM output quirks:
    - leading/trailing whitespace
    - markdown code fences (```json ... ``` or ``` ... ```)
    - leading/trailing prose around the JSON array (regex-extracts the first
      bracketed array as fallback)

    Strict on schema once a JSON array is in hand. On any parse failure
    surfaces a warning that INCLUDES a sample of the raw response (first
    300 chars) so the user can see what the LLM actually wrote without
    re-running with extra verbosity.
    """
    text = raw.strip()
    if not text:
        print(
            f"warning: LLM expansion of {task_id} returned an empty "
            "response; ignoring.",
            file=sys.stderr,
        )
        return []

    # Strip markdown code fences. Modern Claude models often wrap JSON in
    # ```json ... ``` despite the prompt saying not to — silently handle
    # the common case rather than make the user debug a non-JSON warning.
    text = _strip_markdown_fences(text)

    decoded: object | None = None
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        # Fallback: regex-extract the first balanced JSON array. Tolerates
        # prose preambles like "Here are 3 sub-tasks: [...]" without
        # forcing the user to debug a prompt-tuning issue.
        extracted = _extract_first_json_array(text)
        if extracted is not None:
            try:
                decoded = json.loads(extracted)
            except json.JSONDecodeError:
                decoded = None

    if decoded is None:
        sample = raw.strip()[:300]
        if len(raw.strip()) > 300:
            sample += "…"
        print(
            f"warning: LLM expansion of {task_id} returned non-JSON; "
            f"ignoring. First 300 chars of response: {sample!r}",
            file=sys.stderr,
        )
        return []

    if not isinstance(decoded, list):
        print(
            f"warning: LLM expansion of {task_id} returned non-list JSON; ignoring.",
            file=sys.stderr,
        )
        return []

    proposals: list[SubtaskProposal] = []
    for idx, item in enumerate(decoded):
        if not isinstance(item, dict):
            print(
                f"warning: LLM expansion of {task_id}: item {idx} is not an "
                "object; skipping.",
                file=sys.stderr,
            )
            continue
        title = item.get("title")
        description = item.get("description", "")
        acceptance_criteria = item.get("acceptance_criteria", []) or []
        likely_files = item.get("likely_files", []) or []
        if not isinstance(title, str) or not title.strip():
            print(
                f"warning: LLM expansion of {task_id}: item {idx} missing "
                "title; skipping.",
                file=sys.stderr,
            )
            continue
        if not isinstance(acceptance_criteria, list) or not isinstance(likely_files, list):
            print(
                f"warning: LLM expansion of {task_id}: item {idx} has invalid "
                "list fields; skipping.",
                file=sys.stderr,
            )
            continue
        proposals.append(
            SubtaskProposal(
                title=title.strip(),
                description=str(description).strip(),
                acceptance_criteria=[str(c).strip() for c in acceptance_criteria if str(c).strip()],
                likely_files=[str(f).strip() for f in likely_files if str(f).strip()],
            )
        )

    # The prompt requests 2-5; tolerate edge cases but cap upper bound so a
    # runaway LLM cannot flood the output.
    if len(proposals) > _EXPAND_MAX_SUBTASKS:
        proposals = proposals[:_EXPAND_MAX_SUBTASKS]
    if len(proposals) < _EXPAND_MIN_SUBTASKS:
        # Spec says 2-5; fewer than 2 is not a useful split — warn but still
        # return what we got so the caller can decide.
        print(
            f"warning: LLM expansion of {task_id} returned only "
            f"{len(proposals)} sub-task(s); spec asks for "
            f"{_EXPAND_MIN_SUBTASKS}-{_EXPAND_MAX_SUBTASKS}.",
            file=sys.stderr,
        )

    return proposals
