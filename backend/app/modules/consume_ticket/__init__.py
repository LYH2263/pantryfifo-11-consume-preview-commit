"""Consume preview tickets: preview-then-confirm FEFO with race handling.

preview  -> computes the lots/takes that *will* be deducted, stores a ticket,
            touches no qty_remain.
confirm  -> inside one BEGIN IMMEDIATE txn, re-reads stock at commit time,
            reconciles the ticket against concurrent consumes / expire-sweep,
            and either deducts every lot atomically or applies nothing.

Invariants enforced here:
* a lot that is expired / no longer on_shelf at confirm is never deducted;
* shortage -> 409-style result with ``short`` and zero qty_remain changes;
* a ticket is single-use (two confirms of one / two stacked tickets resolve
  deterministically: both reconcile against the same FEFO snapshot ordering);
* non-positive qty is rejected at both endpoints.
"""
import json
from datetime import date, datetime, timezone

from app.db import connect, immediate_tx
from app.engines.fefo import consume_fefo, reconcile_ticket


class TicketError(Exception):
    def __init__(self, status: int, code: str, detail: dict | None = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.detail = detail or {}


def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db(c):
    c.execute("""
    CREATE TABLE IF NOT EXISTS consume_tickets(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      item_id INT NOT NULL,
      qty REAL NOT NULL,
      note TEXT,
      plan_json TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'open',
      created_at TEXT NOT NULL,
      confirmed_at TEXT
    )""")


def _load_item_lots(c, item_id: int) -> list[dict]:
    return [dict(r) for r in c.execute(
        "SELECT * FROM lots WHERE item_id=?", (item_id,))]


def preview(item_id: int, qty: float, note: str = "") -> dict:
    """Plan deductions. Never modifies lot quantities."""
    qty = float(qty)
    if qty <= 0:
        raise TicketError(400, "qty_non_positive")
    c = connect()
    try:
        if not c.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
            raise TicketError(404, "item_not_found")
        lots = _load_item_lots(c, item_id)
        result = consume_fefo(lots, qty, _today())
        if not result["ok"]:  # short: still hand the caller the shortfall
            c.close()
            return {"preview": None, "short": result["short"],
                    "deductions": [], "ok": False}
        cur = c.execute(
            "INSERT INTO consume_tickets(item_id,qty,note,plan_json,status,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (item_id, qty, note, json.dumps(result["deductions"]), "open", _now()))
        c.commit()
        ticket_id = cur.lastrowid
    finally:
        c.close()
    return {"ticket_id": ticket_id, "item_id": item_id, "qty": qty,
            "ok": True, "short": 0.0, "deductions": result["deductions"]}


def confirm(ticket_id: int) -> dict:
    """Reconcile the ticket against commit-time stock, then deduct atomically."""
    c = connect()
    try:
        with immediate_tx(c):
            row = c.execute(
                "SELECT * FROM consume_tickets WHERE id=?", (ticket_id,)).fetchone()
            if row is None:
                raise TicketError(404, "ticket_not_found")
            t = dict(row)
            if t["status"] != "open":
                raise TicketError(409, "ticket_consumed",
                                  {"ticket_id": ticket_id, "status": t["status"]})
            if float(t["qty"]) <= 0:
                raise TicketError(400, "qty_non_positive")

            lots = _load_item_lots(c, t["item_id"])
            planned = json.loads(t["plan_json"])
            result, recomputed = reconcile_ticket(planned, lots, float(t["qty"]), _today())

            if not result["ok"]:
                # Short (or invalid) at commit time: roll back everything.
                raise TicketError(409, result["reason"], {
                    "ticket_id": ticket_id,
                    "short": result["short"],
                    "deductions": result["deductions"],
                })

            deductions = _apply_deductions(c, result["deductions"])
            c.execute(
                "UPDATE consume_tickets SET status='confirmed', confirmed_at=? WHERE id=?",
                (_now(), ticket_id))
            c.execute(
                "INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
                (t.get("note") or "",
                 json.dumps({"ticket_id": ticket_id, "recomputed": recomputed, **result}),
                 _now()))
        return {"ticket_id": ticket_id, "ok": True, "recomputed": recomputed,
                "short": 0.0, "deductions": deductions}
    finally:
        c.close()


def _apply_deductions(c, deductions: list[dict]) -> list[dict]:
    """Conditional UPDATEs; any guard failing aborts the whole txn."""
    today = _today()
    out = []
    for d in deductions:
        take = float(d["take"])
        if take <= 0:
            raise TicketError(400, "qty_non_positive")
        # Guard: lot must still be on_shelf, unexpired and cover the take.
        cur = c.execute(
            """UPDATE lots SET qty_remain = qty_remain - ?
               WHERE id=? AND status='on_shelf'
                 AND (expiry IS NULL OR expiry >= ?)
                 AND qty_remain >= ?""",
            (take, d["lot_id"], today, take))
        if cur.rowcount != 1:
            # Lost the race to a sweep/consume between reconcile and update:
            # force a full abort; nothing is allowed to remain deducted.
            raise TicketError(409, "lot_changed", {"lot_id": d["lot_id"]})
        rem = c.execute("SELECT qty_remain FROM lots WHERE id=?",
                        (d["lot_id"],)).fetchone()["qty_remain"]
        if rem <= 1e-9:
            c.execute(
                "UPDATE lots SET status='consumed', qty_remain=0 WHERE id=? AND qty_remain<=0",
                (d["lot_id"],))
        out.append({"lot_id": d["lot_id"], "take": round(take, 3),
                    "expiry": d.get("expiry"), "qty_remain": round(float(rem), 3)})
    return out


def direct_consume(item_id: int, qty: float, note: str = "") -> dict:
    """One-shot path used by the legacy endpoint: preview + confirm locally."""
    p = preview(item_id, qty, note)
    if not p["ok"]:
        raise TicketError(409, "short", {"short": p["short"], "deductions": p["deductions"]})
    return confirm(p["ticket_id"])
