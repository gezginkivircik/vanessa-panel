"""Yönetim: hizmetler, personel ve çalışma saatleri, izin/kapalı günler, randevu ayarları, görseller."""
from __future__ import annotations

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from ..db import get_db
from ..errors import NotFound, ValidationError
from ..security.rbac import require_permission
from ..services import catalog as svc

bp = Blueprint("catalog", __name__)


def _form() -> dict:
    f = request.form.to_dict()
    f["staff_ids"] = request.form.getlist("staff_ids")
    f["service_ids"] = request.form.getlist("service_ids")
    return f


# ------------------------------------------------------------------ görsel
@bp.get("/media/<int:media_id>")
def media(media_id: int):
    try:
        m = svc.get_media(get_db(), media_id)
    except NotFound:
        abort(404)
    resp = Response(m["data"], mimetype=m["content_type"])
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


# ------------------------------------------------------------------ hizmetler
@bp.get("/panel/hizmetler")
@require_permission("services.manage")
def services():
    return render_template("catalog/services.html", rows=svc.list_services(get_db()), tab="hizmetler")


def _service_form(service_id: int | None):
    db = get_db()
    errors: dict = {}
    if service_id:
        try:
            current = svc.get_service(db, service_id)
        except NotFound:
            abort(404)
    else:
        current = {"is_active": 1, "online_booking": 1, "duration_minutes": 60, "buffer_minutes": 0, "sort_order": 0,
                   "staff_ids": []}
    form = dict(current)
    if service_id:
        form["price"] = f"{current['price_kurus'] / 100:g}"
        form["campaign_price"] = f"{current['campaign_price_kurus'] / 100:g}" if current["campaign_price_kurus"] is not None else ""
    if request.method == "POST":
        form = _form()
        try:
            sid = svc.save_service(db, g.user, form, service_id, image=request.files.get("image"))
            flash("Hizmet kaydedildi.", "success")
            return redirect(url_for("catalog.services"))
        except ValidationError as e:
            errors = e.errors
            form["staff_ids"] = [int(x) for x in form.get("staff_ids", []) if str(x).isdigit()]
            form["image_media_id"] = current.get("image_media_id")
    staff = db.query("SELECT id, full_name, color FROM staff WHERE deleted_at IS NULL ORDER BY sort_order, full_name")
    categories = [r["category"] for r in db.query(
        "SELECT DISTINCT category FROM services WHERE category IS NOT NULL AND deleted_at IS NULL ORDER BY category")]
    return render_template("catalog/service_form.html", form=form, errors=errors, staff=staff, categories=categories,
                           service_id=service_id, tab="hizmetler"), (400 if errors else 200)


@bp.route("/panel/hizmetler/yeni", methods=["GET", "POST"])
@require_permission("services.manage")
def service_new():
    return _service_form(None)


@bp.route("/panel/hizmetler/<int:service_id>", methods=["GET", "POST"])
@require_permission("services.manage")
def service_edit(service_id: int):
    return _service_form(service_id)


@bp.post("/panel/hizmetler/<int:service_id>/sil")
@require_permission("services.manage")
def service_delete(service_id: int):
    try:
        svc.delete_service(get_db(), g.user, service_id)
        flash("Hizmet silindi.", "success")
    except NotFound:
        abort(404)
    except ValidationError as e:
        flash(" ".join(e.errors.values()), "error")
        return redirect(url_for("catalog.service_edit", service_id=service_id))
    return redirect(url_for("catalog.services"))


# ------------------------------------------------------------------ personel
@bp.get("/panel/personel")
@require_permission("staff.manage")
def staff():
    return render_template("catalog/staff.html", rows=svc.list_staff(get_db()), WEEKDAYS=svc.WEEKDAYS, tab="personel")


