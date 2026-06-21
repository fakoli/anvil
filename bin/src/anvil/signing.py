"""Ed25519 signing for portable ``AcceptanceProof``s (B48 part 2).

A per-runner Ed25519 keypair (load-or-generate) lives under ``~/.anvil/keys/``
(override with ``ANVIL_KEYS_DIR`` — tests and CI use this for hermeticity). The
public-key fingerprint is the signer id. Detached signatures over a canonical
byte form let an ``AcceptanceProof`` be verified **off-host with only the public
key** — and a *trust list* of allowed keys is what makes "verifiable without
trusting the producer" meaningful: a syntactically-valid signature from an
unknown key is rejected.

Asymmetric (not HMAC) is the load-bearing choice: the verifier holds only the
public key, so it cannot forge what it verifies.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from anvil.state.models import AcceptanceProof

__all__ = [
    "keys_dir",
    "fingerprint",
    "public_key_to_hex",
    "load_or_create_signer",
    "sign",
    "verify",
    "load_trust_list",
    "is_trusted",
    "sign_proof",
    "verify_acceptance",
]

_PRIV_FILENAME = "ed25519.pem"


def keys_dir() -> Path:
    """Per-runner key directory: ``$ANVIL_KEYS_DIR`` or ``~/.anvil/keys``."""
    env = os.environ.get("ANVIL_KEYS_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".anvil" / "keys"


def fingerprint(public_key_hex: str) -> str:
    """Stable signer id: first 16 hex chars of sha256(raw public-key bytes)."""
    return hashlib.sha256(bytes.fromhex(public_key_hex)).hexdigest()[:16]


def public_key_to_hex(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return raw.hex()


def load_or_create_signer(
    directory: Path | None = None,
) -> tuple[Ed25519PrivateKey, str, str]:
    """Return ``(private_key, public_key_hex, signer_id)``.

    Generates and persists a keypair on first use; the private key is written
    PKCS8/PEM with ``0o600`` permissions. Idempotent: a second call loads it.
    """
    d = directory or keys_dir()
    d.mkdir(parents=True, exist_ok=True)
    priv_path = d / _PRIV_FILENAME
    if priv_path.exists():
        private_key = serialization.load_pem_private_key(
            priv_path.read_bytes(), password=None
        )
        if not isinstance(private_key, Ed25519PrivateKey):  # pragma: no cover
            raise ValueError(f"{priv_path} is not an Ed25519 private key")
    else:
        private_key = Ed25519PrivateKey.generate()
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        # Write 0600 from the start: create with restrictive mode, then write.
        priv_path.touch(mode=0o600, exist_ok=True)
        priv_path.write_bytes(pem)
        priv_path.chmod(0o600)
    pub_hex = public_key_to_hex(private_key.public_key())
    return private_key, pub_hex, fingerprint(pub_hex)


def sign(private_key: Ed25519PrivateKey, message: bytes) -> str:
    """Detached signature (hex) over ``message``."""
    return private_key.sign(message).hex()


def verify(public_key_hex: str, message: bytes, signature_hex: str) -> bool:
    """True iff ``signature_hex`` is a valid signature of ``message`` by the key.

    Catches every failure mode (bad signature, malformed hex, wrong key length)
    and returns False — verification never raises.
    """
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        public_key.verify(bytes.fromhex(signature_hex), message)
        return True
    except (InvalidSignature, ValueError):
        return False


def load_trust_list(path: Path) -> set[str]:
    """Load trusted public keys / fingerprints, one per line ('#' comments).

    A missing file is an empty trust set (so verification fails closed — an
    unknown signer is untrusted rather than silently accepted)."""
    if not path.exists():
        return set()
    trusted: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            trusted.add(line.split()[0])
    return trusted


def is_trusted(public_key_hex: str, trust: set[str]) -> bool:
    """A key is trusted if its full hex OR its fingerprint is in ``trust``."""
    return public_key_hex in trust or fingerprint(public_key_hex) in trust


def sign_proof(
    proof: AcceptanceProof, private_key: Ed25519PrivateKey
) -> AcceptanceProof:
    """Fill ``proof.signature`` with a detached signature over its core bytes.

    Mutates and returns ``proof`` (its ``public_key`` / ``signer_id`` are
    expected to already match ``private_key``)."""
    proof.signature = sign(private_key, proof.signed_bytes())
    return proof


def verify_acceptance(
    proof: AcceptanceProof, trust: set[str]
) -> tuple[bool, list[str]]:
    """Verify an ``AcceptanceProof`` off-host: ``(ok, problems)``.

    Four independent checks, all of which must pass:
      1. the declared algorithm is one this verifier supports (ed25519) — so a
         future second algorithm can't steer a verifier into the wrong primitive;
      2. the detached signature is valid for the embedded public key;
      3. ``signer_id`` is the genuine fingerprint of that public key (so the id
         can't claim to be someone it isn't);
      4. the public key is in the trust list (an unknown signer is rejected —
         this is what makes verification meaningful without trusting the producer).
    """
    problems: list[str] = []
    if proof.algorithm != "ed25519":
        problems.append(f"unsupported signature algorithm: {proof.algorithm!r}")
    if not proof.signature:
        problems.append("proof is unsigned")
    elif not verify(proof.public_key, proof.signed_bytes(), proof.signature):
        problems.append("signature does not verify against the embedded public key")
    if fingerprint(proof.public_key) != proof.signer_id:
        problems.append("signer_id is not the fingerprint of the public key")
    if not is_trusted(proof.public_key, trust):
        problems.append("signer is not in the trust list")
    return (not problems, problems)
