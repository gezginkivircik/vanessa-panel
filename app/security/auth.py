"""Kimlik doğrulama servisi: giriş, oturum, rate limit, hesap kilidi, CSRF."""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from flask import current_app, g, request

from ..audit import audit
from ..db import Connection
from ..timeutil import iso, now_iso, now_utc, parse_iso
from .passwords import dummy_verify, hash_password, verify_password


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def client_ip() -> str:
    if current_app.config.get("TRUST_PROXY"):
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()[:64]
    return (request.remote_addr or "unknown")[:64]


@dataclass
class LoginResult:
    ok: bool
    error: str | None = None
    token: str | None = None
    user: dict | None = None


GENERIC_ERROR = "Kullanıcı adı veya şifre hatalı."


def _record_attempt(db: Connection, username: str, ip: str, success: bool, reason: str) -> None:
    db.insert("login_attempts", {
        "username": username[:60], "ip_address": ip, "success": int(success),
        "reason": reason, "created_at": now_iso(),
    }, returning="")


def ip_rate_limited(db: Connection, ip: str) -> bool:
    cfg = current_app.config
    since = iso(now_utc() - dt.timedelta(minutes=cfg["LOGIN_IP_WINDOW_MINUTES"]))
    n = db.scalar(
        "SELECT COUNT(*) FROM login_attempts WHERE ip_address = ? AND created_at >= ? AND success = 0",
        (ip, since),
    )
    return (n or 0) >= cfg["LOGIN_MAX_ATTEMPTS_PER_IP"]


def attempt_login(db: Connection, username: str, password: str) -> LoginResult:
    cfg = current_app.config
    username = (username or "").strip().lower()
    ip = client_ip()

    if ip_rate_limited(db, ip):
        _record_attempt(db, username, ip, False, "ip_rate_limited")
        return LoginResult(False, "Çok fazla deneme yapıldı. Lütfen birkaç dakika sonra tekrar deneyin.")

    with db.transaction():
        user = db.one("SELECT * FROM users WHERE username = ? AND deleted_at IS NULL", (username,))
        if user is None:
            dummy_verify(password)
            _record_attempt(db, username, ip, False, "unknown_user")
            return LoginResult(False, GENERIC_ERROR)

        locked_until = parse_iso(user["locked_until"])
        if locked_until and locked_until > now_utc():
            _record_attempt(db, username, ip, False, "locked")
            return LoginResult(False, "Hesap çok fazla hatalı deneme nedeniyle geçici olarak kilitlendi. "
                                      "Lütfen daha sonra tekrar deneyin.")

        if not verify_password(password, user["password_hash"]):
            fails = user["failed_login_count"] + 1
            data = {"failed_login_count": fails, "updated_at": now_iso()}
            if fails >= cfg["LOGIN_MAX_FAILS_PER_USER"]:
                data["locked_until"] = iso(now_utc() + dt.timedelta(minutes=cfg["LOGIN_LOCK_MINUTES"]))
                data["failed_login_count"] = 0
                audit("auth.locked", "user", user["id"], f"{user['full_name']} hesabı kilitlendi",
                      db=db, actor=None)
            db.update("users", user["id"], data)
            _record_attempt(db, username, ip, False, "bad_password")
            return LoginResult(False, GENERIC_ERROR)

        if not user["is_active"]:
            _record_attempt(db, username, ip, False, "inactive")
            return LoginResult(False, "Bu hesap pasif durumda. Yönetici ile iletişime geçin.")

        token = secrets.token_urlsafe(32)
        now = now_utc()
        db.insert("user_sessions", {
            "user_id": user["id"],
            "token_hash": _token_hash(token),
            "csrf_token": secrets.token_urlsafe(32),
            "ip_address": ip,
            "user_agent": (request.headers.get("User-Agent") or "")[:255],
            "created_at": iso(now),
            "last_seen_at": iso(now),
            "expires_at": iso(now + dt.timedelta(days=cfg["SESSION_ABSOLUTE_DAYS"])),
        })
        db.update("users", user["id"], {
            "failed_login_count": 0, "locked_until": None,
            "last_login_at": iso(now), "updated_at": iso(now),
        })
        _record_attempt(db, username, ip, True, "ok")
        audit("auth.login", "user", user["id"], f"{user['full_name']} giriş yaptı", db=db, actor=user)
    return LoginResult(True, token=token, user=user)


def load_session(db: Connection, token: str | None) -> tuple[dict, dict] | None:
    """Geçerli oturumu ve kullanıcıyı döner; süresi dolmuşsa None."""
    if not token or len(token) > 200:
        return None
    row = db.one(
        """SELECT s.id AS session_id, s.csrf_token, s.last_seen_at, s.expires_at, s.revoked_at,
                  u.id, u.username, u.full_name, u.role, u.staff_id, u.is_active,
                  u.must_change_password, u.deleted_at
             FROM user_sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ?""",
        (_token_hash(token),),
    )
    if row is None or row["revoked_at"] or row["deleted_at"] or not row["is_active"]:
        return None
    now = now_utc()
    idle = dt.timedelta(minutes=current_app.config["SESSION_IDLE_MINUTES"])
    if parse_iso(row["expires_at"]) <= now or parse_iso(row["last_seen_at"]) + idle <= now:
        db.update("user_sessions", row["session_id"], {"revoked_at": iso(now)})
        return None
    # Her istekte yazmamak için son görülme 1 dakikada bir güncellenir.
    if parse_iso(row["last_seen_at"]) + dt.timedelta(minutes=1) <= now:
        db.update("user_sessions", row["session_id"], {"last_seen_at": iso(now)})
    session = {k: row[k] for k in ("session_id", "csrf_token")}
    user = {k: row[k] for k in ("id", "username", "full_name", "role", "staff_id",
                                "is_active", "must_change_password")}
    return session, user


def revoke_session(db: Connection, session_id: int) -> None:
    db.update("user_sessions", session_id, {"revoked_at": now_iso()})


def revoke_user_sessions(db: Connection, user_id: int, except_session_id: int | None = None) -> None:
    if except_session_id:
        db.execute("UPDATE user_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL AND id <> ?",
                   (now_iso(), user_id, except_session_id))
    else:
        db.execute("UPDATE user_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                   (now_iso(), user_id))


def set_password(db: Connection, user_id: int, new_password: str, must_change: bool = False) -> None:
    db.update("users", user_id, {
        "password_hash": hash_password(new_password),
        "must_change_password": int(must_change),
        "password_changed_at": now_iso(),
        "failed_login_count": 0,
        "locked_until": None,
        "updated_at": now_iso(),
    })


def csrf_valid() -> bool:
    sess = g.get("session")
    if not sess:
        return False
    sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token") or ""
    return hmac.compare_digest(sent.encode(), sess["csrf_token"].encode())
