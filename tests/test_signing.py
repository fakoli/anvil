"""Ed25519 signing, AcceptanceProof, and `anvil proof verify` (B48 part 2).

These lock the trust layer's load-bearing property: a signed AcceptanceProof
verifies **off-host with only the public key**, and an unknown/forged/tampered
proof is rejected — verifiable without trusting the producer.
"""

from __future__ import annotations

import datetime as dt
import json

from typer.testing import CliRunner

from anvil import signing
from anvil.cli import app
from anvil.cli.packet_apply import _resolve_strict_evidence, _strict_evidence_env
from anvil.state.models import AcceptanceProof, CommandProof, EventRange

runner = CliRunner()
_NOW = dt.datetime(2026, 6, 21, 12, 0, 0, tzinfo=dt.UTC)
_H = "a" * 64


def _signer(tmp_path):  # type: ignore[no-untyped-def]
    return signing.load_or_create_signer(tmp_path / "keys")


def _proof(signer_id, pub, *, command="uv run pytest -q", exit_code=0, project_id="proj-1"):  # type: ignore[no-untyped-def]
    return AcceptanceProof(
        project_id=project_id,
        task_id="T001",
        claim_id="CL-1",
        actor="agent-x",
        command_results=[
            CommandProof(
                command=command, exit_code=exit_code, output_sha256=_H, captured_at=_NOW
            )
        ],
        event_range=EventRange(start="E000001", end="E000012"),
        created_at=_NOW,
        signer_id=signer_id,
        public_key=pub,
    )


# ---------------------------------------------------------------------------
# Signing module
# ---------------------------------------------------------------------------


def test_keypair_generate_is_idempotent_and_0600(tmp_path) -> None:  # type: ignore[no-untyped-def]
    priv1, pub1, sid1 = _signer(tmp_path)
    priv2, pub2, sid2 = _signer(tmp_path)  # second call loads, doesn't regenerate
    assert (pub1, sid1) == (pub2, sid2)
    mode = (tmp_path / "keys" / "ed25519.pem").stat().st_mode & 0o777
    assert oct(mode) == "0o600"
    assert sid1 == signing.fingerprint(pub1)


def test_sign_verify_roundtrip_and_tamper(tmp_path) -> None:  # type: ignore[no-untyped-def]
    priv, pub, _ = _signer(tmp_path)
    sig = signing.sign(priv, b"hello")
    assert signing.verify(pub, b"hello", sig) is True
    assert signing.verify(pub, b"world", sig) is False  # wrong message
    assert signing.verify("00" * 32, b"hello", sig) is False  # wrong key
    assert signing.verify(pub, b"hello", "zz") is False  # malformed signature


def test_trust_list_parsing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    f = tmp_path / "trust.txt"
    f.write_text("# a comment\nabc123\n  def456  \n\n", encoding="utf-8")
    assert signing.load_trust_list(f) == {"abc123", "def456"}
    # Missing file -> empty set (fails closed).
    assert signing.load_trust_list(tmp_path / "nope.txt") == set()


# ---------------------------------------------------------------------------
# AcceptanceProof model + sign/verify helpers
# ---------------------------------------------------------------------------


def test_signed_bytes_excludes_envelope_and_is_signing_stable(tmp_path) -> None:  # type: ignore[no-untyped-def]
    priv, pub, sid = _signer(tmp_path)
    proof = _proof(sid, pub)
    before = proof.signed_bytes()
    signing.sign_proof(proof, priv)
    # Signing fills .signature but must NOT change the signed bytes (the
    # envelope is excluded), or a verifier could never reproduce them.
    assert proof.signed_bytes() == before
    for envelope_field in (b"signature", b"public_key", b"signer_id"):
        assert envelope_field not in before


