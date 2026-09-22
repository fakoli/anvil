"""Optional Jev controls, consumer bridge, disclosure, and immutable-state boundary."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from anvil import jev
from anvil.cli import app
from anvil.cli import jev as cli
from anvil.config import load_config, load_merged_config
from anvil.jev import JevConfig

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_jev_home_and_transport(tmp_path, monkeypatch):
    """Jev CLI tests may only use a synthetic home and injected transport."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(jev.Path, "home", lambda: home)
    bounded_request = jev._bounded_request

    def deny_live_transport(config, key, body, transport, deadline):
        if transport is None:
            raise AssertionError("Jev tests require an injected transport")
        return bounded_request(config, key, body, transport, deadline)

    monkeypatch.setattr(jev, "_bounded_request", deny_live_transport)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ANVIL_STATE_LAYOUT", "local")
    monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(tmp_path / "global.yaml"))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    return tmp_path


def invoke(project: Path, *args: str):
    return runner.invoke(app, ["jev", *args, "--cwd", str(project), "--json"])


def payload(result):
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    assert envelope["ok"] is True
    return envelope["data"]


def browser_projection(*, coverage="complete"):
    """Return a bounded synthetic owner-authorized projection."""
    reason = None if coverage == "complete" else "bounded collection"
    return {
        "schema": "browser-element-resolution-projection/v1",
        "request_id": "request-bridge",
        "observation_id": "observation-bridge",
        "source": "dom",
        "target": {"description": "alpha marker", "qualifiers": ["main panel"]},
        "scope": {"kind": "document", "root": "document"},
        "coverage": {"state": coverage, "reason": reason},
        "entities": [
            {
                "id": "e-101",
                "role": "img",
                "text": "Alpha",
                "nearby": "main panel",
                "state": {
                    "exists": True,
                    "in_viewport": True,
                    "occluded": False,
                    "enabled": None,
                },
                "predicate_reasons": {
                    "exists": None,
                    "in_viewport": None,
                    "occluded": None,
                    "enabled": "not_applicable",
                },
            }
        ],
    }


def browser_bridge_envelope(
    *,
    enabled=True,
    capability=True,
    allow_api=True,
    allow_export=True,
    input_value=None,
):
    capabilities = ("browser_element_resolution",) if capability else ()
    return {
        "jev": asdict(JevConfig(enabled=enabled, capabilities=capabilities)),
        "allow_api": allow_api,
        "allow_export": allow_export,
        "capability": "browser_element_resolution",
        "input": browser_projection() if input_value is None else input_value,
    }


def oversized_browser_projection():
    projection = browser_projection()
    entity = projection["entities"][0]
    projection["entities"] = [
        {**entity, "id": f"entity-{index}", "text": "x" * 800}
        for index in range(42)
    ]
    return projection


