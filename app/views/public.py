"""Müşteri web sitesi: tanıtım, hizmetler, online randevu talebi, randevu görüntüleme/iptal, KVKK."""
from __future__ import annotations

import datetime as dt

from flask import Blueprint, Response, abort, redirect, render_template, request, url_for

from ..db import get_db
from ..errors import NotFound, ValidationError
from ..security import precsrf
from ..security.auth import client_ip
from ..services import booking as svc
from ..timeutil import local_today

bp = Blueprint("public", __name__)


@bp.app_context_processor
def _ctx():
    return {"pre_csrf": precsrf.token}


@bp.after_app_request
def _pre_cookie(resp):
    return precsrf.set_cookie_if_new(resp)


def _check_csrf():
    if not precsrf.valid():
        raise ValidationError({"_": "Sayfanın süresi doldu. Lütfen sayfayı yenileyip tekrar deneyin."})


# ------------------------------------------------------------------ tanıtım
@bp.get("/")
def home():
    db = get_db()
    services = svc.services(db)
    return render_template("public/home.html", services=services, categories=svc.categories(services),
                           campaigns=[s for s in services if s["campaign_price_kurus"] is not None],
                           hours=svc.business_hours(db), booking_on=svc.enabled(db))


@bp.get("/hizmetler")
def services():
    db = get_db()
    rows = svc.services(db)
    return render_template("public/services.html", categories=svc.categories(rows), booking_on=svc.enabled(db))


@bp.get("/hizmetler/<slug>")
def service(slug: str):
    db = get_db()
    try:
        s = svc.service_by_slug(db, slug)
    except NotFound:
        abort(404)
    others = [o for o in svc.services(db) if o["category"] == s["category"] and o["id"] != s["id"]][:3]
    return render_template("public/service.html", s=s, others=others, booking_on=svc.enabled(db))


@bp.get("/kvkk")
def kvkk():
    return render_template("public/kvkk.html", version=svc.KVKK_VERSION)


# ------------------------------------------------------------------ online randevu
def _wizard(form: dict | None = None, errors: dict | None = None, status: int = 200):
    db = get_db()
    args = request.values
    all_services = svc.services(db)
    bookable = [s for s in all_services if s["online_booking"]]
    service = next((s for s in bookable if s["slug"] == args.get("hizmet")), None)
    staff = svc.staff_options(db, service["id"]) if service else []
    staff_raw = args.get("uzman", "")
    staff_id = int(staff_raw) if staff_raw.isdigit() else None
    if staff_id and staff_id not in [s["id"] for s in staff]:
        staff_id = None
    staff_chosen = service is not None and (staff_raw == "0" or bool(staff_id) or len(staff) == 1)
    if service and len(staff) == 1:
        staff_id = staff[0]["id"]
    day = svc.valid_day(db, args.get("tarih"))
    try:
        start = dt.date.fromisoformat(args.get("hafta", "")) if args.get("hafta") else local_today()
    except ValueError:
        start = local_today()
    strip = svc.day_strip(db, start) if staff_chosen else []
    times = svc.slots(db, service["id"], day, staff_id) if (staff_chosen and day) else []
    time_s = args.get("saat") if args.get("saat") in times else None
    prev_week = start - dt.timedelta(days=14) if start > local_today() else None
    last = local_today() + dt.timedelta(days=svc.max_days(db))
    next_week = start + dt.timedelta(days=14) if start + dt.timedelta(days=14) <= last else None
    return render_template(
        "public/booking.html", services=bookable, categories=svc.categories(bookable), service=service,
        staff=staff, staff_id=staff_id, staff_raw=("0" if staff_chosen and not staff_id else str(staff_id or "")),
        staff_chosen=staff_chosen, strip=strip, day=day, times=times, time_s=time_s,
        prev_week=prev_week, next_week=next_week, week_start=start,
        form=form or {}, errors=errors or {}, booking_on=svc.enabled(db),
    ), status


@bp.get("/randevu")
def booking():
    return _wizard()


@bp.post("/randevu")
def booking_submit():
    db = get_db()
    form = request.form.to_dict()
    try:
        _check_csrf()
        aid, token = svc.book(db, form, client_ip())
    except ValidationError as e:
        return _wizard(form, e.errors, 400)
    return redirect(url_for("public.my_appointment", token=token, yeni=1))


@bp.get("/randevu/durum/<token>")
def my_appointment(token: str):
    try:
        a = svc.by_token(get_db(), token)
    except NotFound:
        return render_template("public/appointment_missing.html"), 404
    resp = render_template("public/appointment.html", a=a, token=token, just_created=request.args.get("yeni"))
    return resp, 200, {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}


@bp.post("/randevu/durum/<token>/iptal")
def cancel_appointment(token: str):
    db = get_db()
    error = None
    try:
        _check_csrf()
        svc.cancel_by_token(db, token, client_ip())
    except NotFound:
        return render_template("public/appointment_missing.html"), 404
    except ValidationError as e:
        error = " ".join(e.errors.values())
    try:
        a = svc.by_token(db, token)
    except NotFound:
        return render_template("public/appointment_missing.html"), 404
    return render_template("public/appointment.html", a=a, token=token, error=error,
                           cancelled=error is None), (400 if error else 200), \
        {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}


# ------------------------------------------------------------------ SEO
@bp.get("/robots.txt")
def robots():
    body = f"User-agent: *\nAllow: /\nDisallow: /panel\nDisallow: /giris\nDisallow: /randevu/durum/\n" \
           f"Sitemap: {request.url_root}sitemap.xml\n"
    return Response(body, mimetype="text/plain")


@bp.get("/sitemap.xml")
def sitemap():
    db = get_db()
    root = request.url_root.rstrip("/")
    urls = [root + "/", root + url_for("public.services"), root + url_for("public.booking"), root + url_for("public.kvkk")]
    urls += [root + url_for("public.service", slug=s["slug"]) for s in svc.services(db)]
    body = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + \
        "".join(f"  <url><loc>{u}</loc></url>\n" for u in urls) + "</urlset>\n"
    return Response(body, mimetype="application/xml")
