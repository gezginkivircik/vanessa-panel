"""Takip listesi: tekrar zamanı gelen işlemler ve paketi olup randevusu olmayan müşteriler.

Kaynak: service_followups. Bir işlem tamamlanınca (appointments._on_completed) sonraki tarih hesaplanır.
Müşterinin o hizmet için ileri tarihli aktif randevusu varsa listede gösterilmez (zaten planlanmış)."""
from __future__ import annotations

import datetime as dt

from ..audit import audit
from ..db import Connection
from ..errors import NotFound, ValidationError
from ..timeutil import local_today, now_iso, to_local
from .dashboard import package_progress

RESULTS = {"no_answer": "Ulaşılamadı", "later": "Ertelendi", "declined": "İstemiyor", "reopened": "Listeye geri alındı"}
SNOOZE_DAYS = (3, 7, 14, 30)
HORIZON_DAYS = 30
TABS = {"gelen": "Zamanı gelen", "hafta": "Bu hafta", "yaklasan": "30 gün içinde", "ertelenen": "Ertelenen"}


def _base_sql() -> str:
    return """SELECT f.*, c.first_name, c.last_name, c.phone, s.name AS service_name, s.repeat_interval_days,
                     s.tracks_sessions
                FROM service_followups f
                JOIN customers c ON c.id = f.customer_id
                JOIN services s ON s.id = f.service_id
               WHERE f.status IN ('open','notified') AND c.deleted_at IS NULL AND s.deleted_at IS NULL
                 AND NOT EXISTS (SELECT 1 FROM appointments a WHERE a.customer_id = f.customer_id
                                 AND a.service_id = f.service_id AND a.is_active_slot = 1 AND a.starts_at >= ?)"""


def _enrich(db: Connection, rows: list[dict], today: dt.date) -> list[dict]:
    for r in rows:
        due = dt.date.fromisoformat(str(r["due_on"])[:10])
        r["due"] = due
        r["days"] = (today - due).days            # + gecikme, - kalan gün
        r["snoozed"] = bool(r["snoozed_until"]) and dt.date.fromisoformat(str(r["snoozed_until"])[:10]) > today
        cp = db.scalar("SELECT id FROM customer_packages WHERE customer_id = ? AND service_id = ? AND status = 'active' "
                       "ORDER BY purchased_at LIMIT 1", (r["customer_id"], r["service_id"]))
        r["package"] = package_progress(db, cp) if cp else None
        r["last_contact_local"] = to_local(r["last_contact_at"]) if r["last_contact_at"] else None
    return rows


def items(db: Connection, tab: str = "gelen") -> list[dict]:
    today = local_today()
    horizon = today + dt.timedelta(days=HORIZON_DAYS)
    rows = db.query(_base_sql() + " AND f.due_on <= ? ORDER BY f.due_on, c.last_name, c.first_name",
                    (now_iso(), horizon.isoformat()))
    rows = _enrich(db, rows, today)
    if tab == "ertelenen":
        return [r for r in rows if r["snoozed"]]
    active = [r for r in rows if not r["snoozed"]]
    if tab == "hafta":
        return [r for r in active if 0 < -r["days"] <= 7]
    if tab == "yaklasan":
        return [r for r in active if 7 < -r["days"]]
    return [r for r in active if r["days"] >= 0]


def counts(db: Connection) -> dict[str, int]:
    return {k: len(items(db, k)) for k in TABS}


def due_count(db: Connection) -> int:
    return len(items(db, "gelen"))


def get(db: Connection, followup_id: int) -> dict:
    f = db.one("SELECT f.*, c.first_name, c.last_name, s.name AS service_name FROM service_followups f "
               "JOIN customers c ON c.id = f.customer_id JOIN services s ON s.id = f.service_id WHERE f.id = ?",
               (followup_id,))
    if not f:
        raise NotFound()
    return f


def history(db: Connection, followup_id: int) -> list[dict]:
    return db.query("""SELECT fc.*, u.full_name FROM followup_contacts fc LEFT JOIN users u ON u.id = fc.user_id
                       WHERE fc.followup_id = ? ORDER BY fc.created_at DESC, fc.id DESC""", (followup_id,))


