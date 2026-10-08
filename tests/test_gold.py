"""The gold account: grams x the KRX gold close, buys from the user's facts or booked rows, no KIS call in tests."""
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from assetlog import config, db, gold, metrics, web

TODAY = date(2026, 10, 7)
ACC = "manual:gold"
PRICES = {"2026-08-31": 150_000.0, "2026-09-01": 151_000.0, "2026-09-30": 160_000.0, "2026-10-06": 180_000.0,
          "2026-10-07": 178_750.0}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(web.config, "DATA", tmp_path / "data")
    monkeypatch.setattr(web, "today", lambda: TODAY)
    monkeypatch.setattr(config, "load", lambda: {"ASSETLOG_GOLD_ACCOUNT": ACC})
    monkeypatch.setattr(gold.dcmodel, "closes", lambda cfg, code, lo, hi: {
        d: p for d, p in PRICES.items() if lo.isoformat() <= d <= hi.isoformat()})
    conn = db.connect()
    with conn:
        db.upsert_account(conn, ACC, "manual", "금현물")
    conn.close()
    return TestClient(web.app, base_url="http://localhost", client=("127.0.0.1", 50000))


def post(client, path, body):
    r = client.post(path, json=body, headers={"Origin": "http://localhost"})
    assert r.status_code == 200, r.text
    return r.json()


def rows(sql, *args):
    conn = db.connect()
    try:
        return [tuple(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def test_facts_become_buys_and_daily_values_follow_the_gold_price(client):
    post(client, "/api/gold", {"date": "2026-08-31", "grams": 10, "cost": 1_500_000})
    post(client, "/api/gold", {"date": "2026-09-30", "grams": 15, "cost": 2_300_000})  # bought 5 g for 800,000
    assert [(d, k, a, json.loads(raw)["grams"]) for d, k, a, raw in rows(
        "SELECT date, kind, amount, raw FROM transactions WHERE account_id = ? ORDER BY date", ACC)] == [
        ("2026-08-31", "DEPOSIT", 1_500_000, 10), ("2026-09-30", "DEPOSIT", 800_000, 5)]
    snaps = dict(rows("SELECT date, value FROM snapshots WHERE account_id = ?", ACC))
    assert snaps == {"2026-08-31": 1_500_000, "2026-09-01": 1_510_000, "2026-09-30": 2_400_000,
                     "2026-10-06": 2_700_000, "2026-10-07": 15 * 178_750}
    conn = db.connect()
    p = metrics.period(metrics.load(conn), date(2026, 9, 1), TODAY, {ACC})
    conn.close()
    assert p["gain"] == pytest.approx(15 * 178_750 - 1_510_000 - 800_000)  # the price moved, the buy did not count
    assert next(a for a in client.get("/api/accounts").json() if a["id"] == ACC)["mode"] == "gold"


def test_rebuild_is_idempotent_and_a_full_sale_ends_at_zero(client):
    post(client, "/api/gold", {"date": "2026-08-31", "grams": 10, "cost": 1_500_000})
    post(client, "/api/gold", {"date": "2026-10-06", "grams": 0, "cost": 0})
    before = rows("SELECT * FROM snapshots"), rows("SELECT date, amount FROM transactions")
    conn = db.connect()
    with conn:
        gold.rebuild(conn, {}, ACC, TODAY, PRICES)
    conn.close()
    assert (rows("SELECT * FROM snapshots"), rows("SELECT date, amount FROM transactions")) == before
    assert rows("SELECT value FROM snapshots WHERE account_id = ? ORDER BY date DESC LIMIT 1", ACC) == [(0,)]


def test_no_gold_account_configured_is_404(client, monkeypatch):
    monkeypatch.setattr(config, "load", lambda: {})
    r = client.post("/api/gold", json={"date": "2026-10-07", "grams": 1, "cost": 1}, headers={"Origin": "http://localhost"})
    assert r.status_code == 404
