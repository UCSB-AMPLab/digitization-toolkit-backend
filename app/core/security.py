import json
import time
import hmac
import hashlib
import base64
import secrets
from typing import Optional

from fastapi import HTTPException
from app.core.config import settings

SECRET_KEY = settings.SECRET_KEY
DEFAULT_EXPIRE_SECONDS = settings.ACCESS_TOKEN_EXPIRE_SECONDS


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(data: str) -> bytes:
    padding = "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data + padding)


# PBKDF2-HMAC-SHA256 work factor. Self-describing hashes allow future increases;
# login upgrades weaker hashes when the plaintext is available.
_ALGO = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 600_000
_LEGACY_ITERATIONS = 100_000  # original hashes were stored bare as "salt$hash"


def _pbkdf2(password: str, salt: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations).hex()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    return f"{_ALGO}${PBKDF2_ITERATIONS}${salt}${_pbkdf2(password, salt, PBKDF2_ITERATIONS)}"


def verify_password(password: str, hashed: str) -> bool:
    try:
        parts = hashed.split("$")
        if len(parts) == 4 and parts[0] == _ALGO:
            _, iters, salt, digest = parts
            return hmac.compare_digest(_pbkdf2(password, salt, int(iters)), digest)
        if len(parts) == 2:
            # Legacy format: "salt$hash" at the original fixed iteration count.
            salt, digest = parts
            return hmac.compare_digest(_pbkdf2(password, salt, _LEGACY_ITERATIONS), digest)
    except Exception:
        return False
    return False


def needs_rehash(hashed: str) -> bool:
    """True when a stored hash uses a weaker scheme than the current default, so a
    successful login can transparently re-hash the password at full strength."""
    parts = hashed.split("$")
    if len(parts) == 4 and parts[0] == _ALGO:
        try:
            return int(parts[1]) < PBKDF2_ITERATIONS
        except ValueError:
            return True
    return True  # legacy 2-part or anything unrecognized


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
