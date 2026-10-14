"""Demo verisi. Bütün kişiler hayalîdir; telefonlar +90 599 000 xx xx aralığındadır
(Türkiye'de tahsis edilmemiş, gerçek kişilere ait olmayan aralık) ve müşteriler is_demo=1 işaretlidir.
Üretim WhatsApp provider'ı is_demo müşterilere mesaj göndermez (Faz 8)."""
from __future__ import annotations

import datetime as dt
import json
import secrets

from .db import Connection
from .security.passwords import hash_password
from .textutil import fold
from .timeutil import TZ, iso, local_today, now_iso, now_utc

SERVICES = [
    # name, slug, category, duration, price TL, campaign TL, repeat days, tracks_sessions, short
    ("Lazer Epilasyon", "lazer-epilasyon", "Lazer", 30, 1200, 999, 21, 1,
     "Buz başlıklı diode lazer ile konforlu, kalıcı epilasyon."),
    ("Cilt Bakımı", "cilt-bakimi", "Cilt", 60, 1500, None, 20, 0,
     "Cilt tipinize özel derin temizlik ve nem bakımı."),
    ("Hydrafacial", "hydrafacial", "Cilt", 50, 2500, 2100, 30, 0,
     "Vakum ve serum infüzyonu ile anında ışıltı."),
    ("Bölgesel İncelme", "bolgesel-incelme", "Vücut", 45, 1100, None, 7, 1,
     "Hedef bölgelere yönelik sıkılaştırma ve incelme protokolü."),
    ("Kaş Tasarımı", "kas-tasarimi", "Kaş & Kirpik", 30, 450, None, 30, 0,
     "Yüz hatlarınıza uygun kaş şekillendirme."),
    ("Kirpik Lifting", "kirpik-lifting", "Kaş & Kirpik", 45, 900, None, 30, 0,
     "Doğal kirpiklerinize kalıcı kıvrım ve hacim."),
    ("Kalıcı Makyaj", "kalici-makyaj", "Kalıcı Makyaj", 120, 6500, None, 45, 0,
     "Microblading ve dudak renklendirme uygulamaları."),
    ("Manikür", "manikur", "El & Ayak", 45, 500, None, 21, 0,
     "Bakımlı eller için profesyonel manikür."),
    ("Pedikür", "pedikur", "El & Ayak", 60, 650, None, 30, 0,
     "Ayak sağlığı ve estetiği için pedikür."),
]

STAFF = [
    # name, title, color, weekdays(0=Pzt), start, end
    ("Şirin Mercan", "Kurucu · Uzman Estetisyen", "#B08D57", [0, 1, 2, 3, 4, 5], "09:00", "18:00"),
    ("Uzman 2", "Lazer & Cilt Uzmanı", "#8E6C8A", [1, 2, 3, 4, 5], "10:00", "19:00"),
    ("Uzman 3", "Kaş, Kirpik & Tırnak", "#5F7F73", [0, 1, 2, 3, 4], "09:00", "17:00"),
]

STAFF_SERVICES = {
    "Şirin Mercan": ["lazer-epilasyon", "cilt-bakimi", "hydrafacial", "bolgesel-incelme", "kalici-makyaj", "kas-tasarimi"],
    "Uzman 2": ["lazer-epilasyon", "cilt-bakimi", "hydrafacial", "bolgesel-incelme"],
    "Uzman 3": ["kas-tasarimi", "kirpik-lifting", "manikur", "pedikur", "kalici-makyaj"],
}

CUSTOMERS = [
    ("Ayşe", "Yılmaz", "1992-03-18"), ("Zeynep", "Kaya", "1988-10-21"), ("Elif", "Demir", "1995-07-02"),
    ("Merve", "Şahin", "1990-11-30"), ("Büşra", "Çelik", "1997-01-14"), ("Selin", "Arslan", "1993-10-16"),
    ("Derya", "Koç", "1985-05-09"), ("Gizem", "Öztürk", "1999-08-25"), ("Ceren", "Aydın", "1991-02-03"),
    ("Esra", "Yıldız", "1987-12-12"), ("Melis", "Polat", "1996-04-27"), ("İrem", "Kurt", "1994-09-08"),
    ("Damla", "Özkan", "1998-06-19"), ("Nazlı", "Erdem", "1989-10-29"),
]

