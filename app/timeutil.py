"""Zaman yardımcıları. Saklama: UTC ISO-8601 ('YYYY-MM-DDTHH:MM:SSZ'). Gösterim: Europe/Istanbul."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Istanbul")
UTC = dt.timezone.utc

TR_MONTHS = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
             "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
TR_WEEKDAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]

_clock_override: dt.datetime | None = None


def now_utc() -> dt.datetime:
    if _clock_override is not None:
        return _clock_override
    return dt.datetime.now(UTC).replace(microsecond=0)


def set_clock(value: dt.datetime | None) -> None:
    """Testler için sabit saat."""
    global _clock_override
    _clock_override = value.astimezone(UTC) if value else None


def iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Saat dilimi olmayan datetime saklanamaz")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return iso(now_utc())


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    v = value.replace("Z", "+00:00")
    d = dt.datetime.fromisoformat(v)
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d


def to_local(value: str | dt.datetime | None) -> dt.datetime | None:
    d = parse_iso(value) if isinstance(value, str) or value is None else value
    return d.astimezone(TZ) if d else None


def local_today() -> dt.date:
    return now_utc().astimezone(TZ).date()


def local_day_bounds_utc(day: dt.date) -> tuple[str, str]:
    """Yerel bir günün [başlangıç, bitiş) aralığını UTC ISO olarak döner."""
    start = dt.datetime.combine(day, dt.time.min, tzinfo=TZ)
    end = start + dt.timedelta(days=1)
    return iso(start), iso(end)


def local_month_bounds_utc(day: dt.date) -> tuple[str, str]:
    first = day.replace(day=1)
    nxt = (first + dt.timedelta(days=32)).replace(day=1)
    return (iso(dt.datetime.combine(first, dt.time.min, tzinfo=TZ)),
            iso(dt.datetime.combine(nxt, dt.time.min, tzinfo=TZ)))


def fmt_date(value, with_weekday: bool = False) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, str) and len(value) == 10:
        d = dt.date.fromisoformat(value)
    elif isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        d = value
    else:
        d = to_local(value).date()
    s = d.strftime("%d.%m.%Y")
    return f"{s} {TR_WEEKDAYS[d.weekday()]}" if with_weekday else s


def fmt_time(value) -> str:
    d = to_local(value)
    return d.strftime("%H:%M") if d else "—"


def fmt_datetime(value) -> str:
    d = to_local(value)
    return d.strftime("%d.%m.%Y %H:%M") if d else "—"


def fmt_long_date(day: dt.date) -> str:
    return f"{day.day} {TR_MONTHS[day.month - 1]} {day.year}, {TR_WEEKDAYS[day.weekday()]}"
