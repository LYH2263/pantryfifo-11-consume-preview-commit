"""FEFO consume: earliest expiry first among positive remaining lots."""

def sort_lots_fefo(lots: list[dict]) -> list[dict]:
    return sorted(
        [l for l in lots if float(l.get("qty_remain", 0)) > 0],
        key=lambda l: (l.get("expiry") or "9999-99-99", l.get("id") or 0),
    )

def consume_fefo(lots: list[dict], qty: float) -> dict:
    """Return deductions list and leftover demand. Mutates copies only."""
    need = float(qty)
    if need <= 0:
        return {"ok": False, "reason": "qty_non_positive", "deductions": [], "short": 0.0}
    ordered = sort_lots_fefo(lots)
    deductions = []
    for lot in ordered:
        if need <= 0:
            break
        avail = float(lot["qty_remain"])
        take = min(avail, need)
        deductions.append({"lot_id": lot["id"], "take": take, "expiry": lot.get("expiry")})
        need -= take
    if need > 1e-9:
        return {"ok": False, "reason": "short", "deductions": deductions, "short": round(need, 3)}
    return {"ok": True, "reason": "", "deductions": deductions, "short": 0.0}

def reconcile_ticket(deductions: list[dict], current: dict[int, dict], today: str) -> dict:
    """Decide at commit instant whether a preview ticket may still be applied.

    `current` maps lot_id -> live lot row. The ticket is accepted only when
    every candidate lot still exists, is still on the shelf, has not expired
    (by date, even before the sweep flips its status) and still holds at least
    the quantity it held at preview. Nothing here mutates anything; on failure
    `short` aggregates the full unmet demand across all pinned lots.
    """
    if not deductions:
        return {"ok": False, "reason": "empty_ticket", "short": 0.0}
    short = 0.0
    reasons = set()
    total = 0.0
    for d in deductions:
        lot_id = d["lot_id"]
        take = float(d["take"])
        if take <= 0:
            return {"ok": False, "reason": "qty_non_positive", "short": 0.0}
        total += take
        lot = current.get(lot_id)
        if lot is None or lot.get("status") != "on_shelf":
            # delisted: another consume emptied it or the expiry sweep ran.
            reasons.add("lot_changed")
            short += take
            continue
        exp = lot.get("expiry")
        if exp and exp < today:
            # date-expired even if the sweep has not flipped status yet.
            reasons.add("lot_expired")
            short += take
            continue
        remain = float(lot.get("qty_remain", 0))
        if remain + 1e-9 < take:
            reasons.add("short")
            short += take - remain
    if reasons:
        # expired/delisted beats plain shortage in the reported reason.
        reason = "lot_expired" if "lot_expired" in reasons else (
            "lot_changed" if "lot_changed" in reasons else "short")
        return {"ok": False, "reason": reason, "short": round(short, 3)}
    return {"ok": True, "reason": "", "short": 0.0, "total": round(total, 3)}

def expire_lots(lots: list[dict], today: str) -> list[int]:
    """Ids that should leave shelf: remaining>0 and expiry < today."""
    out = []
    for l in lots:
        exp = l.get("expiry")
        if exp and exp < today and float(l.get("qty_remain", 0)) > 0:
            out.append(l["id"])
    return out
