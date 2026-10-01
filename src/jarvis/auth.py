"""Single-user authentication: scrypt password + TOTP, server-side sessions, login throttling."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from .db import Database

SESSION_DAYS = 30
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=128 * 1024 * 1024)
    return "scrypt${}${}${}${}${}".format(SCRYPT_N, SCRYPT_R, SCRYPT_P, base64.b64encode(salt).decode(),
                                          base64.b64encode(digest).decode())


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _, n, r, p, salt, digest = stored.split("$")
        candidate = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                   maxmem=128 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, base64.b64decode(digest))


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_code(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def totp_uri(secret: str, account: str = "Chris", issuer: str = "Jarvis") -> str:
    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}&digits=6&period=30"


class Auth:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._failures: dict[str, list[float]] = {}

    # configuration ----------------------------------------------------------
    @property
    def row(self):
        return self.db.one("SELECT * FROM auth WHERE id = 1")

    @property
    def password_set(self) -> bool:
        return bool(self.row["password_hash"])

    def set_password(self, password: str) -> None:
        if len(password) < 12:
            raise ValueError("Use at least 12 characters.")
        self.db.execute("UPDATE auth SET password_hash = ? WHERE id = 1", (hash_password(password),))
        self.db.execute("DELETE FROM sessions")

    def begin_totp(self) -> str:
        secret = new_totp_secret()
        self.db.execute("UPDATE auth SET totp_secret = ?, totp_enabled = 0 WHERE id = 1", (secret,))
        return secret

    def confirm_totp(self, code: str) -> bool:
        secret = self.row["totp_secret"]
        if not secret or not self._check_code(secret, code, record=True):
            return False
        self.db.execute("UPDATE auth SET totp_enabled = 1 WHERE id = 1")
        return True

    def disable_totp(self) -> None:
        self.db.execute("UPDATE auth SET totp_enabled = 0, totp_secret = NULL WHERE id = 1")

    def _check_code(self, secret: str, code: str, record: bool) -> bool:
        code = "".join(ch for ch in code if ch.isdigit())
        if len(code) != 6:
            return False
        now = int(time.time() // 30)
        last = self.row["totp_last_counter"]
        for counter in (now - 1, now, now + 1):
            if counter > last and hmac.compare_digest(totp_code(secret, counter), code):
                if record:
                    self.db.execute("UPDATE auth SET totp_last_counter = ? WHERE id = 1", (counter,))
                return True
        return False

    # login ------------------------------------------------------------------
    def throttled(self, client: str) -> bool:
        now = time.time()
        recent = [t for t in self._failures.get(client, []) if now - t < 900]
        self._failures[client] = recent
        total = sum(1 for times in self._failures.values() for t in times if now - t < 3600)
        return len(recent) >= 5 or total >= 30

    def login(self, client: str, password: str, code: str) -> str | None:
        if self.throttled(client):
            return None
        row = self.row
        ok = verify_password(password, row["password_hash"])
        if ok and row["totp_enabled"]:
            ok = self._check_code(row["totp_secret"], code or "", record=True)
        if not ok:
            self._failures.setdefault(client, []).append(time.time())
            return None
        self._failures.pop(client, None)
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.db.execute("INSERT INTO sessions (token_hash, created, last_seen, expires) VALUES (?, ?, ?, ?)",
                        (self._hash(token), now, now, now + SESSION_DAYS * 86400))
        self.db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        return token

    def session_valid(self, token: str | None) -> bool:
        if not token:
            return False
        now = time.time()
        row = self.db.one("SELECT * FROM sessions WHERE token_hash = ?", (self._hash(token),))
        if row is None or row["expires"] < now:
            return False
        if now - row["last_seen"] > 300:
            self.db.execute("UPDATE sessions SET last_seen = ?, expires = ? WHERE token_hash = ?",
                            (now, now + SESSION_DAYS * 86400, self._hash(token)))
        return True

    def logout(self, token: str | None) -> None:
        if token:
            self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (self._hash(token),))

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()
