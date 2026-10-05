import json
import uuid
from datetime import date, datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.engines.fefo import consume_fefo, expire_lots, reconcile_ticket

app = FastAPI(title="Pantryfifo", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

@app.get("/api/health")
def health(): return {"ok": True, "project": "pantryfifo"}

@app.get("/api/items")
def items():
    c = connect(); rows = [dict(r) for r in c.execute("SELECT * FROM items")]; c.close(); return rows

@app.get("/api/fridge")
def fridge(layer: str | None = None):
    c = connect()
    q = """SELECT lots.*, items.name, items.layer, items.unit FROM lots
           JOIN items ON items.id=lots.item_id WHERE lots.status='on_shelf'"""
    args = []
    if layer:
        q += " AND items.layer=?"; args.append(layer)
    rows = [dict(r) for r in c.execute(q, args)]; c.close(); return rows

@app.get("/api/alerts")
def alerts():
    c = connect()
    warn = int(c.execute("SELECT value FROM settings WHERE key='warn_days'").fetchone()["value"])
    today = date.today().isoformat()
    rows = [dict(r) for r in c.execute(
        """SELECT lots.*, items.name, items.layer FROM lots JOIN items ON items.id=lots.item_id
           WHERE status='on_shelf' AND qty_remain>0 AND expiry IS NOT NULL""")]
    c.close()
    out = []
    for r in rows:
        if r["expiry"] <= today:
            r["level"] = "expired"
            out.append(r)
        else:
            # simple day diff via fromisoformat
            delta = (date.fromisoformat(r["expiry"]) - date.today()).days
            if delta <= warn:
                r["level"] = "soon"; r["days_left"] = delta; out.append(r)
    return out

class LotIn(BaseModel):
    item_id: int
    qty: float
    expiry: str

@app.post("/api/lots")
def inbound(body: LotIn):
    c = connect()
    item = c.execute("SELECT id FROM items WHERE id=?", (body.item_id,)).fetchone()
    if not item: c.close(); raise HTTPException(404, "item")
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
        (body.item_id, body.qty, body.qty, body.expiry, "on_shelf", "clean"))
    c.commit(); lid = cur.lastrowid; c.close(); return {"id": lid}

class ConsumeIn(BaseModel):
    item_id: int
    qty: float
    note: str = ""

@app.post("/api/consume")
def consume(body: ConsumeIn):
    """Legacy one-shot consume. Plans and deducts inside a single write
    transaction so it cannot interleave with a ticket confirmation."""
    if float(body.qty) <= 0:
        raise HTTPException(400, "qty_non_positive")
    c = connect()
    c.isolation_level = None
    c.execute("BEGIN IMMEDIATE")
    try:
        lots = [dict(r) for r in c.execute(
            "SELECT * FROM lots WHERE item_id=? AND status='on_shelf' AND qty_remain>0",
            (body.item_id,))]
        result = consume_fefo(
            [l for l in lots if not l.get("expiry") or l["expiry"] >= date.today().isoformat()],
            body.qty)
        if not result["ok"]:
            c.execute("ROLLBACK"); c.close(); raise HTTPException(409, result)
        for d in result["deductions"]:
            c.execute("UPDATE lots SET qty_remain = qty_remain - ? WHERE id=?",
                      (d["take"], d["lot_id"]))
            rem = c.execute("SELECT qty_remain FROM lots WHERE id=?",
                            (d["lot_id"],)).fetchone()["qty_remain"]
            if rem <= 1e-9:
                c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?",
                          (d["lot_id"],))
        c.execute("INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
                  (body.note, json.dumps(result), datetime.now(timezone.utc).isoformat()))
        c.execute("COMMIT"); c.close(); return result
    except HTTPException:
        raise
    except Exception:
        c.execute("ROLLBACK"); c.close(); raise

def _fefo_lots(c, item_id: int) -> list[dict]:
    return [dict(r) for r in c.execute(
        "SELECT * FROM lots WHERE item_id=? AND status='on_shelf' AND qty_remain>0", (item_id,))]

