"""Komut satırı: veritabanı kurulumu, demo verisi, kullanıcı oluşturma."""
from __future__ import annotations

import click
from flask import Flask

from .db import connect, migrate


def register_cli(app: Flask) -> None:
    def _conn():
        return connect(app.config["DATABASE_URL"])

    @app.cli.command("init-db")
    def init_db():
        """Migration'ları uygular, temel ayar ve şablonları yükler."""
        from .seed import seed_settings_and_templates
        c = _conn()
        applied = migrate(c)
        with c.transaction():
            seed_settings_and_templates(c)
            from .security.ratelimit import cleanup
            cleanup(c)          # 7 günden eski hız sınırı kayıtlarını sil
        click.echo(f"Uygulanan migration: {', '.join(applied) or 'yok (güncel)'}")
        _bootstrap(c)

    def _bootstrap(c):
        """Shell erişimi olmayan barındırmalarda ilk kurulum (yalnızca veritabanı boşken).
        VANESSA_DEMO=1 + VANESSA_DEMO_PASSWORD → demo verisi; VANESSA_ADMIN_PASSWORD → yalnızca 'sirin' yöneticisi."""
        import os
        from .seed import has_data, seed_demo
        if has_data(c):
            return
        demo_pw = os.environ.get("VANESSA_DEMO_PASSWORD", "")
        admin_pw = os.environ.get("VANESSA_ADMIN_PASSWORD", "")
        if os.environ.get("VANESSA_DEMO") == "1" and len(demo_pw) >= 10:
            seed_demo(c, demo_pw)
            click.echo("Boş veritabanına demo verisi yüklendi (kullanıcılar: sirin, resepsiyon, uzman2).")
        elif len(admin_pw) >= 10:
            from .security.passwords import hash_password
            from .timeutil import now_iso
            now = now_iso()
            with c.transaction():
                c.insert("users", {"username": "sirin", "full_name": "Şirin Mercan", "role": "admin",
                                   "password_hash": hash_password(admin_pw), "must_change_password": 1,
                                   "created_at": now, "updated_at": now})
            click.echo("İlk yönetici 'sirin' oluşturuldu; ilk girişte şifre değiştirilecek.")

    @app.cli.command("seed-demo")
    @click.option("--password", default=None, help="Bütün demo kullanıcılar için ortak şifre (yalnızca geliştirme).")
    def seed_demo_cmd(password):
        """Demo verisini yükler (boş veritabanında)."""
        from .seed import has_data, seed_demo
        if app.config["ENV"] == "production":
            raise click.ClickException("Üretim ortamında demo verisi yüklenemez.")
        c = _conn()
        migrate(c)
        if has_data(c):
            raise click.ClickException("Veritabanında zaten kullanıcı var. Demo verisi yalnızca boş veritabanına yüklenir.")
        creds = seed_demo(c, password)
        click.echo("Demo verisi yüklendi. Giriş bilgileri:")
        for u, p in creds.items():
            click.echo(f"  {u:<12} {p}")

    @app.cli.command("create-user")
    @click.option("--username", prompt=True)
    @click.option("--full-name", prompt="Ad soyad")
    @click.option("--role", type=click.Choice(["admin", "reception", "specialist"]), prompt=True)
    def create_user_cmd(username, full_name, role):
        """Yeni panel kullanıcısı oluşturur; geçici şifre üretir."""
        from .services.users import ValidationError, create_user
        c = _conn()
        migrate(c)
        with app.test_request_context():
            try:
                uid, temp = create_user(c, {"username": username, "full_name": full_name, "role": role})
            except ValidationError as e:
                raise click.ClickException(" ".join(e.errors.values()))
        click.echo(f"Kullanıcı #{uid} oluşturuldu. Geçici şifre: {temp} (ilk girişte değiştirilecek)")