def record(db: Connection, user: dict, followup_id: int, form: dict) -> str:
    f = get(db, followup_id)
    result = form.get("result") or ""
    if result not in ("no_answer", "later", "declined", "reopened"):
        raise ValidationError({"_": "Sonuç seçin."})
    note = (form.get("note") or "").strip()[:300] or None
    today = local_today()
    now = now_iso()
    data: dict = {"updated_at": now}
    if result == "reopened":
        data.update({"status": "open", "snoozed_until": None})
        summary = "listeye geri alındı"
    else:
        data.update({"contact_count": (f["contact_count"] or 0) + 1, "last_contact_at": now,
                     "last_contact_result": result})
        if result == "no_answer":
            data["snoozed_until"] = (today + dt.timedelta(days=1)).isoformat()
            summary = "ulaşılamadı (yarın tekrar listede)"
        elif result == "later":
            days = int(form.get("days") or 7)
            if days not in SNOOZE_DAYS:
                raise ValidationError({"_": "Erteleme süresi geçersiz."})
            data["snoozed_until"] = (today + dt.timedelta(days=days)).isoformat()
            summary = f"{days} gün ertelendi"
        else:
            data.update({"status": "dismissed", "snoozed_until": None})
            summary = "istemiyor — listeden çıkarıldı"
    with db.transaction():
        db.update("service_followups", followup_id, data)
        db.insert("followup_contacts", {"followup_id": followup_id, "result": result, "note": note,
                                        "user_id": user["id"], "created_at": now}, returning="")
        audit(f"followup.{result}", "customer", f["customer_id"],
              f"Takip · {f['first_name']} {f['last_name']} · {f['service_name']}: {summary}"
              + (f" — {note}" if note else ""), db=db)
    return summary


def upsert(db: Connection, customer_id: int, service_id: int, last_done: dt.date, due: dt.date,
           reason: str = "repeat", last_appointment_id: int | None = None) -> None:
    """Sonraki işlem tarihini kaydeder; erteleme ve arama sayacı sıfırlanır."""
    now = now_iso()
    data = {"last_appointment_id": last_appointment_id, "last_done_on": last_done.isoformat(), "due_on": due.isoformat(),
            "status": "open", "reason": reason, "snoozed_until": None, "contact_count": 0,
            "last_contact_at": None, "last_contact_result": None, "updated_at": now}
    ex = db.one("SELECT id FROM service_followups WHERE customer_id = ? AND service_id = ?", (customer_id, service_id))
    if ex:
        db.update("service_followups", ex["id"], data)
    else:
        db.insert("service_followups", {**data, "customer_id": customer_id, "service_id": service_id, "created_at": now})


def rebuild_missing(db: Connection) -> int:
    """Eksik takip kayıtlarını mevcut verilerden üretir (ilk kurulum / eski veriler için). Var olanlara dokunmaz."""
    n = 0
    rows = db.query("""SELECT a.customer_id, a.service_id, a.id, a.starts_at, s.repeat_interval_days
                         FROM appointments a JOIN services s ON s.id = a.service_id
                        WHERE a.status = 'completed' AND s.repeat_interval_days IS NOT NULL
                        ORDER BY a.starts_at""")
    last: dict[tuple, dict] = {}
    for r in rows:
        last[(r["customer_id"], r["service_id"])] = r
    for (cid, sid), r in last.items():
        if db.scalar("SELECT 1 FROM service_followups WHERE customer_id = ? AND service_id = ?", (cid, sid)):
            continue
        done = to_local(r["starts_at"]).date()
        upsert(db, cid, sid, done, done + dt.timedelta(days=int(r["repeat_interval_days"])), "repeat", r["id"])
        n += 1
    for p in db.query("""SELECT cp.customer_id, cp.service_id, cp.purchased_at FROM customer_packages cp
                         WHERE cp.status = 'active'"""):
        if db.scalar("SELECT 1 FROM service_followups WHERE customer_id = ? AND service_id = ?",
                     (p["customer_id"], p["service_id"])):
            continue
        d = to_local(p["purchased_at"]).date()
        upsert(db, p["customer_id"], p["service_id"], d, d, "package")
        n += 1
    return n


def whatsapp_text(r: dict) -> str:
    if r.get("package"):
        pk = r["package"]
        return (f"Merhaba {r['first_name']}, {r['service_name']} paketinizin {pk['next']}. seansının zamanı geldi. "
                f"Size uygun bir gün ve saat için randevu oluşturalım mı? — Vanessa Güzellik Merkezi")
    return (f"Merhaba {r['first_name']}, son {r['service_name']} uygulamanızın üzerinden zaman geçti. "
            f"Bir sonraki bakımınız için uygun olduğunuz günü yazarsanız randevunuzu oluşturalım. — Vanessa Güzellik Merkezi")
