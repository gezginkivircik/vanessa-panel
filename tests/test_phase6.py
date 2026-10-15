"""FAZ 6 testleri: takip listesi (tekrar zamanı gelen işlemler), arama sonucu, erteleme, paket takibi, yetkiler."""
import datetime as dt

from app.services import followups as fu
from app.timeutil import local_today
from tests.test_phase4 import Base as P4Base


class Base(P4Base):
    def setUp(self):
        super().setUp()
        self.login()
        self.tok = self.csrf()

    def fid(self, customer, service):
        return self.db.scalar("SELECT id FROM service_followups WHERE customer_id = ? AND service_id = ?", (customer, service))

    def make_due(self, customer, service, days_ago=3):
        due = local_today() - dt.timedelta(days=days_ago)
        self.db.execute("""UPDATE service_followups SET due_on = ?, status = 'open', snoozed_until = NULL
                           WHERE customer_id = ? AND service_id = ?""", (due.isoformat(), customer, service))

    def post(self, fid, **data):
        return self.client.post(f"/panel/takip/{fid}/kayit", data={"_csrf": self.tok, **data})


class FollowupTests(Base):
    def test_seed_backfill_and_list(self):
        n = self.db.scalar("SELECT COUNT(*) FROM service_followups")
        self.assertGreater(n, 3)
        self.assertEqual(fu.rebuild_missing(self.db), 0)                 # tekrar çalışınca kopya üretmez
        r = self.client.get("/panel/takip")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Takip Listesi", r.text)
        due = fu.items(self.db, "gelen")
        self.assertTrue(due)
        self.assertIn(f"{due[0]['first_name']} {due[0]['last_name']}", r.text)
        self.assertIn("Randevu Oluştur", r.text)

    def test_completion_sets_due_and_resets_contacts(self):
        aid = self.book(self.elif_, self.cilt, self.sirin, self.wed, "10:00")
        self.status(aid, "completed")
        f = self.db.one("SELECT * FROM service_followups WHERE id = ?", (self.fid(self.elif_, self.cilt),))
        self.assertEqual(f["due_on"], (self.wed + dt.timedelta(days=20)).isoformat())
        self.make_due(self.elif_, self.cilt)
        self.post(f["id"], result="no_answer")
        self.assertEqual(self.db.scalar("SELECT contact_count FROM service_followups WHERE id = ?", (f["id"],)), 1)
        aid2 = self.book(self.elif_, self.cilt, self.sirin, self.wed, "14:00")
        self.status(aid2, "completed")
        f2 = self.db.one("SELECT * FROM service_followups WHERE id = ?", (f["id"],))
        self.assertEqual((f2["contact_count"], f2["snoozed_until"], f2["status"]), (0, None, "open"))

    def test_future_appointment_hides_item(self):
        aid = self.book(self.elif_, self.cilt, self.sirin, self.wed, "10:00")
        self.status(aid, "completed")
        self.make_due(self.elif_, self.cilt)
        ids = lambda: [r["id"] for r in fu.items(self.db, "gelen")]
        fid = self.fid(self.elif_, self.cilt)
        self.assertIn(fid, ids())
        nxt = self.book(self.elif_, self.cilt, self.sirin, self.wed, "14:00")
        self.assertNotIn(fid, ids())
        self.status(nxt, "cancelled")                                   # iptal → tekrar listede
        self.assertIn(fid, ids())

    def test_no_answer_later_declined_reopen(self):
        aid = self.book(self.elif_, self.cilt, self.sirin, self.wed, "10:00")
        self.status(aid, "completed")
        self.make_due(self.elif_, self.cilt)
        fid = self.fid(self.elif_, self.cilt)
        r = self.post(fid, result="no_answer", note="Telefon kapalı")
        self.assertEqual(r.status_code, 302)
        f = self.db.one("SELECT * FROM service_followups WHERE id = ?", (fid,))
        self.assertEqual(f["snoozed_until"], (local_today() + dt.timedelta(days=1)).isoformat())
        self.assertEqual((f["contact_count"], f["last_contact_result"]), (1, "no_answer"))
        self.assertNotIn(fid, [x["id"] for x in fu.items(self.db, "gelen")])
        self.assertIn(fid, [x["id"] for x in fu.items(self.db, "ertelenen")])
        self.assertIn("Telefon kapalı", self.db.scalar("SELECT note FROM followup_contacts WHERE followup_id = ?", (fid,)))
        self.assertIsNotNone(self.db.one("SELECT 1 FROM audit_logs WHERE action = 'followup.no_answer'"))
        self.post(fid, result="reopened")
        self.assertIn(fid, [x["id"] for x in fu.items(self.db, "gelen")])
        self.post(fid, result="later", days="14")
        self.assertEqual(self.db.scalar("SELECT snoozed_until FROM service_followups WHERE id = ?", (fid,)),
                         (local_today() + dt.timedelta(days=14)).isoformat())
        self.post(fid, result="later", days="999")                       # geçersiz süre değişiklik yapmaz
        self.assertEqual(self.db.scalar("SELECT contact_count FROM service_followups WHERE id = ?", (fid,)), 2)
        self.post(fid, result="declined")
        self.assertEqual(self.db.scalar("SELECT status FROM service_followups WHERE id = ?", (fid,)), "dismissed")
        for tab in fu.TABS:
            self.assertNotIn(fid, [x["id"] for x in fu.items(self.db, tab)])

    def test_tabs_by_due_date(self):
        aid = self.book(self.elif_, self.cilt, self.sirin, self.wed, "10:00")
        self.status(aid, "completed")
        fid = self.fid(self.elif_, self.cilt)
        self.db.execute("UPDATE service_followups SET due_on = ? WHERE id = ?",
                        ((local_today() + dt.timedelta(days=5)).isoformat(), fid))
        self.assertIn(fid, [x["id"] for x in fu.items(self.db, "hafta")])
        self.db.execute("UPDATE service_followups SET due_on = ? WHERE id = ?",
                        ((local_today() + dt.timedelta(days=20)).isoformat(), fid))
        self.assertIn(fid, [x["id"] for x in fu.items(self.db, "yaklasan")])
        self.db.execute("UPDATE service_followups SET due_on = ? WHERE id = ?",
                        ((local_today() + dt.timedelta(days=60)).isoformat(), fid))
        self.assertFalse(any(fid in [x["id"] for x in fu.items(self.db, t)] for t in fu.TABS))

    def test_package_sale_creates_followup_with_progress(self):
        self.client.post(f"/panel/musteriler/{self.elif_}/paket-sat",
                         data={"_csrf": self.tok, "service_id": self.lazer, "session_count": "8", "total": "4000",
                               "areas": "Yüz", "method": "cash"})
        fid = self.fid(self.elif_, self.lazer)
        item = next(x for x in fu.items(self.db, "gelen") if x["id"] == fid)
        self.assertEqual(item["reason"], "package")
        self.assertEqual((item["package"]["done"], item["package"]["total"], item["package"]["next"]), (0, 8, 1))
        page = self.client.get("/panel/takip").text
        self.assertIn("paket alındı, ilk randevu bekleniyor", page)
        self.assertIn("1. seansının zamanı geldi", fu.whatsapp_text(item))

    def test_whatsapp_button_requires_consent(self):
        aid = self.book(self.elif_, self.cilt, self.sirin, self.wed, "10:00")
        self.status(aid, "completed")
        self.make_due(self.elif_, self.cilt)
        self.db.execute("DELETE FROM customer_consents WHERE customer_id = ?", (self.elif_,))
        page = self.client.get("/panel/takip").text
        block = page.split("Elif Demir")[1].split("</article>")[0]
        self.assertIn("WhatsApp izni yok", block)
        self.db.insert("customer_consents", {"customer_id": self.elif_, "channel": "whatsapp", "category": "service",
                                             "granted": 1, "source": "panel", "created_at": "2026-01-01T00:00:00Z"})
        block = self.client.get("/panel/takip").text.split("Elif Demir")[1].split("</article>")[0]
        self.assertIn("wa.me/90", block)
        self.assertIn("text=Merhaba%20Elif", block)

    def test_permissions_and_dashboard(self):
        self.assertIn("zamanı gelen müşteri", self.client.get("/panel/").text)
        rc = self.app.test_client()
        self.login("resepsiyon", client=rc)
        self.assertEqual(rc.get("/panel/takip").status_code, 200)
        sc = self.app.test_client()
        self.login("uzman2", client=sc)
        self.assertEqual(sc.get("/panel/takip").status_code, 403)
        fid = self.db.scalar("SELECT id FROM service_followups LIMIT 1")
        stok = self.csrf(client=sc)
        self.assertEqual(sc.post(f"/panel/takip/{fid}/kayit", data={"_csrf": stok, "result": "declined"}).status_code, 403)
        self.assertNotIn("zamanı gelen müşteri", sc.get("/panel/").text)
