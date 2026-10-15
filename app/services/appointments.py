"""Randevu motoru: uygun saat hesaplama, oluşturma, çakışma koruması, durum geçişleri, erteleme.

İşlem tamamlandığında: paketli randevularda seans işlenir (Faz 5 temeli) ve hizmetin tekrar
süresine göre "sonraki işlem tarihi" hesaplanır (Faz 6 temeli).
"""
from __future__ import annotations

import datetime as dt

from ..audit import audit
from ..db import Connection, IntegrityError, json_value
from ..errors import NotFound, ValidationError
from ..timeutil import TZ, iso, local_day_bounds_utc, now_iso, now_utc, parse_iso, to_local

STATUS_LABELS = {
    "pending": "Beklemede", "confirmed": "Onaylandı", "arrived": "Geldi", "completed": "İşlem Tamamlandı",
    "cancelled": "İptal Edildi", "no_show": "Gelmedi", "postponed": "Ertelendi",
    "reschedule_needed": "Yeniden Planlama Gerekiyor",
}
ACTIVE = ("pending", "confirmed", "arrived", "reschedule_needed")

# Hangi durumdan hangisine geçilebilir
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "pending": ("confirmed", "arrived", "cancelled", "no_show", "postponed", "reschedule_needed"),
    "confirmed": ("arrived", "completed", "cancelled", "no_show", "postponed", "reschedule_needed", "pending"),
    "arrived": ("completed", "cancelled"),
    "reschedule_needed": ("confirmed", "cancelled", "postponed"),
    "postponed": (),          # ertelenen randevu yeni saat verilerek (reschedule) tekrar aktifleşir
    "completed": (),
    "cancelled": (),
    "no_show": (),
}
ACTION_LABELS = {
    "confirmed": "Onayla", "arrived": "Geldi", "completed": "İşlem Tamamlandı", "cancelled": "İptal Et",
    "no_show": "Gelmedi", "postponed": "Ertele", "reschedule_needed": "Yeniden Planla", "pending": "Beklemeye Al",
}


# ------------------------------------------------------------------ ayarlar
def setting(db: Connection, key: str, default=None):
    row = db.one("SELECT value_json FROM settings WHERE key = ?", (key,))
    return default if row is None else json_value(row["value_json"])


# ------------------------------------------------------------------ yardımcılar
def _hm(value: str) -> dt.time:
    h, m = map(int, value[:5].split(":"))
    return dt.time(h, m)


def _local_dt(day: dt.date, t: dt.time) -> dt.datetime:
    return dt.datetime.combine(day, t, tzinfo=TZ)


def get_service(db: Connection, service_id) -> dict:
    s = db.one("SELECT * FROM services WHERE id = ? AND deleted_at IS NULL", (service_id,))
    if not s:
        raise ValidationError({"service_id": "Hizmet bulunamadı."})
    return s


def staff_for_service(db: Connection, service_id: int) -> list[dict]:
    return db.query("""SELECT st.id, st.full_name, st.color FROM staff st
                       JOIN staff_services ss ON ss.staff_id = st.id
                       WHERE ss.service_id = ? AND st.is_active = 1 AND st.deleted_at IS NULL
                       ORDER BY st.sort_order, st.full_name""", (service_id,))


def service_staff_map(db: Connection) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for r in db.query("""SELECT ss.service_id, ss.staff_id FROM staff_services ss
                         JOIN staff st ON st.id = ss.staff_id WHERE st.is_active = 1 AND st.deleted_at IS NULL"""):
        out.setdefault(r["service_id"], []).append(r["staff_id"])
    return out


