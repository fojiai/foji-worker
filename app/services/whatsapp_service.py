"""
Meta Cloud API client — send and parse WhatsApp messages.

Docs: https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages
"""

import logging

import json

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def _meta_base() -> str:
    return f"https://graph.facebook.com/{get_settings().meta_graph_version}"


def _headers(token: str | None = None) -> dict:
    return {
        "Authorization": f"Bearer {token or get_settings().meta_whatsapp_token}",
        "Content-Type": "application/json",
    }


def send_text(phone_number_id: str, to: str, body: str, token: str | None = None) -> None:
    """Send a plain-text WhatsApp message.

    `token` is the tenant's Meta Cloud API token; falls back to the global
    META_WHATSAPP_TOKEN when the agent has no per-tenant token configured.
    """
    url = f"{_meta_base()}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }
    with httpx.Client(timeout=15) as client:
        resp = client.post(url, json=payload, headers=_headers(token))
    if resp.status_code not in (200, 201):
        logger.error(
            "WhatsApp send failed: status=%d body=%s",
            resp.status_code,
            resp.text[:500],
        )
        if is_auth_failure(resp.status_code, resp.text):
            raise WhatsAppAuthError(resp.text[:300])
        resp.raise_for_status()
    logger.info("WhatsApp message sent to=%s via phone_number_id=%s", to, phone_number_id)


class WhatsAppAuthError(Exception):
    """Meta rejected our token for this tenant.

    Distinct from a transient send failure: retrying will not help, and the
    customer has to reconnect. The handler flags the agent so the dashboard can
    say so, rather than the channel going quiet.
    """


def is_auth_failure(status_code: int, body: str) -> bool:
    """Whether Meta is telling us the token is dead rather than the send bad.

    Code 190 is the expired/invalid-token family; 401/403 cover the rest. We
    check the code in the body too, because Meta returns 400 for some token
    errors.
    """
    if status_code in (401, 403):
        return True
    try:
        error = json.loads(body).get("error", {})
    except (ValueError, AttributeError):
        return False
    if error.get("code") == 190:
        return True
    # 200-series subcodes are permission problems, which also need a reconnect.
    return error.get("type") == "OAuthException" and error.get("code") in (10, 200, 803)


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


def fetch_media(media_id: str, token: str | None = None) -> tuple[bytes, str | None]:
    """
    Download WhatsApp media. Two hops: the media id resolves to a short-lived
    URL, which must then be fetched with the same bearer token.

    Returns (content_bytes, mime_type).
    """
    headers = _headers(token)

    with httpx.Client(timeout=30) as client:
        meta_resp = client.get(f"{_meta_base()}/{media_id}", headers=headers)
        meta_resp.raise_for_status()
        meta = meta_resp.json()

        url = meta.get("url")
        if not url:
            raise ValueError(f"No download URL returned for media {media_id}")

        # The CDN URL still requires the Authorization header.
        binary = client.get(url, headers={"Authorization": headers["Authorization"]})
        binary.raise_for_status()

    return binary.content, meta.get("mime_type")
