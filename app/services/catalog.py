"""Hizmetler, personel, çalışma saatleri, molalar, izin/kapalı günler, görseller ve randevu ayarları."""
from __future__ import annotations

import datetime as dt
import re

from ..audit import audit, diff
from ..db import Connection, IntegrityError, json_value
from ..errors import NotFound, ValidationError
from ..textutil import fold, normalize_phone
from ..timeutil import local_today, now_iso

WEEKDAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
MAX_IMAGE = 2 * 1024 * 1024


# ------------------------------------------------------------------ yardımcılar
def _int(value, name: str, errors: dict, label: str, lo: int | None = None, hi: int | None = None,
         required: bool = True) -> int | None:
    raw = str(value if value is not None else "").strip()
    if raw == "":
        if required:
            errors[name] = f"{label} gerekli."
        return None
    try:
        n = int(raw)
    except ValueError:
        errors[name] = f"{label} sayı olmalı."
        return None
    if (lo is not None and n < lo) or (hi is not None and n > hi):
        errors[name] = f"{label} {lo}–{hi} arasında olmalı."
    return n


def _money(value, name: str, errors: dict, label: str, required: bool = True) -> int | None:
    raw = str(value or "").strip().replace(" ", "").replace("TL", "").replace("₺", "")
    if raw == "":
        if required:
            errors[name] = f"{label} gerekli."
        return None
    # "1.250,50" / "3.000" / "1250.50" / "1250" → kuruş (Türkçe binlik ayırıcı nokta)
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", raw):
        raw = raw.replace(".", "")
    try:
        v = round(float(raw) * 100)
    except ValueError:
        errors[name] = f"{label} geçersiz."
        return None
    if v < 0 or v > 100_000_000:
        errors[name] = f"{label} geçersiz."
    return v


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", fold(text)).strip("-")
    return s or "hizmet"


def _check_time(value: str, name: str, errors: dict, label: str) -> str | None:
    v = (value or "").strip()[:5]
    if not TIME_RE.match(v):
        errors[name] = f"{label} SS:DD biçiminde olmalı."
        return None
    return v


def _future_active_count(db: Connection, column: str, value: int) -> int:
    return db.scalar(f"SELECT COUNT(*) FROM appointments WHERE {column} = ? AND is_active_slot = 1 AND starts_at >= ?",
                     (value, now_iso())) or 0


# ------------------------------------------------------------------ görseller
def save_image(db: Connection, file_storage, kind: str, user: dict) -> int:
    data = file_storage.read(MAX_IMAGE + 1)
    if len(data) > MAX_IMAGE:
        raise ValidationError({"image": "Görsel en fazla 2 MB olabilir."})
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        ctype = "image/png"
    elif data[:3] == b"\xff\xd8\xff":
        ctype = "image/jpeg"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ctype = "image/webp"
    else:
        raise ValidationError({"image": "Yalnızca PNG, JPG veya WEBP görsel yüklenebilir."})
    return db.insert("media", {"kind": kind, "content_type": ctype, "data": data, "size_bytes": len(data),
                               "created_by": user["id"], "created_at": now_iso()})


def get_media(db: Connection, media_id: int) -> dict:
    m = db.one("SELECT content_type, data FROM media WHERE id = ?", (media_id,))
    if not m:
        raise NotFound()
    return m


# ------------------------------------------------------------------ hizmetler
def list_services(db: Connection) -> list[dict]:
    rows = db.query("""SELECT s.*, (SELECT COUNT(*) FROM staff_services ss JOIN staff st ON st.id = ss.staff_id
                                     WHERE ss.service_id = s.id AND st.deleted_at IS NULL AND st.is_active = 1) AS staff_count
                         FROM services s WHERE s.deleted_at IS NULL
                        ORDER BY s.is_active DESC, s.sort_order, s.name""")
    return rows


def get_service(db: Connection, service_id: int) -> dict:
    s = db.one("SELECT * FROM services WHERE id = ? AND deleted_at IS NULL", (service_id,))
    if not s:
        raise NotFound()
    s["staff_ids"] = [r["staff_id"] for r in db.query("SELECT staff_id FROM staff_services WHERE service_id = ?",
                                                      (service_id,))]
    return s