@app.post("/api/consume/preview")
def consume_preview(body: ConsumeIn):
    """Dry run: pin lot/take into a one-shot ticket, never touch qty_remain."""
    if float(body.qty) <= 0:
        raise HTTPException(400, "qty_non_positive")
    c = connect()
    item = c.execute("SELECT id FROM items WHERE id=?", (body.item_id,)).fetchone()
    if not item:
        c.close(); raise HTTPException(404, "item")
    result = consume_fefo(
        [l for l in _fefo_lots(c, body.item_id)
         if not l.get("expiry") or l["expiry"] >= date.today().isoformat()],
        body.qty)
    if not result["ok"]:
        # shortage at preview time -> no ticket, nothing reserved, nothing changed
        c.close(); raise HTTPException(409, result)
    token = uuid.uuid4().hex
    c.execute(
        "INSERT INTO consume_tickets(token,item_id,note,plan_json,created_at,status) VALUES (?,?,?,?,?,?)",
        (token, body.item_id, body.note, json.dumps(result["deductions"]),
         datetime.now(timezone.utc).isoformat(), "open"))
    c.commit(); c.close()
    return {"token": token, "deductions": result["deductions"], "short": 0.0}

class ConfirmIn(BaseModel):
    token: str

@app.post("/api/consume/confirm")
def consume_confirm(body: ConfirmIn):
    """Apply a preview ticket as one atomic all-or-nothing decision.

    Recomputed against the commit-instant state inside one write transaction,
    so a concurrent consume or expiry delist can never produce a success that
    deducts an expired lot or a failure that has already mutated qty_remain.
    """
    c = connect()
    c.isolation_level = None  # autocommit mode: we manage the txn explicitly
    c.execute("BEGIN IMMEDIATE")
    try:
        trow = c.execute("SELECT * FROM consume_tickets WHERE token=?", (body.token,)).fetchone()
        if trow is None:
            c.execute("ROLLBACK"); c.close(); raise HTTPException(404, "ticket")
        ticket = dict(trow)
        if ticket["status"] != "open":
            # stacked confirms of the same ticket: only one lots outcome allowed
            c.execute("ROLLBACK"); c.close()
            raise HTTPException(409, {"ok": False, "reason": "ticket_closed", "short": 0.0,
                                     "deductions": []})
        deductions = json.loads(ticket["plan_json"])
        ids = [d["lot_id"] for d in deductions]
        plc = ",".join("?" for _ in ids)
        current = {r["id"]: dict(r)
                   for r in c.execute(f"SELECT * FROM lots WHERE id IN ({plc})", ids)}
        decision = reconcile_ticket(deductions, current, date.today().isoformat())
        if not decision["ok"]:
            # whole ticket fails: burn it, roll every deduction back (none ran),
            # qty_remain of every lot is untouched
            c.execute("UPDATE consume_tickets SET status='failed' WHERE id=?", (ticket["id"],))
            c.execute("COMMIT"); c.close()
            raise HTTPException(409, {"ok": False, "reason": decision["reason"],
                                     "short": decision["short"], "deductions": deductions})
        for d in deductions:
            cur = c.execute(
                "UPDATE lots SET qty_remain = qty_remain - ? "
                "WHERE id=? AND status='on_shelf' AND qty_remain >= ?",
                (d["take"], d["lot_id"], d["take"]))
            if cur.rowcount != 1:
                # last-line race guard: lock held, this should be unreachable,
                # but never report success while a deduction did not land whole
                c.execute("ROLLBACK"); c.close()
                raise HTTPException(409, {"ok": False, "reason": "lot_changed",
                                          "short": float(d["take"]), "deductions": deductions})
            rem = c.execute("SELECT qty_remain FROM lots WHERE id=?", (d["lot_id"],)).fetchone()["qty_remain"]
            if rem <= 1e-9:
                c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?", (d["lot_id"],))
        result = {"ok": True, "short": 0.0, "deductions": deductions}
        c.execute("INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
                  (ticket.get("note") or "", json.dumps(result),
                   datetime.now(timezone.utc).isoformat()))
        c.execute("UPDATE consume_tickets SET status='confirmed' WHERE id=?", (ticket["id"],))
        c.execute("COMMIT"); c.close()
        return {"token": body.token, **result}
    except HTTPException:
        raise
    except Exception:
        c.execute("ROLLBACK"); c.close(); raise

@app.post("/api/expire-sweep")
def expire_sweep():
    c = connect()
    c.isolation_level = None
    c.execute("BEGIN IMMEDIATE")
    try:
        lots = [dict(r) for r in c.execute("SELECT * FROM lots WHERE status='on_shelf'")]
        ids = expire_lots(lots, date.today().isoformat())
        for i in ids:
            c.execute("UPDATE lots SET status='expired' WHERE id=?", (i,))
        c.execute("COMMIT"); c.close(); return {"expired_ids": ids}
    except Exception:
        c.execute("ROLLBACK"); c.close(); raise

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows
