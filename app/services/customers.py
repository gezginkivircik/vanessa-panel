"""Müşteri CRM servisi: kayıt, arama, profil, zaman çizelgesi, notlar, KVKK izinleri."""
from __future__ import annotations

import datetime as dt
import re

from ..audit import audit, diff
from ..db import Connection, IntegrityError
from ..errors import NotFound, ValidationError
from ..security.rbac import has_permission
from ..textutil import fold, normalize_phone, phone_search_digits, tr_title
from ..timeutil import local_today, now_iso

CHANNELS = {"whatsapp": "WhatsApp", "sms": "SMS", "email": "E-posta", "phone": "Telefon"}
CATEGORIES = {"service": "Randevu ve hizmet bildirimleri", "marketing": "Kampanya ve pazarlama"}
CONSENT_SOURCES = {"panel": "Panelden (sözlü onay)", "paper_form": "Yazılı form", "online_form": "Online form",
                   "whatsapp_reply": "WhatsApp yanıtı"}
METHOD_LABELS = {"cash": "Nakit", "card": "Kredi Kartı", "transfer": "Havale/EFT", "other": "Diğer"}
ACTIVE_STATUSES = ("pending", "confirmed", "arrived", "reschedule_needed")


# ------------------------------------------------------------------ görünürlük
def scope_sql(user: dict, alias: str = "c") -> tuple[str, list]:
    """Uzman yalnızca kendisinde randevusu olan müşterileri görür."""
    if user["role"] == "specialist":
        return (f" AND EXISTS (SELECT 1 FROM appointments x WHERE x.customer_id = {alias}.id AND x.staff_id = ?)",
                [user["staff_id"] or -1])
    return "", []


def get_visible(db: Connection, user: dict, customer_id: int) -> dict:
    scope, sp = scope_sql(user)
    row = db.one(f"SELECT c.* FROM customers c WHERE c.id = ? AND c.deleted_at IS NULL{scope}", [customer_id] + sp)
    if not row:
        raise NotFound()
    return row


# ------------------------------------------------------------------ doğrulama
def _validate(db: Connection, form: dict, customer_id: int | None = None) -> dict:
    errors: dict[str, str] = {}
    extra: dict = {}
    if "full_name" in form and not form.get("first_name"):
        parts = (form.get("full_name") or "").strip().split()
        first, last = (" ".join(parts[:-1]), parts[-1]) if len(parts) > 1 else ((parts[0] if parts else ""), "")
    else:
        first, last = (form.get("first_name") or "").strip(), (form.get("last_name") or "").strip()
    first, last = tr_title(first)[:80], tr_title(last)[:80]
    if not first:
        errors["full_name" if "full_name" in form else "first_name"] = "Ad gerekli."
    if not last:
        errors["full_name" if "full_name" in form else "last_name"] = "Soyad gerekli (ad ve soyadı birlikte yazın)."

    raw_phone = form.get("phone") or ""
    phone = normalize_phone(raw_phone)
    if not phone:
        errors["phone"] = "Geçerli bir cep telefonu girin (örn. 0533 150 43 03)."
    else:
        dup = db.one("SELECT id, first_name, last_name FROM customers WHERE phone = ? AND deleted_at IS NULL", (phone,))
        if dup and dup["id"] != customer_id:
            errors["phone"] = f"Bu numara {dup['first_name']} {dup['last_name']} adına kayıtlı."
            extra["duplicate_id"] = dup["id"]

    birth = (form.get("birth_date") or "").strip() or None
    if birth:
        try:
            b = dt.date.fromisoformat(birth)
            if b > local_today() or b.year < 1900:
                raise ValueError
        except ValueError:
            errors["birth_date"] = "Doğum tarihi geçersiz."
    email = (form.get("email") or "").strip()[:160] or None
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        errors["email"] = "E-posta adresi geçersiz."
    if errors:
        raise ValidationError(errors, extra)
    return {"first_name": first, "last_name": last, "search_name": fold(f"{first} {last}"),
            "phone": phone, "birth_date": birth, "email": email}


