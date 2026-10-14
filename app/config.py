"""Ayarlar ortam değişkenlerinden okunur. Secret'lar koda yazılmaz."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class Config:
    ENV = os.environ.get("VANESSA_ENV", "development")
    DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{BASE_DIR / 'instance' / 'vanessa.db'}")
    SECRET_KEY = os.environ.get("VANESSA_SECRET_KEY") or ""

    AUTH_COOKIE_NAME = "vanessa_sid"
    # Flask'ın imzalı cookie'si yalnızca flash mesajları için kullanılır (kimlik bilgisi taşımaz).
    SESSION_COOKIE_NAME = "vanessa_flash"
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_IDLE_MINUTES = int(os.environ.get("VANESSA_SESSION_IDLE_MINUTES", "720"))   # 12 saat
    SESSION_ABSOLUTE_DAYS = int(os.environ.get("VANESSA_SESSION_ABSOLUTE_DAYS", "7"))

    LOGIN_MAX_FAILS_PER_USER = 5
    LOGIN_LOCK_MINUTES = 15
    LOGIN_MAX_ATTEMPTS_PER_IP = 20
    LOGIN_IP_WINDOW_MINUTES = 15

    TRUST_PROXY = os.environ.get("VANESSA_TRUST_PROXY", "0") == "1"   # reverse proxy arkasında X-Forwarded-For

    WHATSAPP_PROVIDER = os.environ.get("WHATSAPP_PROVIDER", "mock")

    MAX_CONTENT_LENGTH = 5 * 1024 * 1024
    TESTING = False

    @classmethod
    def validate(cls, cfg: dict) -> None:
        if cfg["ENV"] == "production":
            if len(cfg.get("SECRET_KEY") or "") < 32:
                raise RuntimeError("Üretimde VANESSA_SECRET_KEY en az 32 karakter olmalı")
            if not cfg["DATABASE_URL"].startswith(("postgres://", "postgresql://")):
                raise RuntimeError("Üretimde PostgreSQL kullanılmalı (DATABASE_URL)")
        if not cfg.get("SECRET_KEY"):
            # Geliştirmede her başlatmada rastgele anahtar — kalıcı oturum için .env'de tanımlayın.
            cfg["SECRET_KEY"] = secrets.token_hex(32)