def _service_data(db: Connection, form: dict, service_id: int | None) -> tuple[dict, list[int]]:
    errors: dict[str, str] = {}
    name = (form.get("name") or "").strip()[:120]
    if len(name) < 2:
        errors["name"] = "Hizmet adı gerekli."
    data = {
        "name": name,
        "category": (form.get("category") or "").strip()[:60] or None,
        "short_description": (form.get("short_description") or "").strip()[:255] or None,
        "description": (form.get("description") or "").strip()[:5000] or None,
        "duration_minutes": _int(form.get("duration_minutes"), "duration_minutes", errors, "İşlem süresi", 5, 600),
        "buffer_minutes": _int(form.get("buffer_minutes") or 0, "buffer_minutes", errors, "Ara süre", 0, 120),
        "price_kurus": _money(form.get("price"), "price", errors, "Fiyat"),
        "campaign_price_kurus": _money(form.get("campaign_price"), "campaign_price", errors, "Kampanyalı fiyat",
                                       required=False),
        "repeat_interval_days": _int(form.get("repeat_interval_days"), "repeat_interval_days", errors,
                                     "Tekrar süresi", 1, 730, required=False),
        "tracks_sessions": int(bool(form.get("tracks_sessions"))),
        "online_booking": int(bool(form.get("online_booking"))),
        "is_active": int(bool(form.get("is_active"))),
        "sort_order": _int(form.get("sort_order") or 0, "sort_order", errors, "Sıra", 0, 999),
    }
    if (data["campaign_price_kurus"] is not None and data["price_kurus"] is not None
            and data["campaign_price_kurus"] >= data["price_kurus"]):
        errors["campaign_price"] = "Kampanyalı fiyat normal fiyattan düşük olmalı."
    dup = db.scalar("SELECT id FROM services WHERE LOWER(name) = LOWER(?) AND deleted_at IS NULL", (name,))
    if name and dup and dup != service_id:
        errors["name"] = "Bu isimde bir hizmet zaten var."
    staff_ids = []
    for v in form.get("staff_ids", []) or []:
        if str(v).isdigit():
            staff_ids.append(int(v))
    if errors:
        raise ValidationError(errors)
    return data, staff_ids


def _set_service_staff(db: Connection, service_id: int, staff_ids: list[int]) -> None:
    valid = {r["id"] for r in db.query("SELECT id FROM staff WHERE deleted_at IS NULL")}
    db.execute("DELETE FROM staff_services WHERE service_id = ?", (service_id,))
    for sid in sorted(set(staff_ids) & valid):
        db.insert("staff_services", {"staff_id": sid, "service_id": service_id, "created_at": now_iso()}, returning="")


def save_service(db: Connection, user: dict, form: dict, service_id: int | None = None, image=None) -> int:
    data, staff_ids = _service_data(db, form, service_id)
    now = now_iso()
    with db.transaction():
        if image is not None and getattr(image, "filename", ""):
            data["image_media_id"] = save_image(db, image, "service_image", user)
        if form.get("remove_image"):
            data["image_media_id"] = None
        if service_id is None:
            base, slug, i = slugify(data["name"]), slugify(data["name"]), 2
            while db.scalar("SELECT 1 FROM services WHERE slug = ?", (slug,)):
                slug, i = f"{base}-{i}", i + 1
            service_id = db.insert("services", {**data, "slug": slug, "created_at": now, "updated_at": now})
            _set_service_staff(db, service_id, staff_ids)
            audit("service.create", "service", service_id, f"{data['name']} hizmeti eklendi",
                  after={k: v for k, v in data.items() if k != "description"}, db=db)
        else:
            before = get_service(db, service_id)
            db.update("services", service_id, {**data, "updated_at": now})
            _set_service_staff(db, service_id, staff_ids)
            b, a = diff(before, data)
            if sorted(before["staff_ids"]) != sorted(set(staff_ids)):
                b["staff_ids"], a["staff_ids"] = before["staff_ids"], sorted(set(staff_ids))
            if a:
                audit("service.update", "service", service_id, f"{data['name']} hizmeti güncellendi",
                      before=b, after=a, db=db)
    return service_id


def delete_service(db: Connection, user: dict, service_id: int) -> None:
    s = get_service(db, service_id)
    if _future_active_count(db, "service_id", service_id):
        raise ValidationError({"_": "Bu hizmet için ileri tarihli aktif randevular var. Önce hizmeti pasif yapın; "
                                    "randevular tamamlanınca silebilirsiniz."})
    with db.transaction():
        db.update("services", service_id, {"deleted_at": now_iso(), "is_active": 0, "updated_at": now_iso()})
        audit("service.delete", "service", service_id, f"{s['name']} hizmeti silindi", db=db)