TEMPLATES = [
    ("appointment_created", "Randevu oluşturuldu", "service",
     "Merhaba {ad} Hanım 🌸\nVanessa Güzellik Merkezi'nde {tarih} {saat} için {hizmet} randevunuz oluşturuldu.", None),
    ("appointment_reminder", "Randevu hatırlatma", "service",
     "Merhaba {ad} Hanım 🌸\nVanessa Güzellik Merkezi'ndeki {hizmet} randevunuz {gun} saat {saat}'dadır.\n"
     "Randevunuzu onaylıyor musunuz?",
     [{"id": "CONFIRM", "title": "ONAYLIYORUM"}, {"id": "CANCEL", "title": "GELEMEYECEĞİM"},
      {"id": "RESCHEDULE", "title": "SAATİ DEĞİŞTİR"}]),
    ("appointment_confirmed", "Randevu onaylandı", "service",
     "Teşekkürler {ad} Hanım, {tarih} {saat} randevunuz onaylandı. Görüşmek üzere 🌸", None),
    ("appointment_cancelled", "Randevu iptal", "service",
     "{ad} Hanım, {tarih} {saat} randevunuz iptal edildi. Yeni randevu için bize yazabilirsiniz.", None),
    ("appointment_rescheduled", "Yeni randevu zamanı", "service",
     "{ad} Hanım, randevunuz {tarih} {saat} olarak güncellendi.", None),
    ("followup_skin_care", "Cilt bakımı zamanı", "service",
     "Merhaba {ad} Hanım 🌸\nVanessa Güzellik Merkezi'ndeki son cilt bakımınızın üzerinden {gun_sayisi} gün geçti.\n"
     "Yeni bakım zamanınız geldi. Randevunuzu oluşturmak ister misiniz?",
     [{"id": "BOOK", "title": "RANDEVU AL"}]),
    ("followup_laser_session", "Lazer seans zamanı", "service",
     "Merhaba {ad} Hanım 🌸\n{hizmet} {seans_no}. seansınızın zamanı geldi. Randevunuzu oluşturalım mı?",
     [{"id": "BOOK", "title": "RANDEVU AL"}]),
    ("birthday", "Doğum günü", "marketing",
     "Vanessa Güzellik Merkezi ailesi olarak doğum gününüzü kutlarız. 🌸{kampanya_kodu}", None),
    ("campaign", "Kampanya", "marketing", "{mesaj}", None),
    ("custom", "Özel mesaj", "service", "{mesaj}", None),
]

SETTINGS = {
    "business.name": "Vanessa Güzellik Merkezi",
    "business.owner": "Şirin Mercan",
    "business.phone": "+905331504303",
    "business.address": "Mithat Paşa Caddesi, Ersan Taksi Çaprazı, İskenderun / Hatay",
    "business.currency": "TRY",
    "business.timezone": "Europe/Istanbul",
    "reminders.hours_before": [24, 2],
    "reminders.channel": "whatsapp",
    "booking.slot_step_minutes": 15,
    "booking.min_notice_minutes": 60,
    "booking.max_days_ahead": 60,
    "no_show.alert_threshold": 2,
}


def _local(day: dt.date, hhmm: str) -> dt.datetime:
    h, m = map(int, hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(h, m), tzinfo=TZ)


def has_data(db: Connection) -> bool:
    return bool(db.scalar("SELECT COUNT(*) FROM users"))


def seed_settings_and_templates(db: Connection) -> None:
    now = now_iso()
    for key, value in SETTINGS.items():
        if not db.scalar("SELECT 1 FROM settings WHERE key = ?", (key,)):
            db.insert("settings", {"key": key, "value_json": json.dumps(value, ensure_ascii=False),
                                  "updated_at": now}, returning="")
    for key, name, cat, body, buttons in TEMPLATES:
        if not db.scalar("SELECT 1 FROM message_templates WHERE key = ?", (key,)):
            db.insert("message_templates", {"key": key, "name": name, "category": cat, "body": body,
                                            "buttons_json": buttons, "created_at": now, "updated_at": now})


