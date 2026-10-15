"""Takvim, randevu oluşturma, randevu detayı ve uygun saat API'si."""
from __future__ import annotations

import datetime as dt

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

from ..db import get_db
from ..errors import NotFound, ValidationError
from ..security.rbac import has_permission, require_permission
from ..services import appointments as svc
from ..services.customers import get_visible
from ..timeutil import TR_MONTHS, TR_WEEKDAYS, TZ, iso, local_today, to_local

bp = Blueprint("appointments", __name__)


def _day_arg(name: str = "tarih") -> dt.date:
    try:
        return dt.date.fromisoformat(request.args.get(name, ""))
    except ValueError:
        return local_today()


def _bounds(start: dt.date, days: int) -> tuple[str, str]:
    s = dt.datetime.combine(start, dt.time.min, tzinfo=TZ)
    return iso(s), iso(s + dt.timedelta(days=days))


# ------------------------------------------------------------------ takvim
@bp.get("/panel/randevular")
@require_permission("appointments.view")
def calendar():
    db = get_db()
    view = request.args.get("gorunum", "gun")
    if view not in ("gun", "hafta", "ay"):
        view = "gun"
    day = _day_arg()
    staff = db.query("SELECT id, full_name, color FROM staff WHERE is_active = 1 AND deleted_at IS NULL "
                     "ORDER BY sort_order, full_name")
    if g.user["role"] == "specialist":
        staff = [s for s in staff if s["id"] == g.user["staff_id"]]
    ctx = {"view": view, "day": day, "staff": staff, "today": local_today(),
           "TR_MONTHS": TR_MONTHS, "TR_WEEKDAYS": TR_WEEKDAYS}

    if view == "gun":
        a0, a1 = _bounds(day, 1)
        appts = svc.list_range(db, g.user, a0, a1)
        hours = db.one("SELECT MIN(start_time) AS s, MAX(end_time) AS e FROM working_hours WHERE weekday = ?",
                       (day.weekday(),))
        s_val = (hours or {}).get("s") or "09:00"
        e_val = (hours or {}).get("e") or "19:00"
        start_h = int(s_val[:2])
        end_h = int(e_val[:2]) + (0 if e_val[3:5] == "00" else 1)
        start_h = min([start_h] + [to_local(a["starts_at"]).hour for a in appts])
        end_h = max([end_h] + [to_local(a["ends_at"]).hour + 1 for a in appts])
        for a in appts:
            s, e = to_local(a["starts_at"]), to_local(a["ends_at"])
            a["top"] = (s.hour - start_h) * 60 + s.minute
            a["height"] = max(int((e - s).total_seconds() // 60), 20)
        ctx.update(appts=appts, start_h=start_h, end_h=end_h,
                   prev=day - dt.timedelta(days=1), next=day + dt.timedelta(days=1))
    elif view == "hafta":
        monday = day - dt.timedelta(days=day.weekday())
        a0, a1 = _bounds(monday, 7)
        appts = svc.list_range(db, g.user, a0, a1)
        days = [monday + dt.timedelta(days=i) for i in range(7)]
        by_day = {d: [] for d in days}
        for a in appts:
            by_day.setdefault(to_local(a["starts_at"]).date(), []).append(a)
        ctx.update(days=days, by_day=by_day, prev=monday - dt.timedelta(days=7), next=monday + dt.timedelta(days=7))
    else:
        first = day.replace(day=1)
        nxt = (first + dt.timedelta(days=32)).replace(day=1)
        grid_start = first - dt.timedelta(days=first.weekday())
        weeks = -(-((nxt - grid_start).days) // 7)
        a0, a1 = _bounds(grid_start, weeks * 7)
        counts: dict[dt.date, dict] = {}
        for a in svc.list_range(db, g.user, a0, a1):
            d = to_local(a["starts_at"]).date()
            c = counts.setdefault(d, {"total": 0, "cancelled": 0})
            c["total"] += 1
            if a["status"] in ("cancelled", "no_show"):
                c["cancelled"] += 1
        cells = [grid_start + dt.timedelta(days=i) for i in range(weeks * 7)]
        ctx.update(cells=cells, counts=counts, month_first=first,
                   prev=(first - dt.timedelta(days=1)).replace(day=1), next=nxt)
    return render_template("appointments/calendar.html", **ctx)


# ------------------------------------------------------------------ uygun saatler API
@bp.get("/api/randevu/uygun-saatler")
@require_permission("appointments.manage")
def api_slots():
    db = get_db()
    try:
        service_id = int(request.args.get("hizmet", 0))
        day = dt.date.fromisoformat(request.args.get("tarih", ""))
    except ValueError:
        return jsonify(error="Geçersiz istek"), 400
    exclude = request.args.get("haric", type=int)
    staff_param = request.args.get("uzman", type=int)
    staff = svc.staff_for_service(db, service_id)
    if staff_param:
        staff = [s for s in staff if s["id"] == staff_param]
    out = []
    try:
        for s in staff:
            out.append({"staff_id": s["id"], "staff_name": s["full_name"], "color": s["color"],
                        "slots": svc.available_slots(db, s["id"], service_id, day, exclude_appointment_id=exclude)})
    except ValidationError:
        return jsonify(error="Hizmet bulunamadı"), 404
    return jsonify(date=day.isoformat(), staff=out)


# ------------------------------------------------------------------ yeni randevu
def _form_ctx(db, form, errors, customer=None):
    services = db.query("SELECT id, name, duration_minutes, price_kurus, campaign_price_kurus, category "
                        "FROM services WHERE is_active = 1 AND deleted_at IS NULL ORDER BY sort_order, name")
    staff = db.query("SELECT id, full_name FROM staff WHERE is_active = 1 AND deleted_at IS NULL ORDER BY sort_order")
    return {"form": form, "errors": errors, "services": services, "staff": staff, "customer": customer,
            "service_staff": svc.service_staff_map(db), "today": local_today()}


@bp.route("/panel/randevular/yeni", methods=["GET", "POST"])
@require_permission("appointments.manage")
def new():
    db = get_db()
    errors: dict = {}
    form = request.form.to_dict() if request.method == "POST" else {
        "customer_id": request.args.get("musteri", ""), "date": request.args.get("tarih", local_today().isoformat()),
        "service_id": request.args.get("hizmet", ""), "status": "confirmed"}
    customer = None
    if str(form.get("customer_id") or "").isdigit():
        try:
            customer = get_visible(db, g.user, int(form["customer_id"]))
        except NotFound:
            customer = None
    if request.method == "POST":
        try:
            aid = svc.create(db, g.user, form)
            flash("Randevu oluşturuldu.", "success")
            return redirect(url_for("appointments.detail", appointment_id=aid))
        except ValidationError as e:
            errors = e.errors
    return render_template("appointments/new.html", **_form_ctx(db, form, errors, customer)), (400 if errors else 200)


# ------------------------------------------------------------------ detay
@bp.get("/panel/randevular/<int:appointment_id>")
@require_permission("appointments.view")
def detail(appointment_id: int):
    db = get_db()
    try:
        a = svc.get(db, g.user, appointment_id)
    except NotFound:
        abort(404)
    pkg = None
    if a["customer_package_id"]:
        from ..services.dashboard import package_progress
        pkg = package_progress(db, a["customer_package_id"])
    payment = None
    if has_permission(g.user, "payments.view"):
        payment = db.one("SELECT id, status, total_kurus, discount_kurus, paid_kurus FROM payments "
                         "WHERE appointment_id = ? AND status <> 'void' ORDER BY id DESC LIMIT 1", (appointment_id,))
        if payment:
            payment["remaining"] = payment["total_kurus"] - payment["discount_kurus"] - payment["paid_kurus"]
    return render_template("appointments/detail.html", a=a, history=svc.history(db, appointment_id), pkg=pkg,
                           payment=payment,
                           actions=svc.TRANSITIONS.get(a["status"], ()), ACTION_LABELS=svc.ACTION_LABELS,
                           STATUS_LABELS=svc.STATUS_LABELS,
                           can_manage=has_permission(g.user, "appointments.manage"),
                           staff=svc.staff_for_service(db, a["service_id"]),
                           local_start=to_local(a["starts_at"]))


@bp.post("/panel/randevular/<int:appointment_id>/durum")
@require_permission("appointments.manage")
def set_status(appointment_id: int):
    try:
        svc.change_status(get_db(), g.user, appointment_id, request.form.get("status", ""),
                          reason=request.form.get("reason"))
        flash(f"Randevu durumu: {svc.STATUS_LABELS.get(request.form.get('status'), '')}", "success")
    except NotFound:
        abort(404)
    except ValidationError as e:
        flash(" ".join(e.errors.values()), "error")
    return redirect(request.form.get("next") or url_for("appointments.detail", appointment_id=appointment_id))


@bp.post("/panel/randevular/<int:appointment_id>/saat")
@require_permission("appointments.manage")
def reschedule(appointment_id: int):
    try:
        svc.reschedule(get_db(), g.user, appointment_id, request.form.to_dict())
        flash("Randevu saati güncellendi.", "success")
    except NotFound:
        abort(404)
    except ValidationError as e:
        flash(" ".join(e.errors.values()), "error")
    return redirect(url_for("appointments.detail", appointment_id=appointment_id))


@bp.post("/panel/randevular/<int:appointment_id>/not")
@require_permission("appointments.manage")
def note(appointment_id: int):
    svc.update_note(get_db(), g.user, appointment_id, request.form.get("internal_note", ""))
    flash("Not kaydedildi.", "success")
    return redirect(url_for("appointments.detail", appointment_id=appointment_id))


# ------------------------------------------------------------------ hızlı ekle seçici
@bp.get("/panel/yeni")
@require_permission("dashboard.view")
def quick_new():
    return render_template("appointments/quick_new.html")
