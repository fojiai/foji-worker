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
  3. Call foji-ai-api /api/v1/internal/whatsapp/chat (full response, not streamed)
  4. Send the response back via Meta Cloud API
  5. On any failure: log + skip (do NOT raise — let Lambda ack the message)
"""

import json
import logging
import mimetypes

import httpx

from app.core.config import get_settings
from app.core.database import get_session
from app.core.encryption import decrypt
from app.services.agent_resolver import resolve_agent_by_phone
from app.services.whatsapp_service import (
    WhatsAppAuthError,
    fetch_media,
    parse_inbound,
    send_text,
)
from app.utils.s3 import upload_bytes

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
    message_type = msg.get("message_type") or "text"
    media_id = msg.get("media_id")
    media_mime = msg.get("media_mime")
    media_filename = msg.get("media_filename")

    # Text messages need a body; media messages may legitimately have none (no caption).
    if not text and not media_id:
        logger.info("Empty message from %s (id=%s) — skipping", sender, message_id)
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
            token = _agent_token(agent)
            media_key = media_content_type = None
            if media_id:
                # Meta's media URLs expire in minutes, so keep our own copy.
                try:
                    media_key, media_content_type = _store_media(
                        company_id=agent.company_id,
                        agent_id=agent.id,
                        media_id=media_id,
                        fallback_mime=media_mime,
                        token=token,
                    )
                except Exception:
                    logger.exception(
                        "Failed to download WhatsApp media %s — recording without it", media_id
                    )

            _record_inbox_message(
                agent_id=agent.id,
                phone_number_id=phone_number_id,
                wa_id=sender,
                profile_name=profile_name,
                wam_id=message_id,
                text=text or "",
                message_type=message_type,
                media_s3_key=media_key,
                media_content_type=media_content_type,
                media_filename=media_filename,
            )
            logger.info(
                "WhatsApp routed to inbox: agent_id=%d sender=%s message_id=%s",
                agent.id, sender, message_id,
            )
            return

        if not text:
            logger.info(
                "Media message from %s with no caption and agent in AI mode — skipping", sender
            )
            return

        reply = _call_ai_api(agent.agent_token, sender, text, profile_name)

        # Meter before sending. Meta bills per message, so this is the only
        # place the cost can actually be bounded — and it has to be live, not a
        # nightly aggregate, or a customer can outrun their allowance by a day.
        if not _consume_allowance(agent.id):
            logger.warning(
                "WhatsApp allowance exhausted for agent_id=%d — not replying to %s",
                agent.id, sender,
            )
            return

        try:
            send_text(phone_number_id, sender, reply, token=_agent_token(agent))
        except WhatsAppAuthError:
            # The token is dead, not the message. Flag the agent so the dashboard
            # asks the owner to reconnect — otherwise this channel just goes
            # quiet and looks like nobody messaged today.
            _flag_needs_reconnect(agent.id)
            raise

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
    # foji-ai-api mounts every router under /api/v1 (see its main.py). Without
    # the prefix this 404s, and because the reply is what gets sent back to
    # WhatsApp, the customer just never hears anything.
    base = settings.foji_ai_api_url.rstrip("/")
    url = f"{base}/api/v1/internal/whatsapp/chat"
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


def _consume_allowance(agent_id: int, category: str = "service") -> bool:
    """Record one outbound message and ask whether it is within the plan.

    Fails OPEN: if FojiApi is unreachable we still reply. A customer whose
    agent goes silent because our own API blipped is a worse outcome than a
    handful of unmetered messages, and the sweep reconciles nothing here —
    the counter simply misses those sends.
    """
    settings = get_settings()
    url = f"{settings.foji_api_base_url}/api/whatsapp/usage/internal/consume"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                url,
                json={"agentId": agent_id, "category": category},
                headers={"X-Internal-Key": settings.internal_api_key},
            )
        if resp.status_code != 200:
            logger.warning(
                "Usage check for agent %d returned %d — allowing the send",
                agent_id, resp.status_code,
            )
            return True
        return bool(resp.json().get("allowed", True))
    except Exception:
        logger.exception("Usage check for agent %d failed — allowing the send", agent_id)
        return True


def _flag_needs_reconnect(agent_id: int) -> None:
    """Tell FojiApi this agent's WhatsApp connection is broken.

    Best effort: if we cannot reach the API the send failure is already logged,
    and the twice-daily refresh sweep will reach the same conclusion.
    """
    settings = get_settings()
    url = f"{settings.foji_api_base_url}/api/whatsapp/onboarding/internal/needs-reconnect"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                url,
                json={"agentId": agent_id},
                headers={"X-Internal-Key": settings.internal_api_key},
            )
        if resp.status_code not in (200, 204):
            logger.warning(
                "Could not flag agent %d for reconnection: status=%d", agent_id, resp.status_code
            )
    except Exception:
        logger.exception("Could not flag agent %d for reconnection", agent_id)


def _agent_token(agent) -> str | None:
    """The agent's own Meta token, or None to fall back to the global one."""
    if not agent.whats_app_access_token_encrypted:
        return None
    try:
        return decrypt(agent.whats_app_access_token_encrypted)
    except Exception:
        logger.exception(
            "Failed to decrypt WhatsApp token for agent_id=%d — falling back to global token",
            agent.id,
        )
        return None


def _store_media(
    *, company_id: int, agent_id: int, media_id: str, fallback_mime: str | None, token: str | None
) -> tuple[str, str]:
    """Download media from Meta and put it in S3. Returns (s3_key, content_type)."""
    content, mime = fetch_media(media_id, token=token)
    content_type = mime or fallback_mime or "application/octet-stream"
    extension = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".bin"
    key = f"tenant/{company_id}/whatsapp/{agent_id}/{media_id}{extension}"
    upload_bytes(key, content, content_type)
    return key, content_type


def _record_inbox_message(
    *,
    agent_id: int,
    phone_number_id: str,
    wa_id: str,
    profile_name: str | None,
    wam_id: str,
    text: str,
    message_type: str = "text",
    media_s3_key: str | None = None,
    media_content_type: str | None = None,
    media_filename: str | None = None,
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
        "messageType": message_type,
        "mediaS3Key": media_s3_key,
        "mediaContentType": media_content_type,
        "mediaFileName": media_filename,
    }
    headers = {"X-Internal-Key": settings.internal_api_key}

    with httpx.Client(timeout=10) as client:
        resp = client.post(url, json=payload, headers=headers)
    resp.raise_for_status()