def seed_demo(db: Connection, password: str | None = None) -> dict[str, str]:
    """Demo verisini yükler. Kullanıcı adı → şifre sözlüğü döner."""
    today = local_today()
    now = now_iso()
    creds: dict[str, str] = {}

    with db.transaction():
        seed_settings_and_templates(db)

        svc: dict[str, dict] = {}
        for i, (name, slug, cat, dur, price, camp, rep, tracks, short) in enumerate(SERVICES):
            sid = db.insert("services", {
                "name": name, "slug": slug, "category": cat, "short_description": short,
                "description": short, "duration_minutes": dur, "price_kurus": price * 100,
                "campaign_price_kurus": camp * 100 if camp else None, "repeat_interval_days": rep,
                "tracks_sessions": tracks, "sort_order": i, "created_at": now, "updated_at": now,
            })
            svc[slug] = {"id": sid, "duration": dur, "price": (camp or price) * 100, "name": name}

        staff: dict[str, int] = {}
        for i, (name, title, color, days, start, end) in enumerate(STAFF):
            st = db.insert("staff", {"full_name": name, "title": title, "color": color, "sort_order": i,
                                     "created_at": now, "updated_at": now})
            staff[name] = st
            for d in days:
                db.insert("working_hours", {"staff_id": st, "weekday": d, "start_time": start,
                                            "end_time": end, "created_at": now, "updated_at": now})
            db.insert("staff_breaks", {"staff_id": st, "weekday": None, "start_time": "12:30",
                                       "end_time": "13:15", "label": "Öğle arası",
                                       "created_at": now, "updated_at": now})
            for slug in STAFF_SERVICES[name]:
                db.insert("staff_services", {"staff_id": st, "service_id": svc[slug]["id"], "created_at": now},
                          returning="")

        users = [("sirin", "Şirin Mercan", "admin", staff["Şirin Mercan"]),
                 ("resepsiyon", "Resepsiyon", "reception", None),
                 ("uzman2", "Uzman 2", "specialist", staff["Uzman 2"])]
        user_ids = {}
        for username, full, role, st in users:
            pw = password or secrets.token_urlsafe(9)
            creds[username] = pw
            user_ids[username] = db.insert("users", {
                "username": username, "full_name": full, "role": role, "staff_id": st,
                "password_hash": hash_password(pw), "password_changed_at": now,
                "created_at": now, "updated_at": now,
            })
        admin_id = user_ids["sirin"]

        cust: dict[str, int] = {}
        for i, (first, last, birth) in enumerate(CUSTOMERS):
            created = iso(now_utc() - dt.timedelta(days=120 - i * 7))
            cid = db.insert("customers", {
                "first_name": first, "last_name": last, "search_name": fold(f"{first} {last}"),
                "phone": f"+905990000{i + 1:03d}",
                "birth_date": birth, "source": "online" if i % 3 == 0 else "panel", "is_demo": 1,
                "created_by": admin_id, "created_at": created, "updated_at": created,
            })
            cust[f"{first} {last}"] = cid
            db.insert("customer_consents", {"customer_id": cid, "channel": "whatsapp", "category": "service",
                                            "granted": 1, "source": "paper_form", "evidence": "Demo kayıt formu v1",
                                            "recorded_by": admin_id, "created_at": created}, returning="")
            if i % 2 == 0:
                db.insert("customer_consents", {"customer_id": cid, "channel": "whatsapp", "category": "marketing",
                                                "granted": 1, "source": "paper_form",
                                                "evidence": "Demo kayıt formu v1 — kampanya onayı",
                                                "recorded_by": admin_id, "created_at": created}, returning="")

        # Ayşe Yılmaz — 10 seanslık lazer paketi, iki bölge
        ayse = cust["Ayşe Yılmaz"]
        purchased = iso(_local(today - dt.timedelta(days=63), "10:00"))
        pkg = db.insert("packages", {"service_id": svc["lazer-epilasyon"]["id"], "name": "10 Seans Lazer Epilasyon",
                                     "session_count": 10, "price_kurus": 499900, "session_interval_days": 21,
                                     "created_at": now, "updated_at": now})
        cp = db.insert("customer_packages", {
            "customer_id": ayse, "package_id": pkg, "service_id": svc["lazer-epilasyon"]["id"],
            "name": "10 Seans Lazer Epilasyon", "total_price_kurus": 499900, "status": "active",
            "purchased_at": purchased, "created_by": admin_id, "created_at": purchased, "updated_at": purchased,
        })
        areas = {}
        for j, area in enumerate(["Tüm Bacak", "Koltuk Altı"]):
            areas[area] = db.insert("session_areas", {"customer_package_id": cp, "area_name": area,
                                                      "total_sessions": 10, "sort_order": j,
                                                      "created_at": purchased, "updated_at": purchased})

        appts: list[dict] = []

        def add_appt(customer: str, staff_name: str, slug: str, day: dt.date, hhmm: str, status: str,
                     source: str = "panel", package: int | None = None) -> int:
            start = _local(day, hhmm)
            end = start + dt.timedelta(minutes=svc[slug]["duration"])
            active = status in ("pending", "confirmed", "arrived", "reschedule_needed")
            created = iso(start - dt.timedelta(days=5))
            aid = db.insert("appointments", {
                "customer_id": cust[customer], "staff_id": staff[staff_name], "service_id": svc[slug]["id"],
                "customer_package_id": package, "starts_at": iso(start), "ends_at": iso(end), "status": status,
                "is_active_slot": int(active), "source": source, "price_kurus": svc[slug]["price"],
                "created_by": admin_id if source == "panel" else None,
                "confirmed_at": created if status in ("confirmed", "arrived", "completed") else None,
                "cancelled_at": iso(start - dt.timedelta(hours=20)) if status == "cancelled" else None,
                "completed_at": iso(end) if status == "completed" else None,
                "created_at": created, "updated_at": created,
            })
            db.insert("appointment_status_history", {"appointment_id": aid, "from_status": None,
                                                     "to_status": "pending", "changed_by": admin_id,
                                                     "channel": source, "created_at": created})
            if status != "pending":
                db.insert("appointment_status_history", {
                    "appointment_id": aid, "from_status": "pending", "to_status": status,
                    "changed_by": admin_id, "channel": "panel", "created_at": iso(start - dt.timedelta(hours=1)),
                })
            appts.append({"id": aid, "customer": customer, "slug": slug, "start": start, "status": status})
            return aid

        # Ayşe'nin geçmiş 3 seansı + bugün 14:30 4. seans
        for n, back in enumerate([63, 42, 21], start=1):
            day = today - dt.timedelta(days=back)
            while day.weekday() == 6:
                day -= dt.timedelta(days=1)
            aid = add_appt("Ayşe Yılmaz", "Şirin Mercan", "lazer-epilasyon", day, "11:00", "completed", package=cp)
            for area, area_id in areas.items():
                db.insert("sessions", {"customer_package_id": cp, "session_area_id": area_id, "session_number": n,
                                       "status": "completed", "appointment_id": aid, "staff_id": staff["Şirin Mercan"],
                                       "performed_at": iso(_local(day, "11:30")),
                                       "notes": "Ayarlar tolere edildi." if n < 3 else "Hafif kızarıklık, soğuk uygulama önerildi.",
                                       "created_at": now, "updated_at": now})

        work_day = today if today.weekday() != 6 else today + dt.timedelta(days=1)
        tomorrow = work_day + dt.timedelta(days=1)
        if tomorrow.weekday() == 6:
            tomorrow += dt.timedelta(days=1)

        # Bugün
        add_appt("Zeynep Kaya", "Şirin Mercan", "cilt-bakimi", work_day, "09:30", "completed")
        add_appt("Elif Demir", "Uzman 3", "kas-tasarimi", work_day, "10:00", "completed")
        add_appt("Merve Şahin", "Şirin Mercan", "hydrafacial", work_day, "11:00", "arrived")
        add_appt("Büşra Çelik", "Uzman 3", "manikur", work_day, "11:00", "confirmed")
        add_appt("Selin Arslan", "Uzman 2", "lazer-epilasyon", work_day, "13:30", "confirmed", source="online")
        add_appt("Ayşe Yılmaz", "Şirin Mercan", "lazer-epilasyon", work_day, "14:30", "confirmed", package=cp)
        add_appt("Derya Koç", "Uzman 3", "kirpik-lifting", work_day, "14:00", "pending", source="online")
        add_appt("Gizem Öztürk", "Uzman 2", "cilt-bakimi", work_day, "15:30", "confirmed")
        add_appt("Ceren Aydın", "Şirin Mercan", "kas-tasarimi", work_day, "16:00", "cancelled")
        add_appt("Esra Yıldız", "Uzman 2", "hydrafacial", work_day, "17:00", "pending", source="online")
        add_appt("Melis Polat", "Şirin Mercan", "bolgesel-incelme", work_day, "16:30", "confirmed")
        # Yarın
        add_appt("İrem Kurt", "Şirin Mercan", "cilt-bakimi", tomorrow, "10:00", "pending")
        add_appt("Damla Özkan", "Uzman 2", "lazer-epilasyon", tomorrow, "11:00", "confirmed", source="online")
        add_appt("Nazlı Erdem", "Uzman 3", "pedikur", tomorrow, "13:30", "pending")
        add_appt("Zeynep Kaya", "Uzman 3", "kirpik-lifting", tomorrow, "15:00", "confirmed")
        # Geçmiş (bu ay ve önceki)
        past = [("Elif Demir", "Şirin Mercan", "cilt-bakimi", 24, "10:00", "completed"),
                ("Merve Şahin", "Uzman 3", "manikur", 12, "11:00", "completed"),
                ("Büşra Çelik", "Uzman 2", "hydrafacial", 9, "15:00", "completed"),
                ("Gizem Öztürk", "Şirin Mercan", "kas-tasarimi", 6, "09:30", "no_show"),
                ("Nazlı Erdem", "Uzman 2", "cilt-bakimi", 5, "16:00", "completed"),
                ("Damla Özkan", "Uzman 3", "pedikur", 3, "14:00", "cancelled"),
                ("Esra Yıldız", "Şirin Mercan", "cilt-bakimi", 2, "13:30", "completed"),
                ("İrem Kurt", "Uzman 3", "kirpik-lifting", 1, "10:30", "completed")]
        for c, s, slug, back, hhmm, status in past:
            day = today - dt.timedelta(days=back)
            if day.weekday() == 6:
                day -= dt.timedelta(days=1)
            add_appt(c, s, slug, day, hhmm, status)
        db.execute("UPDATE customers SET no_show_count = 1 WHERE id = ?", (cust["Gizem Öztürk"],))

        # Ödemeler
        pay = db.insert("payments", {"customer_id": ayse, "customer_package_id": cp,
                                     "description": "10 Seans Lazer Epilasyon paketi", "total_kurus": 499900,
                                     "paid_kurus": 300000, "status": "partial", "created_by": admin_id,
                                     "created_at": purchased, "updated_at": purchased})
        for amount, method, back in [(200000, "cash", 63), (100000, "card", 21)]:
            db.insert("payment_transactions", {"payment_id": pay, "amount_kurus": amount, "method": method,
                                               "paid_at": iso(_local(today - dt.timedelta(days=back), "11:40")),
                                               "recorded_by": admin_id, "created_at": now})
        for a in appts:
            if a["status"] == "completed" and a["slug"] != "lazer-epilasyon":
                price = svc[a["slug"]]["price"]
                p = db.insert("payments", {"customer_id": cust[a["customer"]], "appointment_id": a["id"],
                                           "description": svc[a["slug"]]["name"], "total_kurus": price,
                                           "paid_kurus": price, "status": "paid", "created_by": admin_id,
                                           "created_at": iso(a["start"]), "updated_at": iso(a["start"])})
                db.insert("payment_transactions", {"payment_id": p, "amount_kurus": price,
                                                   "method": "card" if a["id"] % 2 else "cash",
                                                   "paid_at": iso(a["start"] + dt.timedelta(hours=1)),
                                                   "recorded_by": admin_id, "created_at": now})

        db.insert("customer_notes", {"customer_id": ayse, "author_user_id": admin_id, "is_pinned": 1,
                                     "body": "Bir sonraki seansta paket yenilemesi görüşülecek.",
                                     "created_at": now, "updated_at": now})
        db.insert("notifications", {"user_id": None, "kind": "no_show", "title": "Gizem Öztürk randevusuna gelmedi",
                                    "body": "Kaş Tasarımı randevusu GELMEDİ olarak işaretlendi.",
                                    "customer_id": cust["Gizem Öztürk"], "created_at": now})
    return creds
