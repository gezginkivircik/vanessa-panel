"""FAZ 5 + 7 testleri: paket satışı, bölge bazlı seans işleme, tahsilat, kısmi/tam ödeme,
fazla tahsilat engeli, iade/iptal yetkileri, randevu tamamlanınca borç kaydı."""
import datetime as dt
import re

from app.timeutil import local_today
from tests.test_phase4 import Base, future_weekday


class PaymentBase(Base):
    def setUp(self):
        super().setUp()
        self.login()
        self.tok = self.csrf()

    def last_audit(self, action):
        return self.db.one("SELECT * FROM audit_logs WHERE action = ? ORDER BY id DESC LIMIT 1", (action,))

    def sell(self, customer, **kw):
        data = {"_csrf": self.tok, "service_id": self.lazer, "session_count": "6", "total": "3.000",
                "areas": "Yüz, Koltuk Altı", "first_payment": "1000", "method": "card"}
        data.update(kw)
        return self.client.post(f"/panel/musteriler/{customer}/paket-sat", data=data)


class PackageTests(PaymentBase):
    def test_sell_package_with_areas_and_down_payment(self):
        r = self.sell(self.elif_)
        self.assertEqual(r.status_code, 302)
        self.assertIn("sekme=seanslar", r.headers["Location"])
        cp = self.db.one("SELECT * FROM customer_packages WHERE customer_id = ? ORDER BY id DESC", (self.elif_,))
        self.assertEqual(cp["name"], "6 Seans Lazer Epilasyon")
        self.assertEqual(cp["total_price_kurus"], 300000)
        areas = self.db.query("SELECT area_name, total_sessions FROM session_areas WHERE customer_package_id = ? ORDER BY sort_order",
                              (cp["id"],))
        self.assertEqual([(a["area_name"], a["total_sessions"]) for a in areas], [("Yüz", 6), ("Koltuk Altı", 6)])
        pay = self.db.one("SELECT * FROM payments WHERE customer_package_id = ?", (cp["id"],))
        self.assertEqual((pay["paid_kurus"], pay["status"]), (100000, "partial"))
        self.assertIn("6 Seans Lazer Epilasyon", self.last_audit("package.sell")["summary"])
        page = self.client.get(f"/panel/musteriler/{self.elif_}?sekme=seanslar").text
        self.assertIn("Koltuk Altı", page)
        self.assertIn("1. seansı işle", page)

    def test_sell_validation(self):
        r = self.sell(self.elif_, session_count="0", total="", areas="Yüz, yüz", first_payment="5000")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM customer_packages WHERE customer_id = ?", (self.elif_,)), 0)
        r = self.sell(self.elif_, first_payment="5000")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Peşinat paket tutarından büyük olamaz", r.text)

    def test_future_appointments_link_to_new_package(self):
        aid = self.book(self.elif_, self.lazer, self.sirin, self.wed, "10:00")
        self.assertIsNone(self.db.scalar("SELECT customer_package_id FROM appointments WHERE id = ?", (aid,)))
        self.sell(self.elif_)
        cp = self.db.scalar("SELECT id FROM customer_packages WHERE customer_id = ?", (self.elif_,))
        self.assertEqual(self.db.scalar("SELECT customer_package_id FROM appointments WHERE id = ?", (aid,)), cp)
        # Paketli randevu tamamlanınca ayrıca borç oluşmaz, seans düşer
        self.status(aid, "completed")
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM payments WHERE appointment_id = ?", (aid,)), 0)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM sessions WHERE customer_package_id = ? AND status = 'completed'",
                                        (cp,)), 2)

    def test_record_session_until_package_completes(self):
        self.sell(self.elif_, session_count="2", areas="")
        cp = self.db.scalar("SELECT id FROM customer_packages WHERE customer_id = ?", (self.elif_,))
        area = self.db.scalar("SELECT id FROM session_areas WHERE customer_package_id = ?", (cp,))
        url = f"/panel/musteriler/{self.elif_}/seans/{area}"
        yesterday = (local_today() - dt.timedelta(days=1)).isoformat()
        self.client.post(url, data={"_csrf": self.tok, "performed_on": yesterday, "notes": "Atış 12"})
        s = self.db.one("SELECT * FROM sessions WHERE session_area_id = ? AND session_number = 1", (area,))
        self.assertEqual((s["status"], s["notes"]), ("completed", "Atış 12"))
        # Gelecek tarih reddedilir
        tomorrow = (local_today() + dt.timedelta(days=1)).isoformat()
        self.client.post(url, data={"_csrf": self.tok, "performed_on": tomorrow})
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM sessions WHERE session_area_id = ?", (area,)), 1)
        self.client.post(url, data={"_csrf": self.tok})
        self.assertEqual(self.db.scalar("SELECT status FROM customer_packages WHERE id = ?", (cp,)), "completed")
        # Tamamlanan pakete fazladan seans işlenemez
        self.client.post(url, data={"_csrf": self.tok})
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM sessions WHERE session_area_id = ?", (area,)), 2)
        self.assertIn("2. seans işlendi", self.last_audit("session.record")["summary"])

    def test_session_on_other_customers_area_is_404(self):
        area = self.db.scalar("""SELECT sa.id FROM session_areas sa JOIN customer_packages cp ON cp.id = sa.customer_package_id
                                 WHERE cp.customer_id = ? LIMIT 1""", (self.ayse,))
        r = self.client.post(f"/panel/musteriler/{self.elif_}/seans/{area}", data={"_csrf": self.tok})
        self.assertEqual(r.status_code, 404)


