"""Run the declared host qualification cells once, retaining all outcomes.

uv run --project bin --locked python benchmarks/execution_campaign.py --output /tmp/campaign
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks import execution_scale  # noqa: E402

DEFAULT_PLAN = Path(__file__).with_name("fixtures") / "execution_campaign.json"
PLAN_FIELDS = {
    "tasks", "cohort_size", "command_timeout", "cell_deadline_seconds",
    "deadline_seconds", "native_p95_target_ms", "cells",
}
CELL_FIELDS = {"name", "workers", "durability", "events_storage"}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate plan key: {key}")
        result[key] = value
    return result


def _check_plan(plan):
    if not isinstance(plan, dict) or set(plan) != PLAN_FIELDS:
        raise ValueError(f"Plan requires exactly: {', '.join(sorted(PLAN_FIELDS))}")
    for key in ("tasks", "cohort_size", "command_timeout", "cell_deadline_seconds",
                "deadline_seconds"):
        if type(plan[key]) is not int:
            raise ValueError(f"{key} must be an integer")
    if not 10 <= plan["cell_deadline_seconds"] <= plan["deadline_seconds"] <= 3600:
        raise ValueError("Deadlines require 10 <= cell <= campaign <= 3600 seconds")
    target = plan["native_p95_target_ms"]
    if type(target) not in (int, float) or not math.isfinite(target) or target <= 0:
        raise ValueError("native_p95_target_ms must be finite and positive")
    if not isinstance(plan["cells"], list) or not 1 <= len(plan["cells"]) <= 8:
        raise ValueError("Declare between one and eight host cells")
    names = set()
    for cell in plan["cells"]:
        if not isinstance(cell, dict) or set(cell) != CELL_FIELDS:
            raise ValueError(f"Each cell requires exactly: {', '.join(sorted(CELL_FIELDS))}")
        name = cell["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name):
            raise ValueError("Cell names require 1..64 lowercase letters/digits/hyphens")
        if name in names:
            raise ValueError(f"Duplicate cell name: {name}")
        names.add(name)
        if type(cell["workers"]) is not int:
            raise ValueError("workers must be an integer")
        if not isinstance(cell["durability"], str) or not isinstance(cell["events_storage"], str):
            raise ValueError("durability and events_storage must be strings")
        execution_scale._check_options(**_options(plan, cell))


def _options(plan, cell):
    return {
        **{key: plan[key] for key in ("tasks", "cohort_size", "command_timeout")},
        **{key: cell[key] for key in ("workers", "durability", "events_storage")},
    }


def _load_plan(path):
    with path.open("rb") as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ValueError("Plan exceeds 64 KiB")
    plan = json.loads(raw, object_pairs_hook=_unique_object)
    _check_plan(plan)
    return plan


def _write_json(path, value):
    temporary = path.with_suffix(".pending")
    with temporary.open("w") as output:
        json.dump(value, output, indent=2, allow_nan=False)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def _source_binding():
    campaign_source = Path(__file__).resolve()
    checkout = campaign_source.parents[1]
    source = {
        **execution_scale._source_identity(),
        str(campaign_source.relative_to(checkout)): hashlib.sha256(
            campaign_source.read_bytes()
        ).hexdigest(),
    }
    changed = execution_scale._git(checkout, "diff", "--name-only", "HEAD", "--", *source)
    tracked = set(execution_scale._git(checkout, "ls-files", "--", *source).splitlines())
    return {
        "commit": execution_scale._git(checkout, "rev-parse", "HEAD"),
        "source_sha256": source,
        "source_matches_commit": not changed and tracked == set(source),
    }


def _cell_result(receipt_path, receipt, plan, cell, native_source):
    target = plan["native_p95_target_ms"]
    stages = receipt.get("stages", {})
    gates = {}
    for name in execution_scale.NATIVE_STAGES:
        stage = stages.get(name, {})
        value = stage.get("p95_ms")
        gates[name] = {
            "p95_ms": value,
            "passed": type(stage.get("samples")) is int and stage["samples"] > 0
            and type(value) in (int, float)
            and math.isfinite(value) and 0 <= value <= target,
        }
    correct = (
        receipt.get("validated") is True and receipt.get("child_exit_code") == 0
        and receipt.get("child_stopped") is True and receipt.get("source_unchanged") is True
        and receipt.get("source_sha256") == native_source
        and receipt.get("replay_equal") is True and receipt.get("no_false_acceptance") is True
        and receipt.get("failures") == [] and receipt.get("completed") == plan["tasks"]
        and receipt.get("tasks") == plan["tasks"] and receipt.get("ledger_writers") == 1
        and receipt.get("cohort_size") == plan["cohort_size"]
        and receipt.get("containers") == 0 and receipt.get("executor_limit") == cell["workers"]
        and receipt.get("durability") == cell["durability"]
        and receipt.get("events_storage") == cell["events_storage"]
    )
    return {
        "name": cell["name"], "options": _options(plan, cell),
        "receipt": receipt_path.name,
        "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        "correctness_passed": correct, "native_stage_gates": gates,
        "latency_target_met": all(gate["passed"] for gate in gates.values()),
        "source_unchanged": receipt.get("source_unchanged"),
        "child_stopped": receipt.get("child_stopped"),
        "peak_active_claims": receipt.get("peak_active_claims"),
        "observed_command_overlap": receipt.get("observed_command_overlap"),
        "ledger_writers": receipt.get("ledger_writers"), "containers": receipt.get("containers"),
        "stages": stages, "error": receipt.get("error"),
    }


def run(output, plan):
    """Use one fresh directory as the exclusive reservation for a bounded host campaign."""
    _check_plan(plan)
    binding = _source_binding()
    native_source = execution_scale._source_identity()
    output.mkdir(parents=True, exist_ok=False)
    _write_json(output / "plan.json", plan)
    deadline = time.monotonic() + plan["deadline_seconds"]
    result = {
        "schema": "anvil.execution-campaign.v1", "started_at": datetime.now(UTC).isoformat(),
        "source": binding, "plan_sha256": hashlib.sha256(
            (output / "plan.json").read_bytes()
        ).hexdigest(),
        "cells": [], "complete": False, "qualified": False,
        "native_p95_target_ms": plan["native_p95_target_ms"],
        "limits": [
            "Host command executors only; one ledger writer and zero containers per cell.",
            "Latency misses do not stop already-declared host cells; correctness, custody, "
            "source changes and interruptions do. No retries or physical expansion.",
            "The configured target applies only to named native stages; queue, full scoped "
            "claim and end-to-end durations remain separate in every receipt.",
            "Self-attested fixture proofs do not qualify application acceptance or browser UX.",
        ],
    }
    _write_json(output / "campaign.json", result)
    try:
        for cell in plan["cells"]:
            if _source_binding() != binding:
                raise ValueError("Source changed before the next cell")
            remaining = deadline - time.monotonic()
            if remaining < 10:
                result["stopped_reason"] = "campaign_deadline"
                break
            receipt_path = output / f"{cell['name']}.json"
            receipt = execution_scale._supervise(
                receipt_path, _options(plan, cell), min(plan["cell_deadline_seconds"], remaining)
            )
            recorded = _cell_result(receipt_path, receipt, plan, cell, native_source)
            result["cells"].append(recorded)
            _write_json(output / "campaign.json", result)
            if not recorded["correctness_passed"]:
                result["stopped_reason"] = "cell_failed"
                break
    except KeyboardInterrupt:
        result["stopped_reason"] = "interrupted"
    except Exception as error:
        result["stopped_reason"] = "campaign_error"
        result["error"] = repr(error)
    finally:
        result["ended_at"] = datetime.now(UTC).isoformat()
        try:
            result["source_unchanged"] = _source_binding() == binding
        except Exception as error:
            result["source_unchanged"] = False
            result["source_readback_error"] = repr(error)
        result["complete"] = len(result["cells"]) == len(plan["cells"])
        result["correctness_passed"] = (
            result["complete"] and result["source_unchanged"]
            and all(cell["correctness_passed"] for cell in result["cells"])
            and "stopped_reason" not in result
        )
        result["latency_target_met"] = result["complete"] and all(
            cell["latency_target_met"] for cell in result["cells"]
        )
        result["qualified"] = result["correctness_passed"] and result["latency_target_met"]
        _write_json(output / "campaign.json", result)
    return result


def _interrupt(_signum, _frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        plan = _load_plan(arguments.plan)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    previous_handler = signal.signal(signal.SIGTERM, _interrupt)
    try:
        result = run(arguments.output.resolve(), plan)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
    print(json.dumps({
        "output": str(arguments.output.resolve()), "complete": result["complete"],
        "correctness_passed": result["correctness_passed"],
        "latency_target_met": result["latency_target_met"], "qualified": result["qualified"],
    }))
    return 0 if result["qualified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