# ------------------------------------------------------------------ uygun saatler
def available_slots(db: Connection, staff_id: int, service_id: int, day: dt.date,
                    exclude_appointment_id: int | None = None, min_notice_minutes: int = 0) -> list[str]:
    """Uzmanın o gün, hizmet süresine sığan boş başlangıç saatleri ('HH:MM', yerel saat)."""
    service = get_service(db, service_id)
    duration = service["duration_minutes"] + (service["buffer_minutes"] or 0)

    wh = db.one("SELECT start_time, end_time FROM working_hours WHERE staff_id = ? AND weekday = ?",
                (staff_id, day.weekday()))
    if not wh:
        return []
    d = day.isoformat()
    if db.scalar("""SELECT 1 FROM holidays WHERE (staff_id = ? OR staff_id IS NULL)
                    AND start_date <= ? AND end_date >= ?""", (staff_id, d, d)):
        return []

    blocked: list[tuple[dt.datetime, dt.datetime]] = []
    for b in db.query("SELECT start_time, end_time FROM staff_breaks WHERE staff_id = ? AND (weekday IS NULL OR weekday = ?)",
                      (staff_id, day.weekday())):
        blocked.append((_local_dt(day, _hm(b["start_time"])), _local_dt(day, _hm(b["end_time"]))))
    t0, t1 = local_day_bounds_utc(day)
    params: list = [staff_id, t1, t0]
    extra = ""
    if exclude_appointment_id:
        extra = " AND id <> ?"
        params.append(exclude_appointment_id)
    for a in db.query(f"SELECT starts_at, ends_at FROM appointments WHERE staff_id = ? AND is_active_slot = 1 "
                      f"AND starts_at < ? AND ends_at > ?{extra}", params):
        blocked.append((parse_iso(a["starts_at"]).astimezone(TZ), parse_iso(a["ends_at"]).astimezone(TZ)))

    step = int(setting(db, "booking.slot_step_minutes", 15) or 15)
    open_at = _local_dt(day, _hm(wh["start_time"]))
    close_at = _local_dt(day, _hm(wh["end_time"]))
    earliest = now_utc().astimezone(TZ) + dt.timedelta(minutes=min_notice_minutes)

    slots = []
    cur = open_at
    length = dt.timedelta(minutes=duration)
    while cur + length <= close_at:
        end = cur + length
        if cur >= earliest and not any(s < end and cur < e for s, e in blocked):
            slots.append(cur.strftime("%H:%M"))
        cur += dt.timedelta(minutes=step)
    return slots


# ------------------------------------------------------------------ okuma
def get(db: Connection, user: dict, appointment_id: int) -> dict:
    scope, sp = ("", [])
    if user["role"] == "specialist":
        scope, sp = " AND a.staff_id = ?", [user["staff_id"] or -1]
    a = db.one(f"""SELECT a.*, c.first_name, c.last_name, c.phone, c.no_show_count,
                          s.name AS service_name, s.duration_minutes, s.tracks_sessions,
                          st.full_name AS staff_name, st.color AS staff_color
                     FROM appointments a JOIN customers c ON c.id = a.customer_id
                     JOIN services s ON s.id = a.service_id JOIN staff st ON st.id = a.staff_id
                    WHERE a.id = ?{scope}""", [appointment_id] + sp)
    if not a:
        raise NotFound()
    return a


def history(db: Connection, appointment_id: int) -> list[dict]:
    return db.query("""SELECT h.*, u.full_name AS user_name FROM appointment_status_history h
                       LEFT JOIN users u ON u.id = h.changed_by WHERE h.appointment_id = ?
                       ORDER BY h.created_at, h.id""", (appointment_id,))


def list_range(db: Connection, user: dict, start_utc: str, end_utc: str, staff_id: int | None = None) -> list[dict]:
    where, params = "a.starts_at >= ? AND a.starts_at < ?", [start_utc, end_utc]
    if user["role"] == "specialist":
        where += " AND a.staff_id = ?"
        params.append(user["staff_id"] or -1)
    elif staff_id:
        where += " AND a.staff_id = ?"
        params.append(staff_id)
    return db.query(f"""SELECT a.id, a.starts_at, a.ends_at, a.status, a.staff_id, a.customer_id,
                               c.first_name, c.last_name, s.name AS service_name,
                               st.full_name AS staff_name, st.color AS staff_color
                          FROM appointments a JOIN customers c ON c.id = a.customer_id
                          JOIN services s ON s.id = a.service_id JOIN staff st ON st.id = a.staff_id
                         WHERE {where} ORDER BY a.starts_at, st.sort_order""", params)


# ------------------------------------------------------------------ oluşturma
def _parse_day(value: str | None) -> dt.date:
    try:
        return dt.date.fromisoformat((value or "").strip())
    except ValueError:
        raise ValidationError({"date": "Tarih seçin."})