# ------------------------------------------------------------------ yazma
def create(db: Connection, form: dict, user: dict, source: str = "panel") -> int:
    data = _validate(db, form)
    now = now_iso()
    with db.transaction():
        try:
            cid = db.insert("customers", {**data, "source": source, "created_by": user["id"] if user else None,
                                          "created_at": now, "updated_at": now})
        except IntegrityError:
            raise ValidationError({"phone": "Bu numara başka bir müşteriye kayıtlı."})
        if form.get("consent_service"):
            record_consent(db, cid, "whatsapp", "service", True, form.get("consent_source") or "panel", user,
                           audit_it=False)
        if form.get("consent_marketing"):
            record_consent(db, cid, "whatsapp", "marketing", True, form.get("consent_source") or "panel", user,
                           audit_it=False)
        note = (form.get("note") or "").strip()
        if note:
            add_note(db, cid, note, user, audit_it=False)
        audit("customer.create", "customer", cid, f"{data['first_name']} {data['last_name']} müşteri kaydı oluşturuldu",
              after={k: v for k, v in data.items() if k != "search_name"}, db=db)
    return cid


def update(db: Connection, user: dict, customer_id: int, form: dict) -> None:
    before = get_visible(db, user, customer_id)
    data = _validate(db, form, customer_id)
    b, a = diff(before, data)
    a.pop("search_name", None)
    b.pop("search_name", None)
    if not a:
        return
    with db.transaction():
        db.update("customers", customer_id, {**data, "updated_at": now_iso()})
        audit("customer.update", "customer", customer_id,
              f"{data['first_name']} {data['last_name']} müşteri kaydı güncellendi", before=b, after=a, db=db)


def soft_delete(db: Connection, user: dict, customer_id: int) -> None:
    c = get_visible(db, user, customer_id)
    future = db.scalar(
        f"SELECT COUNT(*) FROM appointments WHERE customer_id = ? AND starts_at >= ? "
        f"AND status IN ({','.join('?' * len(ACTIVE_STATUSES))})", [customer_id, now_iso(), *ACTIVE_STATUSES])
    if future:
        raise ValidationError({"_": "Müşterinin ileri tarihli aktif randevusu var. Önce randevuyu iptal edin."})
    with db.transaction():
        db.update("customers", customer_id, {"deleted_at": now_iso(), "updated_at": now_iso()})
        audit("customer.delete", "customer", customer_id, f"{c['first_name']} {c['last_name']} müşteri kaydı silindi",
              db=db)


# ------------------------------------------------------------------ notlar
def add_note(db: Connection, customer_id: int, body: str, user: dict, audit_it: bool = True) -> int:
    body = (body or "").strip()
    if not body:
        raise ValidationError({"body": "Not boş olamaz."})
    if len(body) > 2000:
        raise ValidationError({"body": "Not en fazla 2000 karakter olabilir."})
    now = now_iso()
    nid = db.insert("customer_notes", {"customer_id": customer_id, "author_user_id": user["id"], "body": body,
                                       "created_at": now, "updated_at": now})
    if audit_it:
        audit("note.create", "customer", customer_id, "Müşteriye dahili not eklendi", db=db)
    return nid


def _get_note(db: Connection, customer_id: int, note_id: int) -> dict:
    n = db.one("SELECT * FROM customer_notes WHERE id = ? AND customer_id = ? AND deleted_at IS NULL",
               (note_id, customer_id))
    if not n:
        raise NotFound()
    return n


def delete_note(db: Connection, user: dict, customer_id: int, note_id: int) -> None:
    n = _get_note(db, customer_id, note_id)
    if n["author_user_id"] != user["id"] and user["role"] != "admin":
        raise ValidationError({"_": "Yalnızca notu yazan kişi veya yönetici silebilir."})
    with db.transaction():
        db.update("customer_notes", note_id, {"deleted_at": now_iso(), "updated_at": now_iso()})
        audit("note.delete", "customer", customer_id, "Dahili not silindi", before={"body": n["body"]}, db=db)


def toggle_pin(db: Connection, customer_id: int, note_id: int) -> None:
    n = _get_note(db, customer_id, note_id)
    db.update("customer_notes", note_id, {"is_pinned": 0 if n["is_pinned"] else 1, "updated_at": now_iso()})


# ------------------------------------------------------------------ KVKK izinleri
def record_consent(db: Connection, customer_id: int, channel: str, category: str, granted: bool, source: str,
                   user: dict | None, evidence: str | None = None, audit_it: bool = True) -> None:
    if channel not in CHANNELS or category not in CATEGORIES:
        raise ValidationError({"_": "Geçersiz izin türü."})
    if source not in CONSENT_SOURCES:
        raise ValidationError({"_": "Geçersiz izin kaynağı."})
    db.insert("customer_consents", {
        "customer_id": customer_id, "channel": channel, "category": category, "granted": int(granted),
        "source": source, "evidence": (evidence or "")[:255] or None,
        "recorded_by": user["id"] if user else None, "created_at": now_iso(),
    }, returning="")
    if audit_it:
        audit("consent.update", "customer", customer_id,
              f"{CHANNELS[channel]} · {CATEGORIES[category]}: {'izin verildi' if granted else 'izin geri alındı'}",
              after={"channel": channel, "category": category, "granted": granted, "source": source}, db=db)