# ------------------------------------------------------------------ personel
def list_staff(db: Connection) -> list[dict]:
    rows = db.query("SELECT * FROM staff WHERE deleted_at IS NULL ORDER BY is_active DESC, sort_order, full_name")
    for r in rows:
        r["services"] = [x["name"] for x in db.query(
            "SELECT s.name FROM staff_services ss JOIN services s ON s.id = ss.service_id "
            "WHERE ss.staff_id = ? AND s.deleted_at IS NULL ORDER BY s.sort_order", (r["id"],))]
        r["hours"] = {h["weekday"]: h for h in db.query("SELECT * FROM working_hours WHERE staff_id = ?", (r["id"],))}
    return rows


def get_staff(db: Connection, staff_id: int) -> dict:
    s = db.one("SELECT * FROM staff WHERE id = ? AND deleted_at IS NULL", (staff_id,))
    if not s:
        raise NotFound()
    s["service_ids"] = [r["service_id"] for r in db.query("SELECT service_id FROM staff_services WHERE staff_id = ?",
                                                          (staff_id,))]
    s["hours"] = {h["weekday"]: h for h in db.query("SELECT * FROM working_hours WHERE staff_id = ?", (staff_id,))}
    s["breaks"] = db.query("SELECT * FROM staff_breaks WHERE staff_id = ? ORDER BY weekday IS NOT NULL, weekday, start_time",
                           (staff_id,))
    s["holidays"] = db.query("SELECT * FROM holidays WHERE staff_id = ? AND end_date >= ? ORDER BY start_date",
                             (staff_id, local_today().isoformat()))
    return s


def save_staff(db: Connection, user: dict, form: dict, staff_id: int | None = None) -> int:
    errors: dict[str, str] = {}
    name = (form.get("full_name") or "").strip()[:120]
    if len(name) < 2:
        errors["full_name"] = "Ad soyad gerekli."
    color = (form.get("color") or "#B08D57").strip()
    if not COLOR_RE.match(color):
        errors["color"] = "Renk geçersiz."
    phone_raw = (form.get("phone") or "").strip()
    phone = normalize_phone(phone_raw) if phone_raw else None
    if phone_raw and not phone:
        errors["phone"] = "Telefon geçersiz."
    data = {
        "full_name": name, "title": (form.get("title") or "").strip()[:120] or None, "phone": phone,
        "color": color, "bio": (form.get("bio") or "").strip()[:2000] or None,
        "is_active": int(bool(form.get("is_active"))),
        "sort_order": _int(form.get("sort_order") or 0, "sort_order", errors, "Sıra", 0, 999),
    }
    # Haftalık çalışma saatleri: day_N_open, day_N_start, day_N_end
    hours: dict[int, tuple[str, str]] = {}
    for d in range(7):
        if form.get(f"day_{d}_open"):
            st = _check_time(form.get(f"day_{d}_start", ""), f"day_{d}", errors, f"{WEEKDAYS[d]} başlangıç")
            en = _check_time(form.get(f"day_{d}_end", ""), f"day_{d}", errors, f"{WEEKDAYS[d]} bitiş")
            if st and en:
                if en <= st:
                    errors[f"day_{d}"] = f"{WEEKDAYS[d]}: bitiş saati başlangıçtan sonra olmalı."
                else:
                    hours[d] = (st, en)
    service_ids = [int(v) for v in (form.get("service_ids") or []) if str(v).isdigit()]
    if staff_id and not data["is_active"] and _future_active_count(db, "staff_id", staff_id):
        errors["is_active"] = ("Bu uzmanın ileri tarihli aktif randevuları var. Pasif yapmadan önce randevuları "
                               "başka bir uzmana aktarın veya iptal edin.")
    if errors:
        raise ValidationError(errors)

    now = now_iso()
    with db.transaction():
        if staff_id is None:
            staff_id = db.insert("staff", {**data, "created_at": now, "updated_at": now})
            before = None
        else:
            before = get_staff(db, staff_id)
            db.update("staff", staff_id, {**data, "updated_at": now})
        db.execute("DELETE FROM working_hours WHERE staff_id = ?", (staff_id,))
        for d, (st, en) in hours.items():
            db.insert("working_hours", {"staff_id": staff_id, "weekday": d, "start_time": st, "end_time": en,
                                        "created_at": now, "updated_at": now})
        valid = {r["id"] for r in db.query("SELECT id FROM services WHERE deleted_at IS NULL")}
        db.execute("DELETE FROM staff_services WHERE staff_id = ?", (staff_id,))
        for sid in sorted(set(service_ids) & valid):
            db.insert("staff_services", {"staff_id": staff_id, "service_id": sid, "created_at": now}, returning="")
        new_hours = {WEEKDAYS[d]: f"{s}-{e}" for d, (s, e) in sorted(hours.items())}
        if before is None:
            audit("staff.create", "staff", staff_id, f"{name} personel kaydı oluşturuldu",
                  after={**data, "calisma_saatleri": new_hours}, db=db)
        else:
            old_hours = {WEEKDAYS[d]: f"{h['start_time']}-{h['end_time']}" for d, h in sorted(before["hours"].items())}
            b, a = diff(before, data)
            if old_hours != new_hours:
                b["calisma_saatleri"], a["calisma_saatleri"] = old_hours, new_hours
            if sorted(before["service_ids"]) != sorted(set(service_ids) & valid):
                b["hizmetler"], a["hizmetler"] = before["service_ids"], sorted(set(service_ids) & valid)
            if a:
                audit("staff.update", "staff", staff_id, f"{name} personel kaydı güncellendi", before=b, after=a, db=db)
    return staff_id


