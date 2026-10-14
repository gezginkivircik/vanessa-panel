"""FAZ 1 testleri: şema, kimlik doğrulama, yetkilendirme, CSRF, rate limit, audit, çakışma koruması."""
import datetime as dt
import hashlib
import threading
import unittest

from app import timeutil
from app.db import IntegrityError, connect, migrate
from app.security.passwords import hash_password, verify_password
from app.services.dashboard import package_progress
from tests.helpers import PASSWORD, AppTestCase

REQUIRED_TABLES = """users staff customers customer_consents services staff_services appointments
appointment_status_history packages customer_packages sessions session_areas payments payment_transactions
customer_notes message_templates messages automations campaigns campaign_recipients working_hours staff_breaks
holidays notifications audit_logs settings""".split()


class SchemaTests(AppTestCase):
    def test_required_tables_exist(self):
        names = {r["name"] for r in self.db.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for t in REQUIRED_TABLES + ["user_sessions", "login_attempts", "webhook_events", "service_followups"]:
            self.assertIn(t, names, t)

    def test_migrate_is_idempotent(self):
        self.assertEqual(migrate(self.db), [])

    def test_foreign_keys_enforced(self):
        with self.assertRaises(IntegrityError):
            self.db.execute("INSERT INTO customer_notes (customer_id, body) VALUES (99999, 'x')")

    def test_phone_unique_among_active_customers(self):
        phone = self.db.scalar("SELECT phone FROM customers LIMIT 1")
        with self.assertRaises(IntegrityError):
            self.db.execute("INSERT INTO customers (first_name, last_name, phone) VALUES ('A', 'B', ?)", (phone,))
        # Soft-delete edilmiş kayıt numarayı serbest bırakır
        self.db.execute("UPDATE customers SET deleted_at = ? WHERE phone = ?", (timeutil.now_iso(), phone))
        self.db.execute("INSERT INTO customers (first_name, last_name, phone) VALUES ('A', 'B', ?)", (phone,))

    def test_automation_dedupe_key_unique(self):
        row = {"message_type": "appointment_reminder", "dedupe_key": "reminder:appt:1:24h",
               "scheduled_at": timeutil.now_iso()}
        self.db.insert("automations", row)
        with self.assertRaises(IntegrityError):
            self.db.insert("automations", row)


class OverlapTests(AppTestCase):
    def _appt(self, conn, start, end, active=1, staff=1):
        return conn.insert("appointments", {
            "customer_id": 1, "staff_id": staff, "service_id": 1, "starts_at": start, "ends_at": end,
            "is_active_slot": active, "status": "pending" if active else "cancelled"})

    def test_db_blocks_overlapping_active_appointments(self):
        self._appt(self.db, "2030-01-07T08:00:00Z", "2030-01-07T08:30:00Z")
        self._appt(self.db, "2030-01-07T08:30:00Z", "2030-01-07T09:00:00Z")          # bitişik: serbest
        self._appt(self.db, "2030-01-07T08:10:00Z", "2030-01-07T08:40:00Z", active=0)  # iptal: bloke etmez
        self._appt(self.db, "2030-01-07T08:10:00Z", "2030-01-07T08:40:00Z", staff=2)   # başka uzman: serbest
        with self.assertRaises(IntegrityError) as ctx:
            self._appt(self.db, "2030-01-07T08:15:00Z", "2030-01-07T08:45:00Z")
        self.assertEqual(ctx.exception.constraint, "ex_appointments_no_overlap")

    def test_cancelled_slot_reactivation_is_blocked_if_taken(self):
        self._appt(self.db, "2030-01-07T10:00:00Z", "2030-01-07T10:30:00Z")
        cid = self._appt(self.db, "2030-01-07T10:00:00Z", "2030-01-07T10:30:00Z", active=0)
        with self.assertRaises(IntegrityError):
            self.db.execute("UPDATE appointments SET is_active_slot = 1 WHERE id = ?", (cid,))

    def test_concurrent_booking_only_one_wins(self):
        results = []
        barrier = threading.Barrier(2)

        def worker():
            conn = connect(self.url)
            barrier.wait()
            try:
                with conn.transaction():
                    self._appt(conn, "2030-02-04T09:00:00Z", "2030-02-04T09:45:00Z")
                results.append("ok")
            except IntegrityError:
                results.append("blocked")
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(results), ["blocked", "ok"])


class PasswordTests(unittest.TestCase):
    def test_hash_not_plaintext_and_salted(self):
        h1, h2 = hash_password("Abcdef12345"), hash_password("Abcdef12345")
        self.assertNotIn("Abcdef12345", h1)
        self.assertNotEqual(h1, h2)
        self.assertTrue(h1.startswith("scrypt$"))
        self.assertTrue(verify_password("Abcdef12345", h1))
        self.assertFalse(verify_password("abcdef12345", h1))
        self.assertFalse(verify_password("x", "bozuk-hash"))


