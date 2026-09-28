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
  5. If the AI can't answer, send a short friendly fallback instead of silence
     (except 402 — plan inactive — where we stay quiet)
  6. On any failure: log + skip (do NOT raise — let Lambda ack the message)
"""

import base64
import json
import logging
import mimetypes
import time

import httpx

from app.core.config import get_settings
from app.core.database import get_session
from app.core.encryption import decrypt
from app.services.agent_resolver import resolve_agent_by_phone
from app.services.whatsapp_service import (
    WhatsAppAuthError,
    WhatsAppBillingError,
    fetch_media,
    mark_read,
    parse_inbound,
    send_text,
)
from app.utils.s3 import upload_bytes

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# What the customer reads when the AI can't answer. Failures aren't retried
# (the handler acks every record), so without these the customer was simply left
# on "visto" — which reads as a business ignoring them, not as a glitch.
_FALLBACK_REPLIES = {
    "error": {
        "PtBr": "Opa, tive um probleminha pra te responder agora 😅 Pode me mandar de novo daqui a pouquinho?",
        "Es": "Uy, tuve un problemita para responderte ahora 😅 ¿Me lo mandas de nuevo en un ratito?",
        "En": "Oops, I had a little trouble answering just now 😅 Could you send that again in a moment?",
    },
    # The business's monthly conversation limit is used up.
    "limit": {
        "PtBr": "No momento não consigo responder por aqui 🙏 Se for urgente, fale com a gente por outro canal.",
        "Es": "En este momento no puedo responder por aquí 🙏 Si es urgente, contáctanos por otro canal.",
        "En": "I can't reply here right now 🙏 If it's urgent, please reach us through another channel.",
    },
    # A voice note we couldn't even download from Meta.
    "voice_failed": {
        "PtBr": "Não consegui ouvir seu áudio agora 😅 Pode me mandar por escrito?",
        "Es": "No pude escuchar tu audio ahora 😅 ¿Me lo puedes escribir?",
        "En": "I couldn't play your voice message just now 😅 Could you type it for me?",
    },
    "voice_too_long": {
        "PtBr": "Esse áudio ficou grande demais pra eu ouvir por aqui 😅 Consegue me mandar um resumo por escrito?",
        "Es": "Ese audio es demasiado largo para escucharlo por aquí 😅 ¿Me mandas un resumen por escrito?",
        "En": "That voice message is too long for me to play here 😅 Could you send me a short summary in text?",
    },
    # A photo, document or video with no caption. Honest about what we can't
    # see yet, instead of ignoring it.
    "media_no_caption": {
        "PtBr": "Recebi! 🙂 Por aqui ainda não consigo abrir imagens e arquivos — me conta por escrito o que você precisa?",
        "Es": "¡Recibido! 🙂 Por aquí todavía no puedo abrir imágenes ni archivos — ¿me cuentas por escrito qué necesitas?",
        "En": "Got it! 🙂 I can't open images or files here yet — could you tell me in text what you need?",
    },
}

# Meta caps WhatsApp media at 16 MB.
_VOICE_MAX_BYTES = 16 * 1024 * 1024
# Media we answer with "tell me in text" when it comes without a caption.
# Stickers, reactions and the like are left alone — a person wouldn't reply to those either.
_DESCRIBABLE_MEDIA = {"image", "document", "video"}

# The Lambda has a hard timeout. Whatever the AI call takes, this much is kept
# back so a fallback can still be metered and sent — otherwise a slow AI reply
# kills the run and the customer gets nothing at all.
_SEND_RESERVE_SECONDS = 8
_DEFAULT_BUDGET_SECONDS = 22


def _deadline(context) -> float:
    """Monotonic time by which the AI work must finish, leaving room to reply."""
    try:
        remaining = context.get_remaining_time_in_millis() / 1000
    except Exception:  # noqa: BLE001 — no Lambda context (tests, local runs)
        remaining = _DEFAULT_BUDGET_SECONDS + _SEND_RESERVE_SECONDS
    return time.monotonic() + max(5.0, remaining - _SEND_RESERVE_SECONDS)


def _time_left(deadline: float) -> float:
    return max(3.0, deadline - time.monotonic())


def _fallback_reply(agent, kind: str) -> str:
    replies = _FALLBACK_REPLIES[kind]
    return replies.get(agent.agent_language or "PtBr") or replies["PtBr"]


def handler(event: dict, context) -> dict:
    """AWS Lambda entry point — handles a batch of SQS records."""
    results = []
    for record in event.get("Records", []):
        try:
            body = json.loads(record["body"])
            _process_message(body, deadline=_deadline(context))
            results.append({"message_id": body.get("message_id"), "status": "ok"})
        except Exception as exc:
            msg_id = record.get("messageId", "unknown")
            logger.exception("Failed to process WhatsApp SQS record messageId=%s", msg_id)
            results.append({"error": str(exc)})
    return {"results": results}


def _process_message(msg: dict, deadline: float | None = None) -> None:
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

        if deadline is None:
            deadline = time.monotonic() + _DEFAULT_BUDGET_SECONDS

        # Blue ticks and "digitando…" straight away, the way a person picks up
        # the phone — the customer sees someone is on it while the AI (and any
        # voice-note transcription) works. Messages we won't answer (a sticker)
        # are still marked read, just without the typing indicator. Inbox mode
        # never gets here: there, ticks wait until a person opens the thread.
        stays_quiet = (
            not text
            and not (message_type == "audio" and media_id)
            and message_type not in _DESCRIBABLE_MEDIA
        )
        mark_read(phone_number_id, message_id, token=_agent_token(agent), typing=not stays_quiet)

        # Voice notes are downloaded and sent to the AI API to be transcribed and
        # answered. Media without a caption used to be dropped silently, which
        # left the customer on "visto".
        audio: bytes | None = None
        audio_mime: str | None = None
        fallback_kind: str | None = None
        if message_type == "audio" and media_id:
            try:
                audio, fetched_mime = fetch_media(
                    media_id, token=_agent_token(agent), timeout=min(15.0, _time_left(deadline))
                )
                audio_mime = fetched_mime or media_mime
                if len(audio) > _VOICE_MAX_BYTES:
                    audio, fallback_kind = None, "voice_too_long"
            except Exception:
                logger.exception("Could not download voice note %s from %s", media_id, sender)
                audio, fallback_kind = None, "voice_failed"
        elif not text:
            if message_type in _DESCRIBABLE_MEDIA:
                fallback_kind = "media_no_caption"
            else:
                logger.info(
                    "%s from %s with no text in AI mode — nothing to answer", message_type, sender
                )
                return

        try:
            if fallback_kind:
                reply = _fallback_reply(agent, fallback_kind)
            else:
                reply = _call_ai_api(
                    agent.agent_token, sender, text or "", profile_name,
                    audio=audio, audio_mime=audio_mime, timeout=_time_left(deadline),
                )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 402:
                # Plan inactive or without WhatsApp — the business isn't paying
                # for this channel, so we don't answer on its behalf.
                logger.warning(
                    "AI API refused agent_id=%d (402: plan inactive or no WhatsApp) — not replying to %s",
                    agent.id, sender,
                )
                return
            logger.warning(
                "AI API returned %d for agent_id=%d — sending a fallback to %s",
                status, agent.id, sender,
            )
            reply = _fallback_reply(agent, "limit" if status == 429 else "error")
        except Exception:
            # Timeout, network, empty reply, 5xx. Better a friendly "try again"
            # than silence.
            logger.exception(
                "AI API failed for agent_id=%d — sending a fallback to %s", agent.id, sender
            )
            reply = _fallback_reply(agent, "error")

        # Meter before sending — the fallback too, so it stays inside the
        # allowance like any other message we send. Meta bills per message, so this is the only
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
        except WhatsAppBillingError:
            # Meta has switched this customer's messaging off for want of a card.
            # Surfacing it as "reconnect" would send them to the wrong place.
            _flag_needs_reconnect(agent.id, reason="billing")
            raise
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
    agent_token: str,
    session_id: str,
    message: str,
    profile_name: str | None = None,
    audio: bytes | None = None,
    audio_mime: str | None = None,
    timeout: float = _DEFAULT_BUDGET_SECONDS,
) -> str:
    """
    Call foji-ai-api's internal WhatsApp endpoint.

    The AI API handles history lookup, context assembly, and provider
    routing — it returns a plain-text string response (not streamed). A voice
    note travels as base64 and is transcribed there.
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
    if audio:
        payload["audio_base64"] = base64.b64encode(audio).decode("ascii")
        payload["audio_mime"] = audio_mime
    headers = {"X-Internal-Key": settings.internal_api_key}

    # Bounded by the Lambda's remaining time (see _deadline) so a slow answer
    # still leaves room to send a fallback.
    with httpx.Client(timeout=timeout) as client:
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


def _flag_needs_reconnect(agent_id: int, reason: str = "reconnect") -> None:
    """Tell FojiApi this agent's WhatsApp connection is broken.

    `reason` distinguishes a dead token ("reconnect") from a WABA that cannot be
    billed ("billing") — different problems with different fixes.

    Best effort: if we cannot reach the API the send failure is already logged,
    and the twice-daily refresh sweep will reach the same conclusion.
    """
    settings = get_settings()
    url = f"{settings.foji_api_base_url}/api/whatsapp/onboarding/internal/needs-reconnect"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                url,
                json={"agentId": agent_id, "reason": reason},
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
