"""
AES-256-GCM decryption for secrets encrypted by FojiApi's EncryptionService.

Format must match exactly (same as foji-ai-api):
  base64(12-byte IV) : base64(ciphertext) : base64(16-byte auth tag)
Key source: ENCRYPTION_KEY env var (base64, 32 bytes), falling back to the
legacy GOOGLE_CALENDAR_ENCRYPTION_KEY — the same key FojiApi and foji-ai-api
use, shared here to decrypt per-agent WhatsApp access tokens.
"""

import base64

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings


def _get_key() -> bytes:
    settings = get_settings()
    key_b64 = settings.encryption_key or settings.google_calendar_encryption_key
    if not key_b64:
        raise RuntimeError(
            "No encryption key set. Set ENCRYPTION_KEY (or the legacy "
            "GOOGLE_CALENDAR_ENCRYPTION_KEY) to a base64-encoded 32-byte value."
        )
    key = base64.b64decode(key_b64)
    if len(key) != 32:
        raise RuntimeError("The encryption key must be exactly 32 bytes (base64-encoded).")
    return key


def decrypt(encrypted: str) -> str:
    """Decrypt a value encrypted by FojiApi's EncryptionService."""
    parts = encrypted.split(":")
    if len(parts) != 3:
        raise ValueError("Invalid encrypted value format. Expected: base64(iv):base64(ciphertext):base64(tag)")

    iv = base64.b64decode(parts[0])
    ciphertext = base64.b64decode(parts[1])
    tag = base64.b64decode(parts[2])

    aesgcm = AESGCM(_get_key())
    # AESGCM expects ciphertext + tag concatenated
    plaintext = aesgcm.decrypt(iv, ciphertext + tag, None)
    return plaintext.decode("utf-8")
