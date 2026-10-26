import json
import sys
import threading
import time
import types
from datetime import date, timedelta
from types import SimpleNamespace
from urllib.parse import quote

import anyio
import pytest
from fastapi.testclient import TestClient

import assetlog
from assetlog import config, db, dcmodel, demo, metrics, sync, web

TODAY = date(2026, 9, 25)  # pinned: demo figures (years, DC staleness) depend on where today falls


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(web.config, "DATA", tmp_path / "data")
    monkeypatch.setattr(web, "today", lambda: TODAY)
    conn = db.connect()
    with conn:
        demo.seed(conn, TODAY)
    conn.close()
    return TestClient(web.app, base_url="http://localhost", client=("127.0.0.1", 50000))


def rows(sql, *args):
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def test_summary_shape(client):
    assert "<canvas" in client.get("/").text
    s = client.get("/api/summary").json()
    assert s["as_of"] == web.today().isoformat() and s["total_value"] > 0 and s["unknown_count"] == 2
    assert {a["id"] for a in s["accounts"]} == {"kb:demo-01", "kis:demo-01", "toss:demo-01", "hana:dc"}
    for key in ("return", "return_annualized", "growth_rate", "net_flow", "gain", "dividend_yield", "warnings",
                "return_ex_div", "return_ex_div_annualized", "dividend_return"):
        assert key in s["ytd"] and key in s["ttm"] and key in s["years"][-1]
    assert s["ttm"]["dividends"] > 0 and s["since_start"]["annual"] is not None
    assert s["since_start"]["annual_ex_div"] < s["since_start"]["annual"]  # the demo pays dividends
    assert s["dividend_payers"] is None  # no holdings read yet
    for p in (s["ytd"], s["ttm"], s["years"][-1], *(a["ytd"] for a in s["accounts"] if a["ytd"])):
        assert p["return_usd"] is not None and p["fx_effect"] == pytest.approx(p["return"] - p["return_usd"])
    assert s["usd_from"] < TODAY.isoformat()  # fx history now reaches back with the ledger, not just today's rate
    assert s["since_start"]["cumulative_usd"] is not None and s["since_start"]["annual_usd"] is not None
    assert len(s["years"]) == 4 and len(s["series"]) == 37 and s["dividends_by_month"]
    assert any("하나은행" in w for w in s["ytd"]["warnings"])  # DC typed in quarterly -> stale


@pytest.mark.parametrize("today", [date(2026, 10, 3), date(2027, 1, 5), date(2027, 4, 7), date(2026, 9, 30)])
def test_demo_dc_stale_right_after_quarter_end(tmp_path, today):
    conn = db.connect(tmp_path / "d.db")
    with conn:
        demo.seed(conn, today)
    assert any("하나은행" in w for w in metrics.summary(conn, today)["ytd"]["warnings"])
    conn.close()


def test_summary_survives_loss_beyond_minus_100pct(client):
    # new account, -3% in 5 days: Modified Dietz gives < -100%, whose annualisation is complex -> was a 500
    assert client.post("/api/accounts", json={"id": "toss:new", "institution": "toss", "name": "새 계좌"}).status_code == 200
    for d, v in ((TODAY - timedelta(days=5), 10_000_000), (TODAY, 9_700_000)):
        assert client.post("/api/snapshots", json={"account_id": "toss:new", "date": d.isoformat(), "value": v}).status_code == 200
    r = client.get("/api/summary")
    assert r.status_code == 200, r.text
    new = next(a for a in r.json()["accounts"] if a["id"] == "toss:new")
    assert new["value"] == 9_700_000 and new["ytd"]["return_annualized"] is None


