"""Türkçe metin ve telefon yardımcıları."""
from __future__ import annotations

import re

_FOLD = str.maketrans({"ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u", "â": "a", "î": "i", "û": "u"})


def tr_lower(s: str) -> str:
    return s.replace("I", "ı").replace("İ", "i").lower()


def tr_title(s: str) -> str:
    """'ayşe NUR' → 'Ayşe Nur' (Türkçe i/İ kurallarıyla)."""
    def cap(w: str) -> str:
        if not w:
            return w
        first = w[0]
        first = "İ" if first == "i" else "I" if first == "ı" else first.upper()
        return first + tr_lower(w[1:])
    return " ".join(cap(w) for w in re.split(r"\s+", s.strip()) if w)


def fold(s: str | None) -> str:
    """Arama için: küçük harf + Türkçe karakterleri ASCII'ye indir. 'AYŞE Yılmaz' → 'ayse yilmaz'."""
    return re.sub(r"\s+", " ", tr_lower(s or "").translate(_FOLD)).strip()


def normalize_phone(raw: str | None) -> str | None:
    """Türkiye cep telefonunu E.164'e çevirir: '0533 150 43 03' → '+905331504303'. Geçersizse None."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0090"):
        digits = digits[4:]
    elif digits.startswith("90") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) == 10 and digits.startswith("5"):
        return "+90" + digits
    return None


def format_phone(e164: str | None) -> str:
    """'+905331504303' → '0533 150 43 03'."""
    if not e164 or not e164.startswith("+90") or len(e164) != 13:
        return e164 or "—"
    d = e164[3:]
    return f"0{d[0:3]} {d[3:6]} {d[6:8]} {d[8:10]}"


def mask_phone(e164: str | None) -> str:
    """Uzman rolü için: '0533 *** ** 03'."""
    f = format_phone(e164)
    if len(f) != 14:
        return "—"
    return f"{f[:4]} *** ** {f[-2:]}"


def phone_search_digits(q: str) -> str | None:
    """Arama kutusundaki rakamları telefon araması için hazırlar: '0533' → '533'."""
    digits = re.sub(r"\D", "", q)
    if len(digits) < 3:
        return None
    if digits.startswith("90") and len(digits) > 10:
        digits = digits[2:]
    if digits.startswith("0"):
        digits = digits[1:]
    return digits or None
