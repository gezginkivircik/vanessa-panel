"""Audit log: kim, ne zaman, hangi kayıt, ne değişti."""
from __future__ import annotations

from typing import Any

from flask import g, has_request_context, request

from .timeutil import now_iso

# Audit kaydına asla yazılmayacak alanlar
_REDACT = {"password_hash", "token_hash", "csrf_token", "reschedule_token_hash"}


def _clean(d: dict | None) -> dict | None:
    if d is None:
        return None
    return {k: ("[gizli]" if k in _REDACT else v) for k, v in d.items()}


def diff(before: dict, after: dict) -> tuple[dict, dict]:
    """Yalnızca değişen alanları döner."""
    keys = [k for k in after if k in before and before[k] != after[k] and k != "updated_at"]
    return {k: before[k] for k in keys}, {k: after[k] for k in keys}


_UNSET: Any = object()


def audit(action: str, entity_type: str | None = None, entity_id: int | None = None,
          summary: str | None = None, before: dict | None = None, after: dict | None = None,
          *, db=None, actor: Any = _UNSET) -> None:
    from .db import get_db

    db = db or get_db()
    user = g.get("user") if (actor is _UNSET and has_request_context()) else (None if actor is _UNSET else actor)
    ip = None
    if has_request_context():
        from .security.auth import client_ip
        ip = client_ip()
    db.insert("audit_logs", {
        "user_id": user["id"] if user else None,
        "actor_label": user["full_name"] if user else "Sistem",
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "summary": (summary or "")[:500],
        "before_json": _clean(before),
        "after_json": _clean(after),
        "ip_address": ip,
        "created_at": now_iso(),
    }, returning="")