def test_manual_snapshot_round_trip(client):
    d = web.today().isoformat()
    assert client.post("/api/snapshots", json={"account_id": "hana:dc", "date": d, "value": 60_000_000}).status_code == 200
    snap = client.get("/api/snapshots", params={"account_id": "hana:dc"}).json()[0]
    assert (snap["date"], snap["value"], snap["source"]) == (d, 60_000_000, "manual")
    hana = next(a for a in client.get("/api/summary").json()["accounts"] if a["id"] == "hana:dc")
    assert (hana["value"], hana["as_of"]) == (60_000_000, d)
    assert client.delete("/api/snapshots", params={"account_id": "hana:dc", "date": d}).status_code == 200
    assert client.delete("/api/snapshots", params={"account_id": "hana:dc", "date": d}).status_code == 404
    kb_snap = client.get("/api/snapshots", params={"account_id": "kb:demo-01"}).json()[0]
    assert client.delete("/api/snapshots", params={"account_id": "kb:demo-01", "date": kb_snap["date"]}).status_code == 400
    # a manual POST must not overwrite (and so unlock deleting) a broker valuation
    over = {"account_id": "kb:demo-01", "date": kb_snap["date"], "value": 1}
    assert client.post("/api/snapshots", json=over).status_code == 409
    assert client.get("/api/snapshots", params={"account_id": "kb:demo-01"}).json()[0] == kb_snap
    assert client.post("/api/snapshots", json={"account_id": "nope", "date": d, "value": 1}).status_code == 404
    future = {"account_id": "hana:dc", "date": "2999-01-01", "value": 1}
    assert client.post("/api/snapshots", json=future).status_code == 422


def test_manual_transaction_round_trip(client):
    body = {"account_id": "hana:dc", "date": "2026-01-15", "kind": "DEPOSIT", "amount": 420_000,
            "memo": "사용자부담금입금"}
    tx_id = client.post("/api/transactions", json=body).json()["id"]
    got = client.get("/api/transactions", params={"account_id": "hana:dc", "limit": 500}).json()
    t = next(t for t in got if t["id"] == tx_id)
    assert (t["kind"], t["amount"], t["source"], t["memo"]) == ("DEPOSIT", 420_000, "manual", "사용자부담금입금")
    assert [t["date"] for t in got] == sorted((t["date"] for t in got), reverse=True)
    assert client.delete(f"/api/transactions/{tx_id}").status_code == 200
    assert client.delete(f"/api/transactions/{tx_id}").status_code == 404
    assert client.post("/api/transactions", json={**body, "kind": "WITHDRAW"}).status_code == 422  # sign contradicts kind
    assert client.post("/api/transactions", json={**body, "account_id": "nope"}).status_code == 404


def test_patch_locks_kind(client):
    (u,) = rows("SELECT id FROM transactions WHERE kind = 'UNKNOWN' AND memo LIKE '타사대체입고%'")
    r = client.patch(f"/api/transactions/{u['id']}", json={"kind": "DEPOSIT", "amount": 3_500_000})
    assert r.status_code == 200
    assert client.get("/api/summary").json()["unknown_count"] == 1
    (t,) = rows("SELECT * FROM transactions WHERE id = ?", u["id"])
    assert (t["kind"], t["amount"], t["kind_locked"]) == ("DEPOSIT", 3_500_000, 1)
    conn = db.connect()  # a re-import of the same row must keep the decision
    with conn:
        db.upsert_transaction(conn, t["account_id"], t["date"], 0, t["memo"], t["source"], t["source_key"])
    conn.close()
    assert rows("SELECT kind, amount FROM transactions WHERE id = ?", u["id"]) == [{"kind": "DEPOSIT", "amount": 3_500_000}]
    assert client.patch("/api/transactions/999999", json={"kind": "INTERNAL"}).status_code == 404


def test_patch_without_amount_checks_stored_sign(client):
    (moved,) = rows("SELECT id FROM transactions WHERE memo LIKE '타사대체입고%'")  # amount 0
    (misc,) = rows("SELECT id FROM transactions WHERE memo = '기타정산'")  # +12,340
    r = client.patch(f"/api/transactions/{moved['id']}", json={"kind": "DEPOSIT"})
    assert r.status_code == 422 and "amount" in r.json()["detail"]
    assert client.patch(f"/api/transactions/{misc['id']}", json={"kind": "WITHDRAW"}).status_code == 422
    assert rows("SELECT kind FROM transactions WHERE id IN (?, ?)", moved["id"], misc["id"]) == [{"kind": "UNKNOWN"}] * 2
    assert client.patch(f"/api/transactions/{misc['id']}", json={"kind": "DEPOSIT"}).status_code == 200
    assert client.patch(f"/api/transactions/{moved['id']}", json={"kind": "INTERNAL"}).status_code == 200