def current_consents(db: Connection, customer_id: int) -> dict[tuple[str, str], dict]:
    """(kanal, kategori) → en son kayıt. Kaydı olmayan = izin yok."""
    rows = db.query("SELECT * FROM customer_consents WHERE customer_id = ? ORDER BY created_at, id", (customer_id,))
    out: dict[tuple[str, str], dict] = {}
    for r in rows:
        out[(r["channel"], r["category"])] = r
    return out


def has_consent(db: Connection, customer_id: int, channel: str, category: str) -> bool:
    r = current_consents(db, customer_id).get((channel, category))
    return bool(r and r["granted"])


# ------------------------------------------------------------------ arama / liste
def _search_where(q: str) -> tuple[str, list]:
    q = (q or "").strip()[:80]
    if not q:
        return "", []
    digits = phone_search_digits(q)
    if digits and re.fullmatch(r"[\d\s()+-]+", q):
        return " AND c.phone LIKE ?", [f"%{digits}%"]
    tokens = fold(q).split()
    if not tokens:
        return "", []
    return "".join(" AND c.search_name LIKE ?" for _ in tokens), [f"%{t}%" for t in tokens]


_LIST_COLS = """c.id, c.first_name, c.last_name, c.phone, c.no_show_count, c.created_at,
    (SELECT MAX(a.starts_at) FROM appointments a WHERE a.customer_id = c.id AND a.status IN ('arrived','completed')) AS last_visit,
    (SELECT MIN(a.starts_at) FROM appointments a WHERE a.customer_id = c.id AND a.starts_at >= ?
        AND a.status IN ('pending','confirmed','reschedule_needed')) AS next_visit"""


def search(db: Connection, user: dict, q: str, limit: int = 8) -> list[dict]:
    where, params = _search_where(q)
    if not where:
        return []
    scope, sp = scope_sql(user)
    return db.query(f"SELECT {_LIST_COLS} FROM customers c WHERE c.deleted_at IS NULL{where}{scope} "
                    "ORDER BY c.search_name LIMIT ?", [now_iso()] + params + sp + [limit])


LIST_FILTERS = {
    "": "Tümü",
    "yaklasan": "Yaklaşan randevusu olan",
    "yeni": "Son 30 günde kayıt olan",
    "gelmedi": "Gelmeme kaydı olan",
}


def list_customers(db: Connection, user: dict, q: str = "", flt: str = "", page: int = 1, per: int = 30):
    where, params = _search_where(q)
    scope, sp = scope_sql(user)
    now = now_iso()
    extra, ep = "", []
    if flt == "yaklasan":
        extra = (" AND EXISTS (SELECT 1 FROM appointments a WHERE a.customer_id = c.id AND a.starts_at >= ? "
                 "AND a.status IN ('pending','confirmed','reschedule_needed'))")
        ep = [now]
    elif flt == "yeni":
        extra, ep = " AND c.created_at >= ?", [(dt.datetime.fromisoformat(now[:-1]) - dt.timedelta(days=30))
                                              .strftime("%Y-%m-%dT%H:%M:%SZ")]
    elif flt == "gelmedi":
        extra = " AND c.no_show_count > 0"
    base = f"FROM customers c WHERE c.deleted_at IS NULL{where}{extra}{scope}"
    total = db.scalar(f"SELECT COUNT(*) {base}", params + ep + sp) or 0
    rows = db.query(f"SELECT {_LIST_COLS} {base} ORDER BY c.search_name LIMIT ? OFFSET ?",
                    [now] + params + ep + sp + [per, (page - 1) * per])
    return rows, total


