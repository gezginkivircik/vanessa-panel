"""Ödeme kalemleri ve tahsilat hareketleri.

Bir ödeme kalemi (payments) bir satıştır: paket, işlem veya manuel borç. Tahsilatlar (payment_transactions)
bu kaleme bağlı hareketlerdir; ödenen tutar ve durum (Ödendi/Kısmi/Ödenmedi) hareketlerden yeniden hesaplanır.
"""
from __future__ import annotations

import datetime as dt

from ..audit import audit
from ..db import Connection
from ..errors import NotFound, ValidationError
from ..timeutil import TZ, iso, local_today, now_iso, now_utc
from .catalog import _money

METHODS = {"cash": "Nakit", "card": "Kredi Kartı", "transfer": "Havale/EFT", "other": "Diğer"}
STATUS = {"paid": "Ödendi", "partial": "Kısmi Ödendi", "unpaid": "Ödenmedi", "void": "İptal"}


def fmt(kurus: int) -> str:
    s = f"{kurus / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return s.removesuffix(",00") + " TL"


def get(db: Connection, payment_id: int) -> dict:
    p = db.one("""SELECT p.*, c.first_name, c.last_name, c.phone FROM payments p
                  JOIN customers c ON c.id = p.customer_id WHERE p.id = ?""", (payment_id,))
    if not p:
        raise NotFound()
    p["transactions"] = db.query("""SELECT t.*, u.full_name AS recorded_by_name FROM payment_transactions t
                                    LEFT JOIN users u ON u.id = t.recorded_by
                                    WHERE t.payment_id = ? ORDER BY t.paid_at, t.id""", (payment_id,))
    p["net"] = p["total_kurus"] - p["discount_kurus"]
    p["remaining"] = p["net"] - p["paid_kurus"]
    return p


def recompute(db: Connection, payment_id: int) -> dict:
    p = db.one("SELECT * FROM payments WHERE id = ?", (payment_id,))
    paid = db.scalar("SELECT COALESCE(SUM(amount_kurus), 0) FROM payment_transactions WHERE payment_id = ?",
                     (payment_id,)) or 0
    net = p["total_kurus"] - p["discount_kurus"]
    if p["status"] == "void":
        status = "void"
    elif paid >= net:
        status = "paid"
    elif paid > 0:
        status = "partial"
    else:
        status = "unpaid"
    db.update("payments", payment_id, {"paid_kurus": paid, "status": status, "updated_at": now_iso()})
    return {"paid": paid, "status": status, "net": net}


# ------------------------------------------------------------------ oluşturma
def create_charge(db: Connection, user: dict | None, customer_id: int, description: str, total_kurus: int,
                  discount_kurus: int = 0, appointment_id: int | None = None,
                  customer_package_id: int | None = None, due_date: str | None = None, audit_it: bool = True) -> int:
    if total_kurus < 0 or discount_kurus < 0 or discount_kurus > total_kurus:
        raise ValidationError({"discount": "İndirim tutarı toplamdan büyük olamaz."})
    now = now_iso()
    pid = db.insert("payments", {
        "customer_id": customer_id, "customer_package_id": customer_package_id, "appointment_id": appointment_id,
        "description": description[:200], "total_kurus": total_kurus, "discount_kurus": discount_kurus,
        "paid_kurus": 0, "status": "paid" if total_kurus - discount_kurus == 0 else "unpaid", "due_date": due_date,
        "created_by": user["id"] if user else None, "created_at": now, "updated_at": now,
    })
    if audit_it:
        audit("payment.create", "payment", pid, f"Ödeme kaydı: {description} · {fmt(total_kurus - discount_kurus)}",
              after={"customer_id": customer_id, "total": total_kurus, "discount": discount_kurus}, db=db,
              **({} if user else {"actor": None}))
    return pid


def create_manual(db: Connection, user: dict, customer_id: int, form: dict) -> int:
    errors: dict[str, str] = {}
    desc = (form.get("description") or "").strip()
    if len(desc) < 2:
        errors["description"] = "Açıklama gerekli."
    total = _money(form.get("total"), "total", errors, "Tutar")
    discount = _money(form.get("discount"), "discount", errors, "İndirim", required=False) or 0
    if total is not None and total <= 0:
        errors["total"] = "Tutar sıfırdan büyük olmalı."
    if errors:
        raise ValidationError(errors)
    with db.transaction():
        pid = create_charge(db, user, customer_id, desc, total, discount)
        if form.get("pay_now"):
            add_transaction(db, user, pid, {"amount": form.get("pay_amount") or str((total - discount) / 100),
                                            "method": form.get("method", "cash")})
    return pid


def charge_for_completed_appointment(db: Connection, user: dict | None, a: dict) -> int | None:
    """Paketsiz, ücretli bir randevu tamamlanınca borç kaydı oluşturur (yoksa)."""
    if a.get("customer_package_id") or not a.get("price_kurus"):
        return None
    if db.scalar("SELECT 1 FROM payments WHERE appointment_id = ? AND status <> 'void'", (a["id"],)):
        return None
    name = db.scalar("SELECT name FROM services WHERE id = ?", (a["service_id"],))
    return create_charge(db, user, a["customer_id"], name, a["price_kurus"], appointment_id=a["id"], audit_it=False)


