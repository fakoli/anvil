"""plan, score, expand, review tasks, list, show commands (Phase 3).

Phase 7 Wave 2: plan / score / expand grow a ``--use-llm`` flag that, when
set, resolves a provider via
:func:`anvil.planning.llm_planner.resolve_planner_provider` (default:
:class:`anvil.planning.llm.ClaudeAgentSDKProvider`, the subscription path)
and threads it into the underlying planning engine functions.  LLM
augmentation is *additive* — the deterministic baseline always runs first;
LLM enrichment is layered on top and may fail open with a stderr warning.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
import yaml

from anvil.cli._helpers import (
    PRD_OPTION,
    _open_backend,
    _require_state_dir,
    _resolve_state_dir,
    _scores_complete,
    canonical_prd_id,
    display_path,
    prd_source_path,
    resolve_actor,
    resolve_prd_id,
)
from anvil.cli._json import (
    JSON_OPTION,
    dump_model,
    dump_models,
    emit_success,
    fail,
    fail_with,
)
from anvil.state.backend import EventRejected
from anvil.state.models import TERMINAL_TASK_STATUSES
from anvil.state.rollup import PrdRollupEntry, compute_prd_rollup

if TYPE_CHECKING:
    from anvil.config import Config
    from anvil.planning.inference import BundlePlanReport, SubtaskProposal
    from anvil.planning.llm import LLMProvider
    from anvil.planning.scoring import ExpansionCandidate


# ---------------------------------------------------------------------------
# Shared helpers — config load + LLM provider resolution
# ---------------------------------------------------------------------------


def _load_config_optional(state_dir: Path) -> Config | None:
    """Load ``.anvil/config.yaml`` if it exists; return None on miss/error.

    Mirrors the soft-load pattern in cli/claim.py: an unreadable or absent
    config never blocks a CLI command — we fall back to env-only resolution
    so ad-hoc scratch projects (and CI without a checked-in config) keep
    working. A bad config emits a stderr warning so the user notices the
    problem without seeing a hard error.

    v1.17.0: load failures used to be silent; we now emit a warning naming
    the exception class + message so misconfigs surface during plan rather
    than during the next CLI invocation.

    T016/B17: the global-config layer (``~/.config/anvil/config.yaml``)
    is merged UNDER the project config so user-wide planning defaults (e.g.
    ``llm_tier`` / ``auto_expand_threshold``) apply here too, with the project
    config overriding them.
    """
    config_path = state_dir / "config.yaml"
    if not config_path.exists():
        return None
    try:
        from anvil.config import load_merged_config

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
            f"({type(exc).__name__}: {exc}); proceeding without it. LLM "
            "resolution falls back to the default agent-sdk provider (env "
            "auto-detect only runs with llm_fallback: true). Fix config.yaml "
            "and re-run to use config.",
            err=True,
        )
        return None


def _resolve_llm_provider(
    use_llm: bool,
    config: Config | None = None,
    model: str | None = None,
) -> LLMProvider | None:
    """Return an LLM provider when ``--use-llm`` is set, else None.

    Delegates to :func:`planning.llm_planner.resolve_planner_provider`
    so the same provider precedence (default ``agent-sdk``; optional
    ``anthropic`` / ``bedrock`` / ``custom`` via explicit config or the
    ``llm_fallback`` env chain) applies to ``--use-llm`` augmentation as to
    the LLM-planner backstop. Single source of truth for provider selection;
    no more divergent env-var checks per call site.

    The default ``agent-sdk`` provider always resolves, so the only exit-1
    path here is an explicitly-pinned ``bedrock``/``custom`` provider that
    cannot be built — the ``PlannerProviderUnavailable`` message names the
    fix.

    ``model`` is the optional ``--model`` CLI override; it is threaded as
    ``model_override`` and wins over the project's ``llm_model`` / ``llm_tier``.
    """
    if not use_llm:
        return None

    # Local import: keeps the provider SDKs out of the import graph for
    # deterministic-only invocations.
    from anvil.planning.llm_planner import (
        PlannerProviderUnavailable,
        resolve_planner_provider,
    )

    try:
        provider, _tier = resolve_planner_provider(config, model_override=model)
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
    from anvil.config import DEFAULT_AUTO_EXPAND_THRESHOLD

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
            f"    $ anvil expand {candidate.task_id} --use-llm"
        )
    typer.echo(
        f"\n{len(queue)} task(s) queued for expansion. Run the command(s) "
        "above to decompose, or set `auto_expand: false` in "
        ".anvil/config.yaml to silence this section."
    )

# review sub-app — registered in __init__.py as app.add_typer(review_app, name="review")
review_app = typer.Typer(
    name="review",
    help="Review lifecycle commands: tasks.",
    no_args_is_help=True,
)


def _render_bundle_plan(report: BundlePlanReport) -> None:
    typer.echo(
        f"  tasks={report.task_count} serial_depth={report.serial_depth} "
        f"overlap_pairs={report.overlap_pair_count}"
    )
    typer.echo(
        f"  proposed_bundles={len(report.proposed_bundles)} "
        f"expected_reviews={report.expected_review_count} "
        f"expected_checkpoints={report.expected_checkpoints}"
    )
    typer.echo(
        "  overlap_files="
        + (", ".join(report.overlap_files) if report.overlap_files else "none")
    )
    typer.echo(
        "  high_risk_policies="
        + (
            ", ".join(report.high_risk_policies)
            if report.high_risk_policies
            else "none"
        )
    )
    for proposal in report.proposed_bundles:
        typer.echo(
            f"  - {proposal.id}: tasks={','.join(proposal.task_ids)} "
            f"depth={proposal.serial_depth} reviews={proposal.expected_reviews} "
            f"angles={','.join(proposal.review_angles)} checkpoint=1"
        )
    for breach in report.limit_breaches:
        typer.echo(f"  LIMIT: {breach}")


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
    prd: str | None = typer.Option(  # noqa: B008
        None,
        "--prd",
        help=(
            "Named PRD to plan (multi-PRD). Reads .anvil/prds/<id>.md and "
            "scopes feature/task creation, orphan-prune, dependency "
            "inference, and proposed->drafted promotion to that PRD's "
            "partition; conflict-group inference still spans ALL PRDs. Omit "
            "for the default PRD (.anvil/prd.md), unchanged pre-v7 behaviour."
        ),
    ),
    use_llm: bool = typer.Option(  # noqa: B008
        False,
        "--use-llm",
        help=(
            "Augment planning with an LLM. Defaults to your Claude "
            "subscription via the Agent SDK (no API key; needs the `claude` "
            "CLI). Deterministic output is always produced first; LLM "
            "enrichment is additive."
        ),
    ),
    model: str | None = typer.Option(  # noqa: B008
        None,
        "--model",
        help=(
            "Override the LLM model for this run (wins over config "
            "llm_model/llm_tier). agent-sdk: a CLI model name like 'sonnet'/"
            "'opus' or a full id; anthropic/bedrock: a model id; custom: the "
            "route name your endpoint serves. Applies to both --use-llm "
            "augmentation and the no-tasks backstop. Omit to use the "
            "configured tier/model or the subscription default."
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
    propose_bundles: bool = typer.Option(  # noqa: B008
        False,
        "--bundles",
        help=(
            "Report deterministic coordinator bundle proposals, review cost, "
            "checkpoints, and throughput-limit breaches."
        ),
    ),
    acknowledge_bundle_limits: bool = typer.Option(  # noqa: B008
        False,
        "--acknowledge-bundle-limits",
        help=(
            "Audit explicit acceptance of an oversized bundle wave. Has no "
            "effect unless --bundles reports a configured limit breach."
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
    from anvil.clock import SystemClock
    from anvil.planning.inference import InferenceResult
    from anvil.planning.llm import LLMProviderError
    from anvil.planning.llm_planner import (
        PlannerProviderUnavailable,
        TaskGenerationError,
        generate_tasks_markdown,
    )
    from anvil.planning.template import parse_prd
    from anvil.state.models import EventDraft

    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="plan", json_output=json_output)

    # Non-fatal warnings collected for the JSON envelope (parse warnings that
    # otherwise go to stderr in human mode).
    plan_warnings: list[str] = []

    # T017: the parse-time prd_id controls id shape AND the partition that
    # plan scopes to. ``--prd v0.2`` reads .anvil/prds/v0.2.md and prunes /
    # promotes only that PRD's rows; the default ('prd' sentinel) keeps bare
    # ids and the default partition, byte-identical to pre-multi-PRD plan.
    parse_prd_id = prd if prd else "prd"

    prd_path = prd_source_path(state_dir, parse_prd_id)
    prd_display = display_path(prd_path)
    if not prd_path.exists():
        if json_output:
            fail(
                "plan",
                f"PRD file not found at {prd_display}. "
                "Author your PRD first, then run `anvil prd parse`.",
                code="not_found",
            )
        typer.echo(
            f"Error: PRD file not found at {prd_display}. "
            "Author your PRD first, then run `anvil prd parse`.",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        markdown = prd_path.read_text(encoding="utf-8")
    except OSError as exc:
        reason = exc.strerror or exc.__class__.__name__
        if json_output:
            fail("plan", f"cannot read {prd_display}: {reason}", code="io_error")
        typer.echo(f"Error: cannot read {prd_display}: {reason}", err=True)
        raise typer.Exit(code=1) from exc

    # v1.17.0: load config once and pass it to every LLM call site so the
    # project's llm_provider / llm_tier / bedrock_* / custom_* knobs apply
    # uniformly to both the --use-llm augmentation path and the no-tasks
    # backstop below.
    config_path = state_dir / "config.yaml"
    if propose_bundles and config_path.exists():
        try:
            from anvil.config import load_merged_config

            config = load_merged_config(config_path)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            message = f"invalid bundle planning config: {exc}"
            if json_output:
                fail("plan", message, code="invalid_bundle_config")
            typer.echo(f"Error: {message}", err=True)
            raise typer.Exit(code=1) from exc
    else:
        config = _load_config_optional(state_dir)

    provider = _resolve_llm_provider(use_llm, config, model=model)
    parsed = parse_prd(markdown, prd_id=parse_prd_id, provider=provider)

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
                model_override=model,
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
        except LLMProviderError as exc:
            # The default agent-sdk provider always *resolves* but can fail at
            # generate() time (missing `claude` CLI / SDK, bad --model, transport
            # error). Before the agent-sdk default flip this surfaced as a
            # PlannerProviderUnavailable at resolve time (caught above); now it
            # is an LLMProviderError from generate(). Catch it for the same clean
            # exit-1 instead of letting it escape as a raw traceback.
            if json_output:
                fail("plan", f"LLM call failed: {exc}", code="llm_error")
            typer.echo(f"Error: LLM call failed: {exc}", err=True)
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
            reason = exc.strerror or exc.__class__.__name__
            if json_output:
                fail(
                    "plan",
                    f"cannot re-read {prd_display}: {reason}",
                    code="io_error",
                )
            typer.echo(f"Error: cannot re-read {prd_display}: {reason}", err=True)
            raise typer.Exit(code=1) from exc

        from anvil.planning._plan_helpers import has_tasks_section
        if not has_tasks_section(current_markdown):
            new_markdown = (
                current_markdown.rstrip() + "\n\n" + gen_result.markdown + "\n"
            )
            try:
                prd_path.write_text(new_markdown, encoding="utf-8")
            except OSError as exc:
                reason = exc.strerror or exc.__class__.__name__
                if json_output:
                    fail(
                        "plan",
                        f"cannot write generated tasks to {prd_display}: {reason}",
                        code="io_error",
                    )
                typer.echo(
                    f"Error: cannot write generated tasks to {prd_display}: {reason}",
                    err=True,
                )
                raise typer.Exit(code=1) from exc

        # Re-parse so the rest of plan() consumes the freshly-appended tasks.
        try:
            markdown = prd_path.read_text(encoding="utf-8")
        except OSError as exc:
            reason = exc.strerror or exc.__class__.__name__
            if json_output:
                fail(
                    "plan",
                    f"cannot re-read {prd_display}: {reason}",
                    code="io_error",
                )
            typer.echo(f"Error: cannot re-read {prd_display}: {reason}", err=True)
            raise typer.Exit(code=1) from exc

        parsed = parse_prd(markdown, prd_id=parse_prd_id, provider=provider)
        llm_generated_count = len(parsed.tasks)
        llm_tier_used = gen_result.provider_used

    bundle_report = None
    if propose_bundles:
        from anvil.config import (
            DEFAULT_BUNDLE_MAX_SERIAL_STAGES,
            DEFAULT_BUNDLE_MAX_TASKS,
        )
        from anvil.planning.inference import (
            BundlePlanningError,
            build_bundle_plan,
        )

        try:
            bundle_report = build_bundle_plan(
                parsed.tasks,
                max_tasks=(
                    config.bundle_max_tasks if config else DEFAULT_BUNDLE_MAX_TASKS
                ),
                max_serial_stages=(
                    config.bundle_max_serial_stages
                    if config
                    else DEFAULT_BUNDLE_MAX_SERIAL_STAGES
                ),
            )
        except BundlePlanningError as exc:
            if json_output:
                fail("plan", str(exc), code="invalid_bundle_graph")
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        if bundle_report.limit_breaches and not acknowledge_bundle_limits:
            message = (
                "Bundle execution wave exceeds configured throughput limits; "
                "replan the graph or pass --acknowledge-bundle-limits."
            )
            if json_output:
                fail_with(
                    "plan",
                    message,
                    code="bundle_limits_exceeded",
                    extra={"bundle_plan": bundle_report.to_dict()},
                )
            typer.echo("Bundle planning report:")
            _render_bundle_plan(bundle_report)
            typer.echo(f"Error: {message}", err=True)
            raise typer.Exit(code=1)
    elif acknowledge_bundle_limits:
        message = "--acknowledge-bundle-limits requires --bundles."
        if json_output:
            fail("plan", message, code="bad_request")
        typer.echo(f"Error: {message}", err=True)
        raise typer.Exit(code=1)

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
        from anvil.planning._plan_helpers import (
            classify_orphans,
            emit_prune_events,
        )

        # T017: the partition this plan run owns. ``parsed.prd.id`` is the
        # MODEL prd_id ('default' for the default PRD, e.g. 'v0.2' for a named
        # one), already collapsed from the 'prd' parse sentinel by the parser.
        # Orphan-prune, dependency inference, and proposed->drafted promotion
        # all scope to this partition; conflict-group inference (below) does
        # NOT — it spans every PRD so cross-PRD file overlaps are detected.
        scope_prd_id = parsed.prd.id

        if (
            bundle_report is not None
            and bundle_report.limit_breaches
            and acknowledge_bundle_limits
        ):
            now = clock.now()
            acknowledged_by = resolve_actor(None)
            backend.append(
                EventDraft(
                    timestamp=now,
                    actor=acknowledged_by,
                    action="bundle.plan_acknowledged",
                    target_kind="prd",
                    target_id=scope_prd_id,
                    payload_json={
                        "prd_id": scope_prd_id,
                        "breaches": list(bundle_report.limit_breaches),
                        "acknowledged_by": acknowledged_by,
                        "created_at": now.isoformat(),
                    },
                )
            )

        # Scope orphan classification to THIS PRD: tasks/features in OTHER
        # PRDs must never be pruned just because they are absent from this
        # PRD's prd.md. Passing the prd_id-filtered lists means the diff only
        # ever flags entities that belong to the partition being re-parsed.
        classification = classify_orphans(
            backend.list_tasks(prd_id=scope_prd_id),
            {t.id for t in parsed.tasks},
            backend.list_features(prd_id=scope_prd_id),
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
                "  • Release the claim (`anvil release` or "
                "`anvil release --force`) so status returns to `ready`\n"
                "  • Complete the work (`anvil apply --approve` for "
                "needs_review tasks)\n"
                "  • Re-add the task to prd.md so it's no longer an orphan\n"
                "  • Run `anvil plan --prune-force` to delete the "
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
                actor="anvil-cli",
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

        # prd_id is Field(exclude=True) on Feature/Task, so model_dump() drops
        # it. Stamp it back into every feature/task payload so the SQL handler
        # writes the row into THIS PRD's partition instead of silently
        # defaulting to 'default' — the whole point of --prd scoping. (T017)
        def _with_prd_id(payload: dict[str, Any], model_prd_id: str) -> dict[str, Any]:
            payload["prd_id"] = model_prd_id
            return payload

        # Emit feature.created for each feature.
        for feature in parsed.features:
            now = clock.now()
            feature_data = _with_prd_id(
                feature.model_dump(mode="json"), feature.prd_id
            )
            draft = EventDraft(
                timestamp=now,
                actor="anvil-cli",
                action="feature.created",
                target_kind="feature",
                target_id=feature.id,
                payload_json=feature_data,
            )
            backend.append(draft)

        # Emit task.created for each task (status proposed at creation time).
        for task in parsed.tasks:
            now = clock.now()
            task_data = _with_prd_id(task.model_dump(mode="json"), task.prd_id)
            draft = EventDraft(
                timestamp=now,
                actor="anvil-cli",
                action="task.created",
                target_kind="task",
                target_id=task.id,
                payload_json=task_data,
            )
            backend.append(draft)

        # ------------------------------------------------------------------
        # Inference (T017): dependency inference + proposed->drafted promotion
        # run over THIS PRD's subset; conflict-group inference spans ALL PRDs.
        #
        # Dependencies are intra-PRD by construction — the subset's strict
        # likely_files subset edges. Conflict groups are coordination
        # signals: a task in PRD-A and a task in PRD-B that touch the same
        # file collide regardless of which PRD owns them, so the conflict
        # scan reads backend.list_tasks() (all partitions). We feed it the
        # in-memory subset (already carrying inferred deps) UNION the
        # already-persisted OTHER-PRD tasks, so a cross-PRD overlap lands in
        # a single CG-* group that both tasks reference.
        # ------------------------------------------------------------------
        from anvil.planning.inference import (
            infer_conflict_groups,
            infer_dependencies,
        )

        subset_with_deps = infer_dependencies(parsed.tasks)
        subset_ids = {t.id for t in subset_with_deps}

        # OTHER-PRD persisted tasks: everything NOT in this partition. The
        # just-emitted subset task.created rows are excluded by id so the
        # in-memory (deps-annotated) copies are the ones fed to the scan.
        other_prd_tasks = [
            t for t in backend.list_tasks() if t.id not in subset_ids
        ]

        all_with_cgs, conflict_groups = infer_conflict_groups(
            subset_with_deps + other_prd_tasks
        )
        cgs_by_id = {t.id: t for t in all_with_cgs}

        # Re-upsert THIS PRD's tasks with inferred dependencies + conflict
        # groups, then promote proposed -> drafted (subset only).
        for base_task in subset_with_deps:
            inferred_task = cgs_by_id[base_task.id]
            now = clock.now()
            task_data = _with_prd_id(
                inferred_task.model_dump(mode="json"), inferred_task.prd_id
            )
            upsert_draft = EventDraft(
                timestamp=now,
                actor="anvil-cli",
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
                    actor="anvil-cli",
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

        # Re-upsert OTHER-PRD tasks whose conflict_groups changed because a
        # cross-PRD overlap with this PRD pulled them into a new CG-* group.
        # The task.created upsert preserves status (it omits status from its
        # ON CONFLICT update set), so a claimed/in_progress sibling in another
        # PRD is not regressed — only its conflict_groups field is refreshed.
        # prd_id is stamped from the task's own partition, never this run's.
        for base_task in other_prd_tasks:
            inferred_task = cgs_by_id[base_task.id]
            if inferred_task.conflict_groups == base_task.conflict_groups:
                continue
            now = clock.now()
            task_data = _with_prd_id(
                inferred_task.model_dump(mode="json"), inferred_task.prd_id
            )
            backend.append(
                EventDraft(
                    timestamp=now,
                    actor="anvil-cli",
                    action="task.created",
                    target_kind="task",
                    target_id=inferred_task.id,
                    payload_json=task_data,
                )
            )

        inference_result = InferenceResult(
            tasks=[cgs_by_id[t.id] for t in subset_with_deps],
            conflict_groups=conflict_groups,
        )

        # CL-4 — persist the inferred ConflictGroups so the conflict_groups
        # table round-trips them (surfaced later by `anvil conflicts`). The
        # task rows already carry the group IDs in their conflict_groups field;
        # these events populate the dedicated table with the full group records.
        for cg in inference_result.conflict_groups:
            now = clock.now()
            backend.append(
                EventDraft(
                    timestamp=now,
                    actor="anvil-cli",
                    action="conflict_group.upserted",
                    target_kind="conflict_group",
                    target_id=cg.id,
                    payload_json=cg.model_dump(mode="json"),
                )
            )

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
                    f"via LLM, or author tasks manually in {prd_display}.",
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
                    **(
                        {
                            "bundle_plan": bundle_report.to_dict(),
                            "bundle_limits_acknowledged": bool(
                                bundle_report.limit_breaches
                                and acknowledge_bundle_limits
                            ),
                        }
                        if bundle_report is not None
                        else {}
                    ),
                },
            )
            return
        if llm_generated_count and llm_tier_used:
            typer.echo(
                f"Planned {len(parsed.features)} features, "
                f"{len(parsed.tasks)} tasks "
                f"({llm_generated_count} generated via LLM ({llm_tier_used}), "
                f"appended to {prd_display})."
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
                f"{prd_display}.",
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
        if bundle_report is not None:
            typer.echo("\nBundle planning report:")
            _render_bundle_plan(bundle_report)
            if bundle_report.limit_breaches and acknowledge_bundle_limits:
                typer.echo("Throughput-limit acknowledgement recorded.")
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
            "summary. Defaults to your Claude subscription via the Agent SDK "
            "(no API key; needs the `claude` CLI). The numeric scores "
            "themselves are never modified by the LLM."
        ),
    ),
    model: str | None = typer.Option(  # noqa: B008
        None,
        "--model",
        help=(
            "Override the LLM model for this run (wins over config "
            "llm_model/llm_tier). See `anvil plan --help` for the per-provider "
            "model-name conventions."
        ),
    ),
    prd: str | None = PRD_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """Score tasks across six dimensions using rule-based heuristics.

    Without TASK_ID: scores all tasks whose scores are incomplete.
    With TASK_ID: scores that single task.

    ``--prd`` (T019) scopes the all-tasks (no TASK_ID) scoring pass to one PRD
    partition via ``list_tasks(prd_id=...)``. Omitting it on a single-PRD
    project keeps the pre-T019 behaviour (all PRDs scored).

    With ``--use-llm`` the deterministic explanation is appended with a 1-3
    sentence trade-off summary from the LLM.  Numeric scores are unaffected.

    Emits a task.scored event per task and prints a summary table.

    v1.21.0: when ``auto_expand`` is enabled (config default: true), the
    summary table is followed by an EXPANSION QUEUE section listing every
    task whose complexity is at/above ``auto_expand_threshold`` (config
    default: 4) with the exact ``anvil expand TXXX --use-llm``
    follow-up command per task.  Queueing is deterministic — the LLM-side
    decomposition only happens when the expand command runs.
    """
    from anvil.clock import SystemClock
    from anvil.planning.scoring import build_recursive_expansion_queue, score_task
    from anvil.state.models import EventDraft

    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="score", json_output=json_output)

    config = _load_config_optional(state_dir)
    # `--model` only takes effect on the LLM path; warn rather than silently
    # ignore it when `--use-llm` is absent (score otherwise runs the
    # deterministic scorer and the override is a no-op).
    if model and not use_llm:
        typer.echo(
            "Warning: --model has no effect without --use-llm; "
            "the deterministic scorer ignores it.",
            err=True,
        )
    provider = _resolve_llm_provider(use_llm, config, model=model)

    backend = _open_backend(state_dir)
    try:
        clock = SystemClock()

        # T019: only scope the all-tasks pass when a PRD is explicitly named
        # (flag or $ANVIL_PRD). With no selection we pass prd_id=None so a
        # single-PRD project scores every task exactly as before. Collapse the
        # default sentinel ('prd') so `--prd prd` matches stored prd_id='default'.
        scoped_prd_id = canonical_prd_id(resolve_prd_id(backend, prd)) if prd else None

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
            all_tasks = backend.list_tasks(prd_id=scoped_prd_id)
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
                actor="anvil-cli",
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
# assumptions subcommand (SL-6) — rank PRD requirements by risk before planning
# ---------------------------------------------------------------------------


_ASSUMPTIONS_DEFAULT_LIMIT = 5


def assumptions(
    limit: int = typer.Option(  # noqa: B008
        _ASSUMPTIONS_DEFAULT_LIMIT,
        "--limit",
        "-n",
        help=(
            "Show the top-N highest-risk assumptions. Pass 0 (or a negative "
            f"number) to show all. Default {_ASSUMPTIONS_DEFAULT_LIMIT}."
        ),
    ),
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Rank PRD requirements by ``blast_radius * uncertainty`` (SL-6).

    Surfaces the highest-blast-radius, lowest-confidence assumptions BEFORE
    planning so a human can pin them down early. Each requirement gets a
    deterministic, rule-based ``blast_radius`` and ``uncertainty`` (both 1-5);
    the ranking key ``priority`` is their product. Uncertainty rises with
    unresolved ``[NEEDS DECISION]`` / ``TBD`` markers, hedging or vague
    language, broad sweeping scope, and underspecified one-liners; it falls
    when the requirement carries concrete, testable language.

    This command is purely ADVISORY — it never blocks claims or mutates state.
    With ``--json`` it emits ``{"ok": true, "command": "assumptions", "data":
    {"assumptions": [...], "count": N, "limit": L}}``. A project with no parsed
    requirements prints a friendly empty result and exits 0.
    """
    from anvil.planning.scoring import rank_assumptions

    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="assumptions", json_output=json_output)

    backend = _open_backend(state_dir)
    try:
        requirements = backend.list_requirements()
    finally:
        backend.close()

    # ``--limit 0`` (or negative) means "show all" — normalise to None so the
    # scorer does not truncate.
    effective_limit = limit if limit > 0 else None
    ranked = rank_assumptions(requirements, limit=effective_limit)

    if json_output:
        emit_success(
            "assumptions",
            {
                "assumptions": [
                    {
                        "requirement_id": a.requirement_id,
                        "text": a.text,
                        "blast_radius": a.blast_radius,
                        "uncertainty": a.uncertainty,
                        "priority": a.priority,
                        "reasons": a.reasons,
                    }
                    for a in ranked
                ],
                "count": len(ranked),
                "limit": limit,
            },
        )
        return

    if not requirements:
        typer.echo(
            "No PRD requirements found. Author a `## Requirements` section in "
            ".anvil/prd.md and run `anvil prd parse` first."
        )
        return

    header = "HIGH-RISK ASSUMPTIONS (ranked by blast_radius x uncertainty)"
    typer.echo(header)
    typer.echo("-" * len(header))
    col = (
        f"{'ReqID':<8} "
        f"{'Blast':>5} "
        f"{'Uncert':>6} "
        f"{'Priority':>8}  "
        "Assumption"
    )
    typer.echo(col)
    typer.echo("-" * len(col))
    for a in ranked:
        title = a.text if len(a.text) <= 50 else a.text[:47] + "..."
        typer.echo(
            f"{a.requirement_id:<8} "
            f"{a.blast_radius:>5} "
            f"{a.uncertainty:>6} "
            f"{a.priority:>8}  "
            f"{title}"
        )
        typer.echo(f"{'':<8} {'':>5} {'':>6} {'':>8}  why: {', '.join(a.reasons)}")

    shown = (
        f"Showing top {len(ranked)} of {len(requirements)} requirement(s)."
        if len(ranked) < len(requirements)
        else f"Showing all {len(requirements)} requirement(s) by priority."
    )
    typer.echo(
        f"\n{shown} "
        "Advisory only — address the high-priority assumptions before planning."
    )


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
    ``.anvil/prd.md``: no leading or trailing whitespace beyond a
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
            "Use LLM augmentation to propose 2-5 sub-tasks. Defaults to your "
            "Claude subscription via the Agent SDK (no API key; needs the "
            "`claude` CLI). Only tasks with complexity at/above the configured "
            "`auto_expand_threshold` (default 4) are decomposed; "
            "lower-complexity tasks return no proposals."
        ),
    ),
    model: str | None = typer.Option(  # noqa: B008
        None,
        "--model",
        help=(
            "Override the LLM model for this run (wins over config "
            "llm_model/llm_tier). See `anvil plan --help` for the per-provider "
            "model-name conventions."
        ),
    ),
    format: str = typer.Option(  # noqa: B008, A002 — Typer convention; A002 ok for CLI flag
        "text",
        "--format",
        help=(
            "Output format: 'text' (default, human-readable per-subtask "
            "block) or 'prd' (markdown blocks matching docs/prd-template.md "
            "— paste directly into the ## Tasks section of "
            ".anvil/prd.md)."
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
    ``auto_expand_threshold`` (default 4; see ``.anvil/config.yaml``)
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

    from anvil.planning.inference import expand_task

    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir)

    config = _load_config_optional(state_dir)
    provider = _resolve_llm_provider(use_llm, config, model=model)

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
                "run `anvil score` first.",
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
            "paste into the ## Tasks section of .anvil/prd.md:\n"
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


def confirm_task_risk_scores(backend: Any, task: Any, now: Any, actor: str) -> None:
    """Confirm a task's engine risk scores (T009): re-emit ``task.scored`` with
    ``blast_radius_confirmed`` / ``review_risk_confirmed`` set, preserving the
    other dimensions. Emits nothing for a task without engine risk scores.

    Shared by the ``review tasks`` gate and the ``init --with-sample`` seeder so
    the two promotion paths cannot drift. NOTE on semantics: promoting a task to
    ready ACCEPTS the engine's heuristic risk scores as trustworthy for ceiling
    routing — the readiness gate checks acceptance criteria + verification, not
    the risk numbers, so this is a lightweight acceptance, not a per-dimension
    human risk sign-off. A later re-score preserves these flags via the merge in
    ``_write_task_scored``.
    """
    from anvil.state.models import EventDraft

    scores = task.scores
    if scores is None or scores.blast_radius is None or scores.review_risk is None:
        return
    score_dict = scores.model_dump()
    explanation = score_dict.pop("explanation", None)
    score_dict["blast_radius_confirmed"] = True
    score_dict["review_risk_confirmed"] = True
    backend.append(
        EventDraft(
            timestamp=now,
            actor=actor,
            action="task.scored",
            target_kind="task",
            target_id=task.id,
            payload_json={
                "task_id": task.id,
                "scores": score_dict,
                "explanation": explanation,
            },
        )
    )


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
    from anvil.clock import SystemClock
    from anvil.state.models import EventDraft
    from anvil.state.transitions import (
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

        # GAP-09: capture the PRD status while the backend is open so we can
        # nudge the user to approve a still-draft PRD after promotion (a hint,
        # not a gate — tasks are still promoted regardless).
        #
        # T021 audit (get_prd no-arg): default-only-correct. `review tasks`
        # promotes drafted/reviewed tasks across ALL PRDs (it is not --prd
        # scoped); the PRD status here only feeds a post-promotion approval
        # hint, so reading the default PRD's status is the right summary signal.
        prd = backend.get_prd()
        prd_status = prd.status.value if prd is not None else None

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
                actor="anvil-cli",
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
                actor="anvil-cli",
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

            # T009 — confirm the engine risk scores at the review gate so the B45
            # ceiling is live for a ceilinged runner (an unconfirmed task is
            # frontier-only). See confirm_task_risk_scores for the exact semantics
            # (a lightweight acceptance, not a human per-dimension sign-off).
            confirm_task_risk_scores(backend, task, now, "anvil-cli")
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

    # GAP-09: a still-draft PRD after planning + task review is an easy thing
    # to forget. Surface a one-line hint (never a hard gate) pointing at the
    # next workflow step so the PRD doesn't silently stay in draft.
    if prd_status == "draft":
        typer.echo(
            "\nHint: PRD is still in draft. Run `anvil prd review` then "
            "`anvil prd review --approve` to approve it before claiming tasks."
        )


# ---------------------------------------------------------------------------
# list subcommand
# ---------------------------------------------------------------------------


def list_tasks(
    status: str | None = typer.Option(  # noqa: B008
        None,
        "--status",
        help="Filter by task status (e.g. ready, drafted, reviewed).",
    ),
    open_only: bool = typer.Option(  # noqa: B008
        False,
        "--open",
        help=(
            "Show only unfinished tasks (hide done/accepted; rejected tasks"
            " await rework and stay open)."
        ),
    ),
    summary: bool = typer.Option(  # noqa: B008
        False,
        "--summary",
        help="Roll up counts per PRD instead of listing every task.",
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
    prd: str | None = PRD_OPTION,
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

    ``--prd`` (T019) scopes the listing to one PRD partition via
    ``list_tasks(prd_id=...)``. Omitting it on a single-PRD project keeps the
    pre-T019 behaviour (all PRDs listed).

    ``--open`` hides terminal tasks (``TERMINAL_TASK_STATUSES``: done and
    accepted — rejected tasks await rework, so they stay open). ``--summary``
    rolls tasks up per PRD via the shared :func:`compute_prd_rollup` helper;
    the Total column always shows true per-PRD totals, and combining with
    ``--open`` hides PRDs that have nothing open. In summary mode the
    ``--json`` payload is ``{"summary": [{"prd", "open", "total",
    "by_status"}, ...], "prd_count": N, "open": X, "total": Y,
    "filters": {...}}`` under the same envelope.
    """
    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="list", json_output=json_output)

    backend = _open_backend(state_dir)
    try:
        # T019: only scope when a PRD is explicitly named (flag or $ANVIL_PRD).
        # No selection -> prd_id=None -> all PRDs, byte-identical to pre-T019.
        # Collapse the default sentinel ('prd') so `--prd prd` matches stored
        # prd_id='default' rather than filtering on a nonexistent id='prd'.
        scoped_prd_id = canonical_prd_id(resolve_prd_id(backend, prd)) if prd else None
        tasks = backend.list_tasks(
            status=status,
            feature_id=feature,
            task_type=task_type,
            prd_id=scoped_prd_id,
        )
        prds = backend.list_prds() if summary else []
    finally:
        backend.close()

    filters_payload = {
        "status": status,
        "open": open_only,
        "feature": feature,
        "task_type": task_type,
        "prd": scoped_prd_id,
    }

    if summary:
        # The rollup sees the fetched (status/feature/type/prd-filtered) tasks
        # UNFILTERED by --open, so Total stays the true per-PRD count; --open
        # only decides which rows are shown.
        _emit_task_summary(
            compute_prd_rollup(prds, tasks, []),
            open_only=open_only,
            json_output=json_output,
            filters=filters_payload,
        )
        return

    if open_only:
        tasks = [t for t in tasks if t.status not in TERMINAL_TASK_STATUSES]

    if json_output:
        emit_success(
            "list",
            {
                "tasks": dump_models(tasks),
                "count": len(tasks),
                "filters": filters_payload,
            },
        )
        return

    if not tasks:
        filters = []
        if status:
            filters.append(f"status={status}")
        if open_only:
            filters.append("open")
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


def _emit_task_summary(
    entries: list[PrdRollupEntry],
    *,
    open_only: bool,
    json_output: bool,
    filters: dict[str, Any],
) -> None:
    """Render per-PRD rollup entries: open count, total, status breakdown.

    Consumes :func:`compute_prd_rollup` (the shared helper behind ``anvil
    status`` and the MCP project-summary tools) so the per-PRD numbers never
    drift between surfaces; only presentation lives here. PRDs with open work
    sort first (then by id) so "is there anything left?" is the top row.
    ``open_only`` hides fully-terminal PRDs from display but the open/total
    footer always reports the whole fetched set — Total never lies.
    """
    rows = []
    for entry in entries:
        open_n = entry.total_tasks - sum(
            entry.task_counts.get(s, 0) for s in TERMINAL_TASK_STATUSES
        )
        # task_counts is exhaustive over TaskStatus; show only what's present.
        counts = {s: n for s, n in entry.task_counts.items() if n}
        rows.append((entry.prd_id or "(none)", open_n, entry.total_tasks, counts))
    # Open work first, then alphabetical — the busy PRDs float to the top.
    rows.sort(key=lambda r: (-r[1], r[0]))

    open_total = sum(r[1] for r in rows)
    grand_total = sum(r[2] for r in rows)
    if open_only:
        rows = [r for r in rows if r[1] > 0]

    if json_output:
        emit_success(
            "list",
            {
                "summary": [
                    {"prd": p, "open": o, "total": t, "by_status": c}
                    for p, o, t, c in rows
                ],
                "prd_count": len(rows),
                "open": open_total,
                "total": grand_total,
                "filters": filters,
            },
        )
        return

    if not rows:
        typer.echo("No open tasks." if open_only else "No tasks found.")
        return

    prd_w = max(len("PRD"), max(len(r[0]) for r in rows))
    header = f"{'PRD':<{prd_w}}  {'Open':>5}  {'Total':>5}  Breakdown"
    typer.echo(header)
    typer.echo("-" * len(header))
    for prd_id, open_n, total, counts in rows:
        # task_counts preserves TaskStatus declaration order — lifecycle order.
        breakdown = ", ".join(f"{s}:{n}" for s, n in counts.items())
        typer.echo(f"{prd_id:<{prd_w}}  {open_n:>5}  {total:>5}  {breakdown}")
    typer.echo(
        f"\n{len(rows)} PRD(s), {open_total} open of {grand_total} total."
    )


# ---------------------------------------------------------------------------
# show subcommand
# ---------------------------------------------------------------------------


def show(
    task_id: str = typer.Argument(..., help="Task ID to display (e.g. T001)."),  # noqa: B008
    prd: str | None = PRD_OPTION,
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

    ``--prd`` (T019) asserts the task belongs to the named PRD partition: an
    explicit ``--prd``/``$ANVIL_PRD`` that doesn't match the task's ``prd_id``
    is a ``not_found`` error (task IDs are globally unique, so the lookup itself
    is unscoped). Omitting it keeps the pre-T019 behaviour unchanged.
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

        # T019: when a PRD is explicitly named, assert the task lives in that
        # partition (get_task is unscoped because IDs are unique). A mismatch
        # is reported as not_found so callers can't silently read across PRDs.
        # Collapse the default sentinel ('prd') so `--prd prd` matches a task
        # stored with prd_id='default' instead of a false mismatch.
        if prd:
            scoped_prd_id = canonical_prd_id(resolve_prd_id(backend, prd))
            if task.prd_id and task.prd_id != scoped_prd_id:
                msg = (
                    f"task '{task_id}' belongs to PRD '{task.prd_id}', "
                    f"not '{scoped_prd_id}'."
                )
                if json_output:
                    fail("show", msg, code="not_found")
                typer.echo(f"Error: {msg}", err=True)
                raise typer.Exit(code=1)

        # Fetch active claims for this task.
        active_claims = backend.list_active_claims()
        task_claims = [c for c in active_claims if c.task_id == task.id]

        # Fetch recent events for this task via the Backend protocol.
        recent_events = backend.list_events(target_id=task.id, target_kind="task", limit=10)
    finally:
        backend.close()

    # retro-opps T003 — derive-only review tier, recomputed at every read from
    # the loaded config (None → module defaults).
    from anvil.cli._helpers import _load_config_optional
    from anvil.planning.scoring import review_tier

    task_review_tier = review_tier(
        task, config=_load_config_optional(state_dir)
    )

    if json_output:
        emit_success(
            "show",
            {
                "task": dump_model(task),
                "review_tier": task_review_tier,
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
    typer.echo(f"Review tier: {task_review_tier}")

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
        typer.echo("  (not yet scored — run `anvil score`)")

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


# ---------------------------------------------------------------------------
# deps subcommand — batch dependency-edit primitive (backlog T022/F007)
# ---------------------------------------------------------------------------


def deps(
    add: list[str] = typer.Option(  # noqa: B008
        None,
        "--add",
        help="Add 'SOURCE->TARGET' (source depends on target). Repeatable. "
        "The arrow is required for scoped IDs containing ':'; the "
        "'SOURCE:TARGET' shorthand is only unambiguous for unscoped IDs.",
    ),
    remove: list[str] = typer.Option(  # noqa: B008
        None,
        "--remove",
        help="Remove 'SOURCE->TARGET'. Repeatable. The arrow is required for "
        "scoped IDs containing ':'; 'SOURCE:TARGET' is unscoped shorthand.",
    ),
    actor: str = typer.Option(  # noqa: B008
        "anvil-cli",
        "--actor",
        help="Actor recorded on the emitted events.",
    ),
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Apply dependency edge edits after whole-batch validation.

    Accepts multiple ``--add`` and ``--remove`` edges. The canonical form is
    ``SOURCE->TARGET``, meaning *source depends on target*. The arrow is required
    when either ID is scoped and contains ``:``; ``SOURCE:TARGET`` remains an
    unambiguous shorthand only for unscoped IDs. The whole request is validated
    before mutation: unknown tasks, self-dependencies, or a resulting cycle
    reject it with NO mutation. After validation, one ``task.created`` upsert is
    appended per changed task (status is preserved). Those appends commit
    separately, so a later append failure can leave earlier task changes
    committed; successful multi-task persistence is not atomic.

    With ``--json`` emits ``{"ok": true, "command": "deps", "data":
    {"changed": [...], "added": [["S","T"], ...], "removed": [...]}}``. A
    rejected batch yields ``{"ok": false, ... "error": {"code": "cycle" |
    "unknown_task" | "self_loop" | "bad_request" | "event_rejected", ...}}``
    and exit 1. ``event_rejected`` uses fixed prose rather than exposing
    backend validation details. A request may contain at most 10,000 edges;
    larger batches fail before state access with ``bad_request``.
    """
    from anvil.clock import SystemClock
    from anvil.planning._plan_helpers import (
        DEPENDENCY_BATCH_LIMIT_MESSAGE,
        DEPENDENCY_EVENT_REJECTED_CODE,
        DEPENDENCY_EVENT_REJECTED_MESSAGE,
        MAX_DEPENDENCY_EDGES_PER_BATCH,
        BatchDepError,
        emit_batch_dep_events,
        parse_dep_edge,
        plan_batch_dep_edits,
    )

    add = add or []
    remove = remove or []
    if not add and not remove:
        msg = "no edges supplied; pass at least one --add or --remove."
        if json_output:
            fail("deps", msg, code="bad_request")
        typer.echo(f"Error: {msg}", err=True)
        raise typer.Exit(code=2)
    if len(add) + len(remove) > MAX_DEPENDENCY_EDGES_PER_BATCH:
        if json_output:
            fail("deps", DEPENDENCY_BATCH_LIMIT_MESSAGE, code="bad_request")
        typer.echo(f"Error: {DEPENDENCY_BATCH_LIMIT_MESSAGE}", err=True)
        raise typer.Exit(code=2)

    # Parse the edge specs first so a malformed spec fails before opening state.
    try:
        edges = [parse_dep_edge(raw, "add") for raw in add] + [
            parse_dep_edge(raw, "remove") for raw in remove
        ]
    except BatchDepError as exc:
        if json_output:
            fail("deps", exc.message, code=exc.code)
        typer.echo(f"Error: {exc.message}", err=True)
        raise typer.Exit(code=2) from exc

    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="deps", json_output=json_output)

    backend = _open_backend(state_dir)
    try:
        clock = SystemClock()
        all_tasks = backend.list_tasks()
        tasks_by_id = {t.id: t for t in all_tasks}

        # Plan + validate the WHOLE batch before emitting anything. A raised
        # BatchDepError here means zero events were appended → no partial apply.
        try:
            batch_plan = plan_batch_dep_edits(all_tasks, edges)
        except BatchDepError as exc:
            if json_output:
                fail("deps", exc.message, code=exc.code)
            typer.echo(f"Error: {exc.message}", err=True)
            raise typer.Exit(code=1) from exc

        event_rejected = False
        try:
            changed = emit_batch_dep_events(
                backend, tasks_by_id, batch_plan, actor=actor, clock=clock
            )
        except EventRejected:
            # Leave the exception context before emitting either CLI surface;
            # the backend reason may contain implementation details even though
            # neither user-facing message does.
            event_rejected = True
        if event_rejected:
            if json_output:
                fail(
                    "deps",
                    DEPENDENCY_EVENT_REJECTED_MESSAGE,
                    code=DEPENDENCY_EVENT_REJECTED_CODE,
                )
            typer.echo(f"Error: {DEPENDENCY_EVENT_REJECTED_MESSAGE}", err=True)
            raise typer.Exit(code=1)
    finally:
        backend.close()

    if json_output:
        emit_success(
            "deps",
            {
                "changed": changed,
                "added": [list(e) for e in batch_plan.added],
                "removed": [list(e) for e in batch_plan.removed],
            },
        )
        return

    typer.echo(
        f"Applied {len(batch_plan.added)} add(s) and "
        f"{len(batch_plan.removed)} remove(s) across {len(changed)} task(s)."
    )
    if changed:
        typer.echo("Changed tasks: " + ", ".join(changed))
    else:
        typer.echo("No dependency changes (all edges were no-ops).")
