"""Ödemeler, tahsilat, paket satışı ve seans işleme ekranları."""
from __future__ import annotations

import datetime as dt

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from ..db import get_db
from ..errors import NotFound, ValidationError
from ..security.rbac import has_permission, require_permission
from ..services import packages as pkg
from ..services import payments as svc
from ..services.customers import get_visible
from ..timeutil import local_day_bounds_utc, local_month_bounds_utc, local_today

bp = Blueprint("payments", __name__)


@bp.app_context_processor
def _ctx():
    return {"PAY_METHODS": svc.METHODS, "PAY_STATUS": svc.STATUS}


# ------------------------------------------------------------------ liste
@bp.get("/panel/odemeler")
@require_permission("payments.view")
def index():
    db = get_db()
    period = request.args.get("donem", "bugun")
    today = local_today()
    if period == "ay":
        start, end = local_month_bounds_utc(today)
    elif period == "dun":
        start, end = local_day_bounds_utc(today - dt.timedelta(days=1))
    else:
        period = "bugun"
        start, end = local_day_bounds_utc(today)
    return render_template("payments/index.html", open=svc.open_balances(db), txs=svc.recent_transactions(db, start, end),
                           totals=svc.totals(db, start, end), period=period,
                           finance=has_permission(g.user, "dashboard.finance"))


# ------------------------------------------------------------------ detay + tahsilat
@bp.route("/panel/odemeler/<int:payment_id>", methods=["GET", "POST"])
@require_permission("payments.view")
def detail(payment_id: int):
    db = get_db()
    errors: dict = {}
    form = {"method": "cash", "paid_on": local_today().isoformat()}
    if request.method == "POST":
        if not has_permission(g.user, "payments.manage"):
            abort(403)
        form = request.form.to_dict()
        if form.get("refund") and not has_permission(g.user, "payments.void"):
            abort(403)
        try:
            svc.add_transaction(db, g.user, payment_id, form, allow_refund=has_permission(g.user, "payments.void"))
            flash("İade kaydedildi." if form.get("refund") else "Tahsilat kaydedildi.", "success")
            return redirect(request.form.get("next") or url_for("payments.detail", payment_id=payment_id))
        except NotFound:
            abort(404)
        except ValidationError as e:
            errors = e.errors
    try:
        p = svc.get(db, payment_id)
    except NotFound:
        abort(404)
    if not form.get("amount") and p["remaining"] > 0:
        form["amount"] = f"{p['remaining'] / 100:g}"
    return render_template("payments/detail.html", p=p, form=form, errors=errors), (400 if errors else 200)


@bp.post("/panel/odemeler/<int:payment_id>/iptal")
@require_permission("payments.void")
def void(payment_id: int):
    try:
        svc.void(get_db(), g.user, payment_id, request.form.get("reason", ""))
        flash("Ödeme kaydı iptal edildi.", "success")
    except NotFound:
        abort(404)
    except ValidationError as e:
        flash(" ".join(e.errors.values()), "error")
    return redirect(url_for("payments.detail", payment_id=payment_id))


# ------------------------------------------------------------------ yeni borç kaydı
def _customer(customer_id: int) -> dict:
    try:
        return get_visible(get_db(), g.user, customer_id)
    except NotFound:
        abort(404)


@bp.route("/panel/musteriler/<int:customer_id>/odeme-yeni", methods=["GET", "POST"])
@require_permission("payments.manage")
def new_charge(customer_id: int):
    c = _customer(customer_id)
    errors, form = {}, {"method": "cash"}
    if request.method == "POST":
        form = request.form.to_dict()
        try:
            pid = svc.create_manual(get_db(), g.user, customer_id, form)
            flash("Ödeme kaydı oluşturuldu.", "success")
            return redirect(url_for("payments.detail", payment_id=pid))
        except ValidationError as e:
            errors = e.errors
    return render_template("payments/new_charge.html", c=c, form=form, errors=errors), (400 if errors else 200)


# ------------------------------------------------------------------ paket satışı
@bp.route("/panel/musteriler/<int:customer_id>/paket-sat", methods=["GET", "POST"])
@require_permission("payments.manage")
def sell_package(customer_id: int):
    db = get_db()
    c = _customer(customer_id)
    errors, form = {}, {"session_count": "10", "method": "cash"}
    services = db.query("SELECT id, name, price_kurus, campaign_price_kurus, tracks_sessions FROM services "
                        "WHERE deleted_at IS NULL AND is_active = 1 ORDER BY tracks_sessions DESC, sort_order, name")
    if request.method == "POST":
        form = request.form.to_dict()
        try:
            pkg.sell(db, g.user, customer_id, form)
            flash("Paket satışı kaydedildi.", "success")
            return redirect(url_for("customers.profile", customer_id=customer_id, sekme="seanslar"))
        except ValidationError as e:
            errors = e.errors
    return render_template("payments/sell_package.html", c=c, form=form, errors=errors, services=services), \
        (400 if errors else 200)


@bp.post("/panel/musteriler/<int:customer_id>/seans/<int:area_id>")
@require_permission("sessions.manage")
def record_session(customer_id: int, area_id: int):
    _customer(customer_id)
    try:
        pkg.record_session(get_db(), g.user, customer_id, area_id, request.form.to_dict())
        flash("Seans işlendi.", "success")
    except NotFound:
        abort(404)
    except ValidationError as e:
        flash(" ".join(e.errors.values()), "error")
    return redirect(url_for("customers.profile", customer_id=customer_id, sekme="seanslar"))
