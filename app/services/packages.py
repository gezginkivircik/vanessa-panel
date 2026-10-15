"""Paket satışı (ör. 10 seans lazer, bölge bazlı) ve seans işleme."""
from __future__ import annotations

import datetime as dt

from ..audit import audit
from ..db import Connection
from ..errors import NotFound, ValidationError
from ..timeutil import TZ, iso, local_today, now_iso, now_utc
from . import payments as pay
from .catalog import _int, _money


def sell(db: Connection, user: dict, customer_id: int, form: dict) -> int:
    errors: dict[str, str] = {}
    service = db.one("SELECT * FROM services WHERE id = ? AND deleted_at IS NULL",
                     (int(form["service_id"]) if str(form.get("service_id") or "").isdigit() else -1,))
    if not service:
        errors["service_id"] = "Hizmet seçin."
    count = _int(form.get("session_count"), "session_count", errors, "Seans sayısı", 1, 100)
    total = _money(form.get("total"), "total", errors, "Paket tutarı")
    areas = [a.strip()[:80] for a in (form.get("areas") or "").replace(";", ",").split(",") if a.strip()]
    if not areas:
        areas = ["Genel"]
    if len(areas) > 12:
        errors["areas"] = "En fazla 12 bölge girilebilir."
    if len(set(a.lower() for a in areas)) != len(areas):
        errors["areas"] = "Aynı bölge iki kez yazılmış."
    first = _money(form.get("first_payment"), "first_payment", errors, "Peşinat", required=False)
    if first and total is not None and first > total:
        errors["first_payment"] = "Peşinat paket tutarından büyük olamaz."
    if errors:
        raise ValidationError(errors)
    name = (form.get("name") or "").strip()[:160] or f"{count} Seans {service['name']}"
    now = now_iso()
    with db.transaction():
        cp = db.insert("customer_packages", {
            "customer_id": customer_id, "service_id": service["id"], "name": name, "total_price_kurus": total,
            "status": "active", "purchased_at": now, "notes": (form.get("notes") or "").strip()[:1000] or None,
            "created_by": user["id"], "created_at": now, "updated_at": now})
        for i, a in enumerate(areas):
            db.insert("session_areas", {"customer_package_id": cp, "area_name": a, "total_sessions": count,
                                        "sort_order": i, "created_at": now, "updated_at": now})
        pid = pay.create_charge(db, user, customer_id, f"{name} paketi", total, customer_package_id=cp, audit_it=False)
        if first:
            pay.add_transaction(db, user, pid, {"amount": str(first / 100), "method": form.get("method") or "cash"})
        # Aynı hizmetin pakete bağlanmamış ileri tarihli randevuları bu pakete bağlanır
        db.execute("""UPDATE appointments SET customer_package_id = ? WHERE customer_id = ? AND service_id = ?
                      AND customer_package_id IS NULL AND is_active_slot = 1 AND starts_at >= ?""",
                   (cp, customer_id, service["id"], now))
        if not db.scalar("SELECT 1 FROM service_followups WHERE customer_id = ? AND service_id = ? AND status <> 'dismissed'",
                         (customer_id, service["id"])):
            from .followups import upsert
            upsert(db, customer_id, service["id"], local_today(), local_today(), "package")
        c = db.one("SELECT first_name, last_name FROM customers WHERE id = ?", (customer_id,))
        audit("package.sell", "customer", customer_id,
              f"{c['first_name']} {c['last_name']}: {name} satıldı ({', '.join(areas)}) · {pay.fmt(total)}",
              after={"package_id": cp, "areas": areas, "sessions": count, "total": total}, db=db)
    return cp


def record_session(db: Connection, user: dict, customer_id: int, area_id: int, form: dict) -> None:
    """Randevusuz yapılan bir seansı elle işler (ör. geçmiş kayıt aktarımı)."""
    area = db.one("""SELECT sa.*, cp.customer_id, cp.id AS cp_id, cp.status AS cp_status, cp.name AS cp_name
                     FROM session_areas sa JOIN customer_packages cp ON cp.id = sa.customer_package_id
                     WHERE sa.id = ?""", (area_id,))
    if not area or area["customer_id"] != customer_id:
        raise NotFound()
    if area["cp_status"] != "active":
        raise ValidationError({"_": "Paket aktif değil."})
    done = db.scalar("SELECT COUNT(*) FROM sessions WHERE session_area_id = ? AND status = 'completed'", (area_id,)) or 0
    if done >= area["total_sessions"]:
        raise ValidationError({"_": "Bu bölgenin bütün seansları tamamlanmış."})
    try:
        d = dt.date.fromisoformat(form.get("performed_on") or local_today().isoformat())
    except ValueError:
        raise ValidationError({"_": "Tarih geçersiz."})
    if d > local_today():
        raise ValidationError({"_": "İleri tarihli seans işlenemez; randevu oluşturun."})
    performed = iso(dt.datetime.combine(d, dt.time(12, 0), tzinfo=TZ)) if d != local_today() else now_iso()
    n = done + 1
    now = now_iso()
    with db.transaction():
        existing = db.one("SELECT id FROM sessions WHERE session_area_id = ? AND session_number = ?", (area_id, n))
        data = {"status": "completed", "performed_at": performed, "notes": (form.get("notes") or "").strip()[:1000] or None,
                "staff_id": user.get("staff_id"), "updated_at": now}
        if existing:
            db.update("sessions", existing["id"], data)
        else:
            db.insert("sessions", {**data, "customer_package_id": area["cp_id"], "session_area_id": area_id,
                                   "session_number": n, "created_at": now})
        remaining = db.scalar("""SELECT COUNT(*) FROM session_areas sa WHERE sa.customer_package_id = ?
                                 AND (SELECT COUNT(*) FROM sessions s WHERE s.session_area_id = sa.id
                                      AND s.status = 'completed') < sa.total_sessions""", (area["cp_id"],))
        if not remaining:
            db.update("customer_packages", area["cp_id"], {"status": "completed", "completed_at": now, "updated_at": now})
        audit("session.record", "customer", customer_id,
              f"{area['cp_name']} · {area['area_name']}: {n}. seans işlendi ({d.strftime('%d.%m.%Y')})", db=db)