def _staff_form(staff_id: int | None):
    db = get_db()
    errors: dict = {}
    if staff_id:
        try:
            current = svc.get_staff(db, staff_id)
        except NotFound:
            abort(404)
    else:
        current = {"is_active": 1, "color": "#8E6C8A", "sort_order": 0, "service_ids": [], "breaks": [], "holidays": [],
                   "hours": {d: {"start_time": "09:00", "end_time": "18:00"} for d in range(6)}}
    form = dict(current)
    if request.method == "POST":
        f = _form()
        try:
            sid = svc.save_staff(db, g.user, f, staff_id)
            flash("Personel kaydedildi.", "success")
            return redirect(url_for("catalog.staff_edit", staff_id=sid))
        except ValidationError as e:
            errors = e.errors
            form.update(f)
            form["service_ids"] = [int(x) for x in f.get("service_ids", []) if str(x).isdigit()]
            form["hours"] = {d: {"start_time": f.get(f"day_{d}_start", ""), "end_time": f.get(f"day_{d}_end", "")}
                             for d in range(7) if f.get(f"day_{d}_open")}
    services = db.query("SELECT id, name, category FROM services WHERE deleted_at IS NULL ORDER BY sort_order, name")
    return render_template("catalog/staff_form.html", form=form, errors=errors, services=services,
                           staff_id=staff_id, WEEKDAYS=svc.WEEKDAYS, tab="personel"), (400 if errors else 200)


@bp.route("/panel/personel/yeni", methods=["GET", "POST"])
@require_permission("staff.manage")
def staff_new():
    return _staff_form(None)


@bp.route("/panel/personel/<int:staff_id>", methods=["GET", "POST"])
@require_permission("staff.manage")
def staff_edit(staff_id: int):
    return _staff_form(staff_id)


@bp.post("/panel/personel/<int:staff_id>/mola")
@require_permission("staff.manage")
def break_add(staff_id: int):
    try:
        svc.add_break(get_db(), g.user, staff_id, request.form.to_dict())
        flash("Mola eklendi.", "success")
    except NotFound:
        abort(404)
    except ValidationError as e:
        flash(" ".join(e.errors.values()), "error")
    return redirect(url_for("catalog.staff_edit", staff_id=staff_id) + "#molalar")


@bp.post("/panel/personel/<int:staff_id>/mola/<int:break_id>/sil")
@require_permission("staff.manage")
def break_delete(staff_id: int, break_id: int):
    try:
        svc.delete_break(get_db(), g.user, staff_id, break_id)
        flash("Mola silindi.", "success")
    except NotFound:
        abort(404)
    return redirect(url_for("catalog.staff_edit", staff_id=staff_id) + "#molalar")


# ------------------------------------------------------------------ izin / kapalı günler
@bp.route("/panel/izinler", methods=["GET", "POST"])
@require_permission("staff.manage")
def holidays():
    db = get_db()
    errors: dict = {}
    form = {"staff_id": request.args.get("personel", "")}
    if request.method == "POST":
        form = request.form.to_dict()
        try:
            _, conflicts = svc.add_holiday(db, g.user, form)
            msg = "İzin / kapalı gün eklendi."
            if conflicts:
                msg += (f" Dikkat: bu aralıkta {conflicts} aktif randevu var; bu randevuları erteleyin veya "
                        "başka uzmana aktarın.")
                flash(msg, "error")
            else:
                flash(msg, "success")
            return redirect(url_for("catalog.holidays"))
        except ValidationError as e:
            errors = e.errors
    staff = db.query("SELECT id, full_name FROM staff WHERE deleted_at IS NULL ORDER BY sort_order")
    return render_template("catalog/holidays.html", rows=svc.list_holidays(db), staff=staff, form=form,
                           errors=errors, tab="izinler"), (400 if errors else 200)


@bp.post("/panel/izinler/<int:holiday_id>/sil")
@require_permission("staff.manage")
def holiday_delete(holiday_id: int):
    try:
        svc.delete_holiday(get_db(), g.user, holiday_id)
        flash("Silindi.", "success")
    except NotFound:
        abort(404)
    return redirect(request.form.get("next") or url_for("catalog.holidays"))


# ------------------------------------------------------------------ ayarlar
@bp.route("/panel/ayarlar", methods=["GET", "POST"])
@require_permission("settings.manage")
def settings():
    db = get_db()
    errors: dict = {}
    values = svc.get_settings(db)
    form = {k: values.get(k, "") for k in svc.SETTING_FIELDS}
    form["reminders.hours_before"] = ", ".join(str(h) for h in (values.get("reminders.hours_before") or []))
    if request.method == "POST":
        form = request.form.to_dict()
        try:
            svc.save_settings(db, g.user, form)
            flash("Ayarlar kaydedildi.", "success")
            return redirect(url_for("catalog.settings"))
        except ValidationError as e:
            errors = e.errors
    return render_template("catalog/settings.html", form=form, errors=errors, FIELDS=svc.SETTING_FIELDS,
                           tab="ayarlar"), (400 if errors else 200)
