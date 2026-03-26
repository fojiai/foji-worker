"""
Lambda handler: file_extraction

Triggered by SQS messages published by FojiApi on file upload.
Message: { "job": "extract_file", "agent_file_id": 123 }

Flow:
  1. Load AgentFile + Agent (for company_id)
  2. Set status → Processing
  3. Download original file from S3 to /tmp
  4. Extract raw text (by content type)
  5. Normalize text
  6. Upload raw.txt + normalized.txt to S3
  7. Chunk normalized text
  8. Upload chunks.jsonl to S3
  9. Update AgentFile with S3 keys, bump ExtractionVersion, set status → Ready
  10. On any failure: status → Failed + error_message
"""

import json
import logging
import os
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.database import get_session
from app.models.agent_file import AgentFile
from app.models.agent import Agent
from app.services import extractors
from app.services.chunker import chunk, normalize
from app.utils.retry import with_retry
from app.utils.s3 import download_to_tmp, extraction_prefix, upload_jsonl, upload_text

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event: dict, context) -> dict:
    """AWS Lambda entry point — handles a batch of SQS records."""
    results = []
    for record in event.get("Records", []):
        try:
            body = json.loads(record["body"])
            agent_file_id = int(body["agent_file_id"])
            _process_file(agent_file_id)
            results.append({"agent_file_id": agent_file_id, "status": "ok"})
        except Exception as exc:
            logger.exception("Unhandled error processing record: %s", record)
            results.append({"error": str(exc)})
    return {"results": results}


def _process_file(agent_file_id: int) -> None:
    db: Session = get_session()
    local_path: str | None = None
    try:
        agent_file = db.get(AgentFile, agent_file_id)
        if not agent_file:
            raise ValueError(f"AgentFile id={agent_file_id} not found")

        agent = db.get(Agent, agent_file.agent_id)
        if not agent:
            raise ValueError(f"Agent id={agent_file.agent_id} not found")

        company_id = agent.company_id
        version = (agent_file.extraction_version or 0) + 1
        prefix = extraction_prefix(company_id, agent_file_id, version)

        # Mark as processing
        agent_file.processing_status = "Processing"
        agent_file.error_message = None
        db.commit()

        # 1. Download original file
        local_path = download_to_tmp(agent_file.s3_key)

        # 2. Extract
        extractor = extractors.get_extractor(agent_file.content_type, agent_file.file_name)
        if not extractor:
            raise ValueError(f"Unsupported file type: {agent_file.content_type} / {agent_file.file_name}")

        raw_text = _extract_with_retry(extractor, local_path)
        if not raw_text.strip():
            raise ValueError("Extraction produced empty text")

        # 3. Normalize
        normalized_text = normalize(raw_text)

        # 4. Upload raw + normalized to S3
        raw_key = f"{prefix}/raw.txt"
        normalized_key = f"{prefix}/normalized.txt"
        upload_text(raw_key, raw_text)
        upload_text(normalized_key, normalized_text)

        # 5. Chunk + upload
        chunks = chunk(normalized_text)
        chunks_key = f"{prefix}/chunks.jsonl"
        upload_jsonl(chunks_key, chunks)

        # 6. Update DB
        agent_file.processing_status = "Ready"
        agent_file.extraction_version = version
        agent_file.extracted_at = datetime.now(timezone.utc)
        agent_file.s3_raw_text_key = raw_key
        agent_file.s3_normalized_text_key = normalized_key
        agent_file.s3_chunks_key = chunks_key
        agent_file.error_message = None
        db.commit()

        logger.info(
            "file_id=%d processed: %d chunks → %s",
            agent_file_id, len(chunks), chunks_key
        )

    except Exception as exc:
        logger.exception("Failed to process file_id=%d", agent_file_id)
        try:
            agent_file = db.get(AgentFile, agent_file_id)
            if agent_file:
                agent_file.processing_status = "Failed"
                agent_file.error_message = str(exc)[:1000]
                db.commit()
        except Exception:
            logger.exception("Could not update failure status for file_id=%d", agent_file_id)
        raise
    finally:
        db.close()
        if local_path and os.path.exists(local_path):
            os.remove(local_path)


@with_retry(max_attempts=2, delay_seconds=1.0)
def _extract_with_retry(extractor, file_path: str) -> str:
    return extractor.extract(file_path)
