"""Herkese açık web sitesi: hizmet listesi ve online randevu talebi.

Online randevular 'Beklemede' durumunda oluşur; panelde onaylanır. Müşteri, kendisine verilen
imzalı ve süreli bağlantıyla randevusunu görüntüleyip iptal edebilir (token yalnızca özet olarak saklanır)."""
from __future__ import annotations

import datetime as dt
import hashlib
import secrets

from ..audit import audit
from ..db import Connection
from ..errors import NotFound, ValidationError
from ..security import ratelimit
from ..textutil import format_phone, normalize_phone, tr_title
from ..timeutil import TZ, iso, local_day_bounds_utc, local_today, now_iso, now_utc, parse_iso, to_local
from . import appointments as appt
from . import customers as cust

WEEKDAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
WEEKDAYS_SHORT = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]
MONTHS_SHORT = ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"]

# Hız sınırları
IP_LIMIT_PER_HOUR = 5
PHONE_ACTIVE_LIMIT = 3
GLOBAL_LIMIT_PER_HOUR = 60
KVKK_VERSION = "kvkk-2026-10"


def enabled(db: Connection) -> bool:
    v = appt.setting(db, "booking.online_enabled", True)
    return bool(v) and v not in ("0", "false")


def max_days(db: Connection) -> int:
    return int(appt.setting(db, "booking.max_days_ahead", 60) or 60)


def min_notice(db: Connection) -> int:
    return int(appt.setting(db, "booking.min_notice_minutes", 60) or 0)


# ------------------------------------------------------------------ hizmetler
def services(db: Connection) -> list[dict]:
    rows = db.query("""SELECT s.id, s.name, s.slug, s.category, s.short_description, s.description, s.duration_minutes,
                              s.price_kurus, s.campaign_price_kurus, s.image_media_id, s.online_booking, s.tracks_sessions
                         FROM services s
                        WHERE s.deleted_at IS NULL AND s.is_active = 1
                          AND EXISTS (SELECT 1 FROM staff_services ss JOIN staff st ON st.id = ss.staff_id
                                      WHERE ss.service_id = s.id AND st.is_active = 1 AND st.deleted_at IS NULL)
                        ORDER BY s.sort_order, s.name""")
    return rows


def categories(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["category"] or "Diğer", []).append(r)
    return list(out.items())


def service_by_slug(db: Connection, slug: str) -> dict:
    for s in services(db):
        if s["slug"] == slug:
            return s
    raise NotFound()


def staff_options(db: Connection, service_id: int) -> list[dict]:
    return appt.staff_for_service(db, service_id)


def business_hours(db: Connection) -> list[dict]:
    """Haftanın her günü için merkezin açık olduğu en erken / en geç saat (personel çalışma saatlerinden)."""
    rows = db.query("""SELECT wh.weekday, MIN(wh.start_time) AS s, MAX(wh.end_time) AS e
                         FROM working_hours wh JOIN staff st ON st.id = wh.staff_id
                        WHERE st.is_active = 1 AND st.deleted_at IS NULL GROUP BY wh.weekday""")
    by = {r["weekday"]: r for r in rows}
    out = []
    for i, name in enumerate(WEEKDAYS):
        r = by.get(i)
        out.append({"day": name, "hours": f"{r['s'][:5]} – {r['e'][:5]}" if r else None,
                    "today": i == local_today().weekday()})
    return out


# ------------------------------------------------------------------ uygunluk
def day_strip(db: Connection, start: dt.date, count: int = 14) -> list[dict]:
    today = local_today()
    last = today + dt.timedelta(days=max_days(db))
    out = []
    d = max(start, today)
    while len(out) < count and d <= last:
        out.append({"date": d, "iso": d.isoformat(), "wd": WEEKDAYS_SHORT[d.weekday()], "day": d.day,
                    "mon": MONTHS_SHORT[d.month - 1], "today": d == today})
        d += dt.timedelta(days=1)
    return out


def valid_day(db: Connection, value: str | None) -> dt.date | None:
    try:
        d = dt.date.fromisoformat(value or "")
    except ValueError:
        return None
    today = local_today()
    if d < today or d > today + dt.timedelta(days=max_days(db)):
        return None
    return d


def slots(db: Connection, service_id: int, day: dt.date, staff_id: int | None) -> list[str]:
    """Uygun başlangıç saatleri. staff_id None → 'fark etmez': herhangi bir uzmanın boş olduğu saatler."""
    notice = min_notice(db)
    staff_ids = [staff_id] if staff_id else [s["id"] for s in staff_options(db, service_id)]
    times: set[str] = set()
    for sid in staff_ids:
        times.update(appt.available_slots(db, sid, service_id, day, min_notice_minutes=notice))
    return sorted(times)


