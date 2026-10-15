"""Basit, veritabanı tabanlı hız sınırı (çok sunuculu kurulumda da çalışır)."""
from __future__ import annotations

import datetime as dt
import hashlib

from flask import current_app

from ..db import Connection
from ..timeutil import iso, now_iso, now_utc


def _h(key: str) -> str:
    salt = current_app.config["SECRET_KEY"] if current_app else ""
    return hashlib.sha256(f"{salt}:{key}".encode()).hexdigest()


def count(db: Connection, bucket: str, key: str, window_minutes: int) -> int:
    since = iso(now_utc() - dt.timedelta(minutes=window_minutes))
    return db.scalar("SELECT COUNT(*) FROM rate_events WHERE bucket = ? AND key_hash = ? AND created_at >= ?",
                     (bucket, _h(key), since)) or 0


def hit(db: Connection, bucket: str, key: str) -> None:
    db.insert("rate_events", {"bucket": bucket, "key_hash": _h(key), "created_at": now_iso()}, returning="")


def cleanup(db: Connection, older_than_days: int = 7) -> None:
    db.execute("DELETE FROM rate_events WHERE created_at < ?", (iso(now_utc() - dt.timedelta(days=older_than_days)),))
