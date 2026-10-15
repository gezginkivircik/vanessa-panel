"""Dashboard metrikleri — doğrudan veritabanından hesaplanır."""
from __future__ import annotations

import datetime as dt

from ..db import Connection
from ..timeutil import local_day_bounds_utc, local_month_bounds_utc, local_today, now_iso

STATUS_LABELS = {
    "pending": "Beklemede", "confirmed": "Onaylandı", "arrived": "Geldi", "completed": "İşlem Tamamlandı",
    "cancelled": "İptal Edildi", "no_show": "Gelmedi", "postponed": "Ertelendi",
    "reschedule_needed": "Yeniden Planlama",
}
CANCELLED = ("cancelled",)


def _scope(user: dict, alias: str = "a") -> tuple[str, list]:
    """Uzman yalnızca kendi randevularını görür."""
    if user["role"] == "specialist":
        return f" AND {alias}.staff_id = ?", [user["staff_id"] or -1]
    return "", []


def package_progress(db: Connection, customer_package_id: int) -> dict | None:
    row = db.one(
        """SELECT cp.id, cp.name, cp.status,
                  (SELECT MAX(total_sessions) FROM session_areas WHERE customer_package_id = cp.id) AS total,
                  (SELECT COUNT(DISTINCT session_number) FROM sessions
                    WHERE customer_package_id = cp.id AND status = 'completed') AS done
             FROM customer_packages cp WHERE cp.id = ?""",
        (customer_package_id,),
    )
    if not row or not row["total"]:
        return None
    return {"name": row["name"], "total": row["total"], "done": row["done"], "next": min(row["done"] + 1, row["total"])}


def get_dashboard(db: Connection, user: dict, finance: bool) -> dict:
    today = local_today()
    t0, t1 = local_day_bounds_utc(today)
    y0, y1 = local_day_bounds_utc(today + dt.timedelta(days=1))
    m0, m1 = local_month_bounds_utc(today)
    scope, sp = _scope(user)

    by_status = {r["status"]: r["n"] for r in db.query(
        f"SELECT a.status, COUNT(*) AS n FROM appointments a WHERE a.starts_at >= ? AND a.starts_at < ?{scope} "
        "GROUP BY a.status", [t0, t1] + sp)}
    today_total = sum(by_status.values())

    def count(where: str, params: list) -> int:
        return db.scalar(f"SELECT COUNT(*) FROM appointments a WHERE {where}{scope}", params + sp) or 0

    data = {
        "today": today,
        "today_total": today_total,
        "today_by_status": by_status,
        "tomorrow_total": count("a.starts_at >= ? AND a.starts_at < ? AND a.status <> 'cancelled'", [y0, y1]),
        "pending_confirmations": count("a.status = 'pending' AND a.starts_at >= ?", [now_iso()]),
        "arrived_today": by_status.get("arrived", 0) + by_status.get("completed", 0),
        "cancelled_today": by_status.get("cancelled", 0),
        "month_appointments": count("a.starts_at >= ? AND a.starts_at < ? AND a.status <> 'cancelled'", [m0, m1]),
        "total_customers": db.scalar("SELECT COUNT(*) FROM customers WHERE deleted_at IS NULL") or 0,
        "month_revenue": None,
        "month_collected": None,
    }
    if finance:
        # Ciro = bu ay oluşturulan satış kalemleri (paket/işlem) — tahsil edilen ayrıca gösterilir.
        data["month_revenue"] = db.scalar(
            "SELECT COALESCE(SUM(total_kurus - discount_kurus), 0) FROM payments "
            "WHERE status <> 'void' AND created_at >= ? AND created_at < ?", (m0, m1))
        data["month_collected"] = db.scalar(
            "SELECT COALESCE(SUM(amount_kurus), 0) FROM payment_transactions WHERE paid_at >= ? AND paid_at < ?",
            (m0, m1))

    base = f"""SELECT a.id, a.starts_at, a.ends_at, a.status, a.customer_package_id,
                      c.id AS customer_id, c.first_name, c.last_name,
                      s.name AS service_name, st.full_name AS staff_name, st.color AS staff_color
                 FROM appointments a
                 JOIN customers c ON c.id = a.customer_id
                 JOIN services s ON s.id = a.service_id
                 JOIN staff st ON st.id = a.staff_id
                WHERE 1 = 1{scope}"""
    nxt = db.one(base + " AND a.starts_at >= ? AND a.status IN ('pending','confirmed') ORDER BY a.starts_at LIMIT 1",
                 sp + [now_iso()])
    if nxt and nxt["customer_package_id"]:
        nxt["package"] = package_progress(db, nxt["customer_package_id"])
    data["next_appointment"] = nxt
    data["today_list"] = db.query(base + " AND a.starts_at >= ? AND a.starts_at < ? ORDER BY a.starts_at, st.sort_order",
                                  sp + [t0, t1])
    data["unread_notifications"] = db.query(
        "SELECT id, title, body, link, created_at FROM notifications WHERE read_at IS NULL "
        "AND (user_id IS NULL OR user_id = ?) ORDER BY created_at DESC LIMIT 5", (user["id"],)
    ) if user["role"] != "specialist" else []
    return data
