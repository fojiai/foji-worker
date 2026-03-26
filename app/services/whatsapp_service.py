"""
Meta Cloud API client — send and parse WhatsApp messages.

Docs: https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages
"""

import logging

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_META_BASE = "https://graph.facebook.com/v19.0"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {get_settings().meta_whatsapp_token}",
        "Content-Type": "application/json",
    }


def send_text(phone_number_id: str, to: str, body: str) -> None:
    """Send a plain-text WhatsApp message."""
    url = f"{_META_BASE}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }
    with httpx.Client(timeout=15) as client:
        resp = client.post(url, json=payload, headers=_headers())
    if resp.status_code not in (200, 201):
        logger.error(
            "WhatsApp send failed: status=%d body=%s",
            resp.status_code,
            resp.text[:500],
        )
        resp.raise_for_status()
    logger.info("WhatsApp message sent to=%s via phone_number_id=%s", to, phone_number_id)


def parse_inbound(body: dict) -> list[dict]:
    """
    Extract inbound messages from a Meta webhook payload.

    Returns a list of normalised message dicts:
      {
        "phone_number_id": str,   # recipient (our number)
        "from": str,              # sender MSISDN (e.g. "5511999998888")
        "message_id": str,
        "text": str | None,
        "timestamp": str,
      }

    Non-text messages (image, audio, etc.) are included with text=None so
    the handler can decide how to respond.
    """
    messages = []
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
            for msg in value.get("messages", []):
                text = None
                if msg.get("type") == "text":
                    text = msg.get("text", {}).get("body")
                messages.append(
                    {
                        "phone_number_id": phone_number_id,
                        "from": msg.get("from", ""),
                        "message_id": msg.get("id", ""),
                        "text": text,
                        "timestamp": msg.get("timestamp", ""),
                    }
                )
    return messages
