"""Symmetric encryption for stored third-party credentials (e.g. Google OAuth refresh tokens)."""
import base64
import hashlib
import secrets
from cryptography.fernet import Fernet, InvalidToken
from pyapp.config import settings

_fallback = secrets.token_urlsafe(48)


def app_secret() -> str:
    return settings.SECRET_KEY or _fallback


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(("vault:" + app_secret()).encode()).digest())
    return Fernet(key)


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Stored credential cannot be decrypted; the SECRET_KEY changed. Reconnect the integration.") from exc
