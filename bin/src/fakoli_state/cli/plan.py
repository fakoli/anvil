"""plan, score, expand, review tasks, list, show commands (Phase 3).

Phase 7 Wave 2: plan / score / expand grow a ``--use-llm`` flag that, when
set, instantiates an :class:`fakoli_state.planning.llm.AnthropicProvider`
and threads it into the underlying planning engine functions.  LLM
augmentation is *additive* — the deterministic baseline always runs first;
LLM enrichment is layered on top and may fail open with a stderr warning.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import typer
import yaml

from fakoli_state.cli._helpers import (
    _PRD_FILENAME,
    _open_backend,
    _require_state_dir,
    _resolve_state_dir,
    _scores_complete,
)
from fakoli_state.cli._json import (
    JSON_OPTION,
    dump_model,
    dump_models,
    emit_success,
    fail,
)
from fakoli_state.state.backend import EventRejected

if TYPE_CHECKING:
    from fakoli_state.config import Config
    from fakoli_state.planning.inference import SubtaskProposal
    from fakoli_state.planning.llm import LLMProvider
    from fakoli_state.planning.scoring import ExpansionCandidate


# ---------------------------------------------------------------------------
# Shared helpers — config load + LLM provider resolution
# ---------------------------------------------------------------------------


def _load_config_optional(state_dir: Path) -> Config | None:
    """Load ``.fakoli-state/config.yaml`` if it exists; return None on miss/error.

    Mirrors the soft-load pattern in cli/claim.py: an unreadable or absent
    config never blocks a CLI command — we fall back to env-only resolution
    so ad-hoc scratch projects (and CI without a checked-in config) keep
    working. A bad config emits a stderr warning so the user notices the
    problem without seeing a hard error.

    v1.17.0: load failures used to be silent; we now emit a warning naming
    the exception class + message so misconfigs surface during plan rather
    than during the next CLI invocation.

    T016/B17: the global-config layer (``~/.config/fakoli-state/config.yaml``)
    is merged UNDER the project config so user-wide planning defaults (e.g.
    ``llm_tier`` / ``auto_expand_threshold``) apply here too, with the project
    config overriding them.
    """
    config_path = state_dir / "config.yaml"
    if not config_path.exists():
        return None
    try:
        from fakoli_state.config import load_merged_config

        return load_merged_config(config_path)
    except (FileNotFoundError, OSError, ValueError, yaml.YAMLError) as exc:
        # Catch the four expected failure modes explicitly:
        #   - FileNotFoundError / OSError — disappeared between the
        #     exists() check above and the load call (TOCTOU)
        #   - ValueError — schema validation in load_config (enum mismatch
        #     on llm_provider / llm_tier / git_ops_mode etc.)
        #   - yaml.YAMLError — malformed YAML
        # An unexpected exception type beyond these is allowed to surface
        # as a real traceback — better diagnostic signal than a silent
        # fall-through. (critic SHOULD FIX #3, PR #65)
        typer.echo(
            f"Warning: config.yaml load failed "
            f"({type(exc).__name__}: {exc}); proceeding with env-only "
            "LLM resolution. Fix config.yaml and re-run to use config.",
            err=True,
        )
        return None


def _resolve_llm_provider(
    use_llm: bool,
    config: Config | None = None,
) -> LLMProvider | None:
    """Return an LLM provider when ``--use-llm`` is set, else None.

    v1.17.0: delegates to :func:`planning.llm_planner.resolve_planner_provider`
    so the same multi-provider precedence (Anthropic API / Bedrock / custom
    OpenAI-compatible) applies to ``--use-llm`` augmentation as to the
    LLM-planner backstop. Single source of truth for provider selection;
    no more divergent env-var checks per call site.

    Exits with code 1 if ``--use-llm`` is set but no provider can be
    resolved — the error message from ``resolve_planner_provider`` lists
    every supported path.
    """
    if not use_llm:
        return None

    # Local import: keeps the provider SDKs out of the import graph for
    # deterministic-only invocations.
    from fakoli_state.planning.llm_planner import (
        PlannerProviderUnavailable,
        resolve_planner_provider,
    )

    try:
        provider, _tier = resolve_planner_provider(config)
    except PlannerProviderUnavailable as exc:
        typer.echo(f"Error: --use-llm cannot resolve a provider.\n{exc}", err=True)
        raise typer.Exit(code=1) from exc
    return provider


def _resolve_auto_expand(config: Config | None) -> tuple[bool, int]:
    """Return the effective ``(auto_expand, auto_expand_threshold)`` pair.

    v1.21.0: a missing config (scratch projects, CI without a checked-in
    config.yaml) falls back to the same defaults the Config dataclass
    declares — auto-expansion on, threshold 4 — so the score → expand loop
    closes everywhere, not just for fully-configured projects.
    """
    from fakoli_state.config import DEFAULT_AUTO_EXPAND_THRESHOLD

    if config is None:
        return True, DEFAULT_AUTO_EXPAND_THRESHOLD
    return config.auto_expand, config.auto_expand_threshold


def _render_expansion_queue(
    queue: list[ExpansionCandidate],
    *,
    threshold: int,
) -> None:
    """Print the EXPANSION QUEUE section after a scoring run.

    One block per candidate: id, complexity, suggested sub-task count,
    title, then the exact follow-up command to run. Silent when the queue
    is empty — no noise for projects whose tasks are all right-sized.
    """
    if not queue:
        return

    header = f"EXPANSION QUEUE (complexity >= {threshold})"
    typer.echo(f"\n{header}")
    typer.echo("-" * len(header))
    for candidate in queue:
        typer.echo(
            f"  {candidate.task_id:<12} "
            f"complexity={candidate.complexity}  "
            f"suggested-subtasks={candidate.suggested_subtasks}  "
            f"{candidate.title}"
        )
        typer.echo(
            f"    $ fakoli-state expand {candidate.task_id} --use-llm"
        )
    typer.echo(
        f"\n{len(queue)} task(s) queued for expansion. Run the command(s) "
        "above to decompose, or set `auto_expand: false` in "
        ".fakoli-state/config.yaml to silence this section."
    )

# review sub-app — registered in __init__.py as app.add_typer(review_app, name="review")
review_app = typer.Typer(
    name="review",
    help="Review lifecycle commands: tasks.",
    no_args_is_help=True,
)


# ---------------------------------------------------------------------------
# plan subcommand
# ---------------------------------------------------------------------------


def plan(
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
    use_llm: bool = typer.Option(  # noqa: B008
        False,
        "--use-llm",
        help=(
            "Augment planning with an LLM (Anthropic). Requires "
            "ANTHROPIC_API_KEY in environment. Deterministic output is "
            "always produced first; LLM enrichment is additive."
        ),
    ),
    no_llm: bool = typer.Option(  # noqa: B008
        False,
        "--no-llm",
        help=(
            "Disable the LLM task-generation backstop. When the PRD has "
            "features+requirements but no `## Tasks` section, default "
            "behaviour is to call the LLM to generate tasks and append "
            "them to prd.md. With --no-llm the CLI fails loudly instead, "
            "matching the pre-v1.15 behaviour for users who prefer to "
            "author tasks manually."
        ),
    ),
    prune_force: bool = typer.Option(  # noqa: B008
        False,
        "--prune-force",
        help=(
            "Force-delete orphan tasks that have advanced past 'ready' "
            "status (claimed / in_progress / needs_review / etc.). Without "
            "this flag, orphans in those statuses cause plan to fail "
            "loudly so the user can release/complete them first. With "
            "this flag, the audit trail (events + evidence + reviews) is "
            "preserved but the task row itself is deleted. Use with care."
        ),
    ),
    json_output: bool = JSON_OPTION,
) -> None:
    """Generate features and tasks from the parsed PRD.

    Re-reads prd.md, emits feature.created and task.created events for each
    feature and task found.  Then runs dependency and conflict-group inference
    and promotes all tasks from proposed to drafted.

    With ``--use-llm`` Task descriptions shorter than
    ``template.DESCRIPTION_SHORT_THRESHOLD`` (currently 50 chars) are
    enriched by the LLM after the deterministic parse.  LLM failures fall
    back to the deterministic description with a stderr warning — they never
    abort plan.

    When the PRD has features+requirements but no ``## Tasks`` section the
    CLI calls the LLM planner (see ``planning.llm_planner``) to draft tasks,
    appends them to ``prd.md``, and re-parses. Pass ``--no-llm`` to opt out
    of this backstop and fail loudly instead.

    Idempotent: running plan twice will not duplicate tasks (INSERT OR REPLACE
    semantics in the SQLite backend handle deduplication by task ID). The
    LLM backstop is also idempotent — once a ``## Tasks`` section exists in
    ``prd.md`` it is never re-appended.
    """
    from fakoli_state.clock import SystemClock
    from fakoli_state.planning.inference import infer_all
    from fakoli_state.planning.llm_planner import (
        PlannerProviderUnavailable,
        TaskGenerationError,
        generate_tasks_markdown,
    )
    from fakoli_state.planning.template import parse_prd
    from fakoli_state.state.models import EventDraft

    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="plan", json_output=json_output)

    # Non-fatal warnings collected for the JSON envelope (parse warnings that
    # otherwise go to stderr in human mode).
    plan_warnings: list[str] = []

    prd_path = state_dir / _PRD_FILENAME
    if not prd_path.exists():
        if json_output:
            fail(
                "plan",
                f"PRD file not found at {prd_path}. "
                "Author your PRD first, then run `fakoli-state prd parse`.",
                code="not_found",
            )
        typer.echo(
            f"Error: PRD file not found at {prd_path}. "
            "Author your PRD first, then run `fakoli-state prd parse`.",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        markdown = prd_path.read_text(encoding="utf-8")
    except OSError as exc:
        if json_output:
            fail("plan", f"cannot read {prd_path}: {exc}", code="io_error")
        typer.echo(f"Error: cannot read {prd_path}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    # v1.17.0: load config once and pass it to every LLM call site so the
    # project's llm_provider / llm_tier / bedrock_* / custom_* knobs apply
    # uniformly to both the --use-llm augmentation path and the no-tasks
    # backstop below.
    config = _load_config_optional(state_dir)

    provider = _resolve_llm_provider(use_llm, config)
    parsed = parse_prd(markdown, prd_id="prd", provider=provider)

    # Non-fatal parse errors are surfaced as warnings during plan.
    if parsed.errors:
        if json_output:
            plan_warnings.extend(
                f"[{err.section}:{err.line}] {err.message}" for err in parsed.errors
            )
        else:
            for err in parsed.errors:
                typer.echo(
                    f"  Warning [{err.section}:{err.line}]: {err.message}",
                    err=True,
                )

    # ------------------------------------------------------------------
    # LLM task-generation backstop (v1.15+)
    #
    # When the PRD has features+requirements but no `## Tasks` section the
    # deterministic parser yields 0 tasks. Previously the CLI happily
    # exited 0 with "Planned N features, 0 tasks" and the user had to
    # remember to invoke the planner subagent. Now we call the LLM
    # planner here, append generated tasks to prd.md, and re-parse so
    # the rest of this command runs over a populated task list.
    # ------------------------------------------------------------------
    llm_generated_count = 0
    llm_tier_used: str | None = None
    if (
        not no_llm
        and len(parsed.tasks) == 0
        and len(parsed.features) > 0
    ):
        try:
            gen_result = generate_tasks_markdown(
                prd=parsed.prd,
                features=parsed.features,
                requirements=parsed.requirements,
                config=config,
            )
        except PlannerProviderUnavailable as exc:
            if json_output:
                fail("plan", str(exc), code="provider_unavailable")
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        except TaskGenerationError as exc:
            if json_output:
                fail(
                    "plan",
                    f"LLM task generation failed: {exc}",
                    code="task_generation_error",
                )
            typer.echo(f"Error: LLM task generation failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        # Idempotency: only append `## Tasks` when the file does not
        # already contain one. Re-running `plan` on a file we previously
        # appended to is a no-op for the file — the parsed.tasks check
        # above is the safeguard, but a defensive markdown re-read +
        # `## Tasks` substring check ensures concurrent writers can't
        # double-append.
        try:
            current_markdown = prd_path.read_text(encoding="utf-8")
        except OSError as exc:
            if json_output:
                fail("plan", f"cannot re-read {prd_path}: {exc}", code="io_error")
            typer.echo(f"Error: cannot re-read {prd_path}: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        from fakoli_state.planning._plan_helpers import has_tasks_section
        if not has_tasks_section(current_markdown):
            new_markdown = (
                current_markdown.rstrip() + "\n\n" + gen_result.markdown + "\n"
            )
            try:
                prd_path.write_text(new_markdown, encoding="utf-8")
            except OSError as exc:
                if json_output:
                    fail(
                        "plan",
                        f"cannot write generated tasks to {prd_path}: {exc}",
                        code="io_error",
                    )
                typer.echo(
                    f"Error: cannot write generated tasks to {prd_path}: {exc}",
                    err=True,
                )
                raise typer.Exit(code=1) from exc

        # Re-parse so the rest of plan() consumes the freshly-appended tasks.
        try:
            markdown = prd_path.read_text(encoding="utf-8")
        except OSError as exc:
            if json_output:
                fail("plan", f"cannot re-read {prd_path}: {exc}", code="io_error")
            typer.echo(f"Error: cannot re-read {prd_path}: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        parsed = parse_prd(markdown, prd_id="prd", provider=provider)
        llm_generated_count = len(parsed.tasks)
        llm_tier_used = gen_result.provider_used

    backend = _open_backend(state_dir)
    try:
        clock = SystemClock()

        # --------------------------------------------------------------
        # Orphan-prune (v1.15.0)
        #
        # Re-parse is supposed to be destructive — the docs (and the prd
        # skill body) say so explicitly. But before v1.15.0 plan emitted
        # task.created/feature.created for everything in the new parse
        # WITHOUT emitting task.deleted/feature.deleted for entities that
        # disappeared from the PRD. The classification + emission logic
        # lives in planning._plan_helpers so the CLI and MCP share one
        # implementation (greptile + critic flagged the previous twin
        # implementations — the CLI version was missing the
        # TransactionAborted catch that the MCP version had).
        # --------------------------------------------------------------
        from fakoli_state.planning._plan_helpers import (
            classify_orphans,
            emit_prune_events,
        )

        classification = classify_orphans(
            backend.list_tasks(),
            {t.id for t in parsed.tasks},
            backend.list_features(),
            {f.id for f in parsed.features},
        )

        if classification.unsafe_task_orphans and not prune_force:
            if json_output:
                orphan_detail = "; ".join(
                    f"{t.id} ({t.status.value}): {t.title}"
                    for t in classification.unsafe_task_orphans
                )
                fail(
                    "plan",
                    f"{len(classification.unsafe_task_orphans)} orphan task(s) "
                    "were removed from prd.md but have advanced past `ready` "
                    "status. Re-run with --prune-force to delete despite the "
                    f"status, or address each one: {orphan_detail}",
                    code="unsafe_orphans",
                )
            typer.echo(
                f"Error: {len(classification.unsafe_task_orphans)} orphan "
                "task(s) were removed from prd.md but have advanced past "
                "`ready` status. Re-parse would lose claim/evidence history "
                "if these were deleted silently. Address each one, OR "
                "re-run with --prune-force to delete despite the status:",
                err=True,
            )
            for t in classification.unsafe_task_orphans:
                typer.echo(
                    f"  - {t.id} ({t.status.value}): {t.title}",
                    err=True,
                )
            typer.echo(
                "\nOptions per task:\n"
                "  • Release the claim (`fakoli-state release` or "
                "`fakoli-state release --force`) so status returns to `ready`\n"
                "  • Complete the work (`fakoli-state apply --approve` for "
                "needs_review tasks)\n"
                "  • Re-add the task to prd.md so it's no longer an orphan\n"
                "  • Run `fakoli-state plan --prune-force` to delete the "
                "row anyway (events + evidence + reviews are preserved as "
                "audit history; the task row itself is removed).",
                err=True,
            )
            raise typer.Exit(code=1)

        # Surface TransactionAborted as a clean CLI error rather than a
        # raw Python traceback. The handler's message is user-actionable
        # as-is (names the blocking IDs and the resolution). Greptile MUST
        # FIX from PR #63 review — previously this catch was missing and
        # the most accessible trigger was "user removes a feature heading
        # from prd.md while keeping its referencing tasks": the feature
        # becomes an orphan, the handler refuses, the CLI crashed.
        try:
            prune_result = emit_prune_events(
                backend,
                classification,
                actor="fakoli-state-cli",
                clock=clock,
                prune_force=prune_force,
            )
        except EventRejected as exc:
            if json_output:
                fail("plan", f"orphan cleanup refused — {exc}", code="event_rejected")
            typer.echo(f"Error: orphan cleanup refused — {exc}", err=True)
            raise typer.Exit(code=1) from exc

        deleted_task_ids = prune_result.pruned_task_ids
        deleted_feature_ids = prune_result.pruned_feature_ids

        # Emit feature.created for each feature.
        for feature in parsed.features:
            now = clock.now()
            feature_data = feature.model_dump(mode="json")
            draft = EventDraft(
                timestamp=now,
                actor="fakoli-state-cli",
                action="feature.created",
                target_kind="feature",
                target_id=feature.id,
                payload_json=feature_data,
            )
            backend.append(draft)

        # Emit task.created for each task (status proposed at creation time).
        for task in parsed.tasks:
            now = clock.now()
            task_data = task.model_dump(mode="json")
            draft = EventDraft(
                timestamp=now,
                actor="fakoli-state-cli",
                action="task.created",
                target_kind="task",
                target_id=task.id,
                payload_json=task_data,
            )
            backend.append(draft)

        # Run inference on the parsed tasks (before they are stored with updated
        # deps/conflict groups — we upsert them via task.created events again).
        inference_result = infer_all(parsed.tasks)

        # Re-upsert tasks with inferred dependencies and conflict groups,
        # then promote proposed → drafted.
        for inferred_task in inference_result.tasks:
            now = clock.now()
            # Upsert with full updated fields.
            task_data = inferred_task.model_dump(mode="json")
            upsert_draft = EventDraft(
                timestamp=now,
                actor="fakoli-state-cli",
                action="task.created",
                target_kind="task",
                target_id=inferred_task.id,
                payload_json=task_data,
            )
            backend.append(upsert_draft)

            # Promote proposed → drafted, but ONLY if the task is currently
            # at 'proposed'. On re-plan, existing tasks may have advanced
            # past 'drafted' (Phase 4+: claimed, in_progress, etc.) and
            # emitting a status_changed for those would error or worse
            # silently regress them. The task.created upsert above does NOT
            # touch status (Greptile PR #38 fix), so existing-task status
            # is preserved; we only need to promote fresh proposed tasks.
            current = backend.get_task(inferred_task.id)
            if current is not None and current.status.value == "proposed":
                now = clock.now()
                status_draft = EventDraft(
                    timestamp=now,
                    actor="fakoli-state-cli",
                    action="task.status_changed",
                    target_kind="task",
                    target_id=inferred_task.id,
                    payload_json={
                        "task_id": inferred_task.id,
                        "from": "proposed",
                        "to": "drafted",
                        "reason": "plan: initial draft after inference",
                    },
                )
                backend.append(status_draft)
        # Echo summary inside the try block so it only runs on full success;
        # otherwise inference_result may be unbound (if append raised
        # before line 173) and the access below would NameError.
        if json_output:
            # The --no-llm zero-tasks opt-out path is still a hard failure in
            # JSON mode: emit the error envelope and exit non-zero.
            if (
                no_llm
                and len(parsed.tasks) == 0
                and len(parsed.features) > 0
            ):
                fail(
                    "plan",
                    "0 tasks generated; pass without --no-llm to auto-generate "
                    f"via LLM, or author tasks manually in {prd_path}.",
                    code="no_tasks",
                )
            emit_success(
                "plan",
                {
                    "features": len(parsed.features),
                    "tasks": len(parsed.tasks),
                    "llm_generated": llm_generated_count,
                    "llm_tier": llm_tier_used,
                    "conflict_groups": len(inference_result.conflict_groups),
                    "pruned_task_ids": list(deleted_task_ids),
                    "pruned_feature_ids": list(deleted_feature_ids),
                    "warnings": plan_warnings,
                },
            )
            return
        if llm_generated_count and llm_tier_used:
            typer.echo(
                f"Planned {len(parsed.features)} features, "
                f"{len(parsed.tasks)} tasks "
                f"({llm_generated_count} generated via LLM ({llm_tier_used}), "
                f"appended to {prd_path})."
            )
        elif (
            no_llm
            and len(parsed.tasks) == 0
            and len(parsed.features) > 0
        ):
            # Opt-out path: the user explicitly disabled the backstop AND
            # the deterministic parse produced zero tasks. There is no
            # work to do downstream, so fail loudly per spec.
            typer.echo(
                f"Planned {len(parsed.features)} features, 0 tasks.",
            )
            typer.echo(
                "Error: 0 tasks generated; pass without --no-llm to "
                "auto-generate via LLM, or author tasks manually in "
                f"{prd_path}.",
                err=True,
            )
            raise typer.Exit(code=1)
        else:
            typer.echo(
                f"Planned {len(parsed.features)} features, "
                f"{len(parsed.tasks)} tasks."
            )
        if inference_result.conflict_groups:
            typer.echo(
                f"Detected {len(inference_result.conflict_groups)} conflict group(s)."
            )
        if deleted_task_ids or deleted_feature_ids:
            # Surface the prune outcome explicitly — the user removed these
            # entities from prd.md and should know the state.db is now in
            # sync, not silently lingering with orphans.
            bits: list[str] = []
            if deleted_task_ids:
                joined = ", ".join(deleted_task_ids)
                bits.append(f"{len(deleted_task_ids)} orphan task(s) ({joined})")
            if deleted_feature_ids:
                joined = ", ".join(deleted_feature_ids)
                bits.append(f"{len(deleted_feature_ids)} orphan feature(s) ({joined})")
            typer.echo(f"Pruned {' and '.join(bits)} removed from prd.md.")
    finally:
        backend.close()


# Helpers `_has_tasks_section` and `_TASKS_HEADING_RE` previously lived
# here in duplicated form alongside the MCP twin in mcp_server.py. As of
# v1.15.0 post-review they live in planning/_plan_helpers.py and both
# layers import from there — see that module's docstring for the
# multi-critic finding that drove the extraction.


# ---------------------------------------------------------------------------
# score subcommand
# ---------------------------------------------------------------------------


def score(
    task_id: str | None = typer.Argument(  # noqa: B008
        None,
        help="Task ID to score. Omit to score all tasks lacking complete scores.",
    ),
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
    use_llm: bool = typer.Option(  # noqa: B008
        False,
        "--use-llm",
        help=(
            "Augment the rule-based explanation with an LLM-written trade-off "
            "summary (Anthropic). Requires ANTHROPIC_API_KEY. The numeric "
            "scores themselves are never modified by the LLM."
        ),
    ),
    json_output: bool = JSON_OPTION,
) -> None:
    """Score tasks across six dimensions using rule-based heuristics.

    Without TASK_ID: scores all tasks whose scores are incomplete.
    With TASK_ID: scores that single task.

    With ``--use-llm`` the deterministic explanation is appended with a 1-3
    sentence trade-off summary from the LLM.  Numeric scores are unaffected.

    Emits a task.scored event per task and prints a summary table.

    v1.21.0: when ``auto_expand`` is enabled (config default: true), the
    summary table is followed by an EXPANSION QUEUE section listing every
    task whose complexity is at/above ``auto_expand_threshold`` (config
    default: 4) with the exact ``fakoli-state expand TXXX --use-llm``
    follow-up command per task.  Queueing is deterministic — the LLM-side
    decomposition only happens when the expand command runs.
    """
    from fakoli_state.clock import SystemClock
    from fakoli_state.planning.scoring import build_recursive_expansion_queue, score_task
    from fakoli_state.state.models import EventDraft

    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="score", json_output=json_output)

    config = _load_config_optional(state_dir)
    provider = _resolve_llm_provider(use_llm, config)

    backend = _open_backend(state_dir)
    try:
        clock = SystemClock()

        if task_id is not None:
            task = backend.get_task(task_id)
            if task is None:
                if json_output:
                    fail("score", f"task '{task_id}' not found.", code="not_found")
                typer.echo(
                    f"Error: task '{task_id}' not found.",
                    err=True,
                )
                raise typer.Exit(code=1)
            tasks_to_score = [task]
        else:
            all_tasks = backend.list_tasks()
            tasks_to_score = [
                t for t in all_tasks if not _scores_complete(t)
            ]

        if not tasks_to_score:
            if json_output:
                # backend.close() runs in the finally below as the function
                # returns; the envelope is the only stdout line either way.
                emit_success("score", {"scored": [], "count": 0, "expansion_queue": []})
                return
            typer.echo("No tasks require scoring.")
            return

        scored_tasks = []
        for task in tasks_to_score:
            computed_score = score_task(task, provider=provider)
            now = clock.now()
            score_payload: dict[str, object] = {
                "task_id": task.id,
                "scores": {
                    "complexity": computed_score.complexity,
                    "parallelizability": computed_score.parallelizability,
                    "context_load": computed_score.context_load,
                    "blast_radius": computed_score.blast_radius,
                    "review_risk": computed_score.review_risk,
                    "agent_suitability": computed_score.agent_suitability,
                },
                "explanation": computed_score.explanation,
            }

            draft = EventDraft(
                timestamp=now,
                actor="fakoli-state-cli",
                action="task.scored",
                target_kind="task",
                target_id=task.id,
                payload_json=score_payload,
            )
            backend.append(draft)
            scored_tasks.append((task, computed_score))

        # v1.21.0 — re-fetch AFTER the task.scored events landed so the
        # expansion queue covers every task at/above threshold (including
        # ones scored in earlier runs), not just this run's batch.
        auto_expand, expand_threshold = _resolve_auto_expand(config)
        expansion_queue = (
            build_recursive_expansion_queue(
                backend.list_tasks(), threshold=expand_threshold
            )
            if auto_expand
            else []
        )
    finally:
        backend.close()

    if json_output:
        emit_success(
            "score",
            {
                "scored": [
                    {"task_id": task.id, "scores": dump_model(s)}
                    for task, s in scored_tasks
                ],
                "count": len(scored_tasks),
                "expansion_queue": [
                    candidate._asdict() for candidate in expansion_queue
                ],
            },
        )
        return

    # Print summary table.
    header = (
        f"{'TaskID':<12} "
        f"{'Complexity':>10} "
        f"{'Parallel':>8} "
        f"{'CtxLoad':>7} "
        f"{'Blast':>5} "
        f"{'Review':>6} "
        f"{'Agent':>5}"
    )
    typer.echo(header)
    typer.echo("-" * len(header))
    for task, s in scored_tasks:
        typer.echo(
            f"{task.id:<12} "
            f"{str(s.complexity):>10} "
            f"{str(s.parallelizability):>8} "
            f"{str(s.context_load):>7} "
            f"{str(s.blast_radius):>5} "
            f"{str(s.review_risk):>6} "
            f"{str(s.agent_suitability):>5}"
        )
    typer.echo(f"\nScored {len(scored_tasks)} task(s).")

    _render_expansion_queue(expansion_queue, threshold=expand_threshold)


# ---------------------------------------------------------------------------
# expand subcommand
# ---------------------------------------------------------------------------


_EXPAND_VALID_FORMATS = ("text", "prd")


def _render_subtask_proposals_as_prd(
    parent_task_id: str,
    proposals: list[SubtaskProposal],
    *,
    parent_feature_id: str | None = None,
    parent_priority: str | None = None,
) -> str:
    """Render proposals as markdown blocks matching ``docs/prd-template.md``.

    Each proposal becomes a ``### {parent_task_id}.N: {title}`` block carrying
    the same field set the PRD parser recognises:

    - ``**Feature:**`` — populated from ``parent_feature_id`` when supplied
      (Phase 9 critic CONSIDER fix); left blank when not, so the user can
      fill it in before ``prd parse``.  Threading the parent's
      ``feature_id`` from the caller eliminates the manual-edit step in the
      ``expand --format prd`` → paste-into-prd.md workflow.
    - ``**Priority:**`` — populated from ``parent_priority`` when supplied;
      defaults to ``medium`` so the block is valid PRD input without further
      editing.  Inheriting the parent's priority is the right default
      because sub-tasks share their parent's shipping urgency.
    - ``**Likely files:**`` — comma-separated relative paths, omitted when
      the proposal has none.
    - Free-form description paragraph (the LLM's description text).
    - ``**Acceptance criteria:**`` — bulleted list, omitted when empty.
    - ``**Verification:**`` — bulleted list, populated with a single
      placeholder ``- TODO: add verification command`` so the block is not
      missing the field; the user replaces it before approving.

    Subtask IDs are emitted as ``{parent_task_id}.N`` (1-based index), per
    ``docs/prd-template.md`` section "ID Conventions" — ``T001.1, T001.2, …``.

    The output is paste-ready into the ``## Tasks`` section of
    ``.fakoli-state/prd.md``: no leading or trailing whitespace beyond a
    single blank line between blocks.
    """
    # Sub-tasks inherit the parent's priority by default (sub-tasks ship
    # under the parent's urgency); ``medium`` is the schema default when the
    # caller does not know the parent's priority (test paths, future callers
    # that only have a list of proposals).
    priority = parent_priority if parent_priority else "medium"
    blocks: list[str] = []
    for idx, sub in enumerate(proposals, start=1):
        sub_id = f"{parent_task_id}.{idx}"
        lines: list[str] = [f"### {sub_id}: {sub.title}", ""]
        # Feature is inherited from the parent in the PRD model.  When the
        # caller threads it through, emit ``**Feature:** <id>`` directly so
        # the paste-into-prd.md workflow has zero manual edits.  When
        # absent, emit the bare label as a placeholder.
        if parent_feature_id:
            lines.append(f"**Feature:** {parent_feature_id}")
        else:
            lines.append("**Feature:**")
        lines.append(f"**Priority:** {priority}")
        if sub.likely_files:
            lines.append("**Likely files:** " + ", ".join(sub.likely_files))
        # Free-form description paragraph (after fields, before acceptance).
        if sub.description:
            lines.append("")
            lines.append(sub.description)
        if sub.acceptance_criteria:
            lines.append("")
            lines.append("**Acceptance criteria:**")
            lines.append("")
            for crit in sub.acceptance_criteria:
                lines.append(f"- {crit}")
        # Verification placeholder — keeps the block schema-complete; the
        # human is expected to replace the TODO before `prd parse`.
        lines.append("")
        lines.append("**Verification:**")
        lines.append("")
        lines.append("- TODO: add verification command")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def expand(
    task_id: str = typer.Argument(..., help="Task ID to expand into subtasks."),  # noqa: B008
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
    use_llm: bool = typer.Option(  # noqa: B008
        False,
        "--use-llm",
        help=(
            "Use LLM augmentation (Anthropic) to propose 2-5 sub-tasks. "
            "Requires ANTHROPIC_API_KEY. Only tasks with complexity at/above "
            "the configured `auto_expand_threshold` (default 4) are "
            "decomposed; lower-complexity tasks return no proposals."
        ),
    ),
    format: str = typer.Option(  # noqa: B008, A002 — Typer convention; A002 ok for CLI flag
        "text",
        "--format",
        help=(
            "Output format: 'text' (default, human-readable per-subtask "
            "block) or 'prd' (markdown blocks matching docs/prd-template.md "
            "— paste directly into the ## Tasks section of "
            ".fakoli-state/prd.md)."
        ),
    ),
) -> None:
    """Expand a task into sub-task proposals via the LLM.

    Without ``--use-llm`` this command refuses with a clear error — the
    deterministic engine never invents sub-tasks; manual authoring in
    prd.md (T001.1, T001.2 …) is the deterministic path.

    With ``--use-llm`` the LLM is asked for 2-5 independently-claimable
    sub-task proposals.  Proposals are printed for the human to paste into
    prd.md; this command does NOT mutate state.  Tasks below the configured
    ``auto_expand_threshold`` (default 4; see ``.fakoli-state/config.yaml``)
    are deemed simple enough to ship as-is.

    With ``--format prd`` the output is rendered as ready-to-paste markdown
    blocks matching ``docs/prd-template.md``.  ``--format text`` (default)
    keeps the legacy per-subtask human-readable block.
    """
    # Validate --format early so the user sees a clean error before the
    # backend / provider initialisation cost.
    if format not in _EXPAND_VALID_FORMATS:
        typer.echo(
            f"Error: --format must be one of {{{', '.join(_EXPAND_VALID_FORMATS)}}}; "
            f"got {format!r}.",
            err=True,
        )
        raise typer.Exit(code=1)

    if not use_llm:
        typer.echo(
            "Error: expand requires --use-llm (Phase 7) OR manual subtask authoring "
            f"in prd.md as {task_id}.1, {task_id}.2 entries.",
            err=True,
        )
        raise typer.Exit(code=1)

    from fakoli_state.planning.inference import expand_task

    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir)

    config = _load_config_optional(state_dir)
    provider = _resolve_llm_provider(use_llm, config)

    # v1.21.0 — the expansion gate honors the project's configured
    # threshold instead of the historical hardcoded ``complexity >= 4``.
    _auto_expand, expand_threshold = _resolve_auto_expand(config)

    backend = _open_backend(state_dir)
    try:
        task = backend.get_task(task_id)
        if task is None:
            typer.echo(f"Error: task '{task_id}' not found.", err=True)
            raise typer.Exit(code=1)
    finally:
        backend.close()

    proposals = expand_task(task, provider=provider, threshold=expand_threshold)

    if not proposals:
        complexity = task.scores.complexity
        if complexity is None:
            typer.echo(
                f"Task {task_id} has no complexity score yet — "
                "run `fakoli-state score` first.",
            )
        elif complexity < expand_threshold:
            typer.echo(
                f"Task {task_id} complexity={complexity} is below the "
                f"expansion threshold (>= {expand_threshold}). "
                "No sub-tasks proposed.",
            )
        else:
            typer.echo(
                f"No sub-task proposals produced for {task_id} "
                "(see warnings on stderr).",
            )
        return

    if format == "prd":
        # PRD mode: emit ready-to-paste markdown blocks. Hint line points the
        # user at the destination file so the paste step is obvious.
        typer.echo(
            f"# {len(proposals)} sub-task block(s) for {task_id} — "
            "paste into the ## Tasks section of .fakoli-state/prd.md:\n"
        )
        typer.echo(
            _render_subtask_proposals_as_prd(
                task_id,
                proposals,
                parent_feature_id=task.feature_id,
                parent_priority=str(task.priority),
            )
        )
        return

    typer.echo(
        f"Proposed {len(proposals)} sub-task(s) for {task_id}. "
        "Paste into prd.md as ### TXxx blocks under the same ## Tasks section."
    )
    for idx, sub in enumerate(proposals, start=1):
        typer.echo(f"\n--- Sub-task {idx} ---")
        typer.echo(f"Title: {sub.title}")
        if sub.description:
            typer.echo(f"Description: {sub.description}")
        if sub.likely_files:
            typer.echo("Likely files: " + ", ".join(sub.likely_files))
        if sub.acceptance_criteria:
            typer.echo("Acceptance criteria:")
            for crit in sub.acceptance_criteria:
                typer.echo(f"  - {crit}")


# ---------------------------------------------------------------------------
# review tasks subcommand
# ---------------------------------------------------------------------------


@review_app.command("tasks")
def review_tasks(
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Promote tasks through the review lifecycle.

    Attempts to promote drafted → reviewed → ready for each eligible task.
    Gate for drafted → reviewed: acceptance_criteria non-empty AND
    verification.commands non-empty.

    Prints a summary of how many tasks were promoted and how many were blocked
    by gates (with reasons).

    With ``--json`` emits ``{"ok": true, "command": "review tasks", "data":
    {"promoted_to_reviewed": [...], "promoted_to_ready": [...],
    "blocked": [{"task_id": "...", "reason": "..."}]}}``.
    """
    from fakoli_state.clock import SystemClock
    from fakoli_state.state.models import EventDraft
    from fakoli_state.state.transitions import (
        TransitionError,
        task_drafted_to_reviewed,
        task_reviewed_to_ready,
    )

    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="review tasks", json_output=json_output)

    backend = _open_backend(state_dir)
    try:
        clock = SystemClock()
        all_tasks = backend.list_tasks()

        drafted_tasks = [t for t in all_tasks if t.status.value == "drafted"]
        reviewed_tasks = [t for t in all_tasks if t.status.value == "reviewed"]

        promoted_to_reviewed: list[str] = []
        promoted_to_ready: list[str] = []
        blocked: list[tuple[str, str]] = []  # (task_id, reason)

        # drafted → reviewed
        for task in drafted_tasks:
            now = clock.now()
            try:
                task_drafted_to_reviewed(task, now)
            except TransitionError as exc:
                blocked.append((task.id, exc.message))
                continue

            draft = EventDraft(
                timestamp=now,
                actor="fakoli-state-cli",
                action="task.status_changed",
                target_kind="task",
                target_id=task.id,
                payload_json={
                    "task_id": task.id,
                    "from": "drafted",
                    "to": "reviewed",
                    "reason": "review tasks: gate passed",
                },
            )
            backend.append(draft)
            promoted_to_reviewed.append(task.id)

        # reviewed → ready (includes tasks that just moved to reviewed above)
        # Re-query to get current state after the drafted → reviewed promotions.
        all_tasks_now = backend.list_tasks()
        newly_reviewed = [
            t for t in all_tasks_now
            if t.status.value == "reviewed"
            and (t.id in promoted_to_reviewed or t.id in [rt.id for rt in reviewed_tasks])
        ]

        for task in newly_reviewed:
            now = clock.now()
            try:
                task_reviewed_to_ready(task, now)
            except TransitionError as exc:
                blocked.append((task.id, exc.message))
                continue

            draft = EventDraft(
                timestamp=now,
                actor="fakoli-state-cli",
                action="task.status_changed",
                target_kind="task",
                target_id=task.id,
                payload_json={
                    "task_id": task.id,
                    "from": "reviewed",
                    "to": "ready",
                    "reason": "review tasks: promoted to ready",
                },
            )
            backend.append(draft)
            promoted_to_ready.append(task.id)
    finally:
        backend.close()

    if json_output:
        emit_success(
            "review tasks",
            {
                "promoted_to_reviewed": promoted_to_reviewed,
                "promoted_to_ready": promoted_to_ready,
                "blocked": [
                    {"task_id": tid, "reason": reason} for tid, reason in blocked
                ],
            },
        )
        return

    total_promoted = len(promoted_to_reviewed) + len(promoted_to_ready)
    typer.echo(f"Promoted {len(promoted_to_reviewed)} task(s) to reviewed.")
    typer.echo(f"Promoted {len(promoted_to_ready)} task(s) to ready.")
    if blocked:
        typer.echo(f"\nBlocked {len(blocked)} task(s):")
        for tid, reason in blocked:
            typer.echo(f"  {tid}: {reason}")
    else:
        typer.echo(f"\n{total_promoted} total promotion(s). No tasks blocked.")


