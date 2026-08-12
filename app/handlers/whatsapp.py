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
from app.core.encryption import decrypt
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
            msg_id = record.get("messageId", "unknown")
            logger.exception("Failed to process WhatsApp SQS record messageId=%s", msg_id)
            results.append({"error": str(exc)})
    return {"results": results}


def _process_message(msg: dict) -> None:
    """Process a single inbound WhatsApp message."""
    phone_number_id = msg.get("phone_number_id", "")
    sender = msg.get("from", "")
    text = msg.get("text")
    message_id = msg.get("message_id", "")
    profile_name = msg.get("profile_name")

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

        # Inbox mode: a human answers from Foji's shared inbox, so record the
        # message and stay silent. Auto-replying here would mean the bot and a
        # team member both answering the same customer.
        if (agent.whats_app_mode or "Agent") == "Inbox":
            _record_inbox_message(
                agent_id=agent.id,
                phone_number_id=phone_number_id,
                wa_id=sender,
                profile_name=profile_name,
                wam_id=message_id,
                text=text,
            )
            logger.info(
                "WhatsApp routed to inbox: agent_id=%d sender=%s message_id=%s",
                agent.id, sender, message_id,
            )
            return

        reply = _call_ai_api(agent.agent_token, sender, text, profile_name)

        # Use the agent's own Meta token if configured; otherwise fall back to the
        # global token. Decryption failure must not drop the message — fall back.
        token = None
        if agent.whats_app_access_token_encrypted:
            try:
                token = decrypt(agent.whats_app_access_token_encrypted)
            except Exception:
                logger.exception(
                    "Failed to decrypt WhatsApp token for agent_id=%d — falling back to global token",
                    agent.id,
                )
        send_text(phone_number_id, sender, reply, token=token)

        logger.info(
            "WhatsApp handled: agent_id=%d sender=%s message_id=%s",
            agent.id,
            sender,
            message_id,
        )
    finally:
        db.close()


def _call_ai_api(
    agent_token: str, session_id: str, message: str, profile_name: str | None = None
) -> str:
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
        "sender_phone": session_id,
        "profile_name": profile_name,
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


def _record_inbox_message(
    *,
    agent_id: int,
    phone_number_id: str,
    wa_id: str,
    profile_name: str | None,
    wam_id: str,
    text: str,
) -> None:
    """
    Store an inbound message in FojiApi's shared inbox. FojiApi owns the Postgres
    schema, so the write goes through its internal endpoint rather than direct SQL.
    """
    settings = get_settings()
    url = f"{settings.foji_api_base_url}/api/whatsapp/inbox/internal/inbound"
    payload = {
        "agentId": agent_id,
        "phoneNumberId": phone_number_id,
        "waId": wa_id,
        "profileName": profile_name,
        "wamId": wam_id,
        "text": text,
    }
    headers = {"X-Internal-Key": settings.internal_api_key}

    with httpx.Client(timeout=10) as client:
        resp = client.post(url, json=payload, headers=headers)
    resp.raise_for_status()
