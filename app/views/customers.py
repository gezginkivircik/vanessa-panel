"""Müşteri CRM ekranları ve hızlı arama API'si."""
from __future__ import annotations

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

from ..db import get_db
from ..errors import NotFound, ValidationError
from ..security.rbac import has_permission, require_permission
from ..services import customers as svc
from ..textutil import format_phone, mask_phone

bp = Blueprint("customers", __name__)


def _phone_display(phone: str) -> str:
    return format_phone(phone) if g.user["role"] != "specialist" else mask_phone(phone)


@bp.app_context_processor
def _ctx():
    return {"phone_display": _phone_display if g.get("user") else format_phone}


# ------------------------------------------------------------------ liste
@bp.get("/panel/musteriler")
@require_permission("customers.view")
def index():
    q = request.args.get("q", "").strip()
    flt = request.args.get("filtre", "")
    if flt not in svc.LIST_FILTERS:
        flt = ""
    page = max(1, request.args.get("sayfa", 1, type=int))
    rows, total = svc.list_customers(get_db(), g.user, q, flt, page)
    return render_template("customers/index.html", rows=rows, total=total, q=q, flt=flt, page=page,
                           pages=max(1, -(-total // 30)), FILTERS=svc.LIST_FILTERS)


@bp.get("/api/musteriler/ara")
@require_permission("customers.view")
def api_search():
    rows = svc.search(get_db(), g.user, request.args.get("q", ""))
    return jsonify(results=[{
        "id": r["id"], "name": f"{r['first_name']} {r['last_name']}", "phone": _phone_display(r["phone"]),
        "url": url_for("customers.profile", customer_id=r["id"]),
    } for r in rows])


# ------------------------------------------------------------------ yeni
@bp.route("/panel/musteriler/yeni", methods=["GET", "POST"])
@require_permission("customers.edit")
def new():
    errors, extra, form = {}, {}, {"consent_service": "1"}
    if request.method == "POST":
        form = request.form.to_dict()
        try:
            cid = svc.create(get_db(), form, g.user)
            flash("Müşteri kaydedildi.", "success")
            return redirect(url_for("customers.profile", customer_id=cid, yeni=1))
        except ValidationError as e:
            errors, extra = e.errors, e.extra
    elif request.args.get("q"):
        q = request.args["q"]
        form["phone" if any(ch.isdigit() for ch in q) else "full_name"] = q
    return render_template("customers/new.html", form=form, errors=errors, extra=extra,
                           SOURCES=svc.CONSENT_SOURCES), (400 if errors else 200)


# ------------------------------------------------------------------ profil
def _load(customer_id: int) -> dict:
    try:
        return svc.profile(get_db(), g.user, customer_id)
    except NotFound:
        abort(404)


@bp.get("/panel/musteriler/<int:customer_id>")
@require_permission("customers.view")
def profile(customer_id: int):
    p = _load(customer_id)
    tab = request.args.get("sekme", "ozet")
    return render_template("customers/profile.html", p=p, c=p["c"], tab=tab, just_created=request.args.get("yeni"),
                           CHANNELS=svc.CHANNELS, CATEGORIES=svc.CATEGORIES, SOURCES=svc.CONSENT_SOURCES,
                           METHOD_LABELS=svc.METHOD_LABELS, MSG_STATUS=svc.MSG_STATUS)


@bp.route("/panel/musteriler/<int:customer_id>/duzenle", methods=["GET", "POST"])
@require_permission("customers.edit")
def edit(customer_id: int):
    db = get_db()
    try:
        c = svc.get_visible(db, g.user, customer_id)
    except NotFound:
        abort(404)
    errors, form = {}, dict(c)
    if request.method == "POST":
        form = request.form.to_dict()
        try:
            svc.update(db, g.user, customer_id, form)
            flash("Müşteri bilgileri güncellendi.", "success")
            return redirect(url_for("customers.profile", customer_id=customer_id))
        except ValidationError as e:
            errors = e.errors
    return render_template("customers/edit.html", c=c, form=form, errors=errors), (400 if errors else 200)


@bp.post("/panel/musteriler/<int:customer_id>/sil")
@require_permission("customers.delete")
def delete(customer_id: int):
    try:
        svc.soft_delete(get_db(), g.user, customer_id)
    except NotFound:
        abort(404)
    except ValidationError as e:
        flash(" ".join(e.errors.values()), "error")
        return redirect(url_for("customers.profile", customer_id=customer_id))
    flash("Müşteri kaydı silindi.", "success")
    return redirect(url_for("customers.index"))


# ------------------------------------------------------------------ notlar
@bp.post("/panel/musteriler/<int:customer_id>/notlar")
@require_permission("notes.write")
def note_add(customer_id: int):
    db = get_db()
    try:
        svc.get_visible(db, g.user, customer_id)
        svc.add_note(db, customer_id, request.form.get("body", ""), g.user)
        flash("Not eklendi.", "success")
    except NotFound:
        abort(404)
    except ValidationError as e:
        flash(" ".join(e.errors.values()), "error")
    return redirect(url_for("customers.profile", customer_id=customer_id, sekme="notlar"))


@bp.post("/panel/musteriler/<int:customer_id>/notlar/<int:note_id>/<string:action>")
@require_permission("notes.write")
def note_action(customer_id: int, note_id: int, action: str):
    db = get_db()
    try:
        svc.get_visible(db, g.user, customer_id)
        if action == "sil":
            svc.delete_note(db, g.user, customer_id, note_id)
        elif action == "sabitle":
            svc.toggle_pin(db, customer_id, note_id)
        else:
            abort(404)
    except NotFound:
        abort(404)
    except ValidationError as e:
        flash(" ".join(e.errors.values()), "error")
    return redirect(url_for("customers.profile", customer_id=customer_id, sekme="notlar"))


# ------------------------------------------------------------------ izinler
@bp.post("/panel/musteriler/<int:customer_id>/izinler")
@require_permission("customers.edit")
def consent(customer_id: int):
    db = get_db()
    try:
        svc.get_visible(db, g.user, customer_id)
        with db.transaction():
            svc.record_consent(db, customer_id, request.form.get("channel", ""), request.form.get("category", ""),
                               request.form.get("granted") == "1", request.form.get("source", "panel"), g.user,
                               evidence=request.form.get("evidence"))
        flash("İletişim izni kaydedildi.", "success")
    except NotFound:
        abort(404)
    except ValidationError as e:
        flash(" ".join(e.errors.values()), "error")
    return redirect(url_for("customers.profile", customer_id=customer_id, sekme="izinler"))


@bp.app_template_global()
def wa_link(phone: str) -> str:
    """Manuel WhatsApp sohbeti (otomasyondan bağımsız): wa.me bağlantısı."""
    return f"https://wa.me/{(phone or '').lstrip('+')}"


@bp.app_template_global()
def can_wa(user) -> bool:
    return has_permission(user, "messages.send_manual")