def add_break(db: Connection, user: dict, staff_id: int, form: dict) -> None:
    get_staff(db, staff_id)
    errors: dict[str, str] = {}
    st = _check_time(form.get("start_time", ""), "break", errors, "Mola başlangıç")
    en = _check_time(form.get("end_time", ""), "break", errors, "Mola bitiş")
    wd = form.get("weekday", "")
    weekday = int(wd) if str(wd).isdigit() and 0 <= int(wd) <= 6 else None
    if st and en and en <= st:
        errors["break"] = "Mola bitişi başlangıçtan sonra olmalı."
    if errors:
        raise ValidationError(errors)
    with db.transaction():
        bid = db.insert("staff_breaks", {"staff_id": staff_id, "weekday": weekday, "start_time": st, "end_time": en,
                                         "label": (form.get("label") or "").strip()[:60] or None,
                                         "created_at": now_iso(), "updated_at": now_iso()})
        audit("staff.break_add", "staff", staff_id,
              f"Mola eklendi: {WEEKDAYS[weekday] if weekday is not None else 'Her gün'} {st}-{en}",
              after={"break_id": bid}, db=db)


def delete_break(db: Connection, user: dict, staff_id: int, break_id: int) -> None:
    b = db.one("SELECT * FROM staff_breaks WHERE id = ? AND staff_id = ?", (break_id, staff_id))
    if not b:
        raise NotFound()
    with db.transaction():
        db.execute("DELETE FROM staff_breaks WHERE id = ?", (break_id,))
        audit("staff.break_delete", "staff", staff_id, f"Mola silindi: {b['start_time']}-{b['end_time']}", db=db)


# ------------------------------------------------------------------ izin / kapalı günler
def list_holidays(db: Connection) -> list[dict]:
    return db.query("""SELECT h.*, st.full_name AS staff_name FROM holidays h LEFT JOIN staff st ON st.id = h.staff_id
                       WHERE h.end_date >= ? ORDER BY h.start_date""", (local_today().isoformat(),))


