"""``anvil proof`` — verify portable signed AcceptanceProofs (B48 part 2).

The off-host verification path: given a proof JSON file and a trust list, confirm
the detached Ed25519 signature, the signer's identity, and that the signer is
trusted — all without the producer's private key.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

from anvil.cli._json import JSON_OPTION, emit_success, fail

proof_app = typer.Typer(
    help="Verify portable signed AcceptanceProofs (B48).",
    no_args_is_help=True,
)


def _default_trust_path() -> Path:
    """Trust-list location: ``$ANVIL_TRUST_LIST`` or ``~/.anvil/trust.txt``."""
    env = os.environ.get("ANVIL_TRUST_LIST")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".anvil" / "trust.txt"


@proof_app.command("verify")
def verify(
    proof_file: Path = typer.Argument(  # noqa: B008
        ..., help="Path to an AcceptanceProof JSON file."
    ),
    trust: Path | None = typer.Option(  # noqa: B008
        None,
        "--trust",
        help=(
            "Trust-list file: one public key (hex) or fingerprint per line, "
            "'#' comments. Default: $ANVIL_TRUST_LIST or ~/.anvil/trust.txt."
        ),
    ),
    project: str | None = typer.Option(  # noqa: B008
        None,
        "--project",
        help=(
            "If given, require the proof's project_id to equal this — rejects a "
            "proof minted in another project (cross-project replay)."
        ),
    ),
    json_output: bool = JSON_OPTION,
) -> None:
    """Verify a signed AcceptanceProof off-host.

    Three checks must ALL pass: the detached Ed25519 signature is valid for the
    embedded public key; ``signer_id`` is that key's true fingerprint; and the
    key is in the trust list (an unknown signer is rejected — this is what makes
    the proof verifiable *without trusting the producer*). With ``--project`` the
    proof's bound ``project_id`` must also match. Exit 0 + an ``ok`` JSON
    envelope when verified; non-zero otherwise.
    """
    from anvil import signing
    from anvil.state.models import AcceptanceProof

    try:
        raw = proof_file.read_text(encoding="utf-8")
    except OSError as exc:
        fail("proof verify", f"cannot read {proof_file}: {exc}", code="not_found")

    try:
        proof = AcceptanceProof.model_validate_json(raw)
    except Exception as exc:  # noqa: BLE001 — any parse/validation failure is a bad file
        fail(
            "proof verify",
            f"{proof_file} is not a valid AcceptanceProof: {exc}",
            code="bad_request",
        )

    trust_path = trust if trust is not None else _default_trust_path()
    trusted = signing.load_trust_list(trust_path)
    if trust is None and not trusted:
        # Fail-closed is correct, but a first-time user should know WHY: there's
        # simply no trust list yet. Informational only — never weakens the gate.
        typer.echo(
            f"Note: no trusted signers found in {trust_path}. Add one signer id "
            "or public key per line to enable verification.",
            err=True,
        )
    ok, problems = signing.verify_acceptance(proof, trusted)
    if project is not None and proof.project_id != project:
        ok = False
        problems = [
            *problems,
            f"proof is for project '{proof.project_id}', not '{project}'",
        ]

    if json_output:
        if ok:
            emit_success(
                "proof verify",
                {
                    "verified": True,
                    "project_id": proof.project_id,
                    "task_id": proof.task_id,
                    "signer_id": proof.signer_id,
                    "trust_list": str(trust_path),
                },
            )
            return
        fail("proof verify", "; ".join(problems), code="proof_unverified")

    if ok:
        typer.echo(
            f"OK: AcceptanceProof for task '{proof.task_id}' verified — "
            f"signer '{proof.signer_id}' is trusted."
        )
        return
    typer.echo(
        f"FAILED: AcceptanceProof for task '{proof.task_id}' did not verify:",
        err=True,
    )
    for problem in problems:
        typer.echo(f"  - {problem}", err=True)
    raise typer.Exit(1)
