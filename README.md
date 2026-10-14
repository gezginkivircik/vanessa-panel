# Vanessa Güzellik Merkezi — Yönetim Platformu

Müşteri, randevu, seans, ödeme ve WhatsApp otomasyonu için yönetim sistemi.
Mimari ve faz planı için bkz. [`docs/MIMARI.md`](docs/MIMARI.md).

## Hızlı başlangıç (geliştirme)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export FLASK_APP=wsgi.py
flask init-db                                   # şemayı kurar
flask seed-demo --password 'VanessaDemo2026'    # demo verisi (yalnızca boş veritabanına yüklenir)
flask run                                       # http://127.0.0.1:5000
```

Panel adresi: `http://127.0.0.1:5000/giris`. Demo kullanıcılar: `sirin` (Yönetici), `resepsiyon` (Resepsiyon), `uzman2` (Uzman).

## Testler

```bash
python -m unittest discover -s tests -t .
```

## Üretim

```bash
export VANESSA_ENV=production VANESSA_SECRET_KEY=... DATABASE_URL=postgresql://...
flask init-db
flask create-user                  # ilk yönetici hesabı
gunicorn -w 3 -b 0.0.0.0:8000 wsgi:app
```

Uygulamayı HTTPS arkasında çalıştırın. Reverse proxy kullanıyorsanız `VANESSA_TRUST_PROXY=1` ayarlayın. Yedekleme için `scripts/backup.sh` kullanılır.

## Logo

Orijinal logo dosyasını `app/static/brand/logo.svg` (veya `.png`) olarak koyun. Dosya oradaysa sistem onu hiç değiştirmeden gösterir. Dosya yoksa yalnızca geçici bir yazı (wordmark) görünür.
