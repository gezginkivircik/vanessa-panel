"""FAZ 4 testleri: uygun saat hesaplama, randevu oluşturma, çakışma, iptal, durumlar, erteleme,
seans düşümü, paket tamamlanma, tekrar tarihi hesaplama, yetkiler."""
import datetime as dt
import re

from app.errors import ValidationError
from app.services import appointments as svc
from app.timeutil import local_today
from tests.helpers import AppTestCase


def future_weekday(weekday: int, min_days: int = 8) -> dt.date:
    d = local_today() + dt.timedelta(days=min_days)
    while d.weekday() != weekday:
        d += dt.timedelta(days=1)
    return d


class Base(AppTestCase):
    def setUp(self):
        super().setUp()
        q = lambda sql, *p: self.db.scalar(sql, p)
        self.sirin = q("SELECT id FROM staff WHERE full_name = 'Şirin Mercan'")
        self.uzman2 = q("SELECT id FROM staff WHERE full_name = 'Uzman 2'")
        self.uzman3 = q("SELECT id FROM staff WHERE full_name = 'Uzman 3'")
        self.cilt = q("SELECT id FROM services WHERE slug = 'cilt-bakimi'")
        self.lazer = q("SELECT id FROM services WHERE slug = 'lazer-epilasyon'")
        self.ayse = q("SELECT id FROM customers WHERE first_name = 'Ayşe'")
        self.elif_ = q("SELECT id FROM customers WHERE first_name = 'Elif'")
        self.wed = future_weekday(2)
        self.admin = self.db.one("SELECT * FROM users WHERE username = 'sirin'")

    def book(self, customer, service, staff, day, time, status="confirmed"):
        from flask import g
        with self.app.test_request_context():
            g.user = self.admin
            return svc.create(self.db, self.admin, {"customer_id": customer, "service_id": service,
                                                    "staff_id": staff, "date": day.isoformat(), "time": time,
                                                    "status": status})

    def status(self, aid, st):
        with self.app.test_request_context():
            svc.change_status(self.db, self.admin, aid, st)

    def slots(self, staff, service, day, **kw):
        return svc.available_slots(self.db, staff, service, day, **kw)


class SlotTests(Base):
    def test_working_hours_breaks_and_duration(self):
        s = self.slots(self.sirin, self.cilt, self.wed)   # 09:00–18:00, mola 12:30–13:15, 60 dk
        self.assertEqual(s[0], "09:00")
        self.assertEqual(s[-1], "17:00")                  # 17:00 + 60 dk = 18:00 sığar
        self.assertIn("11:30", s)                         # 11:30–12:30, molaya değmez
        self.assertNotIn("11:45", s)                      # 11:45–12:45 molaya çakışır
        self.assertNotIn("12:30", s)
        self.assertIn("13:15", s)

    def test_closed_days_and_holidays(self):
        sunday = future_weekday(6)
        self.assertEqual(self.slots(self.sirin, self.cilt, sunday), [])
        self.assertEqual(self.slots(self.uzman2, self.cilt, future_weekday(0)), [])   # Uzman 2 Pazartesi çalışmaz
        self.db.insert("holidays", {"staff_id": self.sirin, "start_date": self.wed.isoformat(),
                                    "end_date": self.wed.isoformat(), "reason": "İzin"})
        self.assertEqual(self.slots(self.sirin, self.cilt, self.wed), [])
        self.assertTrue(self.slots(self.uzman2, self.cilt, self.wed))
        self.db.insert("holidays", {"staff_id": None, "start_date": self.wed.isoformat(),
                                    "end_date": self.wed.isoformat(), "reason": "Bayram"})
        self.assertEqual(self.slots(self.uzman2, self.cilt, self.wed), [])

    def test_past_days_have_no_slots(self):
        self.assertEqual(self.slots(self.sirin, self.cilt, local_today() - dt.timedelta(days=1)), [])


