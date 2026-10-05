import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(db):
    from app.main import app
    return TestClient(app)


def test_preview_confirm_api_flow(client, db, days):
    iid = db.add_item()
    l1 = db.add_lot(iid, 2, days["d1"])
    l2 = db.add_lot(iid, 4, days["d10"])

    r = client.post("/api/consume/preview", json={"item_id": iid, "qty": 3})
    assert r.status_code == 200
    p = r.json()
    assert p["ok"] and [d["lot_id"] for d in p["deductions"]] == [l1, l2]
    assert db.remainders(iid) == {l1: 2.0, l2: 4.0}  # nothing moved

    r2 = client.post("/api/consume/confirm", json={"ticket_id": p["ticket_id"]})
    assert r2.status_code == 200 and r2.json()["ok"]
    assert db.remainders(iid) == {l1: 0.0, l2: 3.0}


def test_preview_non_positive_is_400(client, db):
    iid = db.add_item()
    db.add_lot(iid, 3, None)
    r = client.post("/api/consume/preview", json={"item_id": iid, "qty": 0})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "qty_non_positive"


def test_confirm_short_409_carries_short_and_changes_nothing(client, db, days):
    iid = db.add_item()
    l1 = db.add_lot(iid, 2, days["d1"])
    p = client.post("/api/consume/preview", json={"item_id": iid, "qty": 2}).json()
    db.set_status(l1, "expired")  # swept with nothing else on shelf
    r = client.post("/api/consume/confirm", json={"ticket_id": p["ticket_id"]})
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["code"] == "short" and body["short"] == 2
    assert db.remainders(iid) == {l1: 2.0}


def test_confirm_unknown_and_reused_ticket(client, db, days):
    assert client.post("/api/consume/confirm", json={"ticket_id": 9999}).status_code == 404
    iid = db.add_item()
    db.add_lot(iid, 2, days["d10"])
    p = client.post("/api/consume/preview", json={"item_id": iid, "qty": 1}).json()
    assert client.post("/api/consume/confirm", json={"ticket_id": p["ticket_id"]}).status_code == 200
    r = client.post("/api/consume/confirm", json={"ticket_id": p["ticket_id"]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ticket_consumed"


def test_legacy_consume_still_works(client, db, days):
    iid = db.add_item()
    l1 = db.add_lot(iid, 1, days["d1"])
    l2 = db.add_lot(iid, 3, days["d10"])
    r = client.post("/api/consume", json={"item_id": iid, "qty": 2})
    assert r.status_code == 200 and r.json()["ok"]
    assert db.remainders(iid) == {l1: 0.0, l2: 2.0}
    r2 = client.post("/api/consume", json={"item_id": iid, "qty": 9})
    assert r2.status_code == 409 and r2.json()["detail"]["short"] == 7
    assert db.remainders(iid) == {l1: 0.0, l2: 2.0}  # nothing applied on short