def _pick_staff(db: Connection, service_id: int, day: dt.date, time_s: str) -> int | None:
    """'Fark etmez' seçildiğinde o saatte boş olan ve o gün en az randevusu olan uzman."""
    t0, t1 = local_day_bounds_utc(day)
    best = None
    for s in staff_options(db, service_id):
        if time_s not in appt.available_slots(db, s["id"], service_id, day, min_notice_minutes=min_notice(db)):
            continue
        load = db.scalar("SELECT COUNT(*) FROM appointments WHERE staff_id = ? AND is_active_slot = 1 "
                         "AND starts_at >= ? AND starts_at < ?", (s["id"], t0, t1)) or 0
        if best is None or load < best[0]:
            best = (load, s["id"])
    return best[1] if best else None


# ------------------------------------------------------------------ talep
def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def book(db: Connection, form: dict, ip: str) -> tuple[int, str]:
    """Online randevu talebi oluşturur. (appointment_id, müşteriye verilecek bağlantı token'ı) döner."""
    if not enabled(db):
        raise ValidationError({"_": "Online randevu şu anda kapalı. Lütfen telefonla ulaşın."})
    if (form.get("website") or "").strip():          # bot tuzağı (görünmez alan)
        raise ValidationError({"_": "Talep alınamadı. Lütfen telefonla ulaşın."})

    errors: dict[str, str] = {}
    slug = form.get("hizmet") or ""
    try:
        service = service_by_slug(db, slug)
    except NotFound:
        raise ValidationError({"_": "Hizmet bulunamadı. Lütfen baştan seçin."})
    if not service["online_booking"]:
        raise ValidationError({"_": "Bu hizmet için online randevu alınmıyor; lütfen telefonla ulaşın."})
    day = valid_day(db, form.get("tarih"))
    time_s = (form.get("saat") or "").strip()[:5]
    staff_raw = str(form.get("uzman") or "0")
    staff_id = int(staff_raw) if staff_raw.isdigit() else -1
    if staff_id and staff_id not in [s["id"] for s in staff_options(db, service["id"])]:
        raise ValidationError({"_": "Uzman seçimi geçersiz. Lütfen baştan seçin."})
    if not day or not time_s:
        raise ValidationError({"_": "Tarih ve saat seçin."})

    first = tr_title((form.get("first_name") or "").strip())[:80]
    last = tr_title((form.get("last_name") or "").strip())[:80]
    if len(first) < 2:
        errors["first_name"] = "Adınızı yazın."
    if len(last) < 2:
        errors["last_name"] = "Soyadınızı yazın."
    phone = normalize_phone(form.get("phone"))
    if not phone:
        errors["phone"] = "Geçerli bir cep telefonu yazın (örn. 0532 123 45 67)."
    if not form.get("kvkk"):
        errors["kvkk"] = "Devam etmek için aydınlatma metnini okuduğunuzu onaylayın."
    note = (form.get("note") or "").strip()[:300] or None
    if errors:
        raise ValidationError(errors)

    # Hız sınırları
    if ratelimit.count(db, "booking.ip", ip, 60) >= IP_LIMIT_PER_HOUR \
            or ratelimit.count(db, "booking.global", "all", 60) >= GLOBAL_LIMIT_PER_HOUR:
        raise ValidationError({"_": "Çok fazla talep gönderildi. Lütfen biraz sonra tekrar deneyin veya bizi arayın."})
    active = db.scalar("""SELECT COUNT(*) FROM appointments a JOIN customers c ON c.id = a.customer_id
                          WHERE c.phone = ? AND c.deleted_at IS NULL AND a.source = 'online'
                          AND a.status IN ('pending','confirmed') AND a.starts_at >= ?""", (phone, now_iso())) or 0
    if active >= PHONE_ACTIVE_LIMIT:
        raise ValidationError({"_": "Bu numarayla bekleyen birden fazla randevunuz var. Değişiklik için lütfen bizi arayın."})

    if not staff_id:
        staff_id = _pick_staff(db, service["id"], day, time_s)
    if not staff_id or time_s not in appt.available_slots(db, staff_id, service["id"], day,
                                                          min_notice_minutes=min_notice(db)):
        raise ValidationError({"saat": "Seçtiğiniz saat az önce doldu. Lütfen başka bir saat seçin."})

    # Müşteri: aynı numara varsa o kayıt kullanılır, isim üzerine yazılmaz
    existing = db.one("SELECT id, first_name, last_name FROM customers WHERE phone = ? AND deleted_at IS NULL", (phone,))
    internal = None
    if existing:
        cid = existing["id"]
        if (existing["first_name"], existing["last_name"]) != (first, last):
            internal = f"Online formda yazılan isim: {first} {last}"
        with db.transaction():
            for cat in ("service", "marketing"):
                if form.get(f"consent_{cat}"):
                    cust.record_consent(db, cid, "whatsapp", cat, True, "online_form", None, evidence=KVKK_VERSION)
    else:
        cid = cust.create(db, {"first_name": first, "last_name": last, "phone": phone,
                               "consent_service": form.get("consent_service"),
                               "consent_marketing": form.get("consent_marketing"),
                               "consent_source": "online_form"}, None, source="online")

    aid = appt.create(db, None, {"customer_id": cid, "service_id": service["id"], "staff_id": staff_id,
                                 "date": day.isoformat(), "time": time_s, "status": "pending",
                                 "customer_note": note, "internal_note": internal}, source="online")
    token = secrets.token_urlsafe(24)
    a = db.one("SELECT starts_at FROM appointments WHERE id = ?", (aid,))
    expires = parse_iso(a["starts_at"]) + dt.timedelta(days=2)
    staff_name = db.scalar("SELECT full_name FROM staff WHERE id = ?", (staff_id,))
    with db.transaction():
        db.update("appointments", aid, {"reschedule_token_hash": _hash(token), "reschedule_token_expires_at": iso(expires)})
        db.insert("notifications", {
            "user_id": None, "kind": "online_booking", "customer_id": cid, "appointment_id": aid,
            "title": f"Yeni online randevu talebi: {first} {last}",
            "body": f"{service['name']} · {day.strftime('%d.%m.%Y')} {time_s} · {staff_name} — onay bekliyor",
            "link": f"/panel/randevular/{aid}", "dedupe_key": f"online_booking:{aid}", "created_at": now_iso(),
        })
        ratelimit.hit(db, "booking.ip", ip)
        ratelimit.hit(db, "booking.global", "all")
    return aid, token


