import os
import json
import time
import hmac
import hashlib
import base64
import secrets
from typing import Optional


SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
DEFAULT_EXPIRE_SECONDS = int(os.environ.get("ACCESS_TOKEN_EXPIRE_SECONDS", 3600))


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(data: str) -> bytes:
    padding = "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data + padding)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000)
    return f"{salt}${dk.hex()}"


def verify_password(password: str, hashed: str) -> bool:
    try:
        salt, hash_hex = hashed.split("$", 1)
    except Exception:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000)
    return hmac.compare_digest(dk.hex(), hash_hex)


def create_access_token(subject: str, expires_seconds: Optional[int] = None) -> str:
    if expires_seconds is None:
        expires_seconds = DEFAULT_EXPIRE_SECONDS
    payload = {
        "sub": str(subject),
        "exp": int(time.time()) + int(expires_seconds),
    }
    payload_b = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_enc = _b64u_encode(payload_b)
    sig = hmac.new(SECRET_KEY.encode("utf-8"), payload_enc.encode("utf-8"), hashlib.sha256).digest()
    sig_enc = _b64u_encode(sig)
    return f"{payload_enc}.{sig_enc}"


def verify_access_token(token: str) -> Optional[dict]:
    try:
        payload_enc, sig_enc = token.split(".")
        expected_sig = hmac.new(SECRET_KEY.encode("utf-8"), payload_enc.encode("utf-8"), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64u_encode(expected_sig), sig_enc):
            return None
        payload_b = _b64u_decode(payload_enc)
        payload = json.loads(payload_b)
        if payload.get("exp", 0) < int(time.time()):
            return None
        return payload
    except Exception:
        return None
