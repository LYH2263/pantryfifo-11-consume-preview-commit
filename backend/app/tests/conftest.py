import pytest

from app import seed
from app.db import connect
from datetime import date, timedelta


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    yield Store()


class Store:
    def add_item(self, name="测试品", layer="upper", unit="盒"):
        c = connect()
        cur = c.execute("INSERT INTO items(name,layer,unit) VALUES (?,?,?)", (name, layer, unit))
        c.commit()
        iid = cur.lastrowid
        c.close()
        return iid

    def add_lot(self, item_id, qty, expiry=None, status="on_shelf"):
        c = connect()
        cur = c.execute(
            "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality)"
            " VALUES (?,?,?,?,?,?)",
            (item_id, qty, qty, expiry, status, "clean"))
        c.commit()
        lid = cur.lastrowid
        c.close()
        return lid

    def remainders(self, item_id):
        c = connect()
        rows = {r["id"]: r["qty_remain"] for r in
                c.execute("SELECT id,qty_remain FROM lots WHERE item_id=?", (item_id,))}
        c.close()
        return rows

    def statuses(self, item_id):
        c = connect()
        rows = {r["id"]: r["status"] for r in
                c.execute("SELECT id,status FROM lots WHERE item_id=?", (item_id,))}
        c.close()
        return rows

    def set_status(self, lot_id, status):
        c = connect()
        c.execute("UPDATE lots SET status=? WHERE id=?", (status, lot_id))
        c.commit()
        c.close()

    def set_remain(self, lot_id, qty):
        c = connect()
        c.execute("UPDATE lots SET qty_remain=? WHERE id=?", (qty, lot_id))
        c.commit()
        c.close()


@pytest.fixture
def days():
    today = date.today()
    return {
        "today": today.isoformat(),
        "yesterday": (today - timedelta(days=1)).isoformat(),
        "d1": (today + timedelta(days=1)).isoformat(),
        "d3": (today + timedelta(days=3)).isoformat(),
        "d10": (today + timedelta(days=10)).isoformat(),
    }
