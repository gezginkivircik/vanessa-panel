"""Şifre hash'leme: scrypt (memory-hard), rastgele tuz, sabit zamanlı karşılaştırma."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

_N, _R, _P, _DKLEN = 2 ** 15, 8, 1, 32
_MAXMEM = 128 * _N * _R * 2


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not password:
        raise ValueError("Şifre boş olamaz")
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN, maxmem=_MAXMEM)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        n, r, p = int(n), int(r), int(p)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                            dklen=len(expected), maxmem=128 * n * r * 2)
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


# Zamanlama saldırısına karşı: kullanıcı bulunamasa da aynı maliyette doğrulama yapılır.
_DUMMY_HASH: str | None = None


def dummy_verify(password: str) -> None:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("dummy-password-for-timing")
    verify_password(password, _DUMMY_HASH)


def password_problems(password: str, username: str | None = None) -> list[str]:
    problems = []
    if len(password) < 10:
        problems.append("Şifre en az 10 karakter olmalı.")
    if password.isdigit() or password.isalpha():
        problems.append("Şifre hem harf hem rakam içermeli.")
    if username and username.lower() in password.lower():
        problems.append("Şifre kullanıcı adını içermemeli.")
    if len(password) > 200:
        problems.append("Şifre çok uzun.")
    return problems