def _active_package(db: Connection, customer_id: int, service_id: int) -> int | None:
    return db.scalar("""SELECT id FROM customer_packages WHERE customer_id = ? AND service_id = ? AND status = 'active'
                        ORDER BY purchased_at LIMIT 1""", (customer_id, service_id))


def create(db: Connection, user: dict | None, form: dict, source: str = "panel") -> int:
    errors: dict[str, str] = {}
    try:
        customer_id = int(form.get("customer_id") or 0)
        service_id = int(form.get("service_id") or 0)
        staff_id = int(form.get("staff_id") or 0)
    except ValueError:
        raise ValidationError({"_": "Geçersiz seçim."})
    customer = db.one("SELECT id, first_name, last_name FROM customers WHERE id = ? AND deleted_at IS NULL",
                      (customer_id,))
    if not customer:
        errors["customer_id"] = "Müşteri seçin."
    if not service_id:
        errors["service_id"] = "Hizmet seçin."
    if not staff_id:
        errors["staff_id"] = "Uzman seçin."
    time_s = (form.get("time") or "").strip()
    if not time_s:
        errors["time"] = "Saat seçin."
    if errors:
        raise ValidationError(errors)
    day = _parse_day(form.get("date"))
    service = get_service(db, service_id)
    if not service["is_active"]:
        raise ValidationError({"service_id": "Bu hizmet pasif durumda."})
    if staff_id not in [s["id"] for s in staff_for_service(db, service_id)]:
        raise ValidationError({"staff_id": "Seçilen uzman bu hizmeti vermiyor."})
    status = form.get("status") or "pending"
    if status not in ("pending", "confirmed"):
        status = "pending"

    start = _local_dt(day, _hm(time_s))
    end = start + dt.timedelta(minutes=service["duration_minutes"])
    price = service["campaign_price_kurus"] if service["campaign_price_kurus"] is not None else service["price_kurus"]
    note = (form.get("internal_note") or "").strip()[:2000] or None
    customer_note = (form.get("customer_note") or "").strip()[:500] or None
    min_notice = int(setting(db, "booking.min_notice_minutes", 60) or 0) if source == "online" else 0

    now = now_iso()
    try:
        with db.transaction():
            # Kontrol + yazma aynı yazma transaction'ında (SQLite: BEGIN IMMEDIATE; PG: exclusion constraint)
            if time_s not in available_slots(db, staff_id, service_id, day, min_notice_minutes=min_notice):
                raise ValidationError({"time": "Bu saat artık uygun değil. Lütfen başka bir saat seçin."})
            aid = db.insert("appointments", {
                "customer_id": customer_id, "staff_id": staff_id, "service_id": service_id,
                "customer_package_id": _active_package(db, customer_id, service_id) if service["tracks_sessions"] else None,
                "starts_at": iso(start), "ends_at": iso(end), "status": status, "is_active_slot": 1,
                "source": source, "price_kurus": price, "internal_note": note, "customer_note": customer_note,
                "created_by": user["id"] if user else None,
                "confirmed_at": now if status == "confirmed" else None,
                "created_at": now, "updated_at": now,
            })
            db.insert("appointment_status_history", {"appointment_id": aid, "from_status": None, "to_status": status,
                                                     "changed_by": user["id"] if user else None,
                                                     "channel": source, "created_at": now})
            audit("appointment.create", "appointment", aid,
                  f"{customer['first_name']} {customer['last_name']} · {service['name']} · "
                  f"{start.strftime('%d.%m.%Y %H:%M')} randevusu oluşturuldu", db=db,
                  **({} if user else {"actor": None}))
    except IntegrityError as exc:
        if exc.constraint == "ex_appointments_no_overlap":
            raise ValidationError({"time": "Bu saat az önce başka bir randevuyla doldu. Lütfen başka bir saat seçin."})
        raise
    return aid