def test_default_off_skips_missing_input_and_key(project, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled code attempted a key or network lookup")

    monkeypatch.setattr("anvil.jev.httpx.Client", forbidden)
    report = payload(
        invoke(project, "evaluate", "evidence_triage", "--input", "missing.json")
    )
    assert report["status"] == "disabled"
    assert report["request_started"] is False
    assert report["used"] is False


def test_cli_uses_selected_cwd_for_default_credential_project(project, tmp_path, monkeypatch):
    payload(invoke(project, "enable", "evidence_triage", "--allow-api"))
    source = project / "selected.json"
    source.write_text('{"claim":"Works","observation":"Started"}')
    other = tmp_path / "other"
    other.mkdir()
    (other / ".env").write_text("TYPESAFE_API_KEY=wrong-project-key\n")
    captured = []

    def fake(*args, **kwargs):
        captured.append(kwargs["project_root"])
        return {"status": "disabled", "reason": "disabled", "request_started": False, "used": False, "answers": {}}

    monkeypatch.chdir(other)
    monkeypatch.setattr(cli, "call_jev", fake)
    payload(invoke(project, "evaluate", "evidence_triage", "--input", str(source), "--allow-export"))
    assert captured == [project.resolve()]


def test_enable_is_scoped_explicit_and_preserves_providers(project):
    path = project / ".anvil/config.yaml"
    before = load_config(path)
    blocked = invoke(project, "enable", "evidence_triage")
    assert blocked.exit_code == 1
    assert json.loads(blocked.output)["error"]["code"] == "api_permission_required"
    enabled = invoke(project, "enable", "evidence_triage", "--allow-api")
    assert json.loads(enabled.output)["command"] == "jev enable"
    data = payload(enabled)
    assert data["capabilities"] == ["evidence_triage"]
    assert data["enabled"] is True
    after = load_config(path)
    assert after.llm_provider == before.llm_provider
    assert after.llm_model == before.llm_model
    assert after.llm_fallback == before.llm_fallback
    assert after.strict_evidence == before.strict_evidence
    assert after.llm_allow_api is True
    assert payload(invoke(project, "disable", "evidence_triage"))["capabilities"] == []
    disabled = invoke(project, "disable")
    assert json.loads(disabled.output)["command"] == "jev disable"
    assert payload(disabled)["enabled"] is False
    assert load_config(path).llm_allow_api is True


def test_project_off_overrides_global_on(project):
    global_path = project / "global.yaml"
    global_path.write_text(
        yaml.safe_dump({"jev": {"enabled": True, "capabilities": ["voice_intent"]}})
    )
    assert load_merged_config(project / ".anvil/config.yaml").jev.enabled is False


@pytest.mark.parametrize("capability", ["unknown", "", "../voice_intent"])
def test_unknown_capability_cannot_enable(project, capability):
    result = invoke(project, "enable", capability, "--allow-api")
    assert result.exit_code != 0
    assert load_config(project / ".anvil/config.yaml").jev.enabled is False


def test_export_permission_precedes_input_read(project):
    payload(invoke(project, "enable", "evidence_triage", "--allow-api"))
    report = payload(
        invoke(project, "evaluate", "evidence_triage", "--input", "missing.json")
    )
    assert report["reason"] == "export_permission_required"
    assert report["request_started"] is False


def test_no_jev_overrides_enabled_before_read(project):
    payload(invoke(project, "enable", "evidence_triage", "--allow-api"))
    report = payload(
        invoke(
            project,
            "evaluate",
            "evidence_triage",
            "--input",
            "missing.json",
            "--allow-export",
            "--no-jev",
        )
    )
    assert report["status"] == "disabled"


@pytest.mark.parametrize(
    "content",
    ["{bad", "[]", '{"claim":"x","claim":"y"}', "x" * 65537],
    ids=["invalid_json", "invalid_shape", "duplicate_key", "oversized"],
)
def test_invalid_input_is_safe(project, content):
    payload(invoke(project, "enable", "evidence_triage", "--allow-api"))
    source = project / "selected.json"
    source.write_text(content)
    result = invoke(
        project, "evaluate", "evidence_triage", "--input", str(source), "--allow-export"
    )
    envelope = json.loads(result.output)
    assert (
        envelope.get("error", {}).get("code") == "invalid_input"
        or envelope["data"]["status"] == "blocked"
    )
    assert content not in result.output


def test_bridge_is_stateless_and_discloses_origin(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("bridge must not resolve project state")

    monkeypatch.setattr(cli, "_resolve_state_dir", forbidden)
    data = {
        "jev": {},
        "allow_api": False,
        "allow_export": False,
        "capability": "voice_intent",
        "input": {"text": "hello"},
    }
    report = payload(
        runner.invoke(app, ["jev", "bridge", "--json"], input=json.dumps(data))
    )
    assert report["schema"] == "anvil.jev.annotation.v1"
    assert report["provider"] == "typesafe"
    assert report["status"] == "disabled"
    assert report["request_started"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"allow_api": "true"},
        {"allow_export": 1},
        {"capability": []},
        {"unexpected": "sentinel"},
    ],
)
def test_bridge_rejects_permission_coercion_and_unknown_fields(changes):
    data = {
        "jev": {},
        "allow_api": False,
        "allow_export": False,
        "capability": "voice_intent",
        "input": {"text": "hello"},
        **changes,
    }
    result = runner.invoke(app, ["jev", "bridge", "--json"], input=json.dumps(data))
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "invalid_input"
    assert "sentinel" not in result.output


