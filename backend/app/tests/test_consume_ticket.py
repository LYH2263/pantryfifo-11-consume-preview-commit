import pytest

from app.modules import consume_ticket
from app.modules.consume_ticket import TicketError


def test_preview_then_confirm_deducts_fefo(db, days):
    iid = db.add_item()
    l1 = db.add_lot(iid, 3, days["d10"])
    l2 = db.add_lot(iid, 2, days["d1"])
    p = consume_ticket.preview(iid, 4)
    assert p["ok"] and p["short"] == 0
    assert [(d["lot_id"], d["take"]) for d in p["deductions"]] == [(l2, 2), (l1, 2)]
    # preview must not move any quantity
    assert db.remainders(iid) == {l1: 3.0, l2: 2.0}
    r = consume_ticket.confirm(p["ticket_id"])
    assert r["ok"]
    assert db.remainders(iid) == {l1: 1.0, l2: 0.0}
    assert db.statuses(iid)[l2] == "consumed"


def test_preview_short_creates_no_ticket_and_moves_nothing(db, days):
    iid = db.add_item()
    l1 = db.add_lot(iid, 1, days["d10"])
    p = consume_ticket.preview(iid, 5)
    assert p["ok"] is False and p["short"] == 4
    assert p.get("ticket_id") is None
    assert db.remainders(iid) == {l1: 1.0}


def test_non_positive_qty_rejected(db):
    iid = db.add_item()
    db.add_lot(iid, 5, None)
    for bad in (0, -1):
        with pytest.raises(TicketError) as ei:
            consume_ticket.preview(iid, bad)
        assert ei.value.status == 400 and ei.value.code == "qty_non_positive"


def test_confirm_after_expire_sweep_never_deducts_expired(db, days):
    iid = db.add_item()
    l_planned = db.add_lot(iid, 2, days["d1"])   # valid at preview time
    l_new = db.add_lot(iid, 5, days["d10"])
    p = consume_ticket.preview(iid, 2)
    assert [d["lot_id"] for d in p["deductions"]] == [l_planned]
    # time passes + sweep takes it off shelf before confirm
    db.set_status(l_planned, "expired")
    r = consume_ticket.confirm(p["ticket_id"])
    assert r["ok"] and r["recomputed"] is True
    assert r["deductions"][0]["lot_id"] == l_new
    rem = db.remainders(iid)
    assert rem[l_planned] == 2.0 and rem[l_new] == 3.0
    assert db.statuses(iid)[l_planned] == "expired"


def test_confirm_short_after_other_consume_changes_nothing(db, days):
    iid = db.add_item()
    l1 = db.add_lot(iid, 2, days["d1"])
    l2 = db.add_lot(iid, 3, days["d10"])
    p = consume_ticket.preview(iid, 4)  # 2 from l1, 2 from l2
    # another consume drains l2 to 1 in the meantime
    db.set_remain(l2, 1)
    before = db.remainders(iid)
    with pytest.raises(TicketError) as ei:
        consume_ticket.confirm(p["ticket_id"])
    assert ei.value.status == 409 and ei.value.code == "short"
    assert ei.value.detail["short"] == 1
    # no lot may have lost any qty during the failed confirm
    assert db.remainders(iid) == before
    assert db.remainders(iid)[l1] == 2.0 and db.remainders(iid)[l2] == 1.0
    # ticket stays open: once stock is replenished it recomputes and succeeds
    db.add_lot(iid, 5, days["d3"])
    r = consume_ticket.confirm(p["ticket_id"])
    assert r["ok"] and r["recomputed"] is True


def test_confirm_recomputes_when_planned_lot_partially_consumed(db, days):
    iid = db.add_item()
    l1 = db.add_lot(iid, 2, days["d1"])
    l2 = db.add_lot(iid, 5, days["d10"])
    p = consume_ticket.preview(iid, 4)
    db.set_remain(l1, 1)  # one already consumed elsewhere
    r = consume_ticket.confirm(p["ticket_id"])
    assert r["ok"] and r["recomputed"] is True
    assert [(d["lot_id"], d["take"]) for d in r["deductions"]] == [(l1, 1), (l2, 3)]


def test_ticket_single_use(db, days):
    iid = db.add_item()
    db.add_lot(iid, 5, days["d10"])
    p = consume_ticket.preview(iid, 1)
    assert consume_ticket.confirm(p["ticket_id"])["ok"]
    with pytest.raises(TicketError) as ei:
        consume_ticket.confirm(p["ticket_id"])
    assert ei.value.status == 409 and ei.value.code == "ticket_consumed"


def test_stacked_tickets_deterministic(db, days):
    """Two previews stacked on the same stock, confirm both: allocations are
    FEFO-deterministic and the second confirm never double-deducts."""
    iid = db.add_item()
    l1 = db.add_lot(iid, 2, days["d1"])
    l2 = db.add_lot(iid, 5, days["d10"])
    pa = consume_ticket.preview(iid, 3)
    pb = consume_ticket.preview(iid, 3)
    ra = consume_ticket.confirm(pa["ticket_id"])
    rb = consume_ticket.confirm(pb["ticket_id"])
    assert [(d["lot_id"], d["take"]) for d in ra["deductions"]] == [(l1, 2), (l2, 1)]
    assert rb["recomputed"] is True
    assert [(d["lot_id"], d["take"]) for d in rb["deductions"]] == [(l2, 3)]
    assert db.remainders(iid) == {l1: 0.0, l2: 1.0}


def test_unknown_item_and_ticket(db):
    with pytest.raises(TicketError) as ei:
        consume_ticket.preview(9999, 1)
    assert ei.value.status == 404
    with pytest.raises(TicketError) as ei2:
        consume_ticket.confirm(9999)
    assert ei2.value.status == 404 and ei2.value.code == "ticket_not_found"


def test_confirm_guard_blocks_status_flip_between_read_and_update(db, days, monkeypatch):
    """If the lot no longer satisfies the guard at UPDATE time the whole txn
    rolls back: qty untouched, status untouched, no consumption row."""
    iid = db.add_item()
    lot = db.add_lot(iid, 3, days["d10"])
    p = consume_ticket.preview(iid, 2)

    real_apply = consume_ticket._apply_deductions

    def racy_apply(c, deductions):
        # emulate a sweep landing between reconcile's read and the UPDATE
        c.execute("UPDATE lots SET status='expired' WHERE id=?", (lot,))
        return real_apply(c, deductions)

    monkeypatch.setattr(consume_ticket, "_apply_deductions", racy_apply)
    try:
        with pytest.raises(TicketError) as ei:
            consume_ticket.confirm(p["ticket_id"])
    finally:
        monkeypatch.setattr(consume_ticket, "_apply_deductions", real_apply)
    assert ei.value.code == "lot_changed"
    # atomic rollback: the simulated sweep itself is undone with the txn
    assert db.remainders(iid) == {lot: 3.0}
    assert db.statuses(iid)[lot] == "on_shelf"
    assert consume_ticket.confirm(p["ticket_id"])["ok"]  # ticket still open