def test_delete_refuses_non_manual(client):
    t = client.get("/api/transactions", params={"account_id": "kb:demo-01", "limit": 1}).json()[0]
    r = client.delete(f"/api/transactions/{t['id']}")
    assert r.status_code == 400 and "manual" in r.json()["detail"]
    assert rows("SELECT id FROM transactions WHERE id = ?", t["id"])



def test_delete_account_removes_every_row(client):
    aid = "kb:demo-01"
    assert rows("SELECT 1 FROM transactions WHERE account_id = ?", aid) and rows("SELECT 1 FROM snapshots WHERE account_id = ?", aid)
    r = client.delete(f"/api/accounts/{aid}")
    assert r.status_code == 200 and r.json()["deleted"] == aid and r.json()["rows"]["transactions"] > 0
    for t in ("transactions", "snapshots", "cash_obs", "gold_facts", "coverage"):
        assert not rows(f"SELECT 1 FROM {t} WHERE account_id = ?", aid), t
    assert aid not in [a["id"] for a in client.get("/api/accounts").json()]
    assert client.get("/api/summary").status_code == 200
    assert client.delete(f"/api/accounts/{aid}").status_code == 404


def test_delete_account_refuses_what_a_sync_rebuilds(client, monkeypatch):
    keys = dict.fromkeys(sync.BROKERS["kis"].REQUIRED, "x")
    monkeypatch.setattr(web.config, "load", lambda *a, **k: {**keys, "DC_ACCOUNT": "hana:dc"})
    for aid, word in (("kis:demo-01", "증권사 키"), ("hana:dc", "DC_ACCOUNT")):
        r = client.delete(f"/api/accounts/{aid}")
        assert r.status_code == 400 and word in r.json()["detail"], aid
        assert rows("SELECT 1 FROM accounts WHERE id = ?", aid)
    assert client.delete("/api/accounts/toss:demo-01").status_code == 200  # toss keys unset: nothing brings it back


def test_invalid_kind_rejected(client):
    body = {"account_id": "hana:dc", "date": "2026-01-15", "kind": "BOGUS", "amount": 1}
    assert client.post("/api/transactions", json=body).status_code == 422
    (u,) = rows("SELECT id FROM transactions WHERE memo = '기타정산'")
    assert client.patch(f"/api/transactions/{u['id']}", json={"kind": "BOGUS"}).status_code == 422
    assert client.get("/api/transactions", params={"kind": "BOGUS"}).status_code == 422
    assert client.post("/api/accounts", json={"id": "x", "institution": "x", "name": "x", "kind": "bank"}).status_code == 422


def test_account_add(client):
    assert client.post("/api/accounts", json={"id": "kb:2", "institution": "kb", "name": "KB 2"}).status_code == 200
    again = {"id": "kb:2", "institution": "hana", "name": "퇴직연금", "kind": "pension"}
    assert client.post("/api/accounts", json=again).status_code == 409  # was a silent rename, kind ignored
    assert {"id": "kb:2", "institution": "kb", "name": "KB 2", "kind": "brokerage"} in client.get("/api/accounts").json()


