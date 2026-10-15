"""Oturumsuz (herkese açık) formlar için CSRF koruması.

Tarayıcıya HttpOnly bir rastgele değer çerezi verilir; formdaki gizli alan bu değerin HMAC'idir.
Saldırgan site çerezi okuyamadığı için geçerli bir form alanı üretemez (double-submit, imzalı)."""
from __future__ import annotations

import hashlib
import hmac
import secrets

from flask import current_app, g, request

COOKIE = "vanessa_pre"


def _sign(pre: str) -> str:
    return hmac.new(current_app.config["SECRET_KEY"].encode(), pre.encode(), hashlib.sha256).hexdigest()


def token() -> str:
    pre = request.cookies.get(COOKIE) or g.get("_new_pre")
    if not pre:
        pre = secrets.token_urlsafe(24)
        g._new_pre = pre
    return _sign(pre)


def valid() -> bool:
    pre = request.cookies.get(COOKIE, "")
    sent = request.form.get("_csrf", "")
    return bool(pre) and bool(sent) and hmac.compare_digest(sent, _sign(pre))


def set_cookie_if_new(resp):
    pre = g.get("_new_pre")
    if pre:
        resp.set_cookie(COOKIE, pre, max_age=6 * 3600, httponly=True, samesite="Lax",
                        secure=current_app.config["SESSION_COOKIE_SECURE"], path="/")
    return resp
