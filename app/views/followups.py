"""Takip listesi ekranı."""
from __future__ import annotations

from urllib.parse import quote

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from ..db import get_db
from ..errors import NotFound, ValidationError
from ..security.rbac import require_permission
from ..services import followups as svc
from ..services.customers import has_consent
from ..textutil import format_phone

bp = Blueprint("followups", __name__)


@bp.get("/panel/takip")
@require_permission("customers.view_all")
def index():
    db = get_db()
    tab = request.args.get("sekme", "gelen")
    if tab not in svc.TABS:
        tab = "gelen"
    rows = svc.items(db, tab)
    for r in rows:
        r["phone_display"] = format_phone(r["phone"])
        r["wa_ok"] = has_consent(db, r["customer_id"], "whatsapp", "service")
        r["wa_url"] = f"https://wa.me/{r['phone'].lstrip('+')}?text={quote(svc.whatsapp_text(r))}"
    return render_template("followups/index.html", rows=rows, tab=tab, TABS=svc.TABS, counts=svc.counts(db),
                           RESULTS=svc.RESULTS, SNOOZE_DAYS=svc.SNOOZE_DAYS)


@bp.post("/panel/takip/<int:followup_id>/kayit")
@require_permission("customers.view_all")
def record(followup_id: int):
    try:
        f = svc.get(get_db(), followup_id)
        summary = svc.record(get_db(), g.user, followup_id, request.form.to_dict())
        flash(f"{f['first_name']} {f['last_name']}: {summary}.", "success")
    except NotFound:
        abort(404)
    except ValidationError as e:
        flash(" ".join(e.errors.values()), "error")
    tab = request.form.get("sekme", "gelen")
    return redirect(url_for("followups.index", sekme=tab if tab in svc.TABS else "gelen"))
