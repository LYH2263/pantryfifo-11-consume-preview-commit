"""FEFO consume: earliest expiry first among positive remaining lots.

A consume ticket is planned by ``consume_fefo`` (preview) and re-checked at
confirm time by ``reconcile_ticket`` against the snapshot visible inside the
confirm transaction, so a race with another consumption or an expire-sweep
can never deduct an expired / off-shelf lot nor partially apply a shortage.
"""

EPS = 1e-9


def eligible_lots(lots: list[dict], today: str | None = None) -> list[dict]:
    """On-shelf, positive remaining, and not expired as of ``today``."""
    out = []
    for l in lots:
        if l.get("status", "on_shelf") != "on_shelf":
            continue
        if float(l.get("qty_remain", 0)) <= 0:
            continue
        if today is not None:
            exp = l.get("expiry")
            if exp is not None and exp < today:
                continue
        out.append(l)
    return out


def sort_lots_fefo(lots: list[dict]) -> list[dict]:
    return sorted(
        [l for l in lots if float(l.get("qty_remain", 0)) > 0],
        key=lambda l: (l.get("expiry") or "9999-99-99", l.get("id") or 0),
    )


def _fail(qty: float):
    if float(qty) <= 0:
        return {"ok": False, "reason": "qty_non_positive", "deductions": [], "short": 0.0}
    return None


def consume_fefo(lots: list[dict], qty: float, today: str | None = None) -> dict:
    """Return deductions list and leftover demand. Pure: mutates nothing."""
    bad = _fail(qty)
    if bad is not None:
        return bad
    need = float(qty)
    ordered = sort_lots_fefo(eligible_lots(lots, today))
    deductions = []
    for lot in ordered:
        if need <= EPS:
            break
        avail = float(lot["qty_remain"])
        take = min(avail, need)
        deductions.append({"lot_id": lot["id"], "take": round(take, 3), "expiry": lot.get("expiry")})
        need -= take
    if need > EPS:
        return {"ok": False, "reason": "short", "deductions": deductions, "short": round(need, 3)}
    return {"ok": True, "reason": "", "deductions": deductions, "short": 0.0}


def reconcile_ticket(planned: list[dict], lots: list[dict], qty: float,
                     today: str) -> tuple[dict, bool]:
    """Decide what a ticket must deduct at commit time.

    ``planned`` are the deductions the preview promised; ``lots`` is the
    current snapshot (all lots of the item) read inside the confirm txn.

    The ticket stands only when every planned lot is still on-shelf, unexpired
    and holds at least the planned take, and the takes sum to the ticket qty.
    Otherwise the plan is dropped and FEFO is recomputed from the snapshot —
    which itself may come back ``short`` (caller then applies nothing).

    Returns (result, recomputed). Never plans an expired or off-shelf lot.
    """
    bad = _fail(qty)
    if bad is not None:
        return bad, False
    need = float(qty)
    eligible = eligible_lots(lots, today)
    by_id = {l["id"]: l for l in eligible}
    planned = [{"lot_id": int(d["lot_id"]), "take": float(d["take"])} for d in planned]
    stands = (
        len(planned) > 0
        and abs(sum(d["take"] for d in planned) - need) <= 1e-6
        and all(
            d["take"] > 0
            and (lot := by_id.get(d["lot_id"])) is not None
            and float(lot["qty_remain"]) + EPS >= d["take"]
            for d in planned
        )
    )
    if stands:
        deductions = [
            {"lot_id": d["lot_id"], "take": round(d["take"], 3),
             "expiry": by_id[d["lot_id"]].get("expiry")}
            for d in planned
        ]
        return {"ok": True, "reason": "", "deductions": deductions, "short": 0.0}, False
    return consume_fefo(lots, need, today), True


def expire_lots(lots: list[dict], today: str) -> list[int]:
    """Ids that should leave shelf: remaining>0 and expiry < today."""
    out = []
    for l in lots:
        exp = l.get("expiry")
        if exp and exp < today and float(l.get("qty_remain", 0)) > 0:
            out.append(l["id"])
    return out
