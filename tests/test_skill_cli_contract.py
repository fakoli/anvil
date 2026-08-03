"""Fast source-tree coverage for the reusable skill/CLI contract checker.

Release qualification separately runs the same checker against a manifest
emitted by an isolated wheel install; these tests exercise parsing and failure
behavior without building an artifact on every test invocation.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from scripts.check_skill_cli_contract import (
    ALLOWLIST_PHANTOM,
    ContractError,
    check_invocation,
    code_segments,
    invocations,
    load_manifest,
    validate_docs,
)

from anvil import __version__
from anvil.cli.describe import API_VERSION, cli_command_contracts, cli_command_options

_REPO = Path(__file__).resolve().parents[1]


def _source_manifest() -> dict[str, object]:
    options = cli_command_options()
    commands = sorted(options)
    return {
        "ok": True,
        "command": "describe",
        "data": {
            "engine_version": __version__,
            "api_version": API_VERSION,
            "cli": {
                "commands": commands,
                "options": options,
                "contracts": cli_command_contracts(),
                "contract_count": len(cli_command_contracts()),
                "count": len(commands),
            },
        },
    }


@pytest.fixture(scope="module")
def contract() -> dict[str, object]:
    return load_manifest(_source_manifest())


def test_surface_loaded(contract: dict[str, object]) -> None:
    commands = contract["commands"]
    assert isinstance(commands, list)
    assert len(commands) > 20
    assert "submit" in commands
    assert "prd source-name" in commands


def test_validator_catches_known_bugs(contract: dict[str, object]) -> None:
    assert check_invocation(
        ["anvil", "submit", "T1", "--evidence", "x"], contract
    )
    assert check_invocation(["anvil", "totally-not-a-command"], contract)
    assert check_invocation(
        ["anvil", "submit", "T1", "--not-a-real-flag"], contract
    )
    swap = check_invocation(["anvil", "review", "prd", "approve"], contract)
    assert swap and "unknown subcommand" in swap[0]
    for malformed in ("totally_not_a_command", "Bogus"):
        assert check_invocation(["anvil", malformed], contract)
    for malformed in ("source_name", "Source-name"):
        finding = check_invocation(["anvil", "prd", malformed], contract)
        assert finding and "unknown subcommand" in finding[0]
    assert check_invocation(["anvil", "submit", "[--fabricated]"], contract)

    missing_optional = deepcopy(contract)
    source_node = missing_optional["nodes"]["prd source-name"]  # type: ignore[index]
    source_node["flags"].remove("--prd")  # type: ignore[index]
    finding = check_invocation(
        ["anvil", "prd", "source-name", "[--prd", "<id>]", "--json"],
        missing_optional,
    )
    assert finding and "unknown flag --prd" in finding[0]


def test_validator_accepts_real_commands(contract: dict[str, object]) -> None:
    assert check_invocation(["anvil", "--version"], contract) == []
    assert check_invocation(["anvil", "[--version]"], contract) == []
    assert check_invocation(["anvil", "--fabricated-root-flag"], contract)
    assert check_invocation(["anvil", "[--fabricated-root-flag]"], contract)
    assert check_invocation(["anvil", "prd", "review", "--approve"], contract) == []
    assert check_invocation(
        ["anvil", "submit", "T1", "--commands", "x", "--files-changed", "y"],
        contract,
    ) == []


def test_fenced_shell_continuations_are_one_validated_invocation(
    contract: dict[str, object],
) -> None:
    text = """```bash
anvil submit T012 \\
  --commands 'pytest -q' \\
  --fabricated-continuation-flag value
```
"""
    segments = list(code_segments(text))
    assert segments == [
        (
            2,
            "anvil submit T012 --commands 'pytest -q' "
            "--fabricated-continuation-flag value",
        )
    ]
    tokens = list(invocations(segments[0][1]))
    assert len(tokens) == 1
    assert check_invocation(tokens[0], contract) == [
        "unknown flag --fabricated-continuation-flag for: anvil submit"
    ]
    assert check_invocation(["anvil", "sync", "--fix"], contract) == []
    assert check_invocation(["anvil", "prd", "--file", "x"], contract)
    assert check_invocation(
        ["anvil", "submit", "T012", "--commands", "pytest -x"], contract
    ) == []


def test_phantom_commands_are_not_real(contract: dict[str, object]) -> None:
    commands = contract["commands"]
    assert isinstance(commands, list)
    for name in ALLOWLIST_PHANTOM:
        assert name not in commands, (
            f"anvil {name} now ships but is still allowlisted as a phantom"
        )


def test_every_cited_command_and_flag_exists() -> None:
    findings = validate_docs(_REPO, _source_manifest())
    assert not findings, "Agent-facing command drift:\n  " + "\n  ".join(findings)


def test_named_prd_skill_guards_precede_first_path_operation() -> None:
    first_path_operation = {
        "start-prd": "### Step 2 — Generate the PRD draft",
        "prd": "First resolve the PRD path from the CLI",
        "plan": "### Step 0 — Scan for unresolved decisions",
        "finish": "Once the user lists the open questions, record them.",
    }
    for skill, marker in first_path_operation.items():
        text = (_REPO / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
        if skill in {"start-prd", "prd"}:
            first_gate = text.index("anvil prd source-name --help")
            init = text.index("anvil init --name")
            assert first_gate < init, f"{skill} can initialize before capability proof"
        heading = (
            "#### Required `prd source-name` resolver preflight"
            if skill in {"start-prd", "prd"}
            else "#### Required `prd source-name` capability preflight"
        )
        guard = text.index(heading)
        operation = text.index(marker)
        assert guard < operation, f"{skill} capability guard is too late"
        retained = text.index("Retain that validated value as `PRD_RELATIVE`", guard)
        assert retained < operation, f"{skill} does not retain its validated path"

    start = (_REPO / "skills" / "start-prd" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    answer = start.index("**Question 7 — Release / milestone (optional).**")
    selected = start.index("Choose the canonical ID before resolving any path.")
    resolver = start.index("#### Required `prd source-name` resolver preflight")
    json_call = start.index("anvil prd source-name [--prd <id>] --json", resolver)
    operation = start.index("### Step 2 — Generate the PRD draft")
    assert answer < selected < resolver < json_call < operation

    plan = (_REPO / "skills" / "plan" / "SKILL.md").read_text(encoding="utf-8")
    plan_selection = plan.index("Run `anvil prd list --json` before resolving")
    plan_resolver = plan.index("#### Required `prd source-name` capability preflight")
    plan_edit = plan.index("apply them to the `## Tasks` section")
    assert plan_selection < plan_resolver < plan_edit
    assert "anvil prd parse [--prd <PRD_ID>]" in plan
    assert "anvil plan [--prd <PRD_ID>]" in plan
    assert "anvil score [--prd <PRD_ID>]" in plan
    assert "anvil list [--prd <PRD_ID>] --status ready" in plan
    assert "anvil list [--prd <PRD_ID>] --status drafted" in plan
    assert "anvil show T003 [--prd <PRD_ID>]" in plan

    finish = (_REPO / "skills" / "finish" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    owner = finish.index("data.task.prd_id")
    finish_resolver = finish.index(
        "#### Required `prd source-name` capability preflight"
    )
    finish_edit = finish.index("Once the user lists the open questions, record them.")
    assert owner < finish_resolver < finish_edit
    assert "anvil prd parse [--prd <TASK_PRD_ID>]" in finish


def test_manifest_rejects_empty_or_incomplete_cli_surface() -> None:
    empty = deepcopy(_source_manifest())
    empty["data"]["cli"] = {  # type: ignore[index]
        "commands": [],
        "options": {},
        "contracts": [],
        "contract_count": 0,
        "count": 0,
    }
    with pytest.raises(ContractError, match="non-empty"):
        load_manifest(empty)

    missing_options = deepcopy(_source_manifest())
    missing_options["data"]["cli"]["options"].pop("prd source-name")  # type: ignore[index]
    with pytest.raises(ContractError, match="every command"):
        load_manifest(missing_options)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda payload: payload.update(ok=False), "successful describe"),
        (
            lambda payload: payload["data"]["cli"].update(count=True),
            "must be an integer",
        ),
        (
            lambda payload: payload["data"]["cli"]["commands"].__setitem__(
                0, "Bad Command"
            ),
            "malformed",
        ),
        (
            lambda payload: payload["data"]["cli"]["contracts"][0][
                "flags"
            ].append("---bad"),
            "flags are malformed",
        ),
        (
            lambda payload: payload["data"]["cli"]["contracts"].append(
                deepcopy(payload["data"]["cli"]["contracts"][0])
            ),
            "does not match contracts",
        ),
    ],
)
def test_manifest_rejects_malformed_contracts(mutate: object, match: str) -> None:
    payload = deepcopy(_source_manifest())
    mutate(payload)  # type: ignore[operator]
    with pytest.raises(ContractError, match=match):
        load_manifest(payload)


def test_manifest_rejects_duplicate_node_path_after_consistent_count() -> None:
    payload = deepcopy(_source_manifest())
    cli = payload["data"]["cli"]  # type: ignore[index]
    cli["contracts"].append(deepcopy(cli["contracts"][0]))  # type: ignore[index]
    cli["contract_count"] += 1  # type: ignore[index,operator]

    with pytest.raises(ContractError, match="duplicate manifest CLI contract path"):
        load_manifest(payload)


def test_manifest_rejects_missing_or_non_group_parent_nodes() -> None:
    missing_parent = deepcopy(_source_manifest())
    contracts = missing_parent["data"]["cli"]["contracts"]  # type: ignore[index]
    prd_group = next(item for item in contracts if item["path"] == ["prd"])
    contracts.remove(prd_group)
    missing_parent["data"]["cli"]["contract_count"] -= 1  # type: ignore[index,operator]
    with pytest.raises(ContractError, match="has no parent group"):
        load_manifest(missing_parent)

    command_parent = deepcopy(_source_manifest())
    contracts = command_parent["data"]["cli"]["contracts"]  # type: ignore[index]
    prd_group = next(item for item in contracts if item["path"] == ["prd"])
    prd_group["kind"] = "command"
    with pytest.raises(ContractError, match="descends from a command"):
        load_manifest(command_parent)


def test_published_060_describe_manifest_fails_closed_as_empty() -> None:
    fixture = _REPO / "tests/fixtures/cli-manifests/anvil-0.6.0-describe.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    with pytest.raises(ContractError, match="non-empty"):
        load_manifest(payload)


def test_extracted_060_prd_contract_lacks_source_name() -> None:
    fixture = _REPO / "tests/fixtures/cli-manifests/anvil-0.6.0-prd-contract.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    historical = load_manifest(payload)

    findings = check_invocation(
        ["anvil", "prd", "source-name", "--prd", "release"], historical
    )
    assert findings
    assert "unknown subcommand: anvil prd source-name" in findings[0]
