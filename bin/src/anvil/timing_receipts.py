"""Pure command timing observations; never proofs, renewals or authority."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from anvil.state.hashing import (
    MAX_CANONICAL_JSON_INTEGER,
    MIN_CANONICAL_JSON_INTEGER,
    canonical_json_bytes,
    domain_separated_sha256,
)
from anvil.state.models import ClaimCommandEvidenceCore, HookCommandAttribution

MAX_TIMING_RECEIPT_BYTES = 16_384
TIMING_RECEIPT_DOMAIN = b"anvil.command-timing.v1\0"


class CommandTimingReceipt(BaseModel):
    """Claim-owner observation. Native ingestion must independently bind it.

    UTC endpoints may skew backwards. Monotonic elapsed is a separate fact;
    neither endpoint subtraction nor this model establishes execution authority.
    Missing terminal data stays partial instead of becoming a successful proof.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")

    schema_version: Literal[1] = 1
    receipt_id: StrictStr = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    attribution: HookCommandAttribution
    command_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: StrictStr = Field(max_length=32)
    ended_at: StrictStr | None = Field(default=None, max_length=32)
    elapsed_us: StrictInt | None = Field(default=None, ge=0, le=MAX_CANONICAL_JSON_INTEGER)
    outcome: Literal["succeeded", "failed", "interrupted"]
    exit_code: StrictInt | None = Field(
        default=None, ge=MIN_CANONICAL_JSON_INTEGER, le=MAX_CANONICAL_JSON_INTEGER,
    )
    classification: Literal["environment", "source", "unknown"] | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def _strict_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("timing schema version must be an integer")
        return value

    @field_validator("attribution", mode="before")
    @classmethod
    def _revalidate_attribution(cls, value: Any) -> Any:
        # Revalidate raw scalars; JSON serialization can coerce constructed bools.
        material = dict(value) if isinstance(value, HookCommandAttribution) else value
        if isinstance(material, Mapping) and type(material.get("schema_version", 1)) is not int:
            raise ValueError("attribution schema version must be an integer")
        return HookCommandAttribution.model_validate(material)

    @field_validator("started_at", "ended_at")
    @classmethod
    def _canonical_utc(cls, value: str | None) -> str | None:
        return ClaimCommandEvidenceCore._validate_command_time(value) if value is not None else None

    @model_validator(mode="after")
    def _validate_observation(self) -> CommandTimingReceipt:
        if self.outcome != "interrupted":
            if self.ended_at is None or self.elapsed_us is None or self.exit_code is None:
                raise ValueError("completed timing requires end, elapsed and exit code")
            if (self.exit_code == 0) != (self.outcome == "succeeded"):
                raise ValueError("timing outcome and exit code disagree")
        elif self.exit_code == 0:
            raise ValueError("interrupted timing cannot report successful exit")
        canonical_json_bytes(
            self.model_dump(mode="json"), max_bytes=MAX_TIMING_RECEIPT_BYTES,
            max_string_bytes=MAX_TIMING_RECEIPT_BYTES,
        )
        return self

    @property
    def utc_interval_status(self) -> Literal["partial", "clock_skew", "complete"]:
        if self.ended_at is None:
            return "partial"
        return (
            "clock_skew"
            if datetime.fromisoformat(self.ended_at) < datetime.fromisoformat(self.started_at)
            else "complete"
        )

    def semantic_digest(self) -> str:
        return domain_separated_sha256(
            TIMING_RECEIPT_DOMAIN, self.model_dump(mode="json"),
            max_bytes=MAX_TIMING_RECEIPT_BYTES, max_string_bytes=MAX_TIMING_RECEIPT_BYTES,
        )