class BookingTests(Base):
    def test_create_records_history_and_audit(self):
        aid = self.book(self.elif_, self.cilt, self.sirin, self.wed, "10:00")
        a = self.db.one("SELECT * FROM appointments WHERE id = ?", (aid,))
        self.assertEqual((a["status"], a["is_active_slot"], a["price_kurus"]), ("confirmed", 1, 150000))
        self.assertTrue(a["starts_at"].endswith("07:00:00Z"))      # 10:00 İstanbul = 07:00 UTC
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM appointment_status_history WHERE appointment_id = ?", (aid,)), 1)
        self.assertIsNotNone(self.db.one("SELECT 1 FROM audit_logs WHERE action = 'appointment.create' AND entity_id = ?", (aid,)))

    def test_same_staff_same_time_blocked(self):
        self.book(self.elif_, self.cilt, self.sirin, self.wed, "10:00")
        with self.assertRaises(ValidationError):
            self.book(self.ayse, self.cilt, self.sirin, self.wed, "10:00")
        with self.assertRaises(ValidationError):          # kısmi çakışma: 09:30–10:30
            self.book(self.ayse, self.cilt, self.sirin, self.wed, "09:30")
        self.book(self.ayse, self.cilt, self.uzman2, self.wed, "10:00")   # başka uzman: serbest
        self.assertNotIn("10:00", self.slots(self.sirin, self.cilt, self.wed))

    def test_cancel_frees_slot(self):
        aid = self.book(self.elif_, self.cilt, self.sirin, self.wed, "14:00")
        self.assertNotIn("14:00", self.slots(self.sirin, self.cilt, self.wed))
        self.status(aid, "cancelled")
        a = self.db.one("SELECT status, is_active_slot, cancelled_at FROM appointments WHERE id = ?", (aid,))
        self.assertEqual((a["status"], a["is_active_slot"]), ("cancelled", 0))
        self.assertIsNotNone(a["cancelled_at"])
        self.assertIn("14:00", self.slots(self.sirin, self.cilt, self.wed))
        self.book(self.ayse, self.cilt, self.sirin, self.wed, "14:00")

    def test_staff_must_offer_service(self):
        with self.assertRaises(ValidationError) as ctx:
            self.book(self.elif_, self.lazer, self.uzman3, self.wed, "10:00")
        self.assertIn("staff_id", ctx.exception.errors)

    def test_invalid_transitions_and_no_show(self):
        aid = self.book(self.elif_, self.cilt, self.sirin, self.wed, "15:00")
        self.status(aid, "no_show")
        self.assertEqual(self.db.scalar("SELECT no_show_count FROM customers WHERE id = ?", (self.elif_,)), 1)
        with self.assertRaises(ValidationError):
            self.status(aid, "confirmed")
        # 2. gelmeme → yöneticiye bildirim (eşik 2)
        aid2 = self.book(self.elif_, self.cilt, self.sirin, self.wed, "16:00")
        self.status(aid2, "no_show")
        self.assertTrue(self.db.scalar("SELECT COUNT(*) FROM notifications WHERE kind = 'no_show_repeat' AND customer_id = ?",
                                       (self.elif_,)))

    def test_reschedule(self):
        aid = self.book(self.elif_, self.cilt, self.sirin, self.wed, "10:00")
        other = self.book(self.ayse, self.cilt, self.sirin, self.wed, "14:00")
        with self.app.test_request_context():
            with self.assertRaises(ValidationError):
                svc.reschedule(self.db, self.admin, aid, {"date": self.wed.isoformat(), "time": "14:00"})
            svc.reschedule(self.db, self.admin, aid, {"date": self.wed.isoformat(), "time": "16:00"})
        s = self.slots(self.sirin, self.cilt, self.wed)
        self.assertIn("10:00", s)          # eski saat açıldı
        self.assertNotIn("16:00", s)
        log = self.db.one("SELECT summary FROM audit_logs WHERE action = 'appointment.reschedule' AND entity_id = ?", (aid,))
        self.assertIn("10:00", log["summary"])
        self.assertIn("16:00", log["summary"])
        self.assertTrue(other)