# ------------------------------------------------------------------ profil
def profile(db: Connection, user: dict, customer_id: int) -> dict:
    c = get_visible(db, user, customer_id)
    now = now_iso()
    last = db.one("""SELECT a.starts_at, s.name AS service_name FROM appointments a JOIN services s ON s.id = a.service_id
                     WHERE a.customer_id = ? AND a.status IN ('arrived','completed')
                     ORDER BY a.starts_at DESC LIMIT 1""", (customer_id,))
    nxt = db.one("""SELECT a.id, a.starts_at, a.status, s.name AS service_name, st.full_name AS staff_name
                    FROM appointments a JOIN services s ON s.id = a.service_id JOIN staff st ON st.id = a.staff_id
                    WHERE a.customer_id = ? AND a.starts_at >= ? AND a.status IN ('pending','confirmed','reschedule_needed')
                    ORDER BY a.starts_at LIMIT 1""", (customer_id, now))
    stats = {
        "total_visits": db.scalar("SELECT COUNT(*) FROM appointments WHERE customer_id = ? AND status = 'completed'",
                                  (customer_id,)),
        "cancelled": db.scalar("SELECT COUNT(*) FROM appointments WHERE customer_id = ? AND status = 'cancelled'",
                               (customer_id,)),
        "no_show": c["no_show_count"],
        "last_visit": last["starts_at"] if last else None,
        "last_service": last["service_name"] if last else None,
        "next": nxt,
    }
    finance = has_permission(user, "payments.view")
    if finance:
        stats["total_spent"] = db.scalar(
            "SELECT COALESCE(SUM(t.amount_kurus), 0) FROM payment_transactions t JOIN payments p ON p.id = t.payment_id "
            "WHERE p.customer_id = ?", (customer_id,))
        stats["balance"] = db.scalar(
            "SELECT COALESCE(SUM(total_kurus - discount_kurus - paid_kurus), 0) FROM payments "
            "WHERE customer_id = ? AND status IN ('partial','unpaid')", (customer_id,))

    appointments = db.query(
        """SELECT a.id, a.starts_at, a.status, a.price_kurus, a.source, s.name AS service_name,
                  st.full_name AS staff_name, st.color AS staff_color
             FROM appointments a JOIN services s ON s.id = a.service_id JOIN staff st ON st.id = a.staff_id
            WHERE a.customer_id = ? ORDER BY a.starts_at DESC""", (customer_id,))
    if user["role"] == "specialist":
        for a in appointments:
            a["price_kurus"] = None

    packages = db.query("""SELECT cp.*, s.name AS service_name FROM customer_packages cp
                           JOIN services s ON s.id = cp.service_id WHERE cp.customer_id = ?
                           ORDER BY cp.status = 'active' DESC, cp.purchased_at DESC""", (customer_id,))
    for p in packages:
        p["areas"] = db.query("SELECT * FROM session_areas WHERE customer_package_id = ? ORDER BY sort_order, id",
                              (p["id"],))
        for ar in p["areas"]:
            done = {s["session_number"]: s for s in db.query(
                "SELECT * FROM sessions WHERE session_area_id = ? ORDER BY session_number", (ar["id"],))}
            ar["slots"] = [{"n": n, "s": done.get(n)} for n in range(1, ar["total_sessions"] + 1)]
            ar["done"] = sum(1 for s in done.values() if s["status"] == "completed")
            ar["remaining"] = ar["total_sessions"] - ar["done"]

    payments = []
    if finance:
        payments = db.query("SELECT * FROM payments WHERE customer_id = ? AND status <> 'void' ORDER BY created_at DESC",
                            (customer_id,))
        for p in payments:
            p["transactions"] = db.query("SELECT * FROM payment_transactions WHERE payment_id = ? ORDER BY paid_at",
                                         (p["id"],))
            p["remaining"] = p["total_kurus"] - p["discount_kurus"] - p["paid_kurus"]

    notes = db.query("""SELECT n.*, u.full_name AS author FROM customer_notes n
                        LEFT JOIN users u ON u.id = n.author_user_id
                        WHERE n.customer_id = ? AND n.deleted_at IS NULL
                        ORDER BY n.is_pinned DESC, n.created_at DESC""", (customer_id,))
    messages = db.query("SELECT * FROM messages WHERE customer_id = ? ORDER BY created_at DESC LIMIT 50", (customer_id,))
    return {"c": c, "stats": stats, "appointments": appointments, "packages": packages, "payments": payments,
            "notes": notes, "messages": messages, "consents": current_consents(db, customer_id), "finance": finance,
            "timeline": timeline(db, customer_id, finance)}


STATUS_TEXT = {"pending": "Beklemede", "confirmed": "Onaylandı", "arrived": "Geldi", "completed": "İşlem tamamlandı",
               "cancelled": "İptal edildi", "no_show": "Gelmedi", "postponed": "Ertelendi",
               "reschedule_needed": "Yeniden planlama gerekiyor"}
