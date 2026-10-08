"""Pure timing from one bounded native frontier. Observations are not authority."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from anvil.state.models import ClaimCommandEvidenceCore
from anvil.state.payloads import (
    BundleClaimedPayload,
    BundleClaimReleasedPayload,
    BundleClaimRenewedPayload,
    BundleClaimStalePayload,
    ClaimCreatedPayload,
)
from anvil.timing_receipts import CommandTimingReceipt


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("timing requires an aware native timestamp")
    return parsed.astimezone(UTC)


def _us(delta: timedelta) -> int:
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _interval(start: str | None, end: str | None, *, kind: str) -> dict[str, Any]:
    status, elapsed = "unknown", None
    if start is not None and end is not None:
        elapsed = _us(_utc(end) - _utc(start))
        status = "clock_skew" if elapsed < 0 else kind
        if elapsed < 0:
            elapsed = None
    return {"started_at": start, "ended_at": end, "status": status, "elapsed_us": elapsed}


def _union(intervals: list[tuple[datetime, datetime]]) -> int | None:
    if not intervals:
        return None
    total = 0
    start, end = sorted(intervals)[0]
    for following, stop in sorted(intervals)[1:]:
        if following > end:
            total += _us(end - start)
            start, end = following, stop
        else:
            end = max(end, stop)
    return total + _us(end - start)


def _bound(attribution: dict, claim: dict, project_id: str) -> bool:
    context = claim.get("attestation_context")
    return bool(context) and all(
        attribution.get(key) == value
        for key, value in {
            "project_id": project_id, "task_id": claim["task_id"],
            "claim_id": claim["id"], "generation": claim["generation"],
            "claimed_by": claim["claimed_by"],
            **{key: context[key] for key in (
                "repository_id", "claim_start_sha", "prd_id", "prd_revision", "task_revision",
            )},
        }.items()
    )


def project_attempt_timing(
    *, project_id: str, claims: list[dict], events: list[dict],
    evidence: list[dict], reviews: list[dict], observation_at: str | None = None,
) -> dict[str, Any]:
    """Retain every generation and exact review attempt; never guess an owner.

    Inputs come from the existing query-only, capped attempt reader. No clock,
    backend scan, raw command output or filesystem path is introduced here.
    Callers can supply an explicit observation boundary for running claim age;
    an omitted boundary leaves a stable read and an unknown running duration.
    """
    if observation_at is not None:
        ClaimCommandEvidenceCore._validate_command_time(observation_at)
    by_claim = {claim["id"]: claim for claim in claims}
    if len({(claim["task_id"], claim["generation"]) for claim in claims}) != len(claims):
        raise ValueError("ambiguous claim generation")
    created, terminal, leases = {}, {}, {}
    observations: dict[str, list[dict]] = {claim["id"]: [] for claim in claims}
    seen = {}
    unknown = 0
    for event in events:
        payload, action = event["payload"], event["action"]
        if action == "claim.created":
            source = ClaimCreatedPayload.model_validate(payload).model_dump(mode="json")
            claim = by_claim.get(source["id"])
            if claim is None or source["id"] in created or any(
                source.get(key) != claim.get(key)
                for key in ("task_id", "claimed_by", "attestation_context")
            ) or (source["generation"] is not None and source["generation"] != claim["generation"]):
                raise ValueError("claim timing projection disagrees with its creation event")
            if _utc(source["created_at"]) != _utc(claim["created_at"]):
                raise ValueError("claim timing boundary disagrees with creation")
            if event["target_kind"] != "claim" or event["target_id"] != claim["id"]:
                raise ValueError("claim timing target disagrees with creation")
            created[claim["id"]] = event
            leases[claim["id"]] = source["lease_expires_at"]
        elif action == "bundle.claimed":
            source = BundleClaimedPayload.model_validate(payload).model_dump(mode="json")
            if (event["target_kind"] != "bundle" or event["target_id"] != source["bundle_id"]
                or event["actor"] != source["claimed_by"]):
                raise ValueError("bundle timing target disagrees with creation")
            for member in source["member_claims"]:
                claim = by_claim.get(member["id"])
                if claim is None:
                    continue
                if (
                    claim["id"] in created or claim["bundle_claim_id"] != source["id"]
                    or claim["task_id"] != member["task_id"]
                    or claim["claimed_by"] != source["claimed_by"]
                    or claim.get("attestation_context") is not None
                    or _utc(claim["created_at"]) != _utc(source["created_at"])
                ):
                    raise ValueError("bundle child timing disagrees with creation")
                created[claim["id"]] = event
                leases[claim["id"]] = source["lease_expires_at"]
        elif action in {"bundle.claim_renewed", "bundle.claim_released", "bundle.claim_stale"}:
            model = {"bundle.claim_renewed": BundleClaimRenewedPayload,
                     "bundle.claim_released": BundleClaimReleasedPayload,
                     "bundle.claim_stale": BundleClaimStalePayload}[action]
            source = model.model_validate(payload).model_dump(mode="json")
            actor_field = {"bundle.claim_renewed": "renewed_by",
                           "bundle.claim_released": "released_by",
                           "bundle.claim_stale": "actor"}[action]
            if event["actor"] != source[actor_field]:
                raise ValueError("bundle timing lifecycle actor mismatch")
            for claim in claims:
                if claim.get("bundle_claim_id") != source["bundle_claim_id"]:
                    continue
                creation = created.get(claim["id"])
                if (creation is None or event["target_kind"] != "bundle"
                    or event["target_id"] != creation["payload"]["bundle_id"]
                    or source["bundle_id"] != event["target_id"]
                    or (action != "bundle.claim_stale"
                        and not (action == "bundle.claim_released" and source["force"])
                        and source[actor_field] != claim["claimed_by"])):
                    raise ValueError("bundle timing lifecycle binding mismatch")
                if action == "bundle.claim_renewed":
                    leases[claim["id"]] = source["lease_expires_at"]
                else:
                    terminal.setdefault(claim["id"], event)
        elif action in {"claim.released", "claim.stale"}:
            terminal.setdefault(payload["claim_id"], event)
        elif action == "evidence.submitted":
            claim = by_claim.get(payload.get("claim_id"))
            if claim is not None and claim.get("bundle_claim_id") is None:
                # Native evidence submission auto-releases an ordinary claim.
                # It does not prove owner-root release or a stopped runner.
                terminal.setdefault(claim["id"], event)
        elif action == "claim.renewed":
            leases[payload["claim_id"]] = payload["lease_expires_at"]
        elif action == "progress.noted":
            raw = payload.get("timing")
            if raw is None:
                # Free-form phase names are labels, not standardized boundaries.
                continue
            receipt = CommandTimingReceipt.model_validate(raw)
            attr = receipt.attribution.model_dump(mode="json")
            claim = by_claim.get(attr["claim_id"])
            valid = (
                claim is not None and claim["id"] in created and claim["id"] not in terminal
                and _bound(attr, claim, project_id)
                and event["target_kind"] == "task" and event["target_id"] == claim["task_id"]
                and payload.get("task_id") == claim["task_id"]
                and event["actor"] == payload.get("actor") == claim["claimed_by"]
                and _utc(payload["noted_at"]) == _utc(event["timestamp"])
                and _utc(claim["created_at"]) <= _utc(receipt.started_at)
                <= _utc(event["timestamp"])
                and _utc(event["timestamp"]) < _utc(leases[claim["id"]])
                and (receipt.ended_at is None or
                     _utc(claim["created_at"]) <= _utc(receipt.ended_at)
                     <= _utc(event["timestamp"]))
            )
            if not valid:
                unknown += 1
                continue
            key = (claim["id"], claim["generation"], receipt.receipt_id)
            digest = receipt.semantic_digest()
            if key in seen:
                if seen[key] != digest:
                    raise ValueError("conflicting repeated timing observation")
                continue
            seen[key] = digest
            observations[claim["id"]].append({
                "receipt_id": receipt.receipt_id, "event_id": event["id"],
                "command_sha256": receipt.command_sha256,
                "outcome": receipt.outcome, "classification": receipt.classification or "unknown",
                "monotonic_elapsed_us": receipt.elapsed_us, "exit_code": receipt.exit_code,
                "utc_interval": _interval(receipt.started_at, receipt.ended_at, kind="observed"),
            })

    attempts, all_intervals, monotonic = [], [], []
    proof_seen = set()
    for claim in claims:
        if claim["id"] not in created:
            raise ValueError("claim creation timing unavailable at this frontier")
        owned = [item for item in evidence if item["claim_id"] == claim["id"]]
        owned_reviews = [review for review in reviews if review["review_attempt_id"] in {
            item["id"] for item in owned
        }]
        samples = observations[claim["id"]]
        proof_intervals, proof_us = [], []
        verification_ends = {}
        capture_only = 0
        for item in owned:
            verification_ends[item["id"]] = []
            for proof in item["proofs"]:
                if proof["kind"] not in {"command", "claim_command", "hook_command"}:
                    continue
                core = proof.get("evidence_core")
                if core is None:
                    capture_only += 1
                    continue
                if not _bound(core, claim, project_id) or not (
                    _utc(claim["created_at"]) <= _utc(core["started_at"])
                    <= _utc(core["ended_at"]) <= _utc(item["submitted_at"])
                ):
                    unknown += 1
                    continue
                key = proof.get("semantic_digest")
                if key is None:
                    raise ValueError("command timing core has no native digest")
                if key in proof_seen:
                    continue
                proof_seen.add(key)
                interval = _interval(core["started_at"], core["ended_at"], kind="observed")
                proof_intervals.append(interval)
                verification_ends[item["id"]].append(core["ended_at"])
                if interval["elapsed_us"] is not None:
                    proof_us.append(interval["elapsed_us"])
        intervals = [sample["utc_interval"] for sample in samples] + proof_intervals
        valid_intervals = [
            (_utc(item["started_at"]), _utc(item["ended_at"]))
            for item in intervals if item["status"] == "observed"
        ]
        measured = [sample["monotonic_elapsed_us"] for sample in samples
                    if sample["monotonic_elapsed_us"] is not None]
        all_intervals.extend(valid_intervals)
        monotonic.extend(measured)
        release = terminal.get(claim["id"])
        cycle = _interval(claim["created_at"], release["timestamp"] if release else observation_at,
                          kind="inferred" if release else "running")
        if release is None and observation_at is None:
            cycle["status"] = "running" if claim["status"] == "active" else "unknown"
        handoffs = []
        for item in owned:
            ends = verification_ends[item["id"]]
            end = max(ends, key=_utc) if ends else None
            handoffs.append({
                "evidence_id": item["id"],
                "verification_to_submission": _interval(end, item["submitted_at"], kind="inferred"),
                "reviews": [
                    {"review_id": review["id"], "decision": review["decision"],
                     "submission_to_review": _interval(
                         item["submitted_at"], review["created_at"], kind="inferred")}
                    for review in owned_reviews if review["review_attempt_id"] == item["id"]
                ],
            })
        attempts.append({
            "claim_id": claim["id"], "generation": claim["generation"],
            "claimed_by": claim["claimed_by"], "claim_status": claim["status"],
            "claim_cycle": cycle, "release_event_id": release["id"] if release else None,
            "authoring_freeze_interval": "unknown", "external_wait": "unknown",
            "verification": {
                "observations": samples, "utc_elapsed_union_us": _union(valid_intervals),
                "summed_monotonic_execution_us": sum(measured) if measured else None,
                "summed_proof_utc_interval_us": sum(proof_us) if proof_us else None,
                "proof_interval_samples": len(proof_intervals),
                "capture_only_samples": capture_only,
                "counts": {name: sum(sample["outcome"] == name for sample in samples)
                           for name in ("succeeded", "failed", "interrupted")},
                "classified_counts": {name: sum(
                    sample["classification"] == name for sample in samples)
                                      for name in ("source", "environment", "unknown")},
                "incomplete_intervals": sum(item["status"] == "unknown" for item in intervals),
                "clock_skew_intervals": sum(item["status"] == "clock_skew" for item in intervals),
            },
            "handoffs": handoffs,
        })
    for current, following in zip(attempts, attempts[1:], strict=False):
        current["next_dispatch_delay"] = _interval(
            current["claim_cycle"]["ended_at"] if current["release_event_id"] else None,
            following["claim_cycle"]["started_at"], kind="inferred",
        )
    return {
        "schema_version": 1, "observation_at": observation_at, "attempts": attempts,
        "attempt_count": len(attempts), "unattributed_timing_count": unknown,
        "legacy_review_binding_count": sum(
            review["binding_status"] == "legacy_unknown" for review in reviews),
        "utc_verification_elapsed_union_us": _union(all_intervals),
        "summed_monotonic_execution_us": sum(monotonic) if monotonic else None,
        "forecast": "unavailable", "mutation_authority": False,
    }