# ---------------------------------------------------------------------------
# list subcommand
# ---------------------------------------------------------------------------


def list_tasks(
    status: str | None = typer.Option(  # noqa: B008
        None,
        "--status",
        help="Filter by task status (e.g. ready, drafted, reviewed).",
    ),
    feature: str | None = typer.Option(  # noqa: B008
        None,
        "--feature",
        help="Filter by feature ID (e.g. F001).",
    ),
    task_type: str | None = typer.Option(  # noqa: B008
        None,
        "--type",
        help="Filter by task type (feature, bugfix, refactor, modify).",
    ),
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """List tasks with optional status, feature, and type filters.

    Prints a table: TaskID | Title | Status | Priority | Type | Score | Feature.
    With ``--json`` emits ``{"ok": true, "command": "list", "data":
    {"tasks": [...], "count": N, "filters": {...}}}``.
    """
    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="list", json_output=json_output)

    backend = _open_backend(state_dir)
    try:
        tasks = backend.list_tasks(
            status=status, feature_id=feature, task_type=task_type
        )
    finally:
        backend.close()

    if json_output:
        emit_success(
            "list",
            {
                "tasks": dump_models(tasks),
                "count": len(tasks),
                "filters": {
                    "status": status,
                    "feature": feature,
                    "task_type": task_type,
                },
            },
        )
        return

    if not tasks:
        filters = []
        if status:
            filters.append(f"status={status}")
        if feature:
            filters.append(f"feature={feature}")
        if task_type:
            filters.append(f"type={task_type}")
        filter_str = " (" + ", ".join(filters) + ")" if filters else ""
        typer.echo(f"No tasks found{filter_str}.")
        return

    # Column widths.
    id_w = max(len("TaskID"), max(len(t.id) for t in tasks))
    title_w = min(40, max(len("Title"), max(len(t.title) for t in tasks)))
    status_w = max(len("Status"), max(len(t.status.value) for t in tasks))
    priority_w = max(len("Priority"), max(len(t.priority.value) for t in tasks))
    type_w = max(len("Type"), max(len(t.task_type.value) for t in tasks))
    feature_w = max(len("Feature"), max(len(t.feature_id) for t in tasks))

    header = (
        f"{'TaskID':<{id_w}}  "
        f"{'Title':<{title_w}}  "
        f"{'Status':<{status_w}}  "
        f"{'Priority':<{priority_w}}  "
        f"{'Type':<{type_w}}  "
        f"{'Score':>13}  "
        f"{'Feature':<{feature_w}}"
    )
    typer.echo(header)
    typer.echo("-" * len(header))

    for task in tasks:
        title_display = task.title[:title_w]
        complexity = task.scores.complexity
        agent_suit = task.scores.agent_suitability
        score_str = (
            f"{complexity}/{agent_suit}"
            if complexity is not None and agent_suit is not None
            else "unscored"
        )
        typer.echo(
            f"{task.id:<{id_w}}  "
            f"{title_display:<{title_w}}  "
            f"{task.status.value:<{status_w}}  "
            f"{task.priority.value:<{priority_w}}  "
            f"{task.task_type.value:<{type_w}}  "
            f"{score_str:>13}  "
            f"{task.feature_id:<{feature_w}}"
        )

    typer.echo(f"\n{len(tasks)} task(s) listed.")