def add_holiday(db: Connection, user: dict, form: dict) -> tuple[int, int]:
    """İzin/kapalı gün ekler. (holiday_id, çakışan aktif randevu sayısı) döner."""
    errors: dict[str, str] = {}
    try:
        start = dt.date.fromisoformat(form.get("start_date", ""))
        end = dt.date.fromisoformat(form.get("end_date") or form.get("start_date", ""))
    except ValueError:
        raise ValidationError({"date": "Tarih seçin."})
    if end < start:
        errors["date"] = "Bitiş tarihi başlangıçtan önce olamaz."
    if (end - start).days > 366:
        errors["date"] = "En fazla 1 yıllık aralık girilebilir."
    staff_id = int(form["staff_id"]) if str(form.get("staff_id") or "").isdigit() else None
    if staff_id and not db.scalar("SELECT 1 FROM staff WHERE id = ? AND deleted_at IS NULL", (staff_id,)):
        errors["staff_id"] = "Personel bulunamadı."
    if errors:
        raise ValidationError(errors)
    from ..timeutil import local_day_bounds_utc
    s0, _ = local_day_bounds_utc(start)
    _, e1 = local_day_bounds_utc(end)
    where, params = "is_active_slot = 1 AND starts_at >= ? AND starts_at < ?", [s0, e1]
    if staff_id:
        where += " AND staff_id = ?"
        params.append(staff_id)
    conflicts = db.scalar(f"SELECT COUNT(*) FROM appointments WHERE {where}", params) or 0
    with db.transaction():
        hid = db.insert("holidays", {"staff_id": staff_id, "start_date": start.isoformat(), "end_date": end.isoformat(),
                                     "reason": (form.get("reason") or "").strip()[:160] or None,
                                     "created_by": user["id"], "created_at": now_iso(), "updated_at": now_iso()})
        who = db.scalar("SELECT full_name FROM staff WHERE id = ?", (staff_id,)) if staff_id else "Bütün merkez"
        audit("holiday.create", "holiday", hid, f"{who}: {start.strftime('%d.%m.%Y')} – {end.strftime('%d.%m.%Y')} "
                                                f"kapalı/izin eklendi", db=db)
    return hid, conflicts


def delete_holiday(db: Connection, user: dict, holiday_id: int) -> None:
    h = db.one("SELECT * FROM holidays WHERE id = ?", (holiday_id,))
    if not h:
        raise NotFound()
    with db.transaction():
        db.execute("DELETE FROM holidays WHERE id = ?", (holiday_id,))
        audit("holiday.delete", "holiday", holiday_id, f"İzin/kapalı gün silindi: {h['start_date']} – {h['end_date']}",
              db=db)


# ------------------------------------------------------------------ randevu ayarları
SETTING_FIELDS = {
    "booking.slot_step_minutes": ("Saat aralığı (dk)", 5, 60),
    "booking.min_notice_minutes": ("Online randevu için en az önceden (dk)", 0, 2880),
    "booking.max_days_ahead": ("Kaç gün sonrasına kadar online randevu", 1, 365),
    "no_show.alert_threshold": ("Kaç kez gelmeyince uyarı", 1, 10),
}


def get_settings(db: Connection) -> dict:
    out = {r["key"]: json_value(r["value_json"]) for r in db.query("SELECT key, value_json FROM settings")}
    return out


def save_settings(db: Connection, user: dict, form: dict) -> None:
    errors: dict[str, str] = {}
    values: dict[str, object] = {}
    for key, (label, lo, hi) in SETTING_FIELDS.items():
        values[key] = _int(form.get(key), key, errors, label, lo, hi)
    hours = []
    for part in re.split(r"[,\s]+", form.get("reminders.hours_before", "").strip()):
        if not part:
            continue
        if not part.isdigit() or not (1 <= int(part) <= 168):
            errors["reminders.hours_before"] = "Hatırlatma saatleri 1–168 arası sayılar olmalı (örn. 24, 2)."
            break
        hours.append(int(part))
    if len(hours) > 4:
        errors["reminders.hours_before"] = "En fazla 4 hatırlatma tanımlanabilir."
    values["reminders.hours_before"] = sorted(set(hours), reverse=True)
    if errors:
        raise ValidationError(errors)
    import json
    current = get_settings(db)
    changed = {k: v for k, v in values.items() if current.get(k) != v}
    if not changed:
        return
    with db.transaction():
        for k, v in changed.items():
            if db.scalar("SELECT 1 FROM settings WHERE key = ?", (k,)):
                db.execute("UPDATE settings SET value_json = ?, updated_by = ?, updated_at = ? WHERE key = ?",
                           (json.dumps(v), user["id"], now_iso(), k))
            else:
                db.insert("settings", {"key": k, "value_json": json.dumps(v), "updated_by": user["id"],
                                       "updated_at": now_iso()}, returning="")
        audit("settings.update", "settings", None, "Randevu ayarları güncellendi",
              before={k: current.get(k) for k in changed}, after=changed, db=db)
