"""A principal-only savings account: the user's 납입원금 figures and booked deposits become a ledger with no gain."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from assetlog import config, db, metrics, principal, web

TODAY = date(2026, 10, 7)
ACC = "manual:youth"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(web.config, "DATA", tmp_path / "data")
    monkeypatch.setattr(web, "today", lambda: TODAY)
    monkeypatch.setattr(config, "load", lambda: {"ASSETLOG_PRINCIPAL_ONLY": ACC})
    conn = db.connect()
    with conn:
        db.upsert_account(conn, ACC, "manual", "청년도약계좌")
    conn.close()
    return TestClient(web.app, base_url="http://localhost", client=("127.0.0.1", 50000))


def ledger():
    conn = db.connect()
    try:
        flows = [(r["date"], r["amount"], r["source"]) for r in conn.execute(
            "SELECT date, amount, source FROM transactions WHERE account_id = ? ORDER BY date, source", (ACC,))]
        snaps = [(r["date"], r["value"], r["source"]) for r in conn.execute(
            "SELECT date, value, source FROM snapshots WHERE account_id = ? ORDER BY date", (ACC,))]
        return flows, snaps
    finally:
        conn.close()


def post(client, path, body):
    r = client.post(path, json=body, headers={"Origin": "http://localhost"})
    assert r.status_code == 200, r.text
    return r.json()


def test_balance_figures_become_deposits_and_never_a_gain(client):
    post(client, "/api/snapshots", {"account_id": ACC, "date": "2026-03-31", "value": 3_000_000})
    post(client, "/api/snapshots", {"account_id": ACC, "date": "2026-08-31", "value": 4_200_000})
    flows, snaps = ledger()
    assert flows == [("2026-03-31", 3_000_000, "principal"), ("2026-08-31", 1_200_000, "principal")]
    assert snaps[-1] == ("2026-10-07", 4_200_000, "principal")  # carried to today: never stale

    # a transfer booked from a screenshot explains part of the next gap; only the rest is derived
    post(client, "/api/transactions", {"account_id": ACC, "date": "2026-09-10", "amount": 500_000,
                                       "kind": "DEPOSIT", "memo": "이체"})
    post(client, "/api/snapshots", {"account_id": ACC, "date": "2026-10-07", "value": 5_000_000})
    flows, snaps = ledger()
    assert ("2026-09-10", 500_000, "manual") in flows and ("2026-10-07", 300_000, "principal") in flows
    assert ("2026-09-10", 4_700_000, "principal") in snaps and snaps[-1] == ("2026-10-07", 5_000_000, "manual")

    conn = db.connect()
    p = metrics.period(metrics.load(conn), date(2026, 4, 30), TODAY, {ACC})
    conn.close()
    assert p["gain"] == pytest.approx(0) and p["return"] == pytest.approx(0)


def test_rebuild_is_idempotent_and_follows_deletes(client):
    post(client, "/api/snapshots", {"account_id": ACC, "date": "2026-05-31", "value": 1_000_000})
    before = ledger()
    conn = db.connect()
    with conn:
        principal.rebuild(conn, ACC, TODAY)
    conn.close()
    assert ledger() == before
    r = client.request("DELETE", f"/api/snapshots?account_id={ACC}&date=2026-05-31", headers={"Origin": "http://localhost"})
    assert r.status_code == 200
    assert ledger() == ([], [])  # nothing left to explain


def test_won_savings_carry_no_fx_effect_in_the_dollar_view(client):
    post(client, "/api/snapshots", {"account_id": ACC, "date": "2026-01-31", "value": 1_000_000})
    post(client, "/api/snapshots", {"account_id": ACC, "date": "2026-08-31", "value": 2_000_000})
    conn = db.connect()
    with conn:
        db.upsert_fx(conn, "2025-12-31", "USD", 1500, "test")
        db.upsert_fx(conn, "2026-08-31", "USD", 1300, "test")  # the won got 13% stronger
    acct = lambda krw_only: next(a for a in metrics.summary(conn, TODAY, krw_only)["accounts"] if a["id"] == ACC)["ytd"]
    assert acct([ACC])["return_usd"] == pytest.approx(0) and acct([ACC])["fx_effect"] == pytest.approx(0)
    assert acct(())["return_usd"] > 0.05  # at each day's rate the same won would look like a dollar gain
    conn.close()
