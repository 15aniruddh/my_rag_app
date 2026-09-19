"""S3 staging for uploaded PDFs.

Lambda's /tmp is per-container, so a PDF written while handling the upload
request is gone by the time Inngest calls back for a step - that callback is a
separate invocation with an empty filesystem. Staging the bytes in S3 is what
makes the durable ingest path work at all.

With UPLOAD_S3_BUCKET unset (local development), api.py ingests inline and
nothing here is used.
"""

import os
import tempfile
import uuid
from pathlib import Path

PREFIX = "uploads/"
BUCKET = os.getenv("UPLOAD_S3_BUCKET")


def enabled() -> bool:
    return bool(BUCKET)


def _client():
    import boto3  # provided by the Lambda runtime; not a packaged dependency

    return boto3.client("s3")


def put(payload: bytes, filename: str) -> str:
    """Stage bytes for ingestion and return the key.

    The uuid segment stops two concurrent uploads of the same filename from
    overwriting each other while the first is still being ingested.
    """
    key = f"{PREFIX}{uuid.uuid4()}/{Path(filename).name}"
    _client().put_object(Bucket=BUCKET, Key=key, Body=payload)
    return key


def fetch_to_tmp(key: str) -> str:
    """Download a staged PDF into /tmp. The caller deletes it."""
    dest = Path(tempfile.gettempdir()) / "rag_uploads" / Path(key).name
    dest.parent.mkdir(parents=True, exist_ok=True)
    _client().download_file(BUCKET, key, str(dest))
    return str(dest)


def discard(key: str) -> None:
    """Delete a staged PDF. Safe to repeat: Inngest replays steps."""
    _client().delete_object(Bucket=BUCKET, Key=key)