def test_cross_host_verify_with_only_public_key(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Acceptance (3): re-verify after transport to a second host."""
    priv, pub, sid = _signer(tmp_path)
    proof = _proof(sid, pub)
    signing.sign_proof(proof, priv)
    wire = proof.model_dump_json()  # transport
    received = AcceptanceProof.model_validate_json(wire)  # 2nd host: only the JSON
    ok, problems = signing.verify_acceptance(received, {sid})  # only pubkey/trust
    assert ok, problems


def test_verify_rejects_tamper_untrusted_forged_unsigned(tmp_path) -> None:  # type: ignore[no-untyped-def]
    priv, pub, sid = _signer(tmp_path)
    proof = _proof(sid, pub)
    signing.sign_proof(proof, priv)
    wire = proof.model_dump_json()

    tampered = AcceptanceProof.model_validate_json(wire)
    tampered.actor = "attacker"
    assert signing.verify_acceptance(tampered, {sid})[0] is False

    untrusted = AcceptanceProof.model_validate_json(wire)
    assert signing.verify_acceptance(untrusted, set())[0] is False

    forged = AcceptanceProof.model_validate_json(wire)
    forged.signer_id = "deadbeefdeadbeef"
    assert signing.verify_acceptance(forged, {"deadbeefdeadbeef"})[0] is False

    unsigned = _proof(sid, pub)  # signature == ""
    ok, problems = signing.verify_acceptance(unsigned, {sid})
    assert ok is False
    assert "unsigned" in problems[0]


# ---------------------------------------------------------------------------
# `anvil proof verify` CLI
# ---------------------------------------------------------------------------


def test_verify_rejects_unsupported_algorithm(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A proof declaring a non-ed25519 algorithm is rejected even when the
    signature itself is valid over its (rsa-labelled) payload."""
    priv, pub, sid = _signer(tmp_path)
    proof = _proof(sid, pub)
    proof.algorithm = "rsa"  # part of signed_payload, so sign AFTER setting it
    signing.sign_proof(proof, priv)
    ok, problems = signing.verify_acceptance(proof, {sid})
    assert ok is False
    assert any("algorithm" in p for p in problems)


def test_proof_verify_cli_trusted_and_untrusted(tmp_path) -> None:  # type: ignore[no-untyped-def]
    priv, pub, sid = _signer(tmp_path)
    proof = _proof(sid, pub)
    signing.sign_proof(proof, priv)
    pf = tmp_path / "proof.json"
    pf.write_text(proof.model_dump_json())
    trust = tmp_path / "trust.txt"
    trust.write_text(sid + "\n")

    ok = runner.invoke(
        app, ["proof", "verify", str(pf), "--trust", str(trust), "--json"],
        catch_exceptions=False,
    )
    assert ok.exit_code == 0
    assert json.loads(ok.stdout)["ok"] is True

    empty = tmp_path / "empty.txt"
    empty.write_text("")
    bad = runner.invoke(
        app, ["proof", "verify", str(pf), "--trust", str(empty), "--json"],
        catch_exceptions=False,
    )
    assert bad.exit_code == 1
    assert json.loads(bad.stdout)["error"]["code"] == "proof_unverified"


def test_proof_verify_cli_tampered(tmp_path) -> None:  # type: ignore[no-untyped-def]
    priv, pub, sid = _signer(tmp_path)
    proof = _proof(sid, pub)
    signing.sign_proof(proof, priv)
    data = json.loads(proof.model_dump_json())
    data["command_results"][0]["exit_code"] = 1  # flip a failed run to look passing
    pf = tmp_path / "proof.json"
    pf.write_text(json.dumps(data))
    trust = tmp_path / "trust.txt"
    trust.write_text(sid + "\n")
    res = runner.invoke(
        app, ["proof", "verify", str(pf), "--trust", str(trust), "--json"],
        catch_exceptions=False,
    )
    assert res.exit_code == 1
    assert "signature" in json.loads(res.stdout)["error"]["message"]


def test_proof_verify_cli_project_binding(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """--project rejects a proof minted in a different project (cross-project
    replay): a valid, trusted proof for proj-1 fails --project proj-2."""
    priv, pub, sid = _signer(tmp_path)
    proof = _proof(sid, pub, project_id="proj-1")
    signing.sign_proof(proof, priv)
    pf = tmp_path / "proof.json"
    pf.write_text(proof.model_dump_json())
    trust = tmp_path / "trust.txt"
    trust.write_text(sid + "\n")

    same = runner.invoke(
        app,
        ["proof", "verify", str(pf), "--trust", str(trust), "--project", "proj-1", "--json"],
        catch_exceptions=False,
    )
    assert same.exit_code == 0
    assert json.loads(same.stdout)["data"]["project_id"] == "proj-1"

    cross = runner.invoke(
        app,
        ["proof", "verify", str(pf), "--trust", str(trust), "--project", "proj-2", "--json"],
        catch_exceptions=False,
    )
    assert cross.exit_code == 1
    assert "project" in json.loads(cross.stdout)["error"]["message"]


def test_proof_verify_cli_bad_and_missing_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json}")
    res = runner.invoke(
        app, ["proof", "verify", str(bad), "--json"], catch_exceptions=False
    )
    assert res.exit_code == 1
    assert json.loads(res.stdout)["error"]["code"] == "bad_request"

    res = runner.invoke(
        app, ["proof", "verify", str(tmp_path / "nope.json"), "--json"],
        catch_exceptions=False,
    )
    assert res.exit_code == 1
    assert json.loads(res.stdout)["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Strict-evidence env precedence (B48 acceptance 1)
# ---------------------------------------------------------------------------


def test_strict_evidence_env_precedence(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ANVIL_STRICT_EVIDENCE", "1")
    assert _strict_evidence_env() is True
    # env enables strict when no flag and no config
    assert _resolve_strict_evidence(None, tmp_path) is True
    # explicit flag beats env
    assert _resolve_strict_evidence(False, tmp_path) is False
    monkeypatch.setenv("ANVIL_STRICT_EVIDENCE", "off")
    assert _resolve_strict_evidence(None, tmp_path) is False
    monkeypatch.setenv("ANVIL_STRICT_EVIDENCE", "garbage")
    assert _strict_evidence_env() is None  # unrecognized -> defer
    monkeypatch.delenv("ANVIL_STRICT_EVIDENCE")
    assert _strict_evidence_env() is None


def test_env_disabling_config_enabled_strict_warns(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """The security-reducing override (env=off while config enabled strict) must
    not be silent; the enabling direction stays quiet to avoid fleet noise."""
    (tmp_path / "config.yaml").write_text(
        "project_name: t\nproject_id: t\nstrict_evidence: true\n", encoding="utf-8"
    )
    monkeypatch.setenv("ANVIL_STRICT_EVIDENCE", "false")
    assert _resolve_strict_evidence(None, tmp_path) is False  # env wins
    assert "disabling strict" in capsys.readouterr().err.lower()
    # enabling via env over the same config does NOT warn
    monkeypatch.setenv("ANVIL_STRICT_EVIDENCE", "true")
    assert _resolve_strict_evidence(None, tmp_path) is True
    assert "disabling strict" not in capsys.readouterr().err.lower()
