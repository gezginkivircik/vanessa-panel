"""Yönetim paneli — Faz 1: dashboard, kullanıcılar, audit log."""
from __future__ import annotations

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from ..db import get_db, json_value
from ..security.rbac import has_permission, require_permission
from ..services import users as user_service
from ..services.dashboard import STATUS_LABELS, get_dashboard
from ..timeutil import fmt_long_date, local_today

bp = Blueprint("panel", __name__, url_prefix="/panel")


@bp.app_context_processor
def _panel_ctx():
    return {"STATUS_LABELS": STATUS_LABELS}


def _greeting() -> str:
    from ..timeutil import now_utc, TZ
    h = now_utc().astimezone(TZ).hour
    return "Günaydın" if 5 <= h < 12 else "İyi günler" if h < 18 else "İyi akşamlar"


@bp.get("/")
@require_permission("dashboard.view")
def dashboard():
    data = get_dashboard(get_db(), g.user, finance=has_permission(g.user, "dashboard.finance"))
    first_name = g.user["full_name"].split(" ")[0]
    return render_template("panel/dashboard.html", d=data, greeting=_greeting(), first_name=first_name,
                           today_label=fmt_long_date(local_today()))


# ------------------------------------------------------------------ kullanıcılar
@bp.get("/kullanicilar")
@require_permission("users.manage")
def users():
    rows = get_db().query(
        """SELECT u.id, u.username, u.full_name, u.role, u.is_active, u.last_login_at, u.locked_until,
                  u.must_change_password, s.full_name AS staff_name
             FROM users u LEFT JOIN staff s ON s.id = u.staff_id
            WHERE u.deleted_at IS NULL ORDER BY u.is_active DESC, u.role, u.full_name""")
    return render_template("panel/users.html", users=rows)


def _staff_options():
    return get_db().query("SELECT id, full_name FROM staff WHERE deleted_at IS NULL ORDER BY sort_order, full_name")


@bp.route("/kullanicilar/yeni", methods=["GET", "POST"])
@require_permission("users.manage")
def user_new():
    errors, form = {}, {}
    if request.method == "POST":
        form = request.form.to_dict()
        try:
            uid, temp = user_service.create_user(get_db(), form)
            return render_template("panel/user_password.html", user_id=uid, full_name=form.get("full_name"),
                                   username=form.get("username", "").strip().lower(), temp_password=temp, created=True)
        except user_service.ValidationError as e:
            errors = e.errors
    return render_template("panel/user_form.html", form=form, errors=errors, staff=_staff_options(),
                           is_new=True), (400 if errors else 200)


@bp.route("/kullanicilar/<int:user_id>", methods=["GET", "POST"])
@require_permission("users.manage")
def user_edit(user_id: int):
    db = get_db()
    user = db.one("SELECT * FROM users WHERE id = ? AND deleted_at IS NULL", (user_id,))
    if not user:
        abort(404)
    errors, form = {}, dict(user)
    if request.method == "POST":
        form = request.form.to_dict()
        try:
            user_service.update_user(db, user_id, form, g.user["id"])
            flash("Kullanıcı güncellendi.", "success")
            return redirect(url_for("panel.users"))
        except user_service.ValidationError as e:
            errors = e.errors
    sessions = db.query("SELECT created_at, last_seen_at, ip_address, user_agent FROM user_sessions "
                        "WHERE user_id = ? AND revoked_at IS NULL ORDER BY last_seen_at DESC LIMIT 10", (user_id,))
    return render_template("panel/user_form.html", form=form, errors=errors, staff=_staff_options(),
                           is_new=False, user=user, sessions=sessions), (400 if errors else 200)


@bp.post("/kullanicilar/<int:user_id>/durum")
@require_permission("users.manage")
def user_toggle(user_id: int):
    try:
        user_service.set_active(get_db(), user_id, request.form.get("active") == "1", g.user["id"])
        flash("Kullanıcı durumu güncellendi.", "success")
    except user_service.ValidationError as e:
        flash(" ".join(e.errors.values()), "error")
    return redirect(url_for("panel.users"))


@bp.post("/kullanicilar/<int:user_id>/sifre-sifirla")
@require_permission("users.manage")
def user_reset_password(user_id: int):
    db = get_db()
    try:
        temp = user_service.reset_password(db, user_id)
    except user_service.ValidationError:
        abort(404)
    u = db.one("SELECT full_name, username FROM users WHERE id = ?", (user_id,))
    return render_template("panel/user_password.html", user_id=user_id, full_name=u["full_name"],
                           username=u["username"], temp_password=temp, created=False)


# --------------------------------------------------------------------- audit
ACTION_LABELS = {
    "auth.login": "Giriş", "auth.logout": "Çıkış", "auth.locked": "Hesap kilitlendi",
    "user.create": "Kullanıcı oluşturma", "user.update": "Kullanıcı güncelleme",
    "user.activate": "Kullanıcı aktif", "user.deactivate": "Kullanıcı pasif",
    "user.password_reset": "Şifre sıfırlama", "user.password_change": "Şifre değişikliği",
}


@bp.get("/islem-kayitlari")
@require_permission("audit.view")
def audit_log():
    db = get_db()
    page = max(1, request.args.get("sayfa", 1, type=int))
    per = 50
    action = request.args.get("islem", "")
    where, params = "1 = 1", []
    if action:
        where += " AND action LIKE ?"
        params.append(action.split(".")[0] + ".%")
    total = db.scalar(f"SELECT COUNT(*) FROM audit_logs WHERE {where}", params)
    rows = db.query(f"SELECT * FROM audit_logs WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                    params + [per, (page - 1) * per])
    for r in rows:
        r["before"] = json_value(r["before_json"])
        r["after"] = json_value(r["after_json"])
    return render_template("panel/audit.html", rows=rows, page=page, pages=max(1, -(-total // per)),
                           action=action, ACTION_LABELS=ACTION_LABELS)
