"""Veritabanı katmanı.

Tek bir arayüz üzerinden SQLite (geliştirme/test) ve PostgreSQL (üretim) desteklenir.
SQL'ler `?` parametre işaretiyle yazılır; PostgreSQL'de otomatik `%s`'e çevrilir.
Bütün satırlar dict olarak döner; tarih/saat değerleri ISO string'e normalize edilir.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from flask import current_app, g

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_MACROS = {
    "sqlite": {
        "@ID": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "@REF": "INTEGER",
        "@TS": "TEXT",
        "@DATE": "TEXT",
        "@TIME": "TEXT",
        "@JSON": "TEXT",
        "@NOW": "(strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
    },
    "postgres": {
        "@ID": "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY",
        "@REF": "BIGINT",
        "@TS": "TIMESTAMPTZ",
        "@DATE": "DATE",
        "@TIME": "TIME",
        "@JSON": "JSONB",
        "@NOW": "now()",
    },
}


class IntegrityError(Exception):
    """Motorlardan bağımsız bütünlük hatası (unique / FK / check / exclusion)."""

    def __init__(self, message: str, constraint: str | None = None):
        super().__init__(message)
        self.constraint = constraint


def compile_sql(template: str, dialect: str) -> str:
    out = template
    # Uzun makrolar önce (ör. @REF, @ID çakışmasın diye sıralı)
    for macro in sorted(_MACROS[dialect], key=len, reverse=True):
        out = re.sub(re.escape(macro) + r"\b", _MACROS[dialect][macro], out)
    return out


def split_statements(sql: str) -> list[str]:
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    cleaned = "\n".join(re.sub(r"\s--\s.*$", "", ln) for ln in lines)
    return [s.strip() for s in cleaned.split(";") if s.strip()]


def _normalize(value: Any) -> Any:
    if isinstance(value, _dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, _dt.time):
        return value.strftime("%H:%M")
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


class Connection:
    def __init__(self, raw, dialect: str):
        self.raw = raw
        self.dialect = dialect
        self._tx_depth = 0
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ sql
    def _prep(self, sql: str) -> str:
        if self.dialect == "postgres":
            return sql.replace("%", "%%").replace("?", "%s")
        return sql

    def _cursor_exec(self, sql: str, params: Iterable[Any] = ()):
        params = tuple(params)
        try:
            cur = self.raw.cursor()
            cur.execute(self._prep(sql), params)
            return cur
        except sqlite3.IntegrityError as exc:  # noqa: PERF203
            msg = str(exc)
            constraint = "ex_appointments_no_overlap" if "ex_appointments_no_overlap" in msg else None
            raise IntegrityError(msg, constraint) from exc
        except Exception as exc:  # psycopg hataları
            if self.dialect == "postgres" and exc.__class__.__name__ in {
                "UniqueViolation", "ForeignKeyViolation", "CheckViolation",
                "ExclusionViolation", "NotNullViolation", "IntegrityError",
            }:
                diag = getattr(exc, "diag", None)
                raise IntegrityError(str(exc), getattr(diag, "constraint_name", None)) from exc
            raise

    def _rows(self, cur) -> list[dict]:
        if cur.description is None:
            return []
        cols = [d[0] for d in cur.description]
        return [{c: _normalize(v) for c, v in zip(cols, row)} for row in cur.fetchall()]

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return self._rows(self._cursor_exec(sql, params))

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = self.one(sql, params)
        return None if row is None else next(iter(row.values()))

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        cur = self._cursor_exec(sql, params)
        return cur.rowcount

    def insert(self, table: str, data: dict[str, Any], returning: str = "id") -> Any:
        _check_ident(table)
        cols = list(data.keys())
        for c in cols:
            _check_ident(c)
        sql = (
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
            + (f" RETURNING {returning}" if returning else "")
        )
        cur = self._cursor_exec(sql, [_param(v) for v in data.values()])
        if returning:
            row = cur.fetchone()
            return row[0] if row else None
        return None

    def update(self, table: str, row_id: Any, data: dict[str, Any], key: str = "id") -> int:
        _check_ident(table)
        _check_ident(key)
        for c in data:
            _check_ident(c)
        sets = ", ".join(f"{c} = ?" for c in data)
        return self.execute(
            f"UPDATE {table} SET {sets} WHERE {key} = ?",
            [_param(v) for v in data.values()] + [row_id],
        )

    # ---------------------------------------------------------- transaction
    @contextmanager
    def transaction(self, write: bool = True):
        """İç içe çağrılabilir transaction.

        SQLite'ta yazma transaction'ı `BEGIN IMMEDIATE` ile açılır; böylece aynı anda yalnızca bir
        yazıcı kontrol + yazma adımını yürütebilir (randevu çakışma kontrolü için kritik).
        """
        with self._lock:
            if self._tx_depth == 0:
                if self.dialect == "sqlite":
                    self.raw.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                else:
                    self.raw.execute("BEGIN")
                self._tx_depth = 1
                try:
                    yield self
                except BaseException:
                    self._tx_depth = 0
                    self.raw.execute("ROLLBACK")
                    raise
                else:
                    self._tx_depth = 0
                    self.raw.execute("COMMIT")
            else:
                name = f"sp_{self._tx_depth}"
                self._tx_depth += 1
                self.raw.execute(f"SAVEPOINT {name}")
                try:
                    yield self
                except BaseException:
                    self.raw.execute(f"ROLLBACK TO SAVEPOINT {name}")
                    self.raw.execute(f"RELEASE SAVEPOINT {name}")
                    raise
                else:
                    self.raw.execute(f"RELEASE SAVEPOINT {name}")
                finally:
                    self._tx_depth -= 1

    def close(self):
        try:
            self.raw.close()
        except Exception:  # pragma: no cover
            pass


_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _check_ident(name: str) -> None:
    if not _IDENT.match(name):
        raise ValueError(f"Geçersiz SQL tanımlayıcısı: {name!r}")


def _param(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return int(v)
    return v


def json_value(v: Any) -> Any:
    """JSON sütunlarını (SQLite: str, PG: dict) Python nesnesine çevirir."""
    if v is None or isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return v


# ---------------------------------------------------------------- bağlantı
def dialect_from_url(url: str) -> str:
    if url.startswith("sqlite:"):
        return "sqlite"
    if url.startswith(("postgres://", "postgresql://")):
        return "postgres"
    raise ValueError("DATABASE_URL 'sqlite:///…' veya 'postgresql://…' olmalı")


def connect(url: str) -> Connection:
    dialect = dialect_from_url(url)
    if dialect == "sqlite":
        path = url.split("sqlite:///", 1)[1]
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10)
        raw.execute("PRAGMA foreign_keys = ON")
        raw.execute("PRAGMA busy_timeout = 10000")
        if path != ":memory:":
            raw.execute("PRAGMA journal_mode = WAL")
        return Connection(raw, "sqlite")
    try:
        import psycopg  # type: ignore
    except ImportError as exc:  # pragma: no cover - üretim ortamında kurulur
        raise RuntimeError("PostgreSQL için 'psycopg[binary]' paketi gerekli (requirements.txt)") from exc
    raw = psycopg.connect(url, autocommit=True)
    raw.execute("SET TIME ZONE 'UTC'")
    return Connection(raw, "postgres")


def get_db() -> Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE_URL"])
    return g.db


def close_db(_exc=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------- migration
def _backfill_search_name(conn: Connection) -> None:
    from .textutil import fold
    for r in conn.query("SELECT id, first_name, last_name FROM customers"):
        conn.execute("UPDATE customers SET search_name = ? WHERE id = ?",
                     (fold(f"{r['first_name']} {r['last_name']}"), r["id"]))


# SQL ile yapılamayan veri dönüşümleri (migration ile aynı transaction'da çalışır)
MIGRATION_HOOKS = {"002_customer_search.sql": _backfill_search_name}


def migrate(conn: Connection) -> list[str]:
    """Uygulanmamış migration dosyalarını sırayla uygular. Uygulananların adını döner."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (name VARCHAR(120) PRIMARY KEY, applied_at VARCHAR(30) NOT NULL)"
    )
    applied = {r["name"] for r in conn.query("SELECT name FROM schema_migrations")}
    done: list[str] = []
    files = sorted(p for p in MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    for f in files:
        if f.name in applied:
            continue
        sql = compile_sql(f.read_text(encoding="utf-8"), conn.dialect)
        with conn.transaction():
            for stmt in split_statements(sql):
                conn.execute(stmt)
            if f.name == "001_init.sql":
                if conn.dialect == "postgres":
                    extras = (MIGRATIONS_DIR / "postgres_extras.sql").read_text(encoding="utf-8")
                    for stmt in split_statements(extras):
                        conn.execute(stmt)
                else:
                    extras = (MIGRATIONS_DIR / "sqlite_extras.sql").read_text(encoding="utf-8")
                    body = "\n".join(ln for ln in extras.splitlines() if not ln.strip().startswith("--"))
                    for stmt in re.split(r"(?m)^END;\s*$", body):
                        if stmt.strip():
                            conn.execute(stmt.strip() + "\nEND")
            hook = MIGRATION_HOOKS.get(f.name)
            if hook:
                hook(conn)
            conn.execute(
                "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                (f.name, _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
            )
        done.append(f.name)
    return done
