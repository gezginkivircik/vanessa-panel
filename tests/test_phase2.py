"""FAZ 2 testleri: müşteri CRM — kayıt, arama, profil, notlar, KVKK izinleri, yetki."""
import json
import unittest

from app.services.customers import has_consent
from app.textutil import fold, format_phone, mask_phone, normalize_phone, tr_title
from tests.helpers import AppTestCase


class TextUtilTests(unittest.TestCase):
    def test_phone_normalization(self):
        for raw in ["0533 150 43 03", "5331504303", "+90 533 150 43 03", "0090-533-150-4303", "(0533) 150 43 03"]:
            self.assertEqual(normalize_phone(raw), "+905331504303", raw)
        for bad in ["", "123", "0212 555 44 33", "05331504", "abc"]:
            self.assertIsNone(normalize_phone(bad), bad)
        self.assertEqual(format_phone("+905331504303"), "0533 150 43 03")
        self.assertEqual(mask_phone("+905331504303"), "0533 *** ** 03")

    def test_turkish_fold_and_title(self):
        self.assertEqual(fold("AYŞE Yılmaz"), "ayse yilmaz")
        self.assertEqual(fold("İREM  ÇELİK"), "irem celik")
        self.assertEqual(tr_title("ayşe IŞIK"), "Ayşe Işık")
        self.assertEqual(tr_title("irem"), "İrem")


class CustomerCreateTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.tok = self.csrf()

    def post_new(self, **data):
        payload = {"_csrf": self.tok, "full_name": "Hande Yücel", "phone": "0599 111 22 33"}
        payload.update(data)
        return self.client.post("/panel/musteriler/yeni", data=payload)

    def test_quick_add_creates_customer_with_consents_and_note(self):
        r = self.post_new(birth_date="1994-05-17", note="Hassas cilt.", consent_service="1", consent_marketing="1")
        self.assertEqual(r.status_code, 302)
        c = self.db.one("SELECT * FROM customers WHERE phone = '+905991112233'")
        self.assertEqual((c["first_name"], c["last_name"], c["search_name"]), ("Hande", "Yücel", "hande yucel"))
        self.assertEqual(c["birth_date"], "1994-05-17")
        self.assertIn(f"/panel/musteriler/{c['id']}", r.headers["Location"])
        self.assertTrue(has_consent(self.db, c["id"], "whatsapp", "service"))
        self.assertTrue(has_consent(self.db, c["id"], "whatsapp", "marketing"))
        self.assertEqual(self.db.scalar("SELECT body FROM customer_notes WHERE customer_id = ?", (c["id"],)), "Hassas cilt.")
        log = self.db.one("SELECT * FROM audit_logs WHERE action = 'customer.create' AND entity_id = ?", (c["id"],))
        self.assertEqual(log["actor_label"], "Şirin Mercan")

    def test_marketing_consent_is_opt_in(self):
        self.post_new(consent_service="1")
        cid = self.db.scalar("SELECT id FROM customers WHERE phone = '+905991112233'")
        self.assertTrue(has_consent(self.db, cid, "whatsapp", "service"))
        self.assertFalse(has_consent(self.db, cid, "whatsapp", "marketing"))

    def test_multi_word_first_name(self):
        self.post_new(full_name="ayşe nur kaplan")
        c = self.db.one("SELECT first_name, last_name FROM customers WHERE phone = '+905991112233'")
        self.assertEqual((c["first_name"], c["last_name"]), ("Ayşe Nur", "Kaplan"))

    def test_validation_errors(self):
        r = self.post_new(full_name="Tek", phone="123", birth_date="2999-01-01")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Soyad gerekli", r.text)
        self.assertIn("Geçerli bir cep telefonu", r.text)
        self.assertIn("Doğum tarihi geçersiz", r.text)

    def test_duplicate_phone_points_to_existing(self):
        r = self.post_new(phone="0599 000 00 01")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Ayşe Yılmaz adına kayıtlı", r.text)
        self.assertIn("/panel/musteriler/1", r.text)


class SearchTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.login()

    def api(self, q):
        return json.loads(self.client.get("/api/musteriler/ara", query_string={"q": q}).text)["results"]

    def test_search_by_phone_prefix(self):
        names = [r["name"] for r in self.api("0599")]
        self.assertIn("Ayşe Yılmaz", names)
        self.assertEqual([r["name"] for r in self.api("0599 000 00 03")], ["Elif Demir"])

    def test_search_turkish_insensitive(self):
        for q in ("ayse", "AYŞE", "ayşe yıl", "yilmaz"):
            self.assertEqual([r["name"] for r in self.api(q)], ["Ayşe Yılmaz"], q)
        self.assertEqual([r["name"] for r in self.api("ozturk")], ["Gizem Öztürk"])

    def test_search_requires_login(self):
        c = self.app.test_client()
        self.assertEqual(c.get("/api/musteriler/ara?q=ayse").status_code, 401)

    def test_like_wildcards_are_not_injection(self):
        self.assertEqual(self.api("%' OR 1=1 --"), [])

    def test_list_filters(self):
        r = self.client.get("/panel/musteriler?filtre=gelmedi")
        self.assertIn("Gizem Öztürk", r.text)
        self.assertNotIn("Ayşe Yılmaz", r.text)


class ProfileTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.ayse = self.db.scalar("SELECT id FROM customers WHERE first_name = 'Ayşe'")

    def test_profile_summary(self):
        html = self.client.get(f"/panel/musteriler/{self.ayse}").text
        self.assertIn("Ayşe Yılmaz", html)
        self.assertIn("0599 000 00 01", html)
        self.assertIn("WhatsApp'tan Yaz", html)
        self.assertIn("https://wa.me/905990000001", html)
        self.assertIn("3.000 TL", html)            # toplam harcama (tahsilat)
        self.assertIn("1.999 TL", html)            # kalan borç
        self.assertIn("1. seans tamamlandı", html)  # timeline

    def test_sessions_tab_shows_progress(self):
        html = self.client.get(f"/panel/musteriler/{self.ayse}?sekme=seanslar").text
        self.assertIn("Tüm Bacak", html)
        self.assertIn("Koltuk Altı", html)
        self.assertEqual(html.count("session is-done"), 6)   # 2 bölge × 3 seans
        self.assertEqual(html.count('class="session "'), 14)

    def test_notes_add_pin_delete(self):
        tok = self.csrf()
        self.client.post(f"/panel/musteriler/{self.ayse}/notlar", data={"_csrf": tok, "body": "Paket yenilenecek"})
        nid = self.db.scalar("SELECT id FROM customer_notes WHERE body = 'Paket yenilenecek'")
        self.client.post(f"/panel/musteriler/{self.ayse}/notlar/{nid}/sabitle", data={"_csrf": tok})
        self.assertEqual(self.db.scalar("SELECT is_pinned FROM customer_notes WHERE id = ?", (nid,)), 1)
        self.client.post(f"/panel/musteriler/{self.ayse}/notlar/{nid}/sil", data={"_csrf": tok})
        self.assertIsNotNone(self.db.scalar("SELECT deleted_at FROM customer_notes WHERE id = ?", (nid,)))
        row = self.db.one("SELECT n.*, u.username FROM customer_notes n JOIN users u ON u.id = n.author_user_id WHERE n.id = ?", (nid,))
        self.assertEqual(row["username"], "sirin")

    def test_consent_toggle_keeps_history(self):
        tok = self.csrf()
        for granted in ("0", "1", "0"):
            self.client.post(f"/panel/musteriler/{self.ayse}/izinler", data={
                "_csrf": tok, "channel": "whatsapp", "category": "marketing", "granted": granted, "source": "panel"})
        self.assertFalse(has_consent(self.db, self.ayse, "whatsapp", "marketing"))
        self.assertTrue(has_consent(self.db, self.ayse, "whatsapp", "service"))
        n = self.db.scalar("SELECT COUNT(*) FROM customer_consents WHERE customer_id = ? AND category = 'marketing'", (self.ayse,))
        self.assertEqual(n, 4)  # demo kaydı + 3 değişiklik

    def test_edit_customer_audits_diff(self):
        tok = self.csrf()
        r = self.client.post(f"/panel/musteriler/{self.ayse}/duzenle", data={
            "_csrf": tok, "first_name": "Ayşe", "last_name": "Yılmaz Demir", "phone": "0599 000 00 01",
            "birth_date": "1992-03-18", "email": ""})
        self.assertEqual(r.status_code, 302)
        log = self.db.one("SELECT * FROM audit_logs WHERE action = 'customer.update' AND entity_id = ?", (self.ayse,))
        self.assertIn("Yılmaz Demir", log["after_json"])
        self.assertNotIn("phone", log["after_json"])   # değişmeyen alan yazılmaz
        self.assertEqual(self.db.scalar("SELECT search_name FROM customers WHERE id = ?", (self.ayse,)), "ayse yilmaz demir")

    def test_cannot_delete_customer_with_future_appointment(self):
        tok = self.csrf()
        # İrem Kurt'un yarın randevusu var
        irem = self.db.scalar("SELECT id FROM customers WHERE first_name = 'İrem'")
        self.client.post(f"/panel/musteriler/{irem}/sil", data={"_csrf": tok})
        self.assertIsNone(self.db.scalar("SELECT deleted_at FROM customers WHERE id = ?", (irem,)))
        # Ceren Aydın'ın aktif ileri randevusu yok → silinebilir, listeden kalkar
        ceren = self.db.scalar("SELECT id FROM customers WHERE first_name = 'Ceren'")
        self.client.post(f"/panel/musteriler/{ceren}/sil", data={"_csrf": tok})
        self.assertIsNotNone(self.db.scalar("SELECT deleted_at FROM customers WHERE id = ?", (ceren,)))
        self.assertNotIn("Ceren Aydın", self.client.get("/panel/musteriler").text)
        self.assertEqual(self.client.get(f"/panel/musteriler/{ceren}").status_code, 404)


