"""
Lambda handler: whatsapp

Triggered by SQS messages published when Meta's webhook delivers an inbound
WhatsApp message to FojiApi, which validates the signature and enqueues the
normalized payload.

Message shape:
  {
    "phone_number_id": "1234567890",   # our Meta number
    "from": "5511999998888",           # sender
    "message_id": "wamid.xxx",
    "text": "Hello, I need help",
    "timestamp": "1710000000"
  }

Flow:
  1. Parse the SQS record
  2. Resolve which Agent owns this phone_number_id
  3. Call foji-ai-api /internal/whatsapp/chat (returns full response, not streamed)
  4. Send the response back via Meta Cloud API
  5. On any failure: log + skip (do NOT raise — let Lambda ack the message)
"""

import json
import logging

import httpx

from app.core.config import get_settings
from app.core.database import get_session
from app.services.agent_resolver import resolve_agent_by_phone
from app.services.whatsapp_service import parse_inbound, send_text

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event: dict, context) -> dict:
    """AWS Lambda entry point — handles a batch of SQS records."""
    results = []
    for record in event.get("Records", []):
        try:
            body = json.loads(record["body"])
            _process_message(body)
            results.append({"message_id": body.get("message_id"), "status": "ok"})
        except Exception as exc:
            logger.exception("Unhandled error processing WhatsApp record: %s", record)
            results.append({"error": str(exc)})
    return {"results": results}


def _process_message(msg: dict) -> None:
    """Process a single inbound WhatsApp message."""
    phone_number_id = msg.get("phone_number_id", "")
    sender = msg.get("from", "")
    text = msg.get("text")
    message_id = msg.get("message_id", "")

    if not text:
        logger.info("Non-text message from %s (id=%s) — skipping", sender, message_id)
        return

    db = get_session()
    try:
        agent = resolve_agent_by_phone(db, phone_number_id)
        if not agent:
            logger.warning(
                "No agent for phone_number_id=%s — dropping message_id=%s",
                phone_number_id,
                message_id,
            )
            return

        reply = _call_ai_api(agent.agent_token, sender, text)
        send_text(phone_number_id, sender, reply)

        logger.info(
            "WhatsApp handled: agent_id=%d sender=%s message_id=%s",
            agent.id,
            sender,
            message_id,
        )
    finally:
        db.close()


def _call_ai_api(agent_token: str, session_id: str, message: str) -> str:
    """
    Call foji-ai-api's internal WhatsApp endpoint.

    The AI API handles history lookup, context assembly, and provider
    routing — it returns a plain-text string response (not streamed).
    """
    settings = get_settings()
    url = f"{settings.foji_ai_api_url}/internal/whatsapp/chat"
    payload = {
        "agent_token": agent_token,
        "session_id": f"wa:{session_id}",  # prefix to namespace WhatsApp sessions
        "message": message,
    }
    headers = {"X-Internal-Key": settings.internal_api_key}

    with httpx.Client(timeout=30) as client:
        resp = client.post(url, json=payload, headers=headers)

    resp.raise_for_status()
    data = resp.json()
    reply = data.get("reply", "").strip()

    if not reply:
        raise ValueError("AI API returned an empty reply")

    return reply
