import os
import re
import shutil
import tempfile
import unittest

from app import create_app
from app.db import connect, migrate
from app.seed import seed_demo
from app import timeutil

PASSWORD = "TestSifre2026"
_TEMPLATE_DB: str | None = None


def _template_db() -> str:
    """Demo verisi bir kez yüklenir, her test kopyasını kullanır (hızlı)."""
    global _TEMPLATE_DB
    if _TEMPLATE_DB is None:
        d = tempfile.mkdtemp(prefix="vanessa-tpl-")
        path = os.path.join(d, "tpl.db")
        c = connect(f"sqlite:///{path}")
        migrate(c)
        seed_demo(c, PASSWORD)
        c.raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
        _TEMPLATE_DB = path
    return _TEMPLATE_DB


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vanessa-test-")
        self.db_path = os.path.join(self.tmp, "test.db")
        shutil.copy(_template_db(), self.db_path)
        self.url = f"sqlite:///{self.db_path}"
        self.app = create_app({"DATABASE_URL": self.url, "TESTING": True, "SECRET_KEY": "x" * 40})
        self.client = self.app.test_client()
        self.db = connect(self.url)

    def tearDown(self):
        timeutil.set_clock(None)
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ yardımcılar
    def login(self, username="sirin", password=PASSWORD, client=None, follow=False):
        client = client or self.client
        page = client.get("/giris")
        token = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
        return client.post("/giris", data={"_csrf": token, "username": username, "password": password},
                           follow_redirects=follow)

    def csrf(self, client=None, path="/panel/sifre"):
        client = client or self.client
        page = client.get(path)
        return re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