class CustomerRbacTests(AppTestCase):
    def test_specialist_sees_only_own_customers_masked(self):
        self.login("uzman2")
        html = self.client.get("/panel/musteriler").text
        self.assertIn("Selin Arslan", html)         # Uzman 2 randevusu
        self.assertNotIn("Zeynep Kaya", html)       # yalnızca Şirin/Uzman 3
        self.assertIn("0599 *** **", html)
        self.assertNotIn("0599 000 00", html)
        zeynep = self.db.scalar("SELECT id FROM customers WHERE first_name = 'Zeynep'")
        self.assertEqual(self.client.get(f"/panel/musteriler/{zeynep}").status_code, 404)
        selin = self.db.scalar("SELECT id FROM customers WHERE first_name = 'Selin'")
        prof = self.client.get(f"/panel/musteriler/{selin}").text
        self.assertNotIn("wa.me", prof)
        self.assertNotIn("Toplam harcama", prof)
        self.assertEqual(self.client.get("/panel/musteriler/yeni").status_code, 403)
        self.assertEqual(self.client.get(f"/panel/musteriler/{selin}?sekme=odemeler").status_code, 200)
        self.assertNotIn("Ödeme kaydı", self.client.get(f"/panel/musteriler/{selin}?sekme=odemeler").text)

    def test_specialist_api_search_scoped(self):
        self.login("uzman2")
        res = json.loads(self.client.get("/api/musteriler/ara?q=zeynep").text)["results"]
        self.assertEqual(res, [])

    def test_reception_can_create_but_not_delete(self):
        self.login("resepsiyon")
        tok = self.csrf()
        r = self.client.post("/panel/musteriler/yeni", data={"_csrf": tok, "full_name": "Test Kişi", "phone": "05991234567"})
        self.assertEqual(r.status_code, 302)
        cid = self.db.scalar("SELECT id FROM customers WHERE phone = '+905991234567'")
        self.assertEqual(self.client.post(f"/panel/musteriler/{cid}/sil", data={"_csrf": tok}).status_code, 403)

    def test_reception_cannot_delete_others_note(self):
        self.login("resepsiyon")
        tok = self.csrf()
        ayse = self.db.scalar("SELECT id FROM customers WHERE first_name = 'Ayşe'")
        nid = self.db.scalar("SELECT id FROM customer_notes WHERE customer_id = ?", (ayse,))
        self.client.post(f"/panel/musteriler/{ayse}/notlar/{nid}/sil", data={"_csrf": tok})
        self.assertIsNone(self.db.scalar("SELECT deleted_at FROM customer_notes WHERE id = ?", (nid,)))


if __name__ == "__main__":
    unittest.main()