def test_foreign_host_and_cross_origin_refused(client):
    assert TestClient(web.app).get("/api/summary").status_code == 403  # Host: testserver (DNS rebinding)
    r = client.post("/api/sync", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    # an <img src=".../api/holdings?refresh=1"> on another site would start a broker read
    assert client.get("/api/holdings", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_sync_endpoint(client, monkeypatch):
    seen = {}
    fake = types.ModuleType("assetlog.sync")
    fake.sync = lambda conn, cfg, today: seen.update(today=today) or {"kb": "skipped (not configured)"}
    monkeypatch.setitem(sys.modules, "assetlog.sync", fake)
    monkeypatch.setattr(assetlog, "sync", fake, raising=False)
    assert client.post("/api/sync", headers={"Origin": "http://localhost"}).json() == {"kb": "skipped (not configured)"}
    assert seen["today"] == web.today()


def test_import_csv(client, monkeypatch, tmp_path):
    monkeypatch.setattr(web, "INBOX", tmp_path / "inbox")
    importers = pytest.importorskip("assetlog.importers")
    if "csv" not in importers.FORMATS:
        pytest.skip("no csv format in importers.FORMATS")
    assert client.get("/api/import/formats").json() == importers.FORMATS
    params = {"account_id": "kis:demo-01", "fmt": "csv"}
    assert client.post("/api/import", params={**params, "fmt": "nope"}, content=b"x").status_code == 422
    assert client.post("/api/import", params={**params, "account_id": "nope"}, content=b"x").status_code == 404
    csv = "date,amount,memo\n2026-01-05,1000000,이체입금\n".encode()
    headers = {"X-Filename": "%ED%95%9C%EA%B8%80.csv", "Content-Type": "application/octet-stream"}  # 한글.csv
    r = client.post("/api/import", params=params, content=csv, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 1
    assert rows("SELECT kind, amount FROM transactions WHERE source = 'csv'") == [{"kind": "DEPOSIT", "amount": 1_000_000}]
    bad = client.post("/api/import", params=params, content=b"no,header\n1,2\n", headers=headers)
    assert bad.status_code == 400 and "ValueError" in bad.json()["detail"]
    (kept,) = (tmp_path / "inbox").iterdir()  # a failed import leaves its file for fixing the parser
    assert kept.name.endswith("_failed-csv_한글.csv") and kept.read_bytes() == b"no,header\n1,2\n"
    assert kept.name in bad.json()["detail"]


def test_home_screen_install_assets(client):
    m = client.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.headers["content-type"].startswith("application/manifest+json")
    assert m.json()["start_url"] == "/" and m.json()["display"] == "standalone"
    icon = client.get("/icon.svg")
    assert icon.status_code == 200 and icon.text.startswith("<svg")


def test_inbox_keeps_raw_phone_files(client, monkeypatch, tmp_path):
    monkeypatch.setattr(web, "INBOX", tmp_path / "inbox")
    r = client.post("/api/inbox", content=b"\x89PNG fake", headers={"X-Filename": "..%2F..%2F%EA%B1%B0%EB%9E%98.png"})
    assert r.status_code == 200
    saved = list((tmp_path / "inbox").iterdir())
    assert len(saved) == 1 and saved[0].name == r.json()["saved"] and saved[0].name.endswith("거래.png")
    assert saved[0].read_bytes() == b"\x89PNG fake"
    assert client.post("/api/inbox", content=b"").status_code == 400
    monkeypatch.setattr(web, "MAX_INBOX", 4)
    assert client.post("/api/inbox", content=b"12345").status_code == 413
    assert len(list((tmp_path / "inbox").iterdir())) == 1  # rejected uploads leave nothing behind


def test_holdings_live_fallback_isolation_and_cache(client, monkeypatch):
    from assetlog import sync
    calls, answers = [], [[{"account_id": "kis:demo-01", "total_krw": 5_000_000.0, "positions": [
        {"symbol": "VOO", "name": "VOO", "market": "AMEX", "currency": "USD", "quantity": 1.5, "avg_price": 600.0,
         "price": 700.0, "value_krw": 1_428_000.0, "cost_krw": 1_224_000.0, "pnl_krw": 204_000.0, "pnl_rate": 1 / 6,
         "cost_local": 900.0, "pnl_local": 150.0}]}], []]

    def toss_down(cfg):
        raise RuntimeError("toss down")
    brokers = {"kb": types.SimpleNamespace(REQUIRED=("KB_APP_KEY",), holdings=lambda cfg: 1 / 0),  # not configured
               "kis": types.SimpleNamespace(REQUIRED=("K",), holdings=lambda cfg: calls.append(1) or answers[len(calls) - 1]),
               "toss": types.SimpleNamespace(REQUIRED=("K",), holdings=toss_down)}
    monkeypatch.setattr(sync, "BROKERS", brokers)
    monkeypatch.setattr(web.config, "load", lambda: {"K": "1"})
    monkeypatch.setattr(web, "_held", {})
    latest = {a["id"]: a for a in client.get("/api/summary").json()["accounts"]}

    r = client.get("/api/holdings")
    assert r.status_code == 200, r.text
    got = {a["id"]: a for a in r.json()["accounts"]}
    assert set(got) == set(latest)  # every ledger account, API or not
    kis = got["kis:demo-01"]
    assert (kis["source"], kis["error"], kis["total_krw"], kis["positions"][0]["symbol"]) == ("live", None, 5_000_000.0, "VOO")
    assert (kis["positions"][0]["cost_local"], kis["positions"][0]["pnl_local"]) == (900.0, 150.0)  # dollar P&L passes through
    toss = got["toss:demo-01"]  # its broker failed: the others still answer, it falls back to its snapshot
    assert (toss["source"], toss["positions"]) == ("snapshot", []) and "toss down" in toss["error"]
    assert (toss["total_krw"], toss["as_of"]) == (latest["toss:demo-01"]["value"], latest["toss:demo-01"]["as_of"])
    assert got["kb:demo-01"]["source"] == "snapshot" and "API 키" in got["kb:demo-01"]["error"]
    hana = got["hana:dc"]  # no API at all: not an error
    assert (hana["source"], hana["error"], hana["total_krw"]) == ("snapshot", None, latest["hana:dc"]["value"])

    assert client.get("/api/holdings").json() == r.json() and len(calls) == 1  # cached
    fresh = {a["id"]: a for a in client.get("/api/holdings", params={"refresh": 1}).json()["accounts"]}
    assert len(calls) == 2  # ?refresh=1 re-reads; this time KIS left the account out
    assert fresh["kis:demo-01"]["source"] == "snapshot" and "없어요" in fresh["kis:demo-01"]["error"]
    assert TestClient(web.app).get("/api/holdings").status_code == 403  # host guard still applies


def test_holdings_and_summary_carry_the_dividend_payers(client, monkeypatch):
    from assetlog import sync
    from assetlog.brokers import kis
    assert client.get("/api/summary").json()["dividend_payers"] is None  # before any holdings read
    voo = {"symbol": "VOO", "name": "VOO", "market": "AMEX", "currency": "USD", "quantity": 2.0, "price": 600.0,
           "value_krw": 1_680_000.0}
    held = [{"account_id": "kis:demo-01", "total_krw": 5_000_000.0,
             "positions": [voo, {**voo, "symbol": "TSLA", "name": "TSLA", "value_krw": 560_000.0, "price": 200.0}]}]
    monkeypatch.setattr(sync, "BROKERS", {"kis": types.SimpleNamespace(REQUIRED=("KIS_APP_KEY",), holdings=lambda cfg: held)})
    monkeypatch.setattr(web.config, "load", lambda: {"KIS_APP_KEY": "k", "KIS_APP_SECRET": "s", "KIS_CANO": "1"})
    monkeypatch.setattr(web, "_held", {})
    monkeypatch.setattr(kis, "rights_of", lambda cfg, pdno, excg, start, end: [
        {"acpl_bass_dt": d, "alct_frcr_unpr": "3.5"} for d in ("20260327", "20260626") if pdno == "VOO"])
    for call in ("get", "post"):  # nothing else may reach KIS
        monkeypatch.setattr(kis.requests, call, lambda *a, **k: 1 / 0)
    r = client.get("/api/holdings").json()
    got = {p["symbol"]: p for a in r["accounts"] for p in a["positions"]}
    assert (got["VOO"]["annual_div_krw"], got["VOO"]["div_yield"]) == (pytest.approx(7 * 0.85 * 2 * 1400), pytest.approx(7 * 0.85 / 600))
    assert (got["TSLA"]["annual_div_krw"], got["TSLA"]["div_yield"]) == (0, 0)
    d = client.get("/api/summary").json()["dividend_payers"]
    assert d["as_of"] == r["fetched_at"] and d["missing"] == [] and d["value_krw"] == 1_680_000  # TSLA left out
    assert d["yield"] == pytest.approx(7 * 0.85 / 600) and [p["symbol"] for p in d["positions"]] == ["VOO"]


def test_service_worker_is_served_from_root_uncached(client):
    r = client.get("/sw.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert r.headers["cache-control"] == "no-cache" and "X-Offline" in r.text


def test_a_deposit_entered_by_hand_replaces_the_dc_models_scheduled_one_at_once(client):
    conn = db.connect()
    with conn:
        db.upsert_account(conn, "dc", "hana", "퇴직연금", "pension")
        db.upsert_transaction(conn, "dc", TODAY.replace(day=1), 305_000, "부담금 입금 (예정액으로 추정)", "dc_model",
                              f"dep:{TODAY:%Y-%m}", "DEPOSIT")
    conn.close()
    r = client.post("/api/transactions", json={"account_id": "dc", "date": TODAY.replace(day=1).isoformat(),
                                               "kind": "DEPOSIT", "amount": 400000, "memo": "부담금"})
    assert r.status_code == 200
    assert [x["source"] for x in rows("SELECT source FROM transactions WHERE account_id = 'dc'")] == ["manual"]


def test_a_manual_value_replaces_the_dc_models_but_a_typo_needs_force(client):
    conn = db.connect()
    with conn:
        for d in ("2026-09-23", "2026-09-24"):
            db.upsert_snapshot(conn, "hana:dc", d, 10_000_000, 1000, dcmodel.SOURCE)
    conn.close()
    fix = {"account_id": "hana:dc", "date": "2026-09-23", "value": 10_200_000}
    assert client.post("/api/snapshots", json=fix).status_code == 200  # re-anchoring on a model day: was a 409
    assert rows("SELECT source, value FROM snapshots WHERE account_id = 'hana:dc' AND date = '2026-09-23'") == [
        {"source": "manual", "value": 10_200_000}]
    typo = {"account_id": "hana:dc", "date": TODAY.isoformat(), "value": 102_000_000}  # no model row of its own
    r = client.post("/api/snapshots", json=typo)
    assert r.status_code == 409
    assert (r.json()["detail"]["code"], r.json()["detail"]["model"]) == ("dc_mismatch", 10_000_000)
    assert "2026-09-24" in r.json()["detail"]["message"]
    assert client.post("/api/snapshots", json={**typo, "force": True}).status_code == 200


def test_a_flow_booked_by_hand_replaces_the_estimate_for_its_window_at_once(client):
    conn = db.connect()
    with conn:
        for d in ("2026-09-18", "2026-09-20", "2026-09-22"):  # windows 09-19..20 and 09-21..22
            db.upsert_cash_obs(conn, "kis:demo-01", d, 0, 0, 0, 1400)
        for d in ("2026-09-20", "2026-09-22"):
            db.upsert_transaction(conn, "kis:demo-01", d, 1_000_000, "입금 (잔고로 추정)", "kis_infer", d, "DEPOSIT")
        db.upsert_transaction(conn, "kis:demo-01", "2026-09-19", 0, "타사대체입고", "csv", "x")  # UNKNOWN
    conn.close()
    booked = {"account_id": "kis:demo-01", "date": "2026-09-21", "kind": "DEPOSIT", "amount": 1_000_000}
    assert client.post("/api/transactions", json=booked).status_code == 200
    (moved,) = rows("SELECT id FROM transactions WHERE source = 'csv' AND memo = '타사대체입고'")
    assert client.patch(f"/api/transactions/{moved['id']}", json={"kind": "DEPOSIT", "amount": 500_000}).status_code == 200
    assert rows("SELECT source FROM transactions WHERE account_id = 'kis:demo-01' AND source = 'kis_infer'") == []


def test_import_stops_reading_at_the_limit(client, monkeypatch):
    monkeypatch.setattr(web, "MAX_UPLOAD", 1000)
    pulled = []

    async def post(headers):  # TestClient hands the app the whole body at once, so drive the ASGI app directly
        chunks, sent = iter([b"x" * 100] * 1000), []

        async def receive():
            chunk = next(chunks, None)
            pulled.append(chunk is not None)
            return {"type": "http.request", "body": chunk or b"", "more_body": chunk is not None}

        async def send(message):
            sent.append(message)
        await web.app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                       "scheme": "http", "path": "/api/import", "raw_path": b"/api/import", "root_path": "",
                       "query_string": b"account_id=kis:demo-01&fmt=csv", "client": ("127.0.0.1", 1),
                       "server": ("localhost", 80), "headers": [(b"host", b"localhost"), *headers]}, receive, send)
        return next(m["status"] for m in sent if m["type"] == "http.response.start")

    assert anyio.run(post, []) == 413 and sum(pulled) == 11  # was all 1000 chunks, then the 413
    pulled.clear()
    assert anyio.run(post, [(b"content-length", b"100000")]) == 413 and not any(pulled)


def test_queued_holdings_refreshes_share_one_read(client, monkeypatch):
    calls = []

    def slow_read():
        calls.append(1)
        time.sleep(0.3)
        return {"at": time.monotonic(), "live": {}, "errors": {}, "fetched_at": "t"}
    monkeypatch.setattr(web, "_live_holdings", slow_read)
    monkeypatch.setattr(web, "_held", {})
    first = threading.Thread(target=web.holdings, kwargs={"refresh": True})
    first.start()
    time.sleep(0.1)
    web.holdings(refresh=True)  # asked while the first read ran: takes its result
    first.join()
    assert len(calls) == 1
    web.holdings(refresh=True)
    assert len(calls) == 2


def test_files_are_refused_when_the_disk_runs_low(client, monkeypatch, tmp_path):
    monkeypatch.setattr(web, "INBOX", tmp_path / "inbox")
    monkeypatch.setattr(web, "MIN_FREE", 1 << 62)
    assert client.post("/api/inbox", content=b"x").status_code == 507
    bad = client.post("/api/import", params={"account_id": "kis:demo-01", "fmt": "csv"}, content=b"no,header\n1,2\n")
    assert bad.status_code == 400 and "ValueError" in bad.json()["detail"] and "보관" not in bad.json()["detail"]
    assert not (tmp_path / "inbox").exists()


def test_long_hangul_file_names_are_cut_by_bytes(client, monkeypatch, tmp_path):
    monkeypatch.setattr(web, "INBOX", tmp_path / "inbox")
    bad = client.post("/api/import", params={"account_id": "kis:demo-01", "fmt": "csv"}, content=b"no,header\n1,2\n",
                      headers={"X-Filename": quote("가" * 75 + ".csv")})
    assert bad.status_code == 400 and "보관" in bad.json()["detail"]  # was a 500 (ENAMETOOLONG), file lost
    assert client.post("/api/inbox", content=b"x", headers={"X-Filename": quote("가" * 80)}).status_code == 200
    names = [p.name for p in (tmp_path / "inbox").iterdir()]
    assert len(names) == 2 and all(len(n.encode()) <= 255 for n in names)
    assert any(n.endswith("가.csv") for n in names)


def test_every_response_carries_the_security_headers(client):
    for r in (client.get("/"), client.get("/api/summary"), TestClient(web.app).get("/api/summary")):
        csp = r.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in csp
        assert "font-src 'self' https://cdn.jsdelivr.net" in csp  # the Pretendard variable font
        assert "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net" in csp  # its stylesheet
        assert (r.headers["x-content-type-options"], r.headers["referrer-policy"]) == ("nosniff", "no-referrer")


def test_internet_clients_are_refused_by_their_own_address(client):
    assert client.get("/api/accounts").status_code == 200  # this machine
    for h in ("X-Forwarded-For", "Forwarded"):  # a tunnel on this PC arrives from loopback: refused, not trusted
        assert client.get("/api/accounts", headers={h: "8.8.8.8"}).status_code == 403, h
    direct = TestClient(web.app, base_url="http://localhost", client=("8.8.8.8", 1))  # a port opened by mistake
    assert direct.get("/api/accounts").status_code == 403
    assert direct.get("/api/accounts", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
    assert client.get("/login").status_code == 404  # no login any more
    for path in ("/api/login", "/api/logout"):
        assert client.post(path).status_code == 404, path


TOSS_403 = ("toss POST /oauth2/token: HTTP 403 access_denied: {\"error\": \"access_denied\"} "
            "(is this machine's public IP registered in WTS 설정 > Open API > 허용 IP 관리?)")


def _broker(**out):
    return SimpleNamespace(REQUIRED=(), fetch=lambda cfg, today, since: {
        "accounts": [], "snapshots": [], "transactions": [], **out})


def _failing(msg):
    def fetch(cfg, today, since):
        raise RuntimeError(msg)
    return SimpleNamespace(REQUIRED=(), fetch=fetch)


def test_every_sync_run_records_its_outcome(monkeypatch):
    conn, day = db.connect(":memory:"), date(2026, 9, 26)
    status = lambda: json.loads((config.DATA / "sync.json").read_text())  # noqa: E731
    monkeypatch.setattr(sync, "BROKERS", {"kb": _broker(), "kis": SimpleNamespace(REQUIRED=("KIS_APP_KEY",))})
    sync.sync(conn, {}, day)  # ok, and skipped (not a failure)
    ok = status()
    assert ok["failed"] == [] and ok["ok_at"] == ok["at"] and ok["at"].endswith("+09:00")
    monkeypatch.setattr(sync, "BROKERS", {"kb": _broker(errors=["SWQA2301: HTTP 500"]), "toss": _failing(TOSS_403),
                                          "kis": _failing("KIS tokenP: HTTP 403 EGW00103 유효하지 않은 AppKey입니다." + "x" * 300)})
    sync.sync(conn, {}, day)  # partial, and errors
    s = status()
    assert s["ok_at"] == ok["at"]  # failing since the run after this
    assert [(f["source"], f["hint"]) for f in s["failed"]] == [("kb", None), ("toss", sync.HINTS[0][2]), ("kis", sync.HINTS[1][2])]
    assert s["failed"][0]["message"] == "SWQA2301: HTTP 500" and len(s["failed"][2]["message"]) == 200
    monkeypatch.setattr(sync, "_sync", lambda *a: 1 / 0)
    with pytest.raises(ZeroDivisionError):  # a crash is recorded, and still raised
        sync.sync(conn, {}, day)
    assert status()["failed"] == [{"source": "sync", "message": "ZeroDivisionError: division by zero", "hint": None}]
    assert status()["ok_at"] == ok["at"]
    sync._record({"kis": "error: RuntimeError: KIS tokenP: HTTP 403 EGW00133 접근토큰 발급 잠시 후 다시 시도하세요(1분당 1회)"})
    assert status()["failed"][0]["hint"] is None  # the one-per-minute token limit is not an expired subscription
    sync._record({"kis": "error: ConnectionError: HTTPSConnectionPool(host='openapi.koreainvestment.com', port=9443): "
                          "Max retries exceeded (Caused by NameResolutionError(...))"})
    assert "인터넷 연결" in status()["failed"][0]["hint"]  # the boot catch-up that ran before Wi-Fi was up


def test_summary_carries_the_last_sync_with_hints(client, monkeypatch):
    assert client.get("/api/summary").json()["sync"] is None  # never synced
    monkeypatch.setattr(sync, "BROKERS", {"toss": _failing(TOSS_403), "kis": _failing("ValueError: unexpected layout")})
    assert "error" in client.post("/api/sync", headers={"Origin": "http://localhost"}).json()["toss"]  # the page's (or phone's) sync
    s = client.get("/api/summary").json()["sync"]
    assert s["ok_at"] is None and [f["source"] for f in s["failed"]] == ["toss", "kis"]
    assert "허용 IP 관리" in s["failed"][0]["hint"] and s["failed"][1]["hint"] is None  # no hint for an unknown cause


def test_cli_sync_exits_75_only_when_the_network_was_missing(monkeypatch):
    from assetlog import __main__ as cli
    monkeypatch.setattr(cli.sys, "argv", ["assetlog", "sync"])
    monkeypatch.setattr(db, "connect", lambda: None)
    monkeypatch.setattr(sync, "sync", lambda conn, cfg, today: {"kis": "ok: 1 snapshots, 0 transactions",
                                                                "toss": "error: RuntimeError: toss HTTP 403"})
    cli.main()  # a real failure: no point retrying every 10 min
    monkeypatch.setattr(sync, "sync", lambda conn, cfg, today: {
        "kis": "error: ConnectionError: Max retries exceeded (Caused by NameResolutionError(...))"})
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 75  # deploy/assetlog-sync.service restarts on it


def test_inbox_parts_are_put_back_together_in_any_order(client, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "INBOX", tmp_path / "inbox")
    send = lambda i, body, n=3, up="0123456789abcdef": client.post(
        f"/api/inbox/part?upload={up}&i={i}&n={n}", content=body,
        headers={"Origin": "http://localhost", "X-Filename": "%EB%85%B9%ED%99%94.mp4"})
    assert send(2, b"C").json() == {"part": 2, "of": 3, "have": 1}
    assert send(0, b"A").status_code == 200 and send(0, b"A").json()["have"] == 2  # a repeat replaces its twin
    done = send(1, b"B").json()
    assert done["bytes"] == 3 and done["saved"].endswith("녹화.mp4")
    assert (tmp_path / "inbox" / done["saved"]).read_bytes() == b"ABC"
    assert not (tmp_path / "inbox" / ".parts" / "0123456789abcdef").exists()  # parts gone once the file is whole
    assert send(3, b"x").status_code == 422 and send(0, b"x", up="nothex").status_code == 422
    monkeypatch.setattr(web, "MAX_PART", 2)
    assert send(0, b"123").status_code == 413
