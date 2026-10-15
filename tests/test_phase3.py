"""FAZ 3 testleri: hizmet, personel, çalışma saati, mola, izin, ayarlar ve görsel yönetimi."""
import datetime as dt
import io

from app.services import appointments as appt
from app.timeutil import local_today
from tests.helpers import AppTestCase

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


def next_wd(weekday, min_days=8):
    d = local_today() + dt.timedelta(days=min_days)
    while d.weekday() != weekday:
        d += dt.timedelta(days=1)
    return d


class Base(AppTestCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.tok = self.csrf()
        self.sirin = self.db.scalar("SELECT id FROM staff WHERE full_name = 'Şirin Mercan'")
        self.uzman2 = self.db.scalar("SELECT id FROM staff WHERE full_name = 'Uzman 2'")
        self.cilt = self.db.scalar("SELECT id FROM services WHERE slug = 'cilt-bakimi'")


class ServiceTests(Base):
    def test_create_service_with_staff_image_and_prices(self):
        r = self.client.post("/panel/hizmetler/yeni", data={
            "_csrf": self.tok, "name": "Dermapen", "category": "Cilt", "duration_minutes": "45", "buffer_minutes": "15",
            "price": "1.750,50", "campaign_price": "1500", "repeat_interval_days": "30", "is_active": "1",
            "online_booking": "1", "staff_ids": [str(self.sirin), str(self.uzman2)],
            "image": (io.BytesIO(PNG), "dermapen.png")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        s = self.db.one("SELECT * FROM services WHERE name = 'Dermapen'")
        self.assertEqual((s["slug"], s["price_kurus"], s["campaign_price_kurus"], s["repeat_interval_days"]),
                         ("dermapen", 175050, 150000, 30))
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM staff_services WHERE service_id = ?", (s["id"],)), 2)
        img = self.client.get(f"/media/{s['image_media_id']}")
        self.assertEqual((img.status_code, img.mimetype), (200, "image/png"))
        self.assertTrue(self.db.one("SELECT 1 FROM audit_logs WHERE action = 'service.create' AND entity_id = ?", (s["id"],)))
        # yeni hizmet hemen randevu motorunda: 45 + 15 dk tampon
        slots = appt.available_slots(self.db, self.sirin, s["id"], next_wd(2))
        self.assertIn("09:00", slots)

    def test_reject_non_image_upload(self):
        r = self.client.post("/panel/hizmetler/yeni", data={
            "_csrf": self.tok, "name": "Test", "duration_minutes": "30", "price": "100", "is_active": "1",
            "image": (io.BytesIO(b"<script>alert(1)</script>"), "x.png")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Yalnızca PNG, JPG veya WEBP", r.text)
        self.assertIsNone(self.db.one("SELECT 1 FROM services WHERE name = 'Test'"))

    def test_validation(self):
        r = self.client.post("/panel/hizmetler/yeni", data={"_csrf": self.tok, "name": "Cilt Bakımı",
                                                            "duration_minutes": "0", "price": "100", "campaign_price": "200"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Bu isimde bir hizmet zaten var", r.text)
        self.assertIn("İşlem süresi 5–600", r.text)
        self.assertIn("normal fiyattan düşük", r.text)

    def test_edit_repeat_interval_changes_followup_calc(self):
        self.client.post(f"/panel/hizmetler/{self.cilt}", data={
            "_csrf": self.tok, "name": "Cilt Bakımı", "duration_minutes": "60", "price": "1500",
            "repeat_interval_days": "25", "is_active": "1", "online_booking": "1", "staff_ids": [str(self.sirin)]})
        self.assertEqual(self.db.scalar("SELECT repeat_interval_days FROM services WHERE id = ?", (self.cilt,)), 25)
        log = self.db.one("SELECT * FROM audit_logs WHERE action = 'service.update' AND entity_id = ?", (self.cilt,))
        self.assertIn("25", log["after_json"])
        # Uzman 2 artık bu hizmeti vermiyor
        staff_ids = [s["id"] for s in appt.staff_for_service(self.db, self.cilt)]
        self.assertEqual(staff_ids, [self.sirin])

    def test_inactive_service_cannot_be_booked(self):
        self.db.execute("UPDATE services SET is_active = 0 WHERE id = ?", (self.cilt,))
        r = self.client.post("/panel/randevular/yeni", data={
            "_csrf": self.tok, "customer_id": 3, "service_id": self.cilt, "staff_id": self.sirin,
            "date": next_wd(2).isoformat(), "time": "10:00"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("pasif", r.text)

    def test_delete_blocked_with_future_appointments(self):
        from app.services import appointments as svc
        with self.app.test_request_context():
            admin = self.db.one("SELECT * FROM users WHERE username = 'sirin'")
            svc.create(self.db, admin, {"customer_id": 3, "service_id": self.cilt, "staff_id": self.sirin,
                                        "date": next_wd(2).isoformat(), "time": "10:00"})
        self.client.post(f"/panel/hizmetler/{self.cilt}/sil", data={"_csrf": self.tok})
        self.assertIsNone(self.db.scalar("SELECT deleted_at FROM services WHERE id = ?", (self.cilt,)))
        manikur = self.db.scalar("SELECT id FROM services WHERE slug = 'kalici-makyaj'")
        self.client.post(f"/panel/hizmetler/{manikur}/sil", data={"_csrf": self.tok})
        self.assertIsNotNone(self.db.scalar("SELECT deleted_at FROM services WHERE id = ?", (manikur,)))
        self.assertNotIn("Kalıcı Makyaj", self.client.get("/panel/hizmetler").text)


class StaffTests(Base):
    def staff_form(self, **over):
        data = {"_csrf": self.tok, "full_name": "Uzman 4", "title": "Kirpik Uzmanı", "color": "#336699",
                "is_active": "1", "service_ids": [str(self.cilt)]}
        for d in range(5):
            data[f"day_{d}_open"] = "1"
            data[f"day_{d}_start"] = "10:00"
            data[f"day_{d}_end"] = "16:00"
        data.update(over)
        return data

    def test_create_staff_hours_drive_slots(self):
        r = self.client.post("/panel/personel/yeni", data=self.staff_form())
        self.assertEqual(r.status_code, 302)
        sid = self.db.scalar("SELECT id FROM staff WHERE full_name = 'Uzman 4'")
        slots = appt.available_slots(self.db, sid, self.cilt, next_wd(1))
        self.assertEqual((slots[0], slots[-1]), ("10:00", "15:00"))
        self.assertEqual(appt.available_slots(self.db, sid, self.cilt, next_wd(5)), [])   # Cumartesi kapalı

    def test_hours_validation(self):
        r = self.client.post("/panel/personel/yeni", data=self.staff_form(day_0_end="09:00"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("bitiş saati başlangıçtan sonra", r.text)

    def test_break_add_and_delete(self):
        wed = next_wd(2)
        self.assertIn("15:00", appt.available_slots(self.db, self.sirin, self.cilt, wed))
        self.client.post(f"/panel/personel/{self.sirin}/mola", data={"_csrf": self.tok, "weekday": "2",
                                                                     "start_time": "15:00", "end_time": "15:30"})
        s = appt.available_slots(self.db, self.sirin, self.cilt, wed)
        self.assertNotIn("15:00", s)
        self.assertNotIn("14:30", s)
        self.assertIn("15:30", s)
        bid = self.db.scalar("SELECT id FROM staff_breaks WHERE staff_id = ? AND start_time = '15:00'", (self.sirin,))
        self.client.post(f"/panel/personel/{self.sirin}/mola/{bid}/sil", data={"_csrf": self.tok})
        self.assertIn("15:00", appt.available_slots(self.db, self.sirin, self.cilt, wed))

    def test_cannot_deactivate_staff_with_future_appointments(self):
        from app.services import appointments as svc
        with self.app.test_request_context():
            admin = self.db.one("SELECT * FROM users WHERE username = 'sirin'")
            svc.create(self.db, admin, {"customer_id": 3, "service_id": self.cilt, "staff_id": self.uzman2,
                                        "date": next_wd(2).isoformat(), "time": "11:00"})
        form = self.staff_form(full_name="Uzman 2")
        form.pop("is_active")
        r = self.client.post(f"/panel/personel/{self.uzman2}", data=form)
        self.assertEqual(r.status_code, 400)
        self.assertIn("ileri tarihli aktif randevuları var", r.text)
        self.assertEqual(self.db.scalar("SELECT is_active FROM staff WHERE id = ?", (self.uzman2,)), 1)


class HolidayAndSettingsTests(Base):
    def test_center_holiday_blocks_everyone_and_warns_conflicts(self):
        wed = next_wd(2)
        from app.services import appointments as svc
        with self.app.test_request_context():
            admin = self.db.one("SELECT * FROM users WHERE username = 'sirin'")
            svc.create(self.db, admin, {"customer_id": 3, "service_id": self.cilt, "staff_id": self.sirin,
                                        "date": wed.isoformat(), "time": "10:00"})
        r = self.client.post("/panel/izinler", data={"_csrf": self.tok, "staff_id": "", "start_date": wed.isoformat(),
                                                     "reason": "Bayram"}, follow_redirects=True)
        self.assertIn("1 aktif randevu var", r.text)
        self.assertEqual(appt.available_slots(self.db, self.uzman2, self.cilt, wed), [])
        hid = self.db.scalar("SELECT id FROM holidays WHERE reason = 'Bayram'")
        self.client.post(f"/panel/izinler/{hid}/sil", data={"_csrf": self.tok})
        self.assertTrue(appt.available_slots(self.db, self.uzman2, self.cilt, wed))

    def test_settings_change_slot_step(self):
        wed = next_wd(2)
        self.assertIn("09:15", appt.available_slots(self.db, self.sirin, self.cilt, wed))
        r = self.client.post("/panel/ayarlar", data={"_csrf": self.tok, "booking.slot_step_minutes": "30",
                                                     "booking.min_notice_minutes": "60", "booking.max_days_ahead": "60",
                                                     "no_show.alert_threshold": "3", "reminders.hours_before": "24, 2"})
        self.assertEqual(r.status_code, 302)
        s = appt.available_slots(self.db, self.sirin, self.cilt, wed)
        self.assertNotIn("09:15", s)
        self.assertIn("09:30", s)
        self.assertEqual(appt.setting(self.db, "reminders.hours_before"), [24, 2])
        self.assertEqual(appt.setting(self.db, "no_show.alert_threshold"), 3)

    def test_settings_validation(self):
        r = self.client.post("/panel/ayarlar", data={"_csrf": self.tok, "booking.slot_step_minutes": "1",
                                                     "booking.min_notice_minutes": "60", "booking.max_days_ahead": "60",
                                                     "no_show.alert_threshold": "2", "reminders.hours_before": "abc"})
        self.assertEqual(r.status_code, 400)

    def test_reception_and_specialist_forbidden(self):
        for user in ("resepsiyon", "uzman2"):
            c = self.app.test_client()
            self.login(user, client=c)
            for path in ("/panel/hizmetler", "/panel/personel", "/panel/izinler", "/panel/ayarlar"):
                self.assertEqual(c.get(path).status_code, 403, (user, path))
