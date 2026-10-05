from app.engines.fefo import consume_fefo, expire_lots, reconcile_ticket, sort_lots_fefo

TODAY = "2026-10-05"


def test_fefo_order():
    lots = [
        {"id": 2, "qty_remain": 3, "expiry": "2026-02-01", "status": "on_shelf"},
        {"id": 1, "qty_remain": 2, "expiry": "2026-01-10", "status": "on_shelf"},
    ]
    assert [l["id"] for l in sort_lots_fefo(lots)] == [1, 2]
    r = consume_fefo(lots, 3)
    assert r["ok"] and r["deductions"][0]["lot_id"] == 1 and r["deductions"][0]["take"] == 2
    assert r["deductions"][1]["take"] == 1


def test_short():
    r = consume_fefo([{"id": 1, "qty_remain": 1, "expiry": "2026-01-01", "status": "on_shelf"}], 5)
    assert r["ok"] is False and r["short"] == 4


def test_expire():
    ids = expire_lots([
        {"id": 1, "qty_remain": 1, "expiry": "2025-01-01"},
        {"id": 2, "qty_remain": 1, "expiry": "2027-01-01"},
    ], "2026-01-01")
    assert ids == [1]


def test_non_positive_qty():
    lots = [{"id": 1, "qty_remain": 5, "expiry": None, "status": "on_shelf"}]
    assert consume_fefo(lots, 0)["reason"] == "qty_non_positive"
    assert consume_fefo(lots, -2)["reason"] == "qty_non_positive"
    r, rec = reconcile_ticket([{"lot_id": 1, "take": 0}], lots, -1, TODAY)
    assert r["reason"] == "qty_non_positive" and rec is False


def test_preview_skips_off_shelf_and_expired():
    lots = [
        {"id": 1, "qty_remain": 2, "expiry": "2026-10-01", "status": "on_shelf"},  # expired
        {"id": 2, "qty_remain": 3, "expiry": "2026-10-10", "status": "on_shelf"},
        {"id": 3, "qty_remain": 4, "expiry": "2026-10-06", "status": "expired"},
    ]
    r = consume_fefo(lots, 2, TODAY)
    assert r["ok"]
    assert [d["lot_id"] for d in r["deductions"]] == [2]


def test_reconcile_stands_when_stock_unchanged():
    lots = [
        {"id": 1, "qty_remain": 2, "expiry": "2026-10-06", "status": "on_shelf"},
        {"id": 2, "qty_remain": 5, "expiry": "2026-10-20", "status": "on_shelf"},
    ]
    planned = consume_fefo(lots, 4, TODAY)["deductions"]
    r, rec = reconcile_ticket(planned, lots, 4, TODAY)
    assert r["ok"] and rec is False
    assert [(d["lot_id"], d["take"]) for d in r["deductions"]] == [(1, 2), (2, 2)]


def test_reconcile_recomputes_after_sweep_of_planned_lot():
    """Planned lot got expired-off-shelf between preview and confirm."""
    planned = [{"lot_id": 1, "take": 2}, {"lot_id": 2, "take": 1}]
    now = [
        {"id": 1, "qty_remain": 2, "expiry": "2026-10-01", "status": "expired"},
        {"id": 2, "qty_remain": 4, "expiry": "2026-10-20", "status": "on_shelf"},
    ]
    r, rec = reconcile_ticket(planned, now, 3, TODAY)
    assert r["ok"] and rec is True
    assert r["deductions"] == [{"lot_id": 2, "take": 3, "expiry": "2026-10-20"}]


def test_reconcile_recomputes_after_other_consume():
    """Another consume already drained part of a planned lot."""
    planned = [{"lot_id": 1, "take": 2}, {"lot_id": 2, "take": 2}]
    now = [
        {"id": 1, "qty_remain": 1, "expiry": "2026-10-06", "status": "on_shelf"},
        {"id": 2, "qty_remain": 5, "expiry": "2026-10-20", "status": "on_shelf"},
    ]
    r, rec = reconcile_ticket(planned, now, 4, TODAY)
    assert r["ok"] and rec is True
    assert [(d["lot_id"], d["take"]) for d in r["deductions"]] == [(1, 1), (2, 3)]


def test_reconcile_short_applies_nothing():
    planned = [{"lot_id": 1, "take": 2}, {"lot_id": 2, "take": 2}]
    now = [
        {"id": 1, "qty_remain": 0, "expiry": "2026-10-06", "status": "consumed"},
        {"id": 2, "qty_remain": 1, "expiry": "2026-10-20", "status": "on_shelf"},
    ]
    r, rec = reconcile_ticket(planned, now, 4, TODAY)
    assert r["ok"] is False and r["reason"] == "short"
    assert r["short"] == 3 and rec is True


def test_reconcile_recompute_never_picks_expired_lot():
    planned = [{"lot_id": 1, "take": 3}]
    now = [
        {"id": 1, "qty_remain": 0, "expiry": "2026-10-06", "status": "consumed"},
        {"id": 2, "qty_remain": 2, "expiry": "2026-10-01", "status": "on_shelf"},  # date-expired
        {"id": 3, "qty_remain": 5, "expiry": "2026-10-20", "status": "on_shelf"},
    ]
    r, rec = reconcile_ticket(planned, now, 3, TODAY)
    assert r["ok"] and rec is True
    assert r["deductions"] == [{"lot_id": 3, "take": 3, "expiry": "2026-10-20"}]


def test_two_stacked_tickets_have_one_deterministic_outcome():
    """Two previews against the same stock, confirmed in sequence, must yield
    the same total lot allocation regardless of order (FEFO is a function of
    the snapshot), and the second ticket's recomputed plan is FEFO again."""
    base = [
        {"id": 1, "qty_remain": 2, "expiry": "2026-10-06", "status": "on_shelf"},
        {"id": 2, "qty_remain": 5, "expiry": "2026-10-20", "status": "on_shelf"},
    ]
    tA = consume_fefo(base, 3, TODAY)["deductions"]
    tB = consume_fefo(base, 3, TODAY)["deductions"]
    assert tA == tB  # identical previews

    def run_order(first, second):
        lots = [dict(l) for l in base]
        outcomes = []
        for plan in (first, second):
            r, _ = reconcile_ticket(plan, lots, 3, TODAY)
            assert r["ok"]
            for d in r["deductions"]:
                lot = next(l for l in lots if l["id"] == d["lot_id"])
                lot["qty_remain"] -= d["take"]
            outcomes.append(tuple(sorted((d["lot_id"], d["take"]) for d in r["deductions"])))
        return outcomes, {l["id"]: l["qty_remain"] for l in lots}

    out1, final1 = run_order(tA, tB)
    out2, final2 = run_order(tB, tA)
    assert final1 == final2 == {1: 0, 2: 1}
    assert out1 == out2
