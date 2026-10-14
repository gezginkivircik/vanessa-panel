"""Panel kullanıcı yönetimi (yalnızca yönetici)."""
from __future__ import annotations

import re
import secrets

from ..audit import audit, diff
from ..errors import ValidationError
from ..db import Connection, IntegrityError
from ..security.auth import revoke_user_sessions, set_password
from ..security.passwords import hash_password, password_problems
from ..security.rbac import ROLE_LABELS
from ..timeutil import now_iso

USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,30}$")


def _validate(db: Connection, form: dict, user_id: int | None) -> dict:
    errors: dict[str, str] = {}
    data = {
        "username": (form.get("username") or "").strip().lower(),
        "full_name": (form.get("full_name") or "").strip()[:120],
        "email": (form.get("email") or "").strip()[:160] or None,
        "role": form.get("role") or "",
        "staff_id": int(form["staff_id"]) if str(form.get("staff_id") or "").isdigit() else None,
    }
    if not USERNAME_RE.match(data["username"]):
        errors["username"] = "Kullanıcı adı 3-30 karakter; küçük harf, rakam, nokta, tire veya alt çizgi olmalı."
    if len(data["full_name"]) < 2:
        errors["full_name"] = "Ad soyad gerekli."
    if data["role"] not in ROLE_LABELS:
        errors["role"] = "Geçerli bir rol seçin."
    if data["role"] == "specialist" and not data["staff_id"]:
        errors["staff_id"] = "Uzman rolündeki kullanıcı bir personel kaydına bağlanmalı."
    if data["staff_id"] and not db.scalar("SELECT 1 FROM staff WHERE id = ? AND deleted_at IS NULL", (data["staff_id"],)):
        errors["staff_id"] = "Personel bulunamadı."
    if data["email"] and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", data["email"]):
        errors["email"] = "E-posta adresi geçersiz."
    dup = db.scalar("SELECT id FROM users WHERE username = ? AND deleted_at IS NULL", (data["username"],))
    if dup and dup != user_id:
        errors["username"] = "Bu kullanıcı adı kullanılıyor."
    if errors:
        raise ValidationError(errors)
    return data


def _active_admin_count(db: Connection, excluding: int | None = None) -> int:
    return db.scalar("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1 AND deleted_at IS NULL "
                     "AND id <> ?", (excluding or -1,)) or 0


def create_user(db: Connection, form: dict, password: str | None = None) -> tuple[int, str]:
    """Yeni kullanıcı; geçici şifre döner (ilk girişte değiştirmesi zorunlu)."""
    data = _validate(db, form, None)
    temp = password or secrets.token_urlsafe(9)
    probs = password_problems(temp, data["username"]) if password else []
    if probs:
        raise ValidationError({"password": " ".join(probs)})
    now = now_iso()
    with db.transaction():
        try:
            uid = db.insert("users", {**data, "password_hash": hash_password(temp), "must_change_password": 1,
                                      "created_at": now, "updated_at": now})
        except IntegrityError:
            raise ValidationError({"username": "Bu kullanıcı adı kullanılıyor."})
        audit("user.create", "user", uid, f"{data['full_name']} ({ROLE_LABELS[data['role']]}) kullanıcısı oluşturuldu",
              after=data, db=db)
    return uid, temp


def update_user(db: Connection, user_id: int, form: dict, acting_user_id: int) -> None:
    before = db.one("SELECT username, full_name, email, role, staff_id FROM users WHERE id = ? AND deleted_at IS NULL",
                    (user_id,))
    if not before:
        raise ValidationError({"_": "Kullanıcı bulunamadı."})
    data = _validate(db, form, user_id)
    if before["role"] == "admin" and data["role"] != "admin" and _active_admin_count(db, user_id) == 0:
        raise ValidationError({"role": "Sistemde en az bir aktif yönetici kalmalı."})
    if user_id == acting_user_id and data["role"] != before["role"]:
        raise ValidationError({"role": "Kendi rolünüzü değiştiremezsiniz."})
    b, a = diff(before, data)
    if not a:
        return
    with db.transaction():
        db.update("users", user_id, {**data, "updated_at": now_iso()})
        if "role" in a:
            revoke_user_sessions(db, user_id)   # yetki değişti → yeniden giriş
        audit("user.update", "user", user_id, f"{data['full_name']} kullanıcısı güncellendi", before=b, after=a, db=db)


def set_active(db: Connection, user_id: int, active: bool, acting_user_id: int) -> None:
    user = db.one("SELECT id, full_name, role, is_active FROM users WHERE id = ? AND deleted_at IS NULL", (user_id,))
    if not user:
        raise ValidationError({"_": "Kullanıcı bulunamadı."})
    if user_id == acting_user_id and not active:
        raise ValidationError({"_": "Kendi hesabınızı pasif yapamazsınız."})
    if not active and user["role"] == "admin" and _active_admin_count(db, user_id) == 0:
        raise ValidationError({"_": "Sistemde en az bir aktif yönetici kalmalı."})
    with db.transaction():
        db.update("users", user_id, {"is_active": int(active), "updated_at": now_iso()})
        if not active:
            revoke_user_sessions(db, user_id)
        audit("user.activate" if active else "user.deactivate", "user", user_id,
              f"{user['full_name']} {'aktif' if active else 'pasif'} yapıldı", db=db)


def reset_password(db: Connection, user_id: int) -> str:
    user = db.one("SELECT id, full_name FROM users WHERE id = ? AND deleted_at IS NULL", (user_id,))
    if not user:
        raise ValidationError({"_": "Kullanıcı bulunamadı."})
    temp = secrets.token_urlsafe(9)
    with db.transaction():
        set_password(db, user_id, temp, must_change=True)
        revoke_user_sessions(db, user_id)
        audit("user.password_reset", "user", user_id, f"{user['full_name']} için şifre sıfırlandı", db=db)
    return temp
