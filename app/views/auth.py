"""Giriş / çıkış / şifre değiştirme."""
from __future__ import annotations

import hashlib
import hmac
import secrets

from flask import (Blueprint, current_app, flash, g, make_response, redirect, render_template,
                   request, url_for)

from ..audit import audit
from ..db import get_db
from ..security.auth import attempt_login, revoke_session, revoke_user_sessions, set_password
from ..security.passwords import password_problems, verify_password
from ..security.rbac import login_required

bp = Blueprint("auth", __name__)

PRE_COOKIE = "vanessa_pre"


def _pre_csrf(pre: str) -> str:
    return hmac.new(current_app.config["SECRET_KEY"].encode(), pre.encode(), hashlib.sha256).hexdigest()


def _safe_next(target: str | None) -> str:
    if target and target.startswith("/panel") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("panel.dashboard")


def _set_cookie(resp, name, value, max_age=None):
    resp.set_cookie(name, value, max_age=max_age, httponly=True, samesite="Lax",
                    secure=current_app.config["SESSION_COOKIE_SECURE"], path="/")


@bp.route("/giris", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(_safe_next(request.args.get("next")))

    error = None
    username = ""
    if request.method == "POST":
        pre = request.cookies.get(PRE_COOKIE, "")
        sent = request.form.get("_csrf", "")
        if not pre or not hmac.compare_digest(sent, _pre_csrf(pre)):
            error = "Oturum doğrulaması zaman aşımına uğradı. Lütfen tekrar deneyin."
        else:
            username = request.form.get("username", "")[:60]
            password = request.form.get("password", "")[:200]
            if not username or not password:
                error = "Kullanıcı adı ve şifre gerekli."
            else:
                result = attempt_login(get_db(), username, password)
                if result.ok:
                    resp = redirect(_safe_next(request.form.get("next")))
                    _set_cookie(resp, current_app.config["AUTH_COOKIE_NAME"], result.token,
                                max_age=current_app.config["SESSION_ABSOLUTE_DAYS"] * 86400)
                    resp.delete_cookie(PRE_COOKIE, path="/")
                    return resp
                error = result.error

    pre = secrets.token_urlsafe(24)
    resp = make_response(render_template(
        "auth/login.html", error=error, username=username, pre_csrf=_pre_csrf(pre),
        next=request.values.get("next", ""),
    ), 401 if error else 200)
    _set_cookie(resp, PRE_COOKIE, pre, max_age=3600)
    return resp


@bp.post("/cikis")
@login_required
def logout():
    db = get_db()
    audit("auth.logout", "user", g.user["id"], f"{g.user['full_name']} çıkış yaptı")
    revoke_session(db, g.session["session_id"])
    resp = redirect(url_for("auth.login"))
    resp.delete_cookie(current_app.config["AUTH_COOKIE_NAME"], path="/")
    return resp


@bp.route("/panel/sifre", methods=["GET", "POST"])
@login_required
def change_password():
    errors: list[str] = []
    if request.method == "POST":
        db = get_db()
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        repeat = request.form.get("new_password_repeat", "")
        row = db.one("SELECT password_hash FROM users WHERE id = ?", (g.user["id"],))
        if not verify_password(current, row["password_hash"]):
            errors.append("Mevcut şifre hatalı.")
        if new != repeat:
            errors.append("Yeni şifreler birbiriyle aynı değil.")
        if new == current:
            errors.append("Yeni şifre eskisinden farklı olmalı.")
        errors += password_problems(new, g.user["username"])
        if not errors:
            with db.transaction():
                set_password(db, g.user["id"], new)
                revoke_user_sessions(db, g.user["id"], except_session_id=g.session["session_id"])
                audit("user.password_change", "user", g.user["id"], f"{g.user['full_name']} şifresini değiştirdi")
            flash("Şifreniz güncellendi. Diğer cihazlardaki oturumlar kapatıldı.", "success")
            return redirect(url_for("panel.dashboard"))
    return render_template("auth/change_password.html", errors=errors), (400 if errors else 200)