CHANNEL_TEXT = {"whatsapp": " (WhatsApp üzerinden)", "online": " (online)", "system": " (otomatik)"}
MSG_STATUS = {"queued": "Bekliyor", "sent": "Gönderildi", "delivered": "Teslim edildi", "read": "Okundu",
              "failed": "Başarısız", "received": "Alındı"}


def timeline(db: Connection, customer_id: int, finance: bool) -> list[dict]:
    ev: list[dict] = []
    c = db.one("SELECT created_at, source FROM customers WHERE id = ?", (customer_id,))
    ev.append({"at": c["created_at"], "kind": "customer", "title": "Müşteri kaydı oluşturuldu",
               "detail": "Online randevu" if c["source"] == "online" else None})

    for h in db.query("""SELECT h.*, a.starts_at, s.name AS service_name FROM appointment_status_history h
                         JOIN appointments a ON a.id = h.appointment_id JOIN services s ON s.id = a.service_id
                         WHERE a.customer_id = ?""", (customer_id,)):
        if h["from_status"] is None:
            title = f"{h['service_name']} randevusu oluşturuldu"
        else:
            title = f"{h['service_name']} randevusu: {STATUS_TEXT.get(h['to_status'], h['to_status'])}"
        ev.append({"at": h["created_at"], "kind": "appointment", "status": h["to_status"],
                   "title": title + CHANNEL_TEXT.get(h["channel"], ""),
                   "detail": f"Randevu: {h['starts_at']}", "appt_at": h["starts_at"]})

    for s in db.query("""SELECT s.session_number, MIN(s.performed_at) AS at, GROUP_CONCAT_AREAS AS areas, cp.name
                         FROM sessions s JOIN session_areas sa ON sa.id = s.session_area_id
                         JOIN customer_packages cp ON cp.id = s.customer_package_id
                         WHERE cp.customer_id = ? AND s.status = 'completed'
                         GROUP BY cp.id, cp.name, s.session_number""".replace(
            "GROUP_CONCAT_AREAS", "STRING_AGG(sa.area_name, ', ')" if db.dialect == "postgres"
            else "GROUP_CONCAT(sa.area_name, ', ')"), (customer_id,)):
        ev.append({"at": s["at"], "kind": "session", "title": f"{s['name']}: {s['session_number']}. seans tamamlandı",
                   "detail": s["areas"]})

    for n in db.query("""SELECT n.created_at, n.body, u.full_name FROM customer_notes n
                         LEFT JOIN users u ON u.id = n.author_user_id
                         WHERE n.customer_id = ? AND n.deleted_at IS NULL""", (customer_id,)):
        ev.append({"at": n["created_at"], "kind": "note", "title": f"Not · {n['full_name'] or 'Sistem'}",
                   "detail": n["body"]})

    if finance:
        for t in db.query("""SELECT t.paid_at, t.amount_kurus, t.method, p.description FROM payment_transactions t
                             JOIN payments p ON p.id = t.payment_id WHERE p.customer_id = ?""", (customer_id,)):
            ev.append({"at": t["paid_at"], "kind": "payment", "amount": t["amount_kurus"],
                       "title": "Ödeme alındı" if t["amount_kurus"] > 0 else "İade yapıldı",
                       "detail": f"{p_desc(t)} · {METHOD_LABELS.get(t['method'], t['method'])}"})

    for m in db.query("SELECT * FROM messages WHERE customer_id = ?", (customer_id,)):
        if m["direction"] == "inbound":
            ev.append({"at": m["created_at"], "kind": "message", "title": "Müşteri cevabı", "detail": m["body"]})
        else:
            ev.append({"at": m["created_at"], "kind": "message",
                       "title": f"WhatsApp · {m['message_type']}", "detail": MSG_STATUS.get(m["status"], m["status"])})

    for k in db.query("SELECT * FROM customer_consents WHERE customer_id = ?", (customer_id,)):
        ev.append({"at": k["created_at"], "kind": "consent",
                   "title": f"{CHANNELS[k['channel']]} izni {'verildi' if k['granted'] else 'geri alındı'}",
                   "detail": CATEGORIES[k["category"]]})

    ev.sort(key=lambda e: e["at"] or "", reverse=True)
    return ev


def p_desc(t: dict) -> str:
    return t.get("description") or "Ödeme"