# ------------------------------------------------------------------ durum
def change_status(db: Connection, user: dict | None, appointment_id: int, new_status: str,
                  reason: str | None = None, channel: str = "panel") -> None:
    a = db.one("SELECT * FROM appointments WHERE id = ?", (appointment_id,))
    if not a:
        raise NotFound()
    old = a["status"]
    if new_status not in TRANSITIONS.get(old, ()):
        raise ValidationError({"_": f"'{STATUS_LABELS.get(old, old)}' durumundaki randevu "
                                    f"'{STATUS_LABELS.get(new_status, new_status)}' yapılamaz."})
    now = now_iso()
    data = {"status": new_status, "is_active_slot": int(new_status in ACTIVE), "updated_at": now}
    if new_status == "confirmed":
        data["confirmed_at"] = now
    elif new_status == "cancelled":
        data["cancelled_at"] = now
    elif new_status == "completed":
        data["completed_at"] = now
    try:
        with db.transaction():
            db.update("appointments", appointment_id, data)
            db.insert("appointment_status_history", {"appointment_id": appointment_id, "from_status": old,
                                                     "to_status": new_status, "changed_by": user["id"] if user else None,
                                                     "channel": channel, "reason": (reason or "")[:255] or None,
                                                     "created_at": now})
            if new_status == "no_show":
                _record_no_show(db, a)
            if new_status == "completed":
                _on_completed(db, a, user)
            c = db.one("SELECT first_name, last_name FROM customers WHERE id = ?", (a["customer_id"],))
            audit(f"appointment.{new_status}", "appointment", appointment_id,
                  f"{c['first_name']} {c['last_name']} randevusu: {STATUS_LABELS[old]} → {STATUS_LABELS[new_status]}",
                  before={"status": old}, after={"status": new_status, **({"reason": reason} if reason else {})},
                  db=db, **({} if user else {"actor": None}))
    except IntegrityError as exc:
        if exc.constraint == "ex_appointments_no_overlap":
            raise ValidationError({"_": "Bu saat başka bir randevuyla dolu; önce saati değiştirin."})
        raise


def _record_no_show(db: Connection, a: dict) -> None:
    db.execute("UPDATE customers SET no_show_count = no_show_count + 1, updated_at = ? WHERE id = ?",
               (now_iso(), a["customer_id"]))
    c = db.one("SELECT first_name, last_name, no_show_count FROM customers WHERE id = ?", (a["customer_id"],))
    threshold = int(setting(db, "no_show.alert_threshold", 2) or 2)
    if c["no_show_count"] >= threshold:
        db.insert("notifications", {
            "user_id": None, "kind": "no_show_repeat", "customer_id": a["customer_id"], "appointment_id": a["id"],
            "title": f"{c['first_name']} {c['last_name']} {c['no_show_count']}. kez randevusuna gelmedi",
            "body": "Tekrarlayan gelmeme kaydı. Bir sonraki randevuda teyit almanız önerilir.",
            "link": f"/panel/musteriler/{a['customer_id']}", "created_at": now_iso(),
        })


def _on_completed(db: Connection, a: dict, user: dict | None) -> None:
    performed = a["starts_at"]
    from .payments import charge_for_completed_appointment
    charge_for_completed_appointment(db, user, a)
    # Paketli işlem → her bölgenin sıradaki seansı tamamlanır
    if a["customer_package_id"]:
        cp_id = a["customer_package_id"]
        for area in db.query("SELECT * FROM session_areas WHERE customer_package_id = ?", (cp_id,)):
            done = db.scalar("SELECT COUNT(*) FROM sessions WHERE session_area_id = ? AND status = 'completed'",
                             (area["id"],)) or 0
            if done >= area["total_sessions"]:
                continue
            n = done + 1
            existing = db.one("SELECT id FROM sessions WHERE session_area_id = ? AND session_number = ?", (area["id"], n))
            data = {"status": "completed", "appointment_id": a["id"], "staff_id": a["staff_id"],
                    "performed_at": performed, "updated_at": now_iso()}
            if existing:
                db.update("sessions", existing["id"], data)
            else:
                db.insert("sessions", {**data, "customer_package_id": cp_id, "session_area_id": area["id"],
                                       "session_number": n, "created_at": now_iso()})
        remaining = db.scalar("""SELECT COUNT(*) FROM session_areas sa WHERE sa.customer_package_id = ?
                                 AND (SELECT COUNT(*) FROM sessions s WHERE s.session_area_id = sa.id
                                      AND s.status = 'completed') < sa.total_sessions""", (cp_id,))
        if not remaining:
            db.update("customer_packages", cp_id, {"status": "completed", "completed_at": now_iso(),
                                                   "updated_at": now_iso()})
    # Tekrar süresi → sonraki işlem tarihi
    rep = db.scalar("SELECT repeat_interval_days FROM services WHERE id = ?", (a["service_id"],))
    if rep:
        last = to_local(performed).date()
        due = last + dt.timedelta(days=int(rep))
        ex = db.one("SELECT id FROM service_followups WHERE customer_id = ? AND service_id = ?",
                    (a["customer_id"], a["service_id"]))
        data = {"last_appointment_id": a["id"], "last_done_on": last.isoformat(), "due_on": due.isoformat(),
                "status": "open", "updated_at": now_iso()}
        if ex:
            db.update("service_followups", ex["id"], data)
        else:
            db.insert("service_followups", {**data, "customer_id": a["customer_id"], "service_id": a["service_id"],
                                            "created_at": now_iso()})


