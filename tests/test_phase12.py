"""FAZ 12 testleri: web sitesi, online randevu sihirbazı, imzalı randevu bağlantısı, iptal, güvenlik."""
import datetime as dt
import re

from app.timeutil import local_today, set_clock, now_utc
from tests.helpers import AppTestCase
from tests.test_phase4 import future_weekday

PHONE = "0599 000 09 01"


class Base(AppTestCase):
    def setUp(self):
        super().setUp()
        self.wed = future_weekday(2)
        self.cilt = self.db.scalar("SELECT id FROM services WHERE slug = 'cilt-bakimi'")
        self.sirin = self.db.scalar("SELECT id FROM staff WHERE full_name = 'Şirin Mercan'")

    def page(self, client=None, **params):
        client = client or self.client
        return client.get("/randevu", query_string=params)

    def form_data(self, **kw):
        r = self.page(hizmet="cilt-bakimi", uzman=kw.get("uzman", self.sirin), tarih=self.wed.isoformat(),
                      saat=kw.get("saat", "10:00"))
        m = re.search(r'name="_csrf" value="([^"]+)"', r.text)
        if m:
            self._csrf = m.group(1)
        if not getattr(self, "_csrf", None):
            r = self.page(hizmet="cilt-bakimi", uzman=self.sirin, tarih=self.wed.isoformat(), saat="17:00")
            self._csrf = re.search(r'name="_csrf" value="([^"]+)"', r.text).group(1)
        data = {"_csrf": self._csrf, "hizmet": "cilt-bakimi", "uzman": str(self.sirin),
                "tarih": self.wed.isoformat(), "saat": "10:00", "first_name": "deneme", "last_name": "müşteri",
                "phone": PHONE, "kvkk": "1", "consent_service": "1"}
        data.update(kw)
        return data

    def book(self, **kw):
        return self.client.post("/randevu", data=self.form_data(**kw))