def by_token(db: Connection, token: str) -> dict:
    if not token or len(token) > 64:
        raise NotFound()
    a = db.one("""SELECT a.*, s.name AS service_name, s.duration_minutes, st.full_name AS staff_name,
                         c.first_name, c.last_name, c.phone
                    FROM appointments a JOIN services s ON s.id = a.service_id
                    JOIN staff st ON st.id = a.staff_id JOIN customers c ON c.id = a.customer_id
                   WHERE a.reschedule_token_hash = ?""", (_hash(token),))
    if not a or not a["reschedule_token_expires_at"] or parse_iso(a["reschedule_token_expires_at"]) < now_utc():
        raise NotFound()
    start = to_local(a["starts_at"])
    a["local_start"] = start
    a["date_label"] = f"{start.day} {MONTHS_SHORT[start.month - 1]} {start.year}, {WEEKDAYS[start.weekday()]}"
    a["time_label"] = start.strftime("%H:%M")
    a["phone_display"] = format_phone(a["phone"])
    a["can_cancel"] = a["status"] in ("pending", "confirmed") and \
        start - now_utc().astimezone(TZ) >= dt.timedelta(minutes=max(min_notice(db), 60))
    return a


def cancel_by_token(db: Connection, token: str, ip: str) -> None:
    a = by_token(db, token)
    if not a["can_cancel"]:
        raise ValidationError({"_": "Bu randevu artık online iptal edilemiyor. Lütfen bizi arayın."})
    if ratelimit.count(db, "cancel.ip", ip, 60) >= 10:
        raise ValidationError({"_": "Çok fazla deneme. Lütfen biraz sonra tekrar deneyin."})
    appt.change_status(db, None, a["id"], "cancelled", reason="Müşteri online bağlantıdan iptal etti", channel="online")
    with db.transaction():
        ratelimit.hit(db, "cancel.ip", ip)
        db.insert("notifications", {
            "user_id": None, "kind": "online_cancel", "customer_id": a["customer_id"], "appointment_id": a["id"],
            "title": f"Online iptal: {a['first_name']} {a['last_name']}",
            "body": f"{a['service_name']} · {a['local_start'].strftime('%d.%m.%Y %H:%M')} randevusu müşteri tarafından iptal edildi.",
            "link": f"/panel/randevular/{a['id']}", "dedupe_key": f"online_cancel:{a['id']}", "created_at": now_iso(),
        })