# ------------------------------------------------------------------ yeni saat
def reschedule(db: Connection, user: dict | None, appointment_id: int, form: dict, channel: str = "panel") -> None:
    a = db.one("SELECT * FROM appointments WHERE id = ?", (appointment_id,))
    if not a:
        raise NotFound()
    if a["status"] in ("completed", "cancelled", "no_show"):
        raise ValidationError({"_": "Bu randevunun saati değiştirilemez."})
    staff_id = int(form.get("staff_id") or a["staff_id"])
    time_s = (form.get("time") or "").strip()
    if not time_s:
        raise ValidationError({"time": "Yeni saat seçin."})
    day = _parse_day(form.get("date"))
    service = get_service(db, a["service_id"])
    if staff_id not in [s["id"] for s in staff_for_service(db, a["service_id"])]:
        raise ValidationError({"staff_id": "Seçilen uzman bu hizmeti vermiyor."})
    start = _local_dt(day, _hm(time_s))
    end = start + dt.timedelta(minutes=service["duration_minutes"])
    old_local = to_local(a["starts_at"])
    new_status = a["status"] if a["status"] in ("pending", "confirmed", "arrived") else "confirmed"
    now = now_iso()
    try:
        with db.transaction():
            if time_s not in available_slots(db, staff_id, a["service_id"], day, exclude_appointment_id=appointment_id):
                raise ValidationError({"time": "Bu saat uygun değil. Lütfen başka bir saat seçin."})
            db.update("appointments", appointment_id, {
                "staff_id": staff_id, "starts_at": iso(start), "ends_at": iso(end), "status": new_status,
                "is_active_slot": 1, "updated_at": now,
            })
            db.insert("appointment_status_history", {
                "appointment_id": appointment_id, "from_status": a["status"], "to_status": new_status,
                "changed_by": user["id"] if user else None, "channel": channel,
                "reason": f"Saat değişti: {old_local.strftime('%d.%m %H:%M')} → {start.strftime('%d.%m %H:%M')}",
                "created_at": now})
            c = db.one("SELECT first_name, last_name FROM customers WHERE id = ?", (a["customer_id"],))
            audit("appointment.reschedule", "appointment", appointment_id,
                  f"{c['first_name']} {c['last_name']} randevusu {old_local.strftime('%d.%m.%Y %H:%M')} → "
                  f"{start.strftime('%d.%m.%Y %H:%M')} olarak değiştirildi",
                  before={"starts_at": a["starts_at"], "staff_id": a["staff_id"]},
                  after={"starts_at": iso(start), "staff_id": staff_id}, db=db,
                  **({} if user else {"actor": None}))
    except IntegrityError as exc:
        if exc.constraint == "ex_appointments_no_overlap":
            raise ValidationError({"time": "Bu saat az önce doldu. Lütfen başka bir saat seçin."})
        raise


def update_note(db: Connection, user: dict, appointment_id: int, note: str) -> None:
    db.update("appointments", appointment_id, {"internal_note": (note or "").strip()[:2000] or None,
                                               "updated_at": now_iso()})