# ------------------------------------------------------------------ tahsilat
def add_transaction(db: Connection, user: dict, payment_id: int, form: dict, allow_refund: bool = False) -> int:
    p = db.one("SELECT * FROM payments WHERE id = ?", (payment_id,))
    if not p:
        raise NotFound()
    if p["status"] == "void":
        raise ValidationError({"_": "İptal edilmiş ödeme kaydına tahsilat girilemez."})
    errors: dict[str, str] = {}
    amount = _money(form.get("amount"), "amount", errors, "Tutar")
    method = form.get("method") or ""
    if method not in METHODS:
        errors["method"] = "Ödeme yöntemi seçin."
    refund = bool(form.get("refund")) and allow_refund
    paid_at_raw = (form.get("paid_on") or "").strip()
    if paid_at_raw:
        try:
            d = dt.date.fromisoformat(paid_at_raw)
            if d > local_today():
                errors["paid_on"] = "Gelecek tarihli tahsilat girilemez."
            local_now = now_utc().astimezone(TZ)
            paid_at = iso(dt.datetime.combine(d, local_now.time().replace(microsecond=0), tzinfo=TZ)) \
                if d != local_today() else now_iso()
        except ValueError:
            errors["paid_on"] = "Tarih geçersiz."
            paid_at = now_iso()
    else:
        paid_at = now_iso()
    remaining = p["total_kurus"] - p["discount_kurus"] - p["paid_kurus"]
    if amount is not None and not errors:
        if amount <= 0:
            errors["amount"] = "Tutar sıfırdan büyük olmalı."
        elif refund and amount > p["paid_kurus"]:
            errors["amount"] = f"En fazla {fmt(p['paid_kurus'])} iade edilebilir."
        elif not refund and amount > remaining:
            errors["amount"] = f"Kalan borç {fmt(remaining)}; daha fazlası alınamaz."
    if errors:
        raise ValidationError(errors)
    signed = -amount if refund else amount
    with db.transaction():
        tid = db.insert("payment_transactions", {
            "payment_id": payment_id, "amount_kurus": signed, "method": method, "paid_at": paid_at,
            "note": (form.get("note") or "").strip()[:255] or None, "recorded_by": user["id"], "created_at": now_iso()})
        r = recompute(db, payment_id)
        c = db.one("SELECT first_name, last_name FROM customers WHERE id = ?", (p["customer_id"],))
        audit("payment.refund" if refund else "payment.collect", "payment", payment_id,
              f"{c['first_name']} {c['last_name']} · {p['description']}: {fmt(amount)} {METHODS[method]} "
              f"{'iade edildi' if refund else 'tahsil edildi'} (kalan {fmt(r['net'] - r['paid'])})",
              after={"amount": signed, "method": method, "status": r["status"]}, db=db)
    return tid


def void(db: Connection, user: dict, payment_id: int, reason: str) -> None:
    p = db.one("SELECT * FROM payments WHERE id = ?", (payment_id,))
    if not p:
        raise NotFound()
    if p["paid_kurus"] > 0:
        raise ValidationError({"_": "Tahsilatı olan kayıt iptal edilemez; önce iade girin."})
    if not (reason or "").strip():
        raise ValidationError({"reason": "İptal nedeni gerekli."})
    with db.transaction():
        db.update("payments", payment_id, {"status": "void", "updated_at": now_iso()})
        audit("payment.void", "payment", payment_id, f"Ödeme kaydı iptal edildi: {p['description']} — {reason.strip()}",
              before={"status": p["status"]}, after={"status": "void", "reason": reason.strip()}, db=db)


# ------------------------------------------------------------------ listeler
def open_balances(db: Connection, limit: int = 100) -> list[dict]:
    return db.query("""SELECT p.*, c.first_name, c.last_name, (p.total_kurus - p.discount_kurus - p.paid_kurus) AS remaining
                         FROM payments p JOIN customers c ON c.id = p.customer_id
                        WHERE p.status IN ('unpaid','partial') AND c.deleted_at IS NULL
                        ORDER BY p.created_at DESC LIMIT ?""", (limit,))


def recent_transactions(db: Connection, start_utc: str, end_utc: str) -> list[dict]:
    return db.query("""SELECT t.*, p.description, p.id AS payment_id, c.first_name, c.last_name, c.id AS customer_id,
                              u.full_name AS recorded_by_name
                         FROM payment_transactions t JOIN payments p ON p.id = t.payment_id
                         JOIN customers c ON c.id = p.customer_id LEFT JOIN users u ON u.id = t.recorded_by
                        WHERE t.paid_at >= ? AND t.paid_at < ? ORDER BY t.paid_at DESC, t.id DESC""",
                    (start_utc, end_utc))


def totals(db: Connection, start_utc: str, end_utc: str) -> dict:
    rows = db.query("""SELECT method, COALESCE(SUM(amount_kurus), 0) AS total FROM payment_transactions
                       WHERE paid_at >= ? AND paid_at < ? GROUP BY method""", (start_utc, end_utc))
    by = {r["method"]: r["total"] for r in rows}
    return {"by_method": by, "total": sum(by.values()),
            "outstanding": db.scalar("""SELECT COALESCE(SUM(total_kurus - discount_kurus - paid_kurus), 0) FROM payments
                                        WHERE status IN ('unpaid','partial')""") or 0}
