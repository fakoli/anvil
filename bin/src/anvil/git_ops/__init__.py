"""Bounded Git observation and mutation primitives."""

from anvil.git_ops.worktree import (
    ClaimGitMutation,
    ClaimGitMutationTracker,
    ClaimGitPlan,
    ClaimPlanError,
    ClaimPlanPrecondition,
    GitWorktree,
    apply_claim_plan,
    canonical_git_root,
    claim_git_metadata,
    compensate_claim_plan,
    compensate_claim_plan_tracker,
    resolve_claim_plan,
    revalidate_claim_plan,
)

__all__ = [
    "ClaimGitPlan",
    "ClaimGitMutation",
    "ClaimGitMutationTracker",
    "ClaimPlanError",
    "ClaimPlanPrecondition",
    "GitWorktree",
    "apply_claim_plan",
    "canonical_git_root",
    "claim_git_metadata",
    "compensate_claim_plan",
    "compensate_claim_plan_tracker",
    "resolve_claim_plan",
    "revalidate_claim_plan",
]