# ---------------------------------------------------------------------------
# show subcommand
# ---------------------------------------------------------------------------


def show(
    task_id: str = typer.Argument(..., help="Task ID to display (e.g. T001)."),  # noqa: B008
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Print full task detail in human-readable multi-section format.

    Displays: title, feature, status, priority, scores breakdown (all six
    dimensions + explanation), dependencies, conflict groups, acceptance
    criteria, verification commands, likely files, claim (if any), and
    recent events.

    With ``--json`` emits ``{"ok": true, "command": "show", "data":
    {"task": {...}, "active_claims": [...], "recent_events": [...]}}``.
    A missing task yields ``{"ok": false, ... "error": {"code": "not_found"}}``
    and exit 1.
    """
    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="show", json_output=json_output)

    backend = _open_backend(state_dir)
    try:
        task = backend.get_task(task_id)
        if task is None:
            if json_output:
                fail("show", f"task '{task_id}' not found.", code="not_found")
            typer.echo(f"Error: task '{task_id}' not found.", err=True)
            raise typer.Exit(code=1)

        # Fetch active claims for this task.
        active_claims = backend.list_active_claims()
        task_claims = [c for c in active_claims if c.task_id == task.id]

        # Fetch recent events for this task via the Backend protocol.
        recent_events = backend.list_events(target_id=task.id, target_kind="task", limit=10)
    finally:
        backend.close()

    if json_output:
        emit_success(
            "show",
            {
                "task": dump_model(task),
                "active_claims": dump_models(task_claims),
                "recent_events": [
                    {"action": ev_action, "timestamp": str(ev_ts)}
                    for ev_action, ev_ts in recent_events
                ],
            },
        )
        return

    def _section(title: str) -> None:
        typer.echo(f"\n{title}")
        typer.echo("-" * len(title))

    typer.echo(f"Task {task.id}: {task.title}")
    typer.echo(f"Feature:  {task.feature_id}")
    typer.echo(f"Status:   {task.status.value}")
    typer.echo(f"Priority: {task.priority.value}")

    _section("Scores")
    s = task.scores
    if _scores_complete(task):
        typer.echo(f"  complexity:         {s.complexity}")
        typer.echo(f"  parallelizability:  {s.parallelizability}")
        typer.echo(f"  context_load:       {s.context_load}")
        typer.echo(f"  blast_radius:       {s.blast_radius}")
        typer.echo(f"  review_risk:        {s.review_risk}")
        typer.echo(f"  agent_suitability:  {s.agent_suitability}")
        if s.explanation:
            indented = s.explanation.replace("\n", "\n    ")
            typer.echo(f"\n  Explanation:\n    {indented}")
    else:
        typer.echo("  (not yet scored — run `fakoli-state score`)")

    _section("Dependencies")
    if task.dependencies:
        for dep_id in task.dependencies:
            typer.echo(f"  {dep_id}")
    else:
        typer.echo("  (none)")

    _section("Conflict Groups")
    if task.conflict_groups:
        for cg_id in task.conflict_groups:
            typer.echo(f"  {cg_id}")
    else:
        typer.echo("  (none)")

    _section("Acceptance Criteria")
    if task.acceptance_criteria:
        for criterion in task.acceptance_criteria:
            typer.echo(f"  - {criterion}")
    else:
        typer.echo("  (none — required before review)")

    _section("Verification Commands")
    if task.verification.commands:
        for cmd in task.verification.commands:
            typer.echo(f"  $ {cmd}")
    else:
        typer.echo("  (none — required before review)")

    _section("Likely Files")
    if task.likely_files:
        for f in task.likely_files:
            typer.echo(f"  {f}")
    else:
        typer.echo("  (none specified)")

    _section("Active Claims")
    if task_claims:
        for claim in task_claims:
            typer.echo(f"  {claim.id}: claimed by '{claim.claimed_by}' "
                       f"(expires {claim.lease_expires_at.isoformat()})")
    else:
        typer.echo("  (none)")

    _section("Recent Events")
    if recent_events:
        for ev_action, ev_ts in recent_events:
            typer.echo(f"  [{ev_ts}] {ev_action}")
    else:
        typer.echo("  (none)")