class SiteTests(Base):
    def test_home_and_pages(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        for text in ["Lazer Epilasyon", "0533 150 43 03", "Mithat Paşa", "application/ld+json", "Randevu Al",
                     "Kampanya", "output=embed"]:
            self.assertIn(text, r.text)
        self.assertIn("Cuma", r.text)                                   # çalışma saatleri tablosu
        self.assertEqual(self.client.get("/hizmetler").status_code, 200)
        r = self.client.get("/hizmetler/lazer-epilasyon")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Seanslı uygulama", r.text)
        self.assertEqual(self.client.get("/hizmetler/yok-boyle").status_code, 404)
        self.assertIn("KVKK", self.client.get("/kvkk").text)
        self.assertIn("Disallow: /panel", self.client.get("/robots.txt").text)
        self.assertIn("/hizmetler/hydrafacial", self.client.get("/sitemap.xml").text)

    def test_inactive_service_hidden(self):
        self.db.execute("UPDATE services SET is_active = 0 WHERE slug = 'pedikur'")
        self.assertNotIn("Pedikür", self.client.get("/hizmetler").text)
        self.assertEqual(self.client.get("/hizmetler/pedikur").status_code, 404)

    def test_no_secrets_in_public_html(self):
        html = self.client.get("/").text + self.page(hizmet="cilt-bakimi", uzman=0).text
        for bad in ["SECRET", "password", "DATABASE_URL", "token_hash"]:
            self.assertNotIn(bad, html)


class WizardTests(Base):
    def test_steps(self):
        r = self.page()
        self.assertIn("Hydrafacial", r.text)
        r = self.page(hizmet="cilt-bakimi")
        self.assertIn("Fark etmez", r.text)
        self.assertIn("Uzman 2", r.text)
        r = self.page(hizmet="cilt-bakimi", uzman=self.sirin)
        self.assertIn('class="day"', r.text)
        r = self.page(hizmet="cilt-bakimi", uzman=self.sirin, tarih=self.wed.isoformat())
        self.assertIn(">09:00<", r.text)
        self.assertIn(">10:00<", r.text)
        self.assertNotIn(">12:30<", r.text)                            # öğle molası
        r = self.page(hizmet="cilt-bakimi", uzman=self.sirin, tarih=self.wed.isoformat(), saat="10:00")
        self.assertIn('name="first_name"', r.text)
        self.assertIn('name="website"', r.text)                        # bot tuzağı

    def test_out_of_range_days_and_invalid_time(self):
        far = (local_today() + dt.timedelta(days=200)).isoformat()
        r = self.page(hizmet="cilt-bakimi", uzman=self.sirin, tarih=far)
        self.assertNotIn('class="time"', r.text)
        r = self.page(hizmet="cilt-bakimi", uzman=self.sirin, tarih=self.wed.isoformat(), saat="03:00")
        self.assertNotIn('name="first_name"', r.text)

    def test_min_notice_today(self):
        # Çarşamba 10:20'deyiz → 11:20'den önceki saatler gösterilmez (min. 60 dk)
        set_clock(dt.datetime.combine(self.wed, dt.time(7, 20), tzinfo=dt.timezone.utc))
        r = self.page(hizmet="cilt-bakimi", uzman=self.sirin, tarih=self.wed.isoformat())
        self.assertNotIn(">10:00<", r.text)
        self.assertNotIn(">11:15<", r.text)
        self.assertIn(">11:30<", r.text)


class BookingTests(Base):
    def test_successful_booking_creates_pending_online_appointment(self):
        r = self.book()
        self.assertEqual(r.status_code, 302, r.text[:400])
        loc = r.headers["Location"]
        self.assertIn("/randevu/durum/", loc)
        token = loc.split("/randevu/durum/")[1].split("?")[0]
        c = self.db.one("SELECT * FROM customers WHERE phone = '+905990000901'")
        self.assertEqual((c["first_name"], c["last_name"], c["source"]), ("Deneme", "Müşteri", "online"))
        a = self.db.one("SELECT * FROM appointments WHERE customer_id = ?", (c["id"],))
        self.assertEqual((a["status"], a["source"], a["staff_id"]), ("pending", "online", self.sirin))
        self.assertNotIn(token, a["reschedule_token_hash"])            # yalnızca özet saklanır
        self.assertEqual(len(a["reschedule_token_hash"]), 64)
        consents = {(x["category"], x["granted"], x["source"]) for x in
                    self.db.query("SELECT * FROM customer_consents WHERE customer_id = ?", (c["id"],))}
        self.assertEqual(consents, {("service", 1, "online_form")})   # pazarlama izni verilmedi
        n = self.db.one("SELECT * FROM notifications WHERE kind = 'online_booking'")
        self.assertIn("Deneme Müşteri", n["title"])
        self.assertEqual(n["link"], f"/panel/randevular/{a['id']}")
        page = self.client.get(loc)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Teşekkürler, Deneme", page.text)
        self.assertIn("Onay bekliyor", page.text)
        self.assertEqual(page.headers.get("Cache-Control"), "no-store")
        # Slot artık dolu
        r2 = self.page(hizmet="cilt-bakimi", uzman=self.sirin, tarih=self.wed.isoformat())
        self.assertNotIn(">10:00<", r2.text)

    def test_any_staff_assigns_available_one(self):
        # Şirin 10:00'da dolu → "fark etmez" Uzman 2'ye atar
        self.book()
        r = self.book(uzman="0", phone="0599 000 09 02")
        self.assertEqual(r.status_code, 302)
        uz2 = self.db.scalar("SELECT id FROM staff WHERE full_name = 'Uzman 2'")
        self.assertEqual(self.db.scalar("""SELECT a.staff_id FROM appointments a JOIN customers c ON c.id = a.customer_id
                                           WHERE c.phone = '+905990000902'"""), uz2)

    def test_taken_slot_rejected(self):
        self.book()
        r = self.book(phone="0599 000 09 03")
        self.assertEqual(r.status_code, 400)
        self.assertIn("az önce doldu", r.text)

    def test_existing_customer_reused_without_overwriting_name(self):
        ayse = self.db.one("SELECT * FROM customers WHERE first_name = 'Ayşe'")
        r = self.book(phone=ayse["phone"], first_name="A.", last_name="Yılmazz")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM customers WHERE phone = ?", (ayse["phone"],)), 1)
        self.assertEqual(self.db.scalar("SELECT first_name FROM customers WHERE id = ?", (ayse["id"],)), "Ayşe")
        note = self.db.scalar("SELECT internal_note FROM appointments WHERE customer_id = ? AND reschedule_token_hash IS NOT NULL", (ayse["id"],))
        self.assertIn("Online formda yazılan isim", note)

    def test_validation_and_kvkk_required(self):
        r = self.book(first_name="", phone="123", kvkk="")
        self.assertEqual(r.status_code, 400)
        for msg in ["Adınızı yazın", "Geçerli bir cep telefonu", "aydınlatma metnini"]:
            self.assertIn(msg, r.text)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM appointments a JOIN customers c ON c.id = a.customer_id WHERE c.phone LIKE '+9059900009%'"), 0)

    def test_csrf_and_honeypot(self):
        data = self.form_data()
        data["_csrf"] = "sahte"
        self.assertEqual(self.client.post("/randevu", data=data).status_code, 400)
        r = self.book(website="http://spam")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM appointments a JOIN customers c ON c.id = a.customer_id WHERE c.phone LIKE '+9059900009%'"), 0)

    def test_rate_limit_per_ip(self):
        times = ["09:00", "10:00", "11:00", "14:00", "15:00", "16:00"]
        codes = [self.book(saat=t, phone=f"0599 000 08 {i:02d}").status_code for i, t in enumerate(times)]
        self.assertEqual(codes[:5], [302] * 5)
        self.assertEqual(codes[5], 400)

    def test_disabled(self):
        self.form_data()
        self.db.execute("UPDATE settings SET value_json = 'false' WHERE key = 'booking.online_enabled'")
        self.assertIn("Online randevu şu anda kapalı", self.page().text)
        self.assertEqual(self.book().status_code, 400)

    def test_package_customer_booking_links_package(self):
        ayse = self.db.one("SELECT * FROM customers WHERE first_name = 'Ayşe'")
        data = self.form_data(phone=ayse["phone"], first_name="Ayşe", last_name="Yılmaz")
        data["hizmet"] = "lazer-epilasyon"
        r = self.client.post("/randevu", data=data)
        self.assertEqual(r.status_code, 302)
        self.assertIsNotNone(self.db.scalar("SELECT customer_package_id FROM appointments WHERE customer_id = ? "
                                            "AND source = 'online' AND reschedule_token_hash IS NOT NULL", (ayse["id"],)))


class TokenTests(Base):
    def token(self):
        return self.book().headers["Location"].split("/randevu/durum/")[1].split("?")[0]

    def test_cancel_by_token(self):
        t = self.token()
        page = self.client.get(f"/randevu/durum/{t}").text
        csrf = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        r = self.client.post(f"/randevu/durum/{t}/iptal", data={"_csrf": csrf})
        self.assertEqual(r.status_code, 200)
        self.assertIn("iptal edildi", r.text)
        a = self.db.one("SELECT a.* FROM appointments a JOIN customers c ON c.id = a.customer_id WHERE c.phone = '+905990000901'")
        self.assertEqual((a["status"], a["is_active_slot"]), ("cancelled", 0))
        self.assertIsNotNone(self.db.one("SELECT 1 FROM notifications WHERE kind = 'online_cancel'"))
        h = self.db.one("SELECT * FROM appointment_status_history WHERE appointment_id = ? ORDER BY id DESC", (a["id"],))
        self.assertEqual(h["channel"], "online")

    def test_bad_and_expired_tokens(self):
        self.assertEqual(self.client.get("/randevu/durum/olmayan-token").status_code, 404)
        t = self.token()
        self.db.execute("UPDATE appointments SET reschedule_token_expires_at = '2000-01-01T00:00:00Z' WHERE reschedule_token_hash IS NOT NULL")
        self.assertEqual(self.client.get(f"/randevu/durum/{t}").status_code, 404)

    def test_cannot_cancel_close_to_start(self):
        t = self.token()
        set_clock(dt.datetime.combine(self.wed, dt.time(6, 30), tzinfo=dt.timezone.utc))   # 09:30 yerel, randevu 10:00
        page = self.client.get(f"/randevu/durum/{t}").text
        self.assertIn("online iptal kapalı", page)


class PanelTests(Base):
    def test_notification_link_marks_read_and_settings_toggle(self):
        self.book()
        self.login()
        dash = self.client.get("/panel/").text
        self.assertIn("Yeni online randevu talebi", dash)
        nid = self.db.scalar("SELECT id FROM notifications WHERE kind = 'online_booking'")
        r = self.client.get(f"/panel/bildirim/{nid}")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/panel/randevular/", r.headers["Location"])
        self.assertIsNotNone(self.db.scalar("SELECT read_at FROM notifications WHERE id = ?", (nid,)))
        detail = self.client.get(r.headers["Location"]).text
        self.assertIn("Online talep", detail)
        tok = self.csrf()
        self.client.post("/panel/ayarlar", data={"_csrf": tok, "booking.slot_step_minutes": "15",
                                                 "booking.min_notice_minutes": "60", "booking.max_days_ahead": "60",
                                                 "no_show.alert_threshold": "2", "reminders.hours_before": "24, 2"})
        self.client.post("/panel/cikis", data={"_csrf": tok})
        self.assertIn("Online randevu şu anda kapalı", self.app.test_client().get("/randevu").text)
