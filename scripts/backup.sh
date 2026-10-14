#!/usr/bin/env bash
# Günlük PostgreSQL yedeği (cron: 0 3 * * *). 30 günden eski yedekler silinir.
# Gerekli: DATABASE_URL, BACKUP_DIR. Yedekleri ayrıca sunucu dışı depolamaya kopyalayın.
set -euo pipefail
: "${DATABASE_URL:?}"; : "${BACKUP_DIR:?}"
mkdir -p "$BACKUP_DIR"
f="$BACKUP_DIR/vanessa-$(date -u +%Y%m%dT%H%M%SZ).dump"
pg_dump --format=custom --no-owner "$DATABASE_URL" > "$f"
pg_restore --list "$f" > /dev/null   # yedeğin okunabilir olduğunu doğrula
find "$BACKUP_DIR" -name 'vanessa-*.dump' -mtime +30 -delete
echo "OK $f"
# Geri yükleme: pg_restore --clean --no-owner -d "$DATABASE_URL" vanessa-XXXX.dump
