"""Panel authentication & HMAC-signed HttpOnly session cookies."""

import hashlib
import hmac
import http.cookies
import os
import secrets
import time
from typing import Optional

from .config import get_server_config
from .database import Database


SESSION_COOKIE_NAME = "ovh_sniper_session"
SESSION_TTL_SECONDS = 7 * 86400


def hash_password(password: str, salt: Optional[str] = None) -> str:
    salt_hex = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 120_000)
    return f"pbkdf2_sha256${salt_hex}${dk.hex()}"


def verify_password_hash(password: str, stored_hash: str) -> bool:
    if not stored_hash or not password:
        return False
    parts = stored_hash.split("$")
    if len(parts) != 3 or parts[0] != "pbkdf2_sha256":
        return False
    expected = hash_password(password, salt=parts[1])
    return hmac.compare_digest(expected, stored_hash)


class PanelAuth:
    """Handles password verification, API-key header auth, and signed session cookies."""

    def __init__(self, db: Database, env_password: Optional[str] = None):
        self.db = db
        self._env_password = (
            env_password if env_password is not None else str(get_server_config().get("panel_password") or "")
        )
        self._signing_secret = self._load_or_create_signing_secret()

    def _load_or_create_signing_secret(self) -> bytes:
        existing = self.db.kv_get("panel_signing_secret")
        if isinstance(existing, str) and len(existing) >= 32:
            return existing.encode("utf-8")
        new_secret = secrets.token_hex(32)
        self.db.kv_set("panel_signing_secret", new_secret, ttl_seconds=10 * 365 * 86400)
        return new_secret.encode("utf-8")

    def is_auth_enabled(self) -> bool:
        if self._env_password:
            return True
        stored = str(self.db.get_setting("panel_password_hash", "") or "")
        return bool(stored)

    def verify_secret(self, candidate: str) -> bool:
        if not self.is_auth_enabled():
            return True
        if not candidate:
            return False
        stored = str(self.db.get_setting("panel_password_hash", "") or "")
        if stored and verify_password_hash(candidate, stored):
            return True
        if self._env_password and hmac.compare_digest(candidate, self._env_password):
            return True
        return False

    def set_panel_password(self, new_password: str) -> None:
        if not new_password:
            self.db.update_settings({"panel_password_hash": ""})
        else:
            self.db.update_settings({"panel_password_hash": hash_password(new_password)})

    def create_session_token(self) -> str:
        exp = int(time.time()) + SESSION_TTL_SECONDS
        nonce = secrets.token_hex(12)
        payload = f"{exp}.{nonce}"
        sig = hmac.new(self._signing_secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{payload}.{sig}"

    def verify_session_token(self, token: str) -> bool:
        if not token or token.count(".") != 2:
            return False
        exp_str, nonce, sig = token.split(".", 2)
        try:
            exp = int(exp_str)
        except ValueError:
            return False
        if exp < int(time.time()):
            return False
        payload = f"{exp_str}.{nonce}"
        expected = hmac.new(self._signing_secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

    def is_request_authorized(self, headers: dict) -> bool:
        if not self.is_auth_enabled():
            return True
        lower_headers = {str(k).lower(): v for k, v in headers.items()}
        # 1. X-API-Key or Bearer token
        api_key = lower_headers.get("x-api-key") or ""
        if api_key and self.verify_secret(str(api_key).strip()):
            return True
        auth_hdr = str(lower_headers.get("authorization") or "")
        if auth_hdr.lower().startswith("bearer "):
            tok = auth_hdr[7:].strip()
            if self.verify_secret(tok) or self.verify_session_token(tok):
                return True
        # 2. Signed session cookie
        cookie_hdr = str(lower_headers.get("cookie") or "")
        if cookie_hdr:
            jar = http.cookies.SimpleCookie()
            try:
                jar.load(cookie_hdr)
                morsel = jar.get(SESSION_COOKIE_NAME)
                if morsel and self.verify_session_token(morsel.value):
                    return True
            except Exception:
                pass
        return False