class PaymentTests(PaymentBase):
    def ayse_payment(self):
        return self.db.one("SELECT * FROM payments WHERE customer_id = ? AND customer_package_id IS NOT NULL", (self.ayse,))

    def test_partial_to_paid_and_overpay_blocked(self):
        p = self.ayse_payment()
        self.assertEqual((p["paid_kurus"], p["status"]), (300000, "partial"))
        url = f"/panel/odemeler/{p['id']}"
        page = self.client.get(url).text
        self.assertIn("1.999", page)                       # kalan önerilir
        r = self.client.post(url, data={"_csrf": self.tok, "amount": "2500", "method": "cash"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("daha fazlası alınamaz", r.text)
        self.client.post(url, data={"_csrf": self.tok, "amount": "999", "method": "transfer"})
        self.assertEqual(self.db.scalar("SELECT status FROM payments WHERE id = ?", (p["id"],)), "partial")
        self.client.post(url, data={"_csrf": self.tok, "amount": "1000", "method": "card"})
        p2 = self.db.one("SELECT * FROM payments WHERE id = ?", (p["id"],))
        self.assertEqual((p2["paid_kurus"], p2["status"]), (499900, "paid"))
        self.assertIn("Ayşe Yılmaz", self.last_audit("payment.collect")["summary"])
        # Müşteri profilinde kalan borç düşer
        prof = self.client.get(f"/panel/musteriler/{self.ayse}?sekme=odemeler").text
        self.assertIn("Ödendi", prof)

    def test_validation_future_date_and_method(self):
        p = self.ayse_payment()
        tomorrow = (local_today() + dt.timedelta(days=1)).isoformat()
        r = self.client.post(f"/panel/odemeler/{p['id']}", data={"_csrf": self.tok, "amount": "10", "method": "bitcoin",
                                                                  "paid_on": tomorrow})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Ödeme yöntemi seçin", r.text)
        self.assertIn("Gelecek tarihli", r.text)

    def test_completed_appointment_creates_unpaid_charge_once(self):
        aid = self.book(self.elif_, self.cilt, self.sirin, self.wed, "10:00")
        self.status(aid, "arrived")
        self.status(aid, "completed")
        rows = self.db.query("SELECT * FROM payments WHERE appointment_id = ?", (aid,))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "unpaid")
        price = self.db.scalar("SELECT price_kurus FROM appointments WHERE id = ?", (aid,))
        self.assertEqual(rows[0]["total_kurus"], price)
        page = self.client.get(f"/panel/randevular/{aid}").text
        self.assertIn("Tahsilat Al", page)
        self.assertIn(f"/panel/odemeler/{rows[0]['id']}", page)

    def test_manual_charge_with_pay_now(self):
        r = self.client.post(f"/panel/musteriler/{self.elif_}/odeme-yeni",
                             data={"_csrf": self.tok, "description": "Serum satışı", "total": "450", "discount": "50",
                                   "pay_now": "1", "method": "cash"})
        self.assertEqual(r.status_code, 302)
        p = self.db.one("SELECT * FROM payments WHERE customer_id = ? AND description = 'Serum satışı'", (self.elif_,))
        self.assertEqual((p["total_kurus"], p["discount_kurus"], p["paid_kurus"], p["status"]), (45000, 5000, 40000, "paid"))

    def test_refund_and_void_admin_only(self):
        p = self.ayse_payment()
        # Resepsiyon tahsilat girebilir ama iade / iptal yapamaz
        rc = self.app.test_client()
        self.login("resepsiyon", client=rc)
        rtok = self.csrf(client=rc)
        self.assertEqual(rc.post(f"/panel/odemeler/{p['id']}", data={"_csrf": rtok, "amount": "100", "method": "cash",
                                                                      "refund": "1"}).status_code, 403)
        self.assertEqual(rc.post(f"/panel/odemeler/{p['id']}/iptal", data={"_csrf": rtok, "reason": "x"}).status_code, 403)
        self.assertEqual(rc.post(f"/panel/odemeler/{p['id']}", data={"_csrf": rtok, "amount": "100",
                                                                      "method": "cash"}).status_code, 302)
        self.assertNotIn("Yönetici İşlemleri", rc.get(f"/panel/odemeler/{p['id']}").text)
        # Yönetici iade girer
        self.client.post(f"/panel/odemeler/{p['id']}", data={"_csrf": self.tok, "amount": "500", "method": "cash",
                                                              "refund": "1", "note": "Memnuniyet"})
        self.assertEqual(self.db.scalar("SELECT paid_kurus FROM payments WHERE id = ?", (p["id"],)), 260000)
        self.assertIsNotNone(self.last_audit("payment.refund"))
        # Tahsilatı olan kayıt iptal edilemez
        self.client.post(f"/panel/odemeler/{p['id']}/iptal", data={"_csrf": self.tok, "reason": "deneme"})
        self.assertNotEqual(self.db.scalar("SELECT status FROM payments WHERE id = ?", (p["id"],)), "void")
        # Tahsilatsız kayıt iptal edilir ve açık bakiyeden düşer
        aid = self.book(self.elif_, self.cilt, self.sirin, self.wed, "10:00")
        self.status(aid, "completed")
        pid = self.db.scalar("SELECT id FROM payments WHERE appointment_id = ?", (aid,))
        self.client.post(f"/panel/odemeler/{pid}/iptal", data={"_csrf": self.tok, "reason": "Ücretsiz rötuş"})
        self.assertEqual(self.db.scalar("SELECT status FROM payments WHERE id = ?", (pid,)), "void")
        self.assertIn("Ücretsiz rötuş", self.last_audit("payment.void")["summary"])

    def test_specialist_cannot_see_payments(self):
        sc = self.app.test_client()
        self.login("uzman2", client=sc)
        self.assertEqual(sc.get("/panel/odemeler").status_code, 403)
        self.assertEqual(sc.get(f"/panel/odemeler/{self.ayse_payment()['id']}").status_code, 403)
        self.assertEqual(sc.get(f"/panel/musteriler/{self.ayse}/paket-sat").status_code, 403)

    def test_payments_index(self):
        r = self.client.get("/panel/odemeler?donem=ay")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Açık Bakiyeler", r.text)
        self.assertIn("Ayşe Yılmaz", r.text)
        self.assertIn("Bekleyen alacak", r.text)
        rc = self.app.test_client()
        self.login("resepsiyon", client=rc)
        page = rc.get("/panel/odemeler").text
        self.assertNotIn("Bekleyen alacak", page)          # ciro özeti yalnız yönetici
        self.assertIn("Açık Bakiyeler", page)
