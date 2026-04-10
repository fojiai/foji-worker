"""S3 helpers — download files and upload extraction artifacts."""

import json
import logging
import os
import tempfile

import boto3
from botocore.exceptions import ClientError

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def _client():
    cfg = get_settings()
    return boto3.client("s3", region_name=cfg.aws_region)


def download_to_tmp(s3_key: str) -> str:
    """Download an S3 object to /tmp and return the local file path."""
    cfg = get_settings()
    suffix = os.path.splitext(s3_key)[-1] or ".bin"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir="/tmp")
    _client().download_fileobj(cfg.aws_s3_bucket, s3_key, tmp)
    tmp.close()
    logger.debug("Downloaded s3://%s/%s → %s", cfg.aws_s3_bucket, s3_key, tmp.name)
    return tmp.name


def upload_text(s3_key: str, text: str) -> None:
    """Upload a UTF-8 string as a text/plain object."""
    cfg = get_settings()
    _client().put_object(
        Bucket=cfg.aws_s3_bucket,
        Key=s3_key,
        Body=text.encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
    )
    logger.debug("Uploaded text → s3://%s/%s (%d chars)", cfg.aws_s3_bucket, s3_key, len(text))


def upload_jsonl(s3_key: str, records: list[dict]) -> None:
    """Upload a list of dicts as JSONL (one JSON object per line)."""
    cfg = get_settings()
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
    _client().put_object(
        Bucket=cfg.aws_s3_bucket,
        Key=s3_key,
        Body=body.encode("utf-8"),
        ContentType="application/x-ndjson; charset=utf-8",
    )
    logger.debug(
        "Uploaded %d chunks JSONL → s3://%s/%s", len(records), cfg.aws_s3_bucket, s3_key
    )


def extraction_prefix(company_id: int, file_id: int, version: int) -> str:
    return f"tenant/{company_id}/files/{file_id}/extractions/{version}"
