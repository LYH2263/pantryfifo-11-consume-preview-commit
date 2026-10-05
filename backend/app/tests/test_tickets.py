import json
from datetime import date, timedelta

import pytest
from fastapi import HTTPException

from app import seed
from app.db import connect
from app.main import ConfirmIn, ConsumeIn, consume_confirm, consume_preview


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    c = connect()
    c.execute("DELETE FROM lots")
    c.execute("DELETE FROM consume_tickets")
    c.commit()
    c.close()
    return tmp_path


def add_lot(item_id, remain, expiry, status="on_shelf"):
    c = connect()
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) "
        "VALUES (?,?,?,?,?,?)",
        (item_id, remain, remain, expiry, status, "clean"))
    c.commit()
    lid = cur.lastrowid
    c.close()
    return lid


def remains():
    c = connect()
    rows = {r["id"]: (r["qty_remain"], r["status"])
            for r in c.execute("SELECT id,qty_remain,status FROM lots")}
    c.close()
    return rows


def err(exc):
    d = exc.detail
    return d if isinstance(d, dict) else {"reason": d}


def test_non_positive_qty_fails_without_ticket(db):
    with pytest.raises(HTTPException) as e:
        consume_preview(ConsumeIn(item_id=1, qty=0))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e2:
        consume_preview(ConsumeIn(item_id=1, qty=-2))
    assert e2.value.status_code == 400
    c = connect()
    assert c.execute("SELECT COUNT(*) n FROM consume_tickets").fetchone()["n"] == 0
    c.close()


def test_preview_short_creates_no_ticket_and_changes_nothing(db):
    add_lot(1, 1, "2099-01-01")
    before = remains()
    with pytest.raises(HTTPException) as e:
        consume_preview(ConsumeIn(item_id=1, qty=5))
    assert e.value.status_code == 409
    assert e.value.detail["reason"] == "short"
    assert e.value.detail["short"] == 4
    assert remains() == before
    c = connect()
    assert c.execute("SELECT COUNT(*) n FROM consume_tickets").fetchone()["n"] == 0
    c.close()


def test_preview_does_not_touch_any_quantity(db):
    l1 = add_lot(1, 2, "2099-01-01")
    l2 = add_lot(1, 3, "2099-02-01")
    before = remains()
    r = consume_preview(ConsumeIn(item_id=1, qty=4))
    assert [d["lot_id"] for d in r["deductions"]] == [l1, l2]
    assert r["deductions"][0]["take"] == 2
    assert r["deductions"][1]["take"] == 2
    assert remains() == before  # shelf columns / layers / top bar source unchanged


def test_confirm_applies_ticket(db):
    l1 = add_lot(1, 2, "2099-01-01")
    l2 = add_lot(1, 3, "2099-02-01")
    pv = consume_preview(ConsumeIn(item_id=1, qty=4))
    r = consume_confirm(ConfirmIn(token=pv["token"]))
    assert r["ok"]
    got = remains()
    assert got[l1] == (0, "consumed")
    assert got[l2] == (1, "on_shelf")
    c = connect()
    assert c.execute("SELECT status FROM consume_tickets WHERE token=?",
                     (pv["token"],)).fetchone()["status"] == "confirmed"
    c.close()


def test_double_confirm_only_one_outcome(db):
    add_lot(1, 2, "2099-01-01")
    pv = consume_preview(ConsumeIn(item_id=1, qty=1))
    consume_confirm(ConfirmIn(token=pv["token"]))
    between = remains()
    with pytest.raises(HTTPException) as e:
        consume_confirm(ConfirmIn(token=pv["token"]))
    assert e.value.status_code == 409
    assert err(e.value)["reason"] == "ticket_closed"
    assert remains() == between  # second confirm deducts nothing again


def test_two_stacked_tickets_only_one_lots_result(db):
    l1 = add_lot(1, 3, "2099-01-01")
    a = consume_preview(ConsumeIn(item_id=1, qty=3))
    b = consume_preview(ConsumeIn(item_id=1, qty=3))
    assert consume_confirm(ConfirmIn(token=a["token"]))["ok"]
    after_first = remains()
    with pytest.raises(HTTPException) as e:
        consume_confirm(ConfirmIn(token=b["token"]))
    assert e.value.status_code == 409
    detail = err(e.value)
    assert detail["short"] > 0
    assert remains() == after_first  # losing ticket never deducts
    assert remains()[l1][0] == 0


def test_confirm_after_sweep_delist_is_atomic_no_quantity_touched(db):
    l1 = add_lot(1, 2, "2099-01-01")
    l2 = add_lot(1, 3, "2099-02-01")
    pv = consume_preview(ConsumeIn(item_id=1, qty=4))
    # expiry sweep delists the first pinned lot between preview and confirm
    c = connect()
    c.execute("UPDATE lots SET status='expired' WHERE id=?", (l1,))
    c.commit()
    c.close()
    before = remains()
    with pytest.raises(HTTPException) as e:
        consume_confirm(ConfirmIn(token=pv["token"]))
    detail = err(e.value)
    assert e.value.status_code == 409
    assert detail["reason"] in ("lot_changed", "lot_expired")
    assert detail["short"] > 0
    assert remains() == before  # neither lot reduced, failure mutated nothing
    c = connect()
    assert c.execute("SELECT status FROM consume_tickets WHERE token=?",
                     (pv["token"],)).fetchone()["status"] == "failed"
    c.close()


def test_confirm_rejects_date_expired_lot_even_without_sweep(db):
    # on_shelf at preview time; date rolls past expiry before confirm
    l1 = add_lot(1, 2, (date.today() - timedelta(days=1)).isoformat())
    l2 = add_lot(1, 3, "2099-02-01")
    # preview itself skips date-expired lots -> ticket only spans the fresh lot
    pv = consume_preview(ConsumeIn(item_id=1, qty=2))
    assert {d["lot_id"] for d in pv["deductions"]} == {l2}
    # now force an expired lot into a ticket to prove the commit-time guard
    c = connect()
    c.execute("UPDATE consume_tickets SET plan_json=? WHERE token=?",
              (json.dumps([{"lot_id": l1, "take": 2}]), pv["token"]))
    c.commit()
    c.close()
    before = remains()
    with pytest.raises(HTTPException) as e:
        consume_confirm(ConfirmIn(token=pv["token"]))
    assert err(e.value)["reason"] == "lot_expired"
    assert remains() == before


def test_confirm_short_after_race_consume_reduces_nothing(db):
    l1 = add_lot(1, 2, "2099-01-01")
    l2 = add_lot(1, 3, "2099-02-01")
    pv = consume_preview(ConsumeIn(item_id=1, qty=4))
    # another consume takes 2 off the second lot between preview and confirm
    c = connect()
    c.execute("UPDATE lots SET qty_remain=qty_remain-2 WHERE id=?", (l2,))
    c.commit()
    c.close()
    before = remains()
    with pytest.raises(HTTPException) as e:
        consume_confirm(ConfirmIn(token=pv["token"]))
    detail = err(e.value)
    assert e.value.status_code == 409
    assert detail["reason"] == "short"
    assert detail["short"] == 1
    assert remains() == before  # all-or-nothing: first lot untouched too
    # failed ticket cannot be retried
    with pytest.raises(HTTPException) as e2:
        consume_confirm(ConfirmIn(token=pv["token"]))
    assert err(e2.value)["reason"] == "ticket_closed"


def test_unknown_token_404(db):
    with pytest.raises(HTTPException) as e:
        consume_confirm(ConfirmIn(token="nope"))
    assert e.value.status_code == 404
