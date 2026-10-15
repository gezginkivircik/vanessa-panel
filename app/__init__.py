"""Vanessa Güzellik Merkezi — uygulama fabrikası."""
from __future__ import annotations

import logging
import secrets
from pathlib import Path

from flask import Flask, abort, g, jsonify, render_template, request

from . import timeutil
from .config import Config
from .db import close_db, get_db
from .security.auth import csrf_valid, load_session
from .security.rbac import ROLE_LABELS, has_permission

CSRF_EXEMPT_PREFIXES = ("/webhooks/",)   # webhook'lar imza ile doğrulanır (Faz 10)


def create_app(overrides: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)
    if overrides:
        app.config.update(overrides)
    Config.validate(app.config)
    app.config.update(
        SESSION_COOKIE_SECURE=app.config["ENV"] == "production",
        PREFERRED_URL_SCHEME="https" if app.config["ENV"] == "production" else "http",
    )
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    app.teardown_appcontext(close_db)

    # ------------------------------------------------------------ oturum
    @app.before_request
    def _load_user():
        g.user = None
        g.session = None
        g.csp_nonce = secrets.token_urlsafe(16)
        if request.endpoint == "static":
            return
        token = request.cookies.get(app.config["AUTH_COOKIE_NAME"])
        if token:
            loaded = load_session(get_db(), token)
            if loaded:
                g.session, g.user = loaded

    @app.before_request
    def _csrf_protect():
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return
        if request.path.startswith(CSRF_EXEMPT_PREFIXES):
            return
        if request.endpoint == "auth.login":
            return   # giriş formu kendi ön-oturum CSRF kontrolünü yapar
        if not csrf_valid():
            abort(400, description="Güvenlik doğrulaması başarısız (CSRF). Sayfayı yenileyip tekrar deneyin.")

    # --------------------------------------------------- güvenlik başlıkları
    @app.after_request
    def _security_headers(resp):
        nonce = g.get("csp_nonce", "")
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "frame-src https://www.google.com https://maps.google.com; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if app.config["ENV"] == "production":
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if g.get("user"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ------------------------------------------------------------ şablon
    logo_dir = Path(app.static_folder) / "brand"

    @app.context_processor
    def _inject():
        logo = None
        for name in ("logo.svg", "logo.png", "logo.webp", "logo.jpg"):
            if (logo_dir / name).exists():
                logo = f"brand/{name}"
                break
        return {
            "current_user": g.get("user"),
            "csrf_token": (g.session or {}).get("csrf_token") if g.get("session") else None,
            "can": lambda perm: has_permission(g.get("user"), perm),
            "ROLE_LABELS": ROLE_LABELS,
            "csp_nonce": g.get("csp_nonce", ""),
            "logo_path": logo,
            "BRAND": {
                "name": "Vanessa Güzellik Merkezi",
                "owner": "Şirin Mercan",
                "phone_display": "0533 150 43 03",
                "phone_e164": "+905331504303",
                "address": "Mithat Paşa Caddesi, Ersan Taksi Çaprazı, İskenderun / Hatay",
            },
        }

    app.jinja_env.filters.update(
        date=timeutil.fmt_date,
        time=timeutil.fmt_time,
        datetime=timeutil.fmt_datetime,
        money=lambda kurus: "—" if kurus is None else
        f"{kurus / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".").removesuffix(",00") + " TL",
    )

    # ---------------------------------------------------------- blueprint
    from .views.auth import bp as auth_bp
    from .views.panel import bp as panel_bp
    from .views.public import bp as public_bp
    from .views.customers import bp as customers_bp
    from .views.appointments import bp as appointments_bp
    from .views.catalog import bp as catalog_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(panel_bp)
    app.register_blueprint(customers_bp)
    app.register_blueprint(appointments_bp)
    app.register_blueprint(catalog_bp)

    @app.get("/healthz")
    def healthz():
        try:
            get_db().scalar("SELECT 1")
            return jsonify(status="ok")
        except Exception:  # pragma: no cover
            return jsonify(status="db_error"), 503

    # -------------------------------------------------------------- hatalar
    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(429)
    def _http_error(err):
        titles = {400: "Geçersiz istek", 403: "Bu sayfaya erişim yetkiniz yok",
                  404: "Sayfa bulunamadı", 429: "Çok fazla istek"}
        if request.path.startswith("/api/"):
            return jsonify(error=titles.get(err.code, "Hata")), err.code
        return render_template("error.html", code=err.code, title=titles.get(err.code, "Hata"),
                               description=getattr(err, "description", None)), err.code

    from .cli import register_cli
    register_cli(app)
    return app
