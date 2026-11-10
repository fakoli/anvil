"""Campaigns preserve first observations and stop before unsafe continuation."""

import hashlib
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from benchmarks import execution_campaign, execution_scale


def _plan():
    return {
        "tasks": 4, "cohort_size": 2, "command_timeout": 10,
        "cell_deadline_seconds": 60, "deadline_seconds": 180,
        "native_p95_target_ms": 2,
        "cells": [
            {"name": "first", "workers": 1, "durability": "relaxed", "events_storage": "local"},
            {"name": "second", "workers": 2, "durability": "strict", "events_storage": "git"},
        ],
    }


@pytest.fixture
def campaign_source(monkeypatch):
    source = {"source.py": "frozen"}
    binding = {"commit": "frozen", "source_sha256": source, "source_matches_commit": True}
    monkeypatch.setattr(execution_scale, "_source_identity", lambda: deepcopy(source))
    monkeypatch.setattr(execution_campaign, "_source_binding", lambda: deepcopy(binding))
    return source, binding


def _receipt(options, source, **changes):
    return {
        "validated": True, "child_exit_code": 0, "child_stopped": True,
        "source_unchanged": True, "source_sha256": source, "replay_equal": True,
        "no_false_acceptance": True, "failures": [], "completed": options["tasks"],
        "tasks": options["tasks"], "cohort_size": options["cohort_size"],
        "ledger_writers": 1, "containers": 0,
        "executor_limit": options["workers"], "durability": options["durability"],
        "events_storage": options["events_storage"], "peak_active_claims": options["tasks"],
        "observed_command_overlap": options["workers"],
        "stages": {name: {"samples": 4, "p95_ms": 1} for name in execution_scale.NATIVE_STAGES},
        **changes,
    }


def test_miss_retained_without_retry_while_declared_host_cells_complete(
    tmp_path, monkeypatch, campaign_source,
):
    source, _ = campaign_source
    calls = []

    def supervise(path, options, deadline):
        calls.append((path.name, options, deadline))
        receipt = _receipt(options, source)
        if len(calls) == 1:
            receipt["stages"]["claim_append"]["p95_ms"] = 3
        path.write_text(json.dumps(receipt))
        return receipt

    monkeypatch.setattr(execution_scale, "_supervise", supervise)
    destination = tmp_path / "campaign"
    result = execution_campaign.run(destination, _plan())
    assert result["complete"] and result["correctness_passed"]
    assert not result["latency_target_met"] and not result["qualified"]
    assert [call[0] for call in calls] == ["first.json", "second.json"]
    assert all(call[2] == 60 for call in calls)
    assert result["cells"][0]["native_stage_gates"]["claim_append"] == {
        "p95_ms": 3, "passed": False,
    }
    for cell in result["cells"]:
        assert cell["receipt_sha256"] == hashlib.sha256(
            (destination / cell["receipt"]).read_bytes()
        ).hexdigest()
    assert json.loads((destination / "campaign.json").read_text()) == result
    before = {path.name: path.read_bytes() for path in destination.iterdir()}
    with pytest.raises(FileExistsError):
        execution_campaign.run(destination, _plan())
    assert {path.name: path.read_bytes() for path in destination.iterdir()} == before
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["source", "custody", "correctness", "interrupted"])
def test_failed_boundary_preserves_first_receipt_and_stops_next_cell(
    tmp_path, monkeypatch, campaign_source, failure,
):
    source, binding = campaign_source
    calls = []

    def supervise(path, options, _deadline):
        calls.append(path)
        receipt = _receipt(options, source)
        if failure == "source":
            binding["commit"] = "changed"
        elif failure == "custody":
            receipt["child_stopped"] = False
        else:
            receipt["validated"] = False
            receipt["failures"] = ["retained failure"]
            if failure == "interrupted":
                receipt["interrupted"] = True
        path.write_text(json.dumps(receipt))
        return receipt

    monkeypatch.setattr(execution_scale, "_supervise", supervise)
    result = execution_campaign.run(tmp_path / "failed", _plan())
    assert len(calls) == 1 and calls[0].is_file()
    assert not result["qualified"] and not result["complete"]
    assert not result["correctness_passed"]
    assert result["stopped_reason"] == ("campaign_error" if failure == "source" else "cell_failed")
    assert not (tmp_path / "failed/second.json").exists()


def test_campaign_deadline_starts_no_cell(tmp_path, monkeypatch, campaign_source):
    plan = _plan()
    plan["deadline_seconds"] = plan["cell_deadline_seconds"] = 10
    times = iter([100, 101])
    monkeypatch.setattr(execution_campaign.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(execution_scale, "_supervise", lambda *_: pytest.fail("deadline passed"))
    result = execution_campaign.run(tmp_path / "expired", plan)
    assert result["stopped_reason"] == "campaign_deadline"
    assert not result["complete"] and not result["qualified"] and result["cells"] == []


@pytest.mark.parametrize("invalid", ["path", "duplicate", "unknown", "budget", "type"])
def test_invalid_plan_refuses_before_reserving_or_executing(
    tmp_path, monkeypatch, campaign_source, invalid,
):
    plan = _plan()
    if invalid == "path":
        plan["cells"][0]["name"] = "../escape"
    elif invalid == "duplicate":
        plan["cells"][1]["name"] = "first"
    elif invalid == "unknown":
        plan["cells"][0]["openshell"] = True
    elif invalid == "budget":
        plan["tasks"] = 2001
    else:
        plan["cells"][0]["workers"] = True
    monkeypatch.setattr(execution_scale, "_supervise", lambda *_: pytest.fail("invalid plan ran"))
    with pytest.raises(ValueError):
        execution_campaign.run(tmp_path / "invalid", plan)
    assert not (tmp_path / "invalid").exists()


def test_duplicate_json_fields_refuse_instead_of_replacing_a_declared_target(tmp_path):
    path = tmp_path / "ambiguous.json"
    path.write_text('{"native_p95_target_ms":15,"native_p95_target_ms":1000}')
    with pytest.raises(ValueError, match="Duplicate plan key"):
        execution_campaign._load_plan(path)


@pytest.mark.skipif(os.name != "posix", reason="Existing supervisor requires process groups")
def test_real_cli_campaign_uses_native_supervision_and_retains_actual_captures(tmp_path):
    try:
        os.killpg(os.getpgrp(), 0)
    except (AttributeError, OSError) as error:
        pytest.skip(f"POSIX process-group capability unavailable: {error!r}")
    plan = _plan()
    # This small smoke checks the runner, not the production campaign's 15ms target.
    plan["native_p95_target_ms"] = 10000
    plan_path = tmp_path / "smoke-plan.json"
    plan_path.write_text(json.dumps(plan))
    output = tmp_path / "smoke"
    process = subprocess.run(
        [sys.executable, str(Path(execution_campaign.__file__)), "--plan", str(plan_path),
         "--output", str(output)],
        capture_output=True, text=True, timeout=180,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads((output / "campaign.json").read_text())
    assert result["qualified"] and result["correctness_passed"] and result["source_unchanged"]
    assert len(result["cells"]) == 2
    for cell in result["cells"]:
        native = json.loads((output / cell["receipt"]).read_text())
        assert native["child_started"] and native["child_stopped"]
        assert native["completed"] == native["peak_active_claims"] == 4
        assert native["replay_equal"] and native["no_false_acceptance"]
        assert len(list((output / f"{cell['name']}.work/captures").glob("*.proof.json"))) == 4