def test_bridge_uses_closed_rubric_and_does_not_echo_input(monkeypatch):
    seen = []

    def fake(config, capability, state, questions, **kwargs):
        seen.append((state, questions))
        return {
            "schema": "anvil.jev.annotation.v1",
            "provider": "typesafe",
            "model": config.model,
            "status": "completed",
            "reason": "validated",
            "request_started": True,
            "used": True,
            "answers": {"intent": {"type": "choice", "choice": "conversation"}},
        }

    monkeypatch.setattr(cli, "call_jev", fake)
    data = {
        "jev": asdict(JevConfig(enabled=True, capabilities=("voice_intent",))),
        "allow_api": True,
        "allow_export": True,
        "capability": "voice_intent",
        "input": {"text": "private selected utterance"},
    }
    result = runner.invoke(app, ["jev", "bridge", "--json"], input=json.dumps(data))
    assert payload(result)["used"] is True
    assert "private selected utterance" not in result.output
    assert "intent" in seen[0][1]


@pytest.mark.parametrize(
    ("name", "envelope", "reason"),
    [
        ("disabled", browser_bridge_envelope(enabled=False), "disabled"),
        ("capability", browser_bridge_envelope(capability=False), "capability_disabled"),
        ("api", browser_bridge_envelope(allow_api=False), "api_permission_required"),
        ("export", browser_bridge_envelope(allow_export=False), "export_permission_required"),
    ],
)
def test_browser_bridge_guards_withhold_projection_before_call(
    monkeypatch, name, envelope, reason
):
    original = cli.call_jev
    calls = []

    def tracked(config, capability, state, questions, **kwargs):
        calls.append((capability, state, questions, kwargs))
        return original(config, capability, state, questions, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("guarded input reached serialization, credentials, or transport")

    marker = f"owner-material-{name}"
    envelope["input"]["target"]["description"] = marker
    monkeypatch.setattr(cli, "call_jev", tracked)
    monkeypatch.setattr("anvil.jev_questions.build_questions", forbidden)
    monkeypatch.setattr(jev, "_json_bytes", forbidden)
    monkeypatch.setattr(jev, "resolve_api_key", forbidden)
    monkeypatch.setattr(jev, "_bounded_request", forbidden)

    result = runner.invoke(app, ["jev", "bridge", "--json"], input=json.dumps(envelope))
    report = payload(result)

    assert report["reason"] == reason
    assert marker not in result.output
    assert len(calls) == 1
    capability, state, questions, _ = calls[0]
    assert capability == "browser_element_resolution"
    assert state == questions == {}


@pytest.mark.parametrize(
    "input_value",
    [
        {**browser_projection(), "origin": "outside the projection"},
        {
            **browser_projection(),
            "entities": [
                {
                    **browser_projection()["entities"][0],
                    "id": "NO_MATCH_IN_CANDIDATES",
                }
            ],
        },
        {
            **browser_projection(),
            "entities": [
                {
                    **browser_projection()["entities"][0],
                    "id": "https:example.test",
                }
            ],
        },
        {
            **browser_projection(),
            "scope": {"kind": "document", "root": " opaque-root"},
        },
        {
            **browser_projection(),
            "target": {
                "description": "password: synthetic-browser-only",
                "qualifiers": [],
            },
        },
        oversized_browser_projection(),
    ],
    ids=[
        "unknown_field", "reserved_identifier", "url_entity_identifier",
        "whitespace_scope_root", "recognized_credential", "request_limit",
    ],
)
def test_browser_bridge_refuses_invalid_input_before_adapter_boundaries(
    monkeypatch, input_value
):
    original = cli.call_jev
    calls = []

    def tracked(config, capability, state, questions, **kwargs):
        calls.append((state, questions, kwargs))
        return original(config, capability, state, questions, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("refused input reached a credential or transport boundary")

    monkeypatch.setattr(cli, "call_jev", tracked)
    monkeypatch.setattr(jev, "resolve_api_key", forbidden)
    monkeypatch.setattr(jev, "_bounded_request", forbidden)
    result = runner.invoke(
        app,
        ["jev", "bridge", "--json"],
        input=json.dumps(browser_bridge_envelope(input_value=input_value)),
    )

    report = payload(result)
    assert report["reason"] == "invalid_or_sensitive_input"
    assert report["request_started"] is False
    assert calls == [({}, {}, {"allow_api": False, "project_root": None})]
    assert "synthetic-browser-only" not in result.output
    assert "x" * 64 not in result.output


def _browser_response(choice):
    options = {
        "e-101": 0.25,
        "NO_MATCH_IN_CANDIDATES": 0.25,
        "AMBIGUOUS": 0.25,
        "NEEDS_VISUAL_EVIDENCE": 0.25,
    }
    return {
        "model": "jev-1.13.0",
        "answers": {
            "selection": {
                "type": "choice",
                "choice": choice,
                "confidence": 1,
                "probabilities": options,
            }
        },
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


@pytest.mark.parametrize(
    "choice",
    ["e-101", "NO_MATCH_IN_CANDIDATES", "AMBIGUOUS", "NEEDS_VISUAL_EVIDENCE"],
)
def test_browser_bridge_accepts_only_the_offered_selection_and_abstentions(
    monkeypatch, choice
):
    bodies = []

    def response(config, key, body, transport, deadline):
        bodies.append(json.loads(body))
        return True, "completed", "received", json.dumps(_browser_response(choice)).encode()

    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    monkeypatch.setattr(jev, "_bounded_request", response)
    result = runner.invoke(
        app,
        ["jev", "bridge", "--json"],
        input=json.dumps(browser_bridge_envelope()),
    )

    report = payload(result)
    assert report["status"] == "completed"
    assert report["answers"] == _browser_response(choice)["answers"]
    assert len(bodies) == 1
    assert set(bodies[0]["questions"]["selection"]["criteria"]) == {
        "e-101",
        "NO_MATCH_IN_CANDIDATES",
        "AMBIGUOUS",
        "NEEDS_VISUAL_EVIDENCE",
    }


@pytest.mark.parametrize(
    "content",
    [
        json.dumps(_browser_response("unoffered")).encode(),
        (
            b'{"model":"jev-1.13.0","model":"jev-1.13.0",'
            b'"answers":{},"usage":{"input_tokens":1,"output_tokens":1}}'
        ),
        json.dumps({
            **_browser_response("e-101"),
            "answers": {
                "selection": {
                    **_browser_response("e-101")["answers"]["selection"],
                    "choice": ["e-101"],
                }
            },
        }).encode(),
    ],
    ids=["unoffered", "duplicate", "malformed"],
)
def test_browser_bridge_refuses_unoffered_duplicate_and_malformed_responses(monkeypatch, content):
    def response(config, key, body, transport, deadline):
        return True, "completed", "received", content

    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    monkeypatch.setattr(jev, "_bounded_request", response)
    report = payload(
        runner.invoke(
            app,
            ["jev", "bridge", "--json"],
            input=json.dumps(browser_bridge_envelope()),
        )
    )

    assert report["status"] == "invalid_response"
    assert report["request_started"] is True
    assert report["used"] is False
    assert report["answers"] == {}


def test_completed_browser_selection_is_stateless_advice(project, monkeypatch):
    events = project / ".anvil/events.jsonl"
    before = events.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("bridge must not resolve or mutate Anvil state")

    def completed(config, capability, state, questions, **kwargs):
        assert capability == "browser_element_resolution"
        assert "selection" in questions
        return {
            "schema": "anvil.jev.annotation.v1",
            "provider": "typesafe",
            "model": config.model,
            "status": "completed",
            "reason": "validated",
            "request_started": True,
            "used": True,
            "answers": {"selection": {"type": "choice", "choice": "e-101"}},
        }

    monkeypatch.setattr(cli, "_resolve_state_dir", forbidden)
    monkeypatch.setattr(cli, "_resolve_project_dir", forbidden)
    monkeypatch.setattr(cli, "call_jev", completed)
    report = payload(
        runner.invoke(
            app,
            ["jev", "bridge", "--json"],
            input=json.dumps(browser_bridge_envelope()),
        )
    )

    assert report["answers"]["selection"]["choice"] == "e-101"
    assert events.read_bytes() == before


def test_evaluate_advice_never_writes_events(project, monkeypatch):
    payload(invoke(project, "enable", "evidence_triage", "--allow-api"))
    source = project / "selected.json"
    source.write_text(
        json.dumps(
            {
                "claim": "The routed request succeeds.",
                "observation": "The process started.",
            }
        )
    )
    events = project / ".anvil/events.jsonl"
    before = events.read_bytes()
    report = {
        "schema": "anvil.jev.annotation.v1",
        "model": "jev-1.13.0",
        "status": "completed",
        "reason": "validated",
        "request_started": True,
        "used": True,
        "answers": {"relation": {"type": "choice", "choice": "supports"}},
    }
    monkeypatch.setattr(cli, "call_jev", lambda *args, **kwargs: dict(report))
    assert payload(
        invoke(
            project,
            "evaluate",
            "evidence_triage",
            "--input",
            str(source),
            "--allow-export",
        )
    )["used"]
    assert events.read_bytes() == before


def test_late_result_is_discarded_after_disable(project, monkeypatch):
    payload(invoke(project, "enable", "voice_intent", "--allow-api"))
    source = project / "selected.json"
    source.write_text('{"text":"hello"}')

    def fake(*args, **kwargs):
        cli._save_settings(project / ".anvil/config.yaml", JevConfig())
        return {
            "model": "jev-1.13.0",
            "status": "completed",
            "reason": "validated",
            "request_started": True,
            "used": True,
            "answers": {"intent": {"choice": "conversation"}},
        }

    monkeypatch.setattr(cli, "call_jev", fake)
    report = payload(
        invoke(
            project,
            "evaluate",
            "voice_intent",
            "--input",
            str(source),
            "--allow-export",
        )
    )
    assert report["reason"] == "policy_changed"
    assert report["request_started"] is True
    assert report["used"] is False
    assert report["answers"] == {}


def test_assess_keeps_local_findings_and_never_emits_events(project):
    source = project / "draft.md"
    source.write_text(
        "# Project: Demo\n\n## Summary\nA demo.\n\n## Goals\n- Help a user.\n\n## Requirements\n- R001: It works.\n\n## Acceptance Criteria\n- It works well.\n"
    )
    events = project / ".anvil/events.jsonl"
    before = events.read_bytes()
    data = payload(invoke(project, "assess", "--file", str(source)))
    assert data["advisory"] is True
    assert data["findings"]
    assert data["jev"]["status"] == "disabled"
    assert events.read_bytes() == before


def test_configuration_rejects_invalid_jev(project):
    path = project / ".anvil/config.yaml"
    data = yaml.safe_load(path.read_text())
    data["jev"] = {"enabled": "false"}
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError):
        load_config(path)


def test_assess_discards_changed_source(project, monkeypatch):
    payload(invoke(project, "enable", "prd_review", "--allow-api"))
    source = project / "draft.md"
    source.write_text(
        "# Project: Demo\n\n## Summary\nA demo.\n\n## Goals\n- Help.\n\n"
        "## Requirements\n- R001: It works.\n\n## Acceptance Criteria\n- It works well.\n"
    )

    def fake(*args, **kwargs):
        source.write_text(source.read_text().replace("works well", "fails safely"))
        return {
            "used": True,
            "request_started": True,
            "answers": {"criterion1_success": {"score": 2}},
        }

    monkeypatch.setattr(cli, "call_jev", fake)
    result = payload(invoke(project, "assess", "--file", str(source), "--allow-export"))
    assert result["jev"]["reason"] == "source_changed"
    assert result["jev"]["answers"] == {}
    assert result["jev"]["request_started"] is True
    assert result["jev"]["used"] is False


def test_prd_documents_parse_and_cover_all_capabilities():
    from anvil.planning.template import parse_prd

    root = Path(__file__).resolve().parents[1] / "docs/specs/jev-integration"
    for name in (
        "foundation",
        "semantic-assurance",
        "agent-assistance",
        "serving-assistance",
    ):
        result = parse_prd((root / f"{name}.md").read_text(), prd_id=f"jev-{name}")
        assert not result.errors
        assert result.requirements and result.features and result.tasks
        assert all(task.verification.commands for task in result.tasks)


def test_audit_default_off_never_reads_input(project):
    report = payload(
        invoke(project, "audit", "--input", "missing.json", "--allow-export")
    )
    assert report["status"] == "disabled"
    assert report["request_started"] is False


def test_audit_records_independent_advice_without_state_writes(project, monkeypatch):
    payload(invoke(project, "enable", "evidence_triage", "--allow-api"))
    selected = project / "audit.json"
    selected.write_text(
        json.dumps(
            [
                {
                    "id": "handoff",
                    "capability": "evidence_triage",
                    "input": {
                        "claim": "The feature is deployed.",
                        "observation": "Source tests passed.",
                    },
                },
                {
                    "id": "disabled",
                    "capability": "voice_intent",
                    "input": {"text": "hi"},
                },
            ]
        )
    )
    events = project / ".anvil/events.jsonl"
    before = events.read_bytes()
    seen = []

    def fake(config, capability, state, questions, **kwargs):
        seen.append(capability)
        return {
            "model": config.model,
            "status": "completed",
            "reason": "validated",
            "request_started": capability == "evidence_triage",
            "used": bool(questions),
            "answers": {"relation": {"choice": "insufficient"}} if questions else {},
        }

    monkeypatch.setattr(cli, "call_jev", fake)
    report = payload(
        invoke(project, "audit", "--input", str(selected), "--allow-export")
    )
    assert report["complete"] is True
    assert report["request_count"] == 1
    assert [row["id"] for row in report["items"]] == ["handoff", "disabled"]
    assert events.read_bytes() == before


def test_audit_stops_after_policy_change(project, monkeypatch):
    payload(invoke(project, "enable", "evidence_triage", "--allow-api"))
    selected = project / "audit.json"
    selected.write_text(
        json.dumps(
            [
                {
                    "id": f"item{i}",
                    "capability": "evidence_triage",
                    "input": {"claim": "Works", "observation": "Started"},
                }
                for i in range(3)
            ]
        )
    )
    calls = []

    def fake(*args, **kwargs):
        calls.append(1)
        cli._save_settings(project / ".anvil/config.yaml", JevConfig())
        return {
            "request_started": True,
            "answers": {"relation": {"choice": "supports"}},
            "used": True,
        }

    monkeypatch.setattr(cli, "call_jev", fake)
    report = payload(
        invoke(project, "audit", "--input", str(selected), "--allow-export")
    )
    assert len(calls) == 1
    assert report["complete"] is False
    assert report["items"][0]["annotation"]["answers"] == {}
    assert report["request_count"] == 1


def test_evaluate_discards_result_when_selected_file_changes(project, monkeypatch):
    payload(invoke(project, "enable", "voice_intent", "--allow-api"))
    selected = project / "selected.json"
    selected.write_text('{"text":"hello"}')

    def fake(*args, **kwargs):
        selected.write_text('{"text":"goodbye"}')
        return {
            "model": "jev-1.13.0",
            "request_started": True,
            "answers": {},
            "used": True,
        }

    monkeypatch.setattr(cli, "call_jev", fake)
    report = payload(
        invoke(
            project,
            "evaluate",
            "voice_intent",
            "--input",
            str(selected),
            "--allow-export",
        )
    )
    assert report["reason"] == "source_changed"
    assert report["used"] is False


def test_selected_input_rejects_non_regular_source(tmp_path):
    with pytest.raises((OSError, ValueError)):
        cli._selected_input(tmp_path)


def test_concurrent_config_edit_is_retained_at_publication(project, monkeypatch):
    import importlib

    scan = importlib.import_module("anvil.cli.scan")

    config_path = project / ".anvil/config.yaml"
    original = config_path.read_bytes()
    concurrent = original + b"\nconcurrent_setting: preserve-me\n"
    capture = scan._atomic_capture_replace
    calls = []

    def racing_capture(path, replacement, captured):
        if not calls:
            calls.append(1)
            path.write_bytes(concurrent)
        capture(path, replacement, captured)

    monkeypatch.setattr(scan, "_atomic_capture_replace", racing_capture)
    result = invoke(project, "enable", "evidence_triage", "--allow-api")
    assert result.exit_code == 1
    assert config_path.read_bytes() == concurrent
    assert load_config(config_path).jev.enabled is False


def test_changed_config_before_save_is_refused(project):
    path = project / ".anvil/config.yaml"
    expected = load_merged_config(path)
    path.write_text(
        path.read_text().replace("strict_evidence: false", "strict_evidence: true")
    )
    with pytest.raises(ValueError, match="config_changed"):
        cli._save_settings(path, JevConfig(enabled=True), expected=expected)
    assert load_config(path).strict_evidence is True


@pytest.mark.parametrize("command", ["evaluate", "assess", "audit"])
def test_disable_then_reenable_invalidates_pending_advice(
    project, monkeypatch, command
):
    capability = "prd_review" if command == "assess" else "evidence_triage"
    payload(invoke(project, "enable", capability, "--allow-api"))
    path = project / ".anvil/config.yaml"
    original = load_merged_config(path)
    selected = project / "selected.json"
    value = {"claim": "Works", "observation": "Started"}
    if command == "assess":
        selected = project / "draft.md"
        selected.write_text(
            "# Project: Demo\n\n## Summary\nDemo.\n\n## Goals\n- Help.\n\n"
            "## Requirements\n- R001: It works.\n\n## Acceptance Criteria\n- It works.\n"
        )
        args = [command, "--file", str(selected)]
    else:
        selected.write_text(
            json.dumps(
                value
                if command == "evaluate"
                else [{"id": "item", "capability": capability, "input": value}]
            )
        )
        args = [
            command,
            *([capability] if command == "evaluate" else []),
            "--input",
            str(selected),
        ]

    def fake(*args, **kwargs):
        cli._save_settings(path, JevConfig())
        cli._save_settings(path, original.jev)
        assert load_merged_config(path) == original
        return {
            "used": True,
            "request_started": True,
            "answers": {"relation": {"choice": "supports"}},
        }

    monkeypatch.setattr(cli, "call_jev", fake)
    data = payload(invoke(project, *args, "--allow-export"))
    report = (
        data["jev"]
        if command == "assess"
        else data["items"][0]["annotation"]
        if command == "audit"
        else data
    )
    assert report["used"] is False
    assert report["answers"] == {}
    assert report["request_started"] is True