class AuthTests(AppTestCase):
    def test_login_page_and_success(self):
        r = self.login()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], "/panel/")
        cookie = [h for h in r.headers.getlist("Set-Cookie") if h.startswith("vanessa_sid=")][0]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        token = cookie.split(";")[0].split("=", 1)[1]
        # DB'de yalnızca özet var
        self.assertIsNone(self.db.one("SELECT 1 FROM user_sessions WHERE token_hash = ?", (token,)))
        self.assertIsNotNone(self.db.one("SELECT 1 FROM user_sessions WHERE token_hash = ?",
                                         (hashlib.sha256(token.encode()).hexdigest(),)))
        self.assertEqual(self.client.get("/panel/").status_code, 200)

    def test_panel_requires_login(self):
        r = self.client.get("/panel/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/giris", r.headers["Location"])

    def test_wrong_password_generic_error(self):
        r = self.login(password="yanlis-sifre-1")
        self.assertEqual(r.status_code, 401)
        self.assertIn("Kullanıcı adı veya şifre hatalı", r.text)
        r2 = self.login(username="olmayan", password="yanlis-sifre-1")
        self.assertIn("Kullanıcı adı veya şifre hatalı", r2.text)

    def test_login_requires_pre_session_csrf(self):
        r = self.client.post("/giris", data={"username": "sirin", "password": PASSWORD})
        self.assertEqual(r.status_code, 401)
        self.assertNotIn("vanessa_sid=", " ".join(r.headers.getlist("Set-Cookie")))

    def test_account_lockout_and_unlock(self):
        for _ in range(5):
            self.login(password="yanlis-sifre-1")
        r = self.login()  # doğru şifre ama kilitli
        self.assertEqual(r.status_code, 401)
        self.assertIn("kilitlendi", r.text)
        self.assertTrue(self.db.scalar("SELECT COUNT(*) FROM audit_logs WHERE action = 'auth.locked'"))
        timeutil.set_clock(timeutil.now_utc() + dt.timedelta(minutes=16))
        self.assertEqual(self.login().status_code, 302)

    def test_ip_rate_limit(self):
        for i in range(20):
            self.login(username=f"yok{i}", password="yanlis-sifre-1")
        r = self.login()
        self.assertIn("Çok fazla deneme", r.text)

    def test_logout_requires_csrf_and_revokes(self):
        self.login()
        self.assertEqual(self.client.post("/cikis").status_code, 400)       # CSRF yok
        token = self.csrf()
        r = self.client.post("/cikis", data={"_csrf": token})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/panel/").status_code, 302)     # oturum kapandı
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM user_sessions WHERE revoked_at IS NULL"), 0)

    def test_session_idle_timeout(self):
        self.login()
        timeutil.set_clock(timeutil.now_utc() + dt.timedelta(hours=13))
        self.assertEqual(self.client.get("/panel/").status_code, 302)

    def test_open_redirect_blocked(self):
        page = self.client.get("/giris")
        import re
        tok = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
        r = self.client.post("/giris", data={"_csrf": tok, "username": "sirin", "password": PASSWORD,
                                             "next": "//kotu-site.com/panel"})
        self.assertEqual(r.headers["Location"], "/panel/")

    def test_inactive_user_cannot_login(self):
        self.db.execute("UPDATE users SET is_active = 0 WHERE username = 'resepsiyon'")
        self.assertEqual(self.login("resepsiyon").status_code, 401)

    def test_security_headers(self):
        r = self.client.get("/giris")
        self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")

    def test_change_password_flow(self):
        self.login()
        tok = self.csrf()
        r = self.client.post("/panel/sifre", data={"_csrf": tok, "current_password": PASSWORD,
                                                   "new_password": "kisa", "new_password_repeat": "kisa"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/panel/sifre", data={"_csrf": tok, "current_password": PASSWORD,
                                                   "new_password": "YeniGuclu2026", "new_password_repeat": "YeniGuclu2026"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.login(password="YeniGuclu2026", client=self.app.test_client()).status_code, 302)


class RbacTests(AppTestCase):
    def test_specialist_cannot_access_admin_pages(self):
        self.login("uzman2")
        for path in ("/panel/kullanicilar", "/panel/islem-kayitlari", "/panel/kullanicilar/yeni"):
            self.assertEqual(self.client.get(path).status_code, 403, path)

    def test_reception_cannot_see_finance_or_audit(self):
        self.login("resepsiyon")
        r = self.client.get("/panel/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Ciro", r.text)
        self.assertNotIn("Tahsilat", r.text)
        self.assertEqual(self.client.get("/panel/islem-kayitlari").status_code, 403)

    def test_admin_sees_finance(self):
        self.login("sirin")
        self.assertIn("Bu Ay Ciro", self.client.get("/panel/").text)

    def test_specialist_dashboard_scoped_to_own_appointments(self):
        self.login("uzman2")
        html = self.client.get("/panel/").text
        self.assertIn("Uzman 2", html)
        self.assertNotIn("Zeynep Kaya", html)    # Şirin Mercan'ın müşterisi
        self.assertNotIn("Ciro", html)

    def test_forged_post_without_permission(self):
        self.login("resepsiyon")
        tok = self.csrf()
        r = self.client.post("/panel/kullanicilar/yeni", data={"_csrf": tok, "username": "hacker",
                                                               "full_name": "X", "role": "admin"})
        self.assertEqual(r.status_code, 403)
        self.assertIsNone(self.db.one("SELECT 1 FROM users WHERE username = 'hacker'"))


class UserManagementTests(AppTestCase):
    def test_create_user_forces_password_change_and_audits(self):
        self.login()
        tok = self.csrf()
        r = self.client.post("/panel/kullanicilar/yeni", data={
            "_csrf": tok, "username": "resepsiyon2", "full_name": "Yeni Resepsiyon", "role": "reception"})
        self.assertEqual(r.status_code, 200)
        import re
        temp = re.search(r'class="secret">([^<]+)<', r.text).group(1)
        row = self.db.one("SELECT * FROM users WHERE username = 'resepsiyon2'")
        self.assertEqual(row["must_change_password"], 1)
        self.assertNotIn(temp, row["password_hash"])
        audit = self.db.one("SELECT * FROM audit_logs WHERE action = 'user.create' AND entity_id = ?", (row["id"],))
        self.assertEqual(audit["actor_label"], "Şirin Mercan")
        self.assertNotIn("password", audit["after_json"] or "")
        # yeni kullanıcı giriş yapınca şifre değiştirmeye yönlenir
        c2 = self.app.test_client()
        self.login("resepsiyon2", temp, client=c2)
        r = c2.get("/panel/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/panel/sifre", r.headers["Location"])

    def test_validation(self):
        self.login()
        tok = self.csrf()
        r = self.client.post("/panel/kullanicilar/yeni", data={"_csrf": tok, "username": "A B", "full_name": "",
                                                               "role": "specialist"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Kullanıcı adı 3-30", r.text)
        self.assertIn("personel kaydına bağlanmalı", r.text)

    def test_cannot_deactivate_self_or_last_admin(self):
        self.login()
        tok = self.csrf()
        me = self.db.scalar("SELECT id FROM users WHERE username = 'sirin'")
        self.client.post(f"/panel/kullanicilar/{me}/durum", data={"_csrf": tok, "active": "0"})
        self.assertEqual(self.db.scalar("SELECT is_active FROM users WHERE id = ?", (me,)), 1)

    def test_role_change_revokes_sessions(self):
        c2 = self.app.test_client()
        self.login("resepsiyon", client=c2)
        self.assertEqual(c2.get("/panel/").status_code, 200)
        self.login()
        tok = self.csrf()
        uid = self.db.scalar("SELECT id FROM users WHERE username = 'resepsiyon'")
        r = self.client.post(f"/panel/kullanicilar/{uid}", data={"_csrf": tok, "username": "resepsiyon",
                                                                 "full_name": "Resepsiyon", "role": "specialist",
                                                                 "staff_id": "3"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(c2.get("/panel/").status_code, 302)   # yeniden giriş gerekli
        log = self.db.one("SELECT * FROM audit_logs WHERE action = 'user.update' AND entity_id = ?", (uid,))
        self.assertIn("reception", log["before_json"])
        self.assertIn("specialist", log["after_json"])

    def test_reset_password(self):
        self.login()
        tok = self.csrf()
        uid = self.db.scalar("SELECT id FROM users WHERE username = 'uzman2'")
        r = self.client.post(f"/panel/kullanicilar/{uid}/sifre-sifirla", data={"_csrf": tok})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.login("uzman2", client=self.app.test_client()).status_code, 401)


class DemoDataTests(AppTestCase):
    def test_demo_customers_are_marked_and_fictional_range(self):
        rows = self.db.query("SELECT phone, is_demo FROM customers")
        self.assertTrue(rows)
        for r in rows:
            self.assertEqual(r["is_demo"], 1)
            self.assertTrue(r["phone"].startswith("+90599000"))

    def test_ayse_laser_package_progress(self):
        cp = self.db.scalar("SELECT cp.id FROM customer_packages cp JOIN customers c ON c.id = cp.customer_id "
                            "WHERE c.first_name = 'Ayşe'")
        p = package_progress(self.db, cp)
        self.assertEqual((p["done"], p["total"], p["next"]), (3, 10, 4))

    def test_dashboard_counts_match_db(self):
        self.login()
        html = self.client.get("/panel/").text
        t0, t1 = timeutil.local_day_bounds_utc(timeutil.local_today())
        n = self.db.scalar("SELECT COUNT(*) FROM appointments WHERE starts_at >= ? AND starts_at < ?", (t0, t1))
        self.assertIn(f">{n}<small>randevu</small>", html.replace("\n", ""))


if __name__ == "__main__":
    unittest.main()