class CompletionTests(Base):
    def test_laser_session_decrements_and_package_completes(self):
        cp = self.db.scalar("SELECT id FROM customer_packages WHERE customer_id = ?", (self.ayse,))
        aid = self.book(self.ayse, self.lazer, self.sirin, self.wed, "09:00")
        self.assertEqual(self.db.scalar("SELECT customer_package_id FROM appointments WHERE id = ?", (aid,)), cp)
        self.status(aid, "arrived")
        self.status(aid, "completed")
        rows = self.db.query("""SELECT sa.area_name, COUNT(s.id) AS done FROM session_areas sa
                                LEFT JOIN sessions s ON s.session_area_id = sa.id AND s.status = 'completed'
                                WHERE sa.customer_package_id = ? GROUP BY sa.area_name""", (cp,))
        self.assertEqual({r["area_name"]: r["done"] for r in rows}, {"Tüm Bacak": 4, "Koltuk Altı": 4})
        # Kalan 6 seansı tamamla → paket tamamlandı (10/10)
        times = ["09:30", "10:00", "10:30", "11:00", "11:30", "13:30"]
        for t in times:
            a = self.book(self.ayse, self.lazer, self.sirin, self.wed, t)
            self.status(a, "completed")
        self.assertEqual(self.db.scalar("SELECT status FROM customer_packages WHERE id = ?", (cp,)), "completed")
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM sessions WHERE customer_package_id = ? AND status = 'completed'",
                                        (cp,)), 20)
        # Paket tamamlandıktan sonraki lazer randevusu pakete bağlanmaz
        a = self.book(self.ayse, self.lazer, self.sirin, self.wed, "14:00")
        self.assertIsNone(self.db.scalar("SELECT customer_package_id FROM appointments WHERE id = ?", (a,)))

    def test_skin_care_next_date_is_20_days_later(self):
        aid = self.book(self.elif_, self.cilt, self.sirin, self.wed, "10:00")
        self.status(aid, "completed")
        f = self.db.one("SELECT * FROM service_followups WHERE customer_id = ? AND service_id = ?", (self.elif_, self.cilt))
        self.assertEqual(f["last_done_on"], self.wed.isoformat())
        self.assertEqual(f["due_on"], (self.wed + dt.timedelta(days=20)).isoformat())
        # Tekrar süresi panelden değişebilir (sabit kod değil)
        self.db.execute("UPDATE services SET repeat_interval_days = 25 WHERE id = ?", (self.cilt,))
        aid2 = self.book(self.elif_, self.cilt, self.sirin, self.wed, "14:00")
        self.status(aid2, "completed")
        self.assertEqual(self.db.scalar("SELECT due_on FROM service_followups WHERE customer_id = ? AND service_id = ?",
                                        (self.elif_, self.cilt)), (self.wed + dt.timedelta(days=25)).isoformat())


class HttpTests(Base):
    def test_full_http_flow(self):
        self.login()
        r = self.client.get(f"/api/randevu/uygun-saatler?hizmet={self.cilt}&tarih={self.wed}&uzman={self.sirin}")
        self.assertEqual(r.json["staff"][0]["slots"][0], "09:00")
        tok = self.csrf()
        r = self.client.post("/panel/randevular/yeni", data={
            "_csrf": tok, "customer_id": self.elif_, "service_id": self.cilt, "staff_id": self.sirin,
            "date": self.wed.isoformat(), "time": "09:00", "status": "pending"})
        self.assertEqual(r.status_code, 302)
        aid = int(re.search(r"/(\d+)$", r.headers["Location"]).group(1))
        html = self.client.get(f"/panel/randevular/{aid}").text
        self.assertIn("Elif Demir", html)
        self.assertIn("Onayla", html)
        self.client.post(f"/panel/randevular/{aid}/durum", data={"_csrf": tok, "status": "confirmed"})
        self.assertEqual(self.db.scalar("SELECT status FROM appointments WHERE id = ?", (aid,)), "confirmed")
        cal = self.client.get(f"/panel/randevular?tarih={self.wed}").text
        self.assertIn("Elif Demir", cal)
        self.assertEqual(self.client.get(f"/panel/randevular?gorunum=hafta&tarih={self.wed}").status_code, 200)
        self.assertEqual(self.client.get(f"/panel/randevular?gorunum=ay&tarih={self.wed}").status_code, 200)

    def test_specialist_read_only_and_scoped(self):
        aid = self.book(self.elif_, self.cilt, self.sirin, self.wed, "10:00")
        self.login("uzman2")
        cal = self.client.get(f"/panel/randevular?tarih={self.wed}").text
        self.assertNotIn("Elif Demir", cal)
        self.assertEqual(self.client.get(f"/panel/randevular/{aid}").status_code, 404)
        self.assertEqual(self.client.get("/panel/randevular/yeni").status_code, 403)
        self.assertEqual(self.client.get(f"/api/randevu/uygun-saatler?hizmet={self.cilt}&tarih={self.wed}").status_code, 403)

    def test_reception_can_book(self):
        self.login("resepsiyon")
        tok = self.csrf()
        r = self.client.post("/panel/randevular/yeni", data={
            "_csrf": tok, "customer_id": self.elif_, "service_id": self.cilt, "staff_id": self.uzman2,
            "date": self.wed.isoformat(), "time": "11:00", "status": "confirmed"})
        self.assertEqual(r.status_code, 302)
