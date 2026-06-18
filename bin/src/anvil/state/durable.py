"""Durable storage seam for anvil — push/pull events.jsonl to a remote store.

Local SQLite stays the working store. Events.jsonl is the source of truth.
"Durable backup" = push/pull that file (and optionally state.db) to a remote.

# ponytail: 2 methods only — enough for GCS/Azure to drop in later without
# touching CLI or backup.py. Upgrade path: add list_snapshots(), delete().
"""

from __future__ import annotations

from typing import Protocol


class S3Error(RuntimeError):
    """Clean error raised when an S3 push/pull fails due to AWS SDK issues.

    Callers (backup.py) catch this and surface a human-readable message
    via typer.echo + raise typer.Exit(code=1) — no raw botocore traceback.
    """


class DurableStore(Protocol):
    def push(self, local_path: str, remote_key: str) -> str:
        """Upload local_path; return the canonical URI (e.g. s3://bucket/key).

        # ponytail: no list_snapshots/delete — add when retention policy is needed
        """
        ...

    def pull(self, remote_key: str, local_path: str) -> None:
        """Download remote_key into local_path, overwriting if present."""
        ...


class S3DurableStore:
    """boto3-backed DurableStore. Satisfies the Protocol structurally."""

    def __init__(
        self,
        bucket: str,
        prefix: str = "",           # trailing slash appended if non-empty
        region: str | None = None,  # falls through to AWS env chain
        profile: str | None = None, # falls through to AWS env chain
    ) -> None:
        # ponytail: lazy import — boto3 may not be installed
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError(
                "S3 durable store requires boto3. "
                "Install it with: pip install 'anvil-state[s3]'"
            ) from exc
        # ponytail: no multipart upload — sufficient for events.jsonl (<100MB
        # typical); upgrade to upload_fileobj + multipart_chunksize if files grow
        session = boto3.Session(
            region_name=region, profile_name=profile
        )
        self._s3 = session.client("s3")
        self._bucket = bucket
        self._prefix = (prefix.rstrip("/") + "/") if prefix else ""

    def _key(self, remote_key: str) -> str:
        return self._prefix + remote_key

    def push(self, local_path: str, remote_key: str) -> str:
        import botocore.exceptions

        key = self._key(remote_key)
        try:
            self._s3.upload_file(local_path, self._bucket, key)
        except botocore.exceptions.NoCredentialsError as exc:
            raise S3Error(
                "No AWS credentials found. Configure credentials via environment "
                "variables (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY), an AWS "
                "profile, or an IAM role."
            ) from exc
        except botocore.exceptions.ClientError as exc:
            raise S3Error(f"S3 push failed: {exc}") from exc
        return f"s3://{self._bucket}/{key}"

    def pull(self, remote_key: str, local_path: str) -> None:
        import botocore.exceptions

        try:
            self._s3.download_file(self._bucket, self._key(remote_key), local_path)
        except botocore.exceptions.NoCredentialsError as exc:
            raise S3Error(
                "No AWS credentials found. Configure credentials via environment "
                "variables (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY), an AWS "
                "profile, or an IAM role."
            ) from exc
        except botocore.exceptions.ClientError as exc:
            raise S3Error(f"S3 pull failed: {exc}") from exc
