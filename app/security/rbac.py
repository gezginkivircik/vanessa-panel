"""Rol bazlı erişim kontrolü. Bütün kontroller sunucu tarafında yapılır."""
from __future__ import annotations

from functools import wraps

from flask import abort, g, redirect, request, url_for

ROLE_LABELS = {"admin": "Yönetici", "reception": "Resepsiyon", "specialist": "Uzman"}

# İzinler bilerek ayrıntılı tutuldu; yeni ekranlar mevcut izinlere bağlanır.
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "admin": frozenset({
        "dashboard.view", "dashboard.finance",
        "customers.view", "customers.edit", "customers.delete", "customers.view_all",
        "notes.write",
        "appointments.view", "appointments.view_all", "appointments.manage",
        "sessions.manage",
        "payments.view", "payments.manage",
        "services.manage", "staff.manage",
        "messages.view", "messages.send_manual", "templates.manage", "campaigns.manage",
        "reports.view", "export.data",
        "users.manage", "settings.manage", "audit.view",
        "notifications.view",
    }),
    "reception": frozenset({
        "dashboard.view",
        "customers.view", "customers.edit", "customers.view_all",
        "notes.write",
        "appointments.view", "appointments.view_all", "appointments.manage",
        "sessions.manage",
        "payments.view", "payments.manage",          # tahsilat girer; ciro raporu göremez
        "messages.view", "messages.send_manual",
        "notifications.view",
    }),
    "specialist": frozenset({
        "dashboard.view",
        "customers.view",                            # yalnızca kendi randevusu olan müşteriler
        "notes.write",
        "appointments.view",                         # yalnızca kendi randevuları
        "sessions.manage",
        "notifications.view",
    }),
}


def has_permission(user: dict | None, permission: str) -> bool:
    if not user or not user.get("is_active"):
        return False
    return permission in ROLE_PERMISSIONS.get(user.get("role", ""), frozenset())


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.get("user") is None:
            if request.path.startswith("/api/"):
                abort(401)
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        if g.user.get("must_change_password") and request.endpoint not in {
            "auth.change_password", "auth.logout", "static"
        }:
            return redirect(url_for("auth.change_password"))
        return view(*args, **kwargs)
    return wrapper


def require_permission(*permissions: str):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapper(*args, **kwargs):
            if not all(has_permission(g.user, p) for p in permissions):
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return decorator
