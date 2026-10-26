"""cloud.run against a fake Worker (no network): ops go through the real app on a demo ledger, state goes back up."""
import base64
import json
import os
import stat
import time
from datetime import date, datetime, timedelta
from unittest import mock
from urllib.parse import urlsplit

import pytest

from assetlog import cloud, db, demo, web

TODAY = date(2026, 9, 25)
CFG = {"CLOUD_URL": "https://assetlog.example.workers.dev/", "CLOUD_PC_KEY": "pc-key-secret",
       "CF_ACCESS_CLIENT_ID": "id.access", "CF_ACCESS_CLIENT_SECRET": "access-secret"}


class Resp:
    def __init__(self, body, status=200):
        self.status_code, self.body, self.text = status, body, json.dumps(body)

    def json(self):
        return self.body


class Worker:
    """Stands in for requests.Session against the Worker. Honours ?after= unless `stale` (then it offers every op, even
    reported ones, as a broken Worker might); `down` makes the report endpoint fail, `refuse` the PUT of those keys.
    state["items"] collects one key per PUT /pc/state/<key>, as the real Worker now takes them (finding #2: one
    request per key)."""
    def __init__(self, ops, stale=False, down=False, refuse=()):
        self.headers, self.ops, self.stale, self.down, self.refuse = {}, ops, stale, down, refuse
        self.calls, self.results, self.state = [], {}, {"items": {}, "pushed_at": None}

    def request(self, method, url, **kw):
        assert kw["timeout"] and kw["allow_redirects"] is False
        path = urlsplit(url).path
        self.calls.append((method, path, dict(self.headers)))
        assert len(self.calls) < 100, "runaway pull loop"
        if (method, path) == ("GET", "/pc/ops"):
            if self.stale:
                return Resp({"ops": self.ops})
            after = int(kw["params"]["after"])
            return Resp({"ops": [o for o in self.ops if o["id"] > after and o["id"] not in self.results][:20]})
        if method == "POST" and path.startswith("/pc/ops/"):
            if self.down:
                return Resp({"detail": "down"}, 503)
            self.results[int(path.rsplit("/", 1)[1])] = kw["json"]
            return Resp({"ok": True})
        if method == "PUT" and path.startswith("/pc/state/"):
            key, pushed_at = path.rsplit("/", 1)[1], kw["headers"]["X-Pushed-At"]
            if key in self.refuse:
                return Resp({"detail": "down"}, 503)
            self.state["items"][key] = json.loads(kw["data"])  # the Worker itself never parses this; the test does
            self.state["pushed_at"] = pushed_at
            return Resp({"saved": key, "pushed_at": pushed_at})
        return Resp({"detail": "Not Found"}, 404)


def op(id, method, path, body=b"", query="", content_type="application/json", filename=None):
    if isinstance(body, dict):
        body = json.dumps(body).encode()
    return {"id": id, "at": "2026-09-25T10:00:00Z", "method": method, "path": path, "query": query,
            "content_type": content_type, "filename": filename, "body_b64": base64.b64encode(body).decode()}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(web.config, "DATA", tmp_path / "data")
    monkeypatch.setattr(web, "today", lambda: TODAY)
    monkeypatch.setattr(web, "INBOX", tmp_path / "inbox")
    monkeypatch.setattr(web, "_held", {})
    monkeypatch.setattr(web, "_live_holdings", lambda: {  # no broker reads
        "at": time.monotonic(), "live": {}, "errors": {}, "fetched_at": "2026-09-25T10:00:00+09:00"})
    conn = db.connect()
    with conn:
        demo.seed(conn, TODAY)
    conn.close()
    (tmp_path / "data").mkdir()
    return tmp_path / "data"


def run(monkeypatch, worker, cfg=CFG):
    monkeypatch.setattr(cloud.requests, "Session", lambda: worker)
    return cloud.run(cfg)


def rows(sql, *args):
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def months(items):
    return {k for k in items if k.startswith(cloud.MONTH)}


def txs(items):
    """The pushed months put back together as the Worker's read does: newest key first, each in its pushed order."""
    return [t for k in sorted(months(items), reverse=True) for t in items[k]]


def ledger_ids():  # web.py's /api/transactions order
    return [r["id"] for r in rows("SELECT id FROM transactions ORDER BY date DESC, id DESC")]


def ledger_months():
    return {cloud.MONTH + r["m"] for r in rows("SELECT DISTINCT substr(date, 1, 7) AS m FROM transactions")}


OPS = [
    op(1, "POST", "/api/accounts", {"id": "toss:new", "institution": "toss", "name": "새 계좌"}),
    op(2, "POST", "/api/snapshots", {"account_id": "toss:new", "date": "2026-09-24", "value": 1_234_567}),
    op(3, "POST", "/api/snapshots", {"account_id": "nope:1", "date": "2026-09-24", "value": 1}),  # 404
    op(4, "POST", "/api/transactions", {"account_id": "toss:new", "date": "2026-09-24", "kind": "BOGUS", "amount": 1}),
    op(5, "POST", "/api/import", "date,amount,memo\n2026-01-05,1000000,이체입금\n".encode(),
       query="account_id=kis%3Ademo-01&fmt=csv", content_type="application/octet-stream",
       filename="%ED%95%9C%EA%B8%80.csv"),
    op(6, "POST", "/api/inbox", b"\x89PNG"),  # the Worker refuses these; the PC must not apply one either
]


def test_ops_applied_through_the_app_and_state_pushed(ledger, monkeypatch):
    tx_before = rows("SELECT COUNT(*) AS n FROM transactions")[0]["n"]
    raw_id = rows("SELECT MIN(id) AS id FROM transactions")[0]["id"]
    conn = db.connect()
    with conn:
        conn.execute("UPDATE transactions SET raw = ? WHERE id = ?", ("x" * 3000, raw_id))
    conn.close()

    origins, refusal = [], web._refusal
    monkeypatch.setattr(web, "_refusal", lambda r: origins.append((r.method, r.headers.get("origin"))) or refusal(r))
    w = Worker(OPS)
    report = run(monkeypatch, w)
    assert {o for m, o in origins if m != "GET"} == {"http://localhost"}  # the app's Origin guard saw this app

    assert rows("SELECT value, source FROM snapshots WHERE account_id = 'toss:new'") == [
        {"value": 1_234_567, "source": "manual"}]  # a manual snapshot, as the page's POST would make
    assert rows("SELECT kind, amount FROM transactions WHERE source = 'csv'") == [
        {"kind": "DEPOSIT", "amount": 1_000_000}]  # query, Content-Type and X-Filename came through
    assert rows("SELECT COUNT(*) AS n FROM transactions")[0]["n"] == tx_before + 1
    assert not (ledger.parent / "inbox").exists()
    assert {k: v["status"] for k, v in w.results.items()} == {1: "done", 2: "done", 3: "error", 4: "error",
                                                              5: "done", 6: "error"}
    assert "unknown account 'nope:1'" in w.results[3]["result"]
    assert w.results[4]["result"].startswith("kind: ")  # a validation list, shown as the page shows it
    assert "/api/inbox" in w.results[6]["result"]
    assert report["applied"] == 3 and report["errors"] == 3

    items = w.state["items"]
    n = len(ledger_months())
    assert n > 30 and months(items) == ledger_months()  # one key per month, never the old single "transactions"
    assert set(items) - months(items) == {"summary", "accounts", "holdings", "snapshots", "formats"}
    assert report["pushed"] == ["accounts", "formats", "holdings", "snapshots", "summary", f"transactions-* ({n})"]
    assert report["left_out"] == {}
    assert w.state["pushed_at"].endswith("+09:00")
    assert "toss:new" in {a["id"] for a in items["accounts"]}  # pushed after the ops were applied
    assert len(txs(items)) == tx_before + 1 and [t["id"] for t in txs(items)] == ledger_ids()  # the app's order
    assert max(len(t["raw"] or "") for t in txs(items)) == 2000
    assert items["formats"]["csv"] and items["summary"]["total_value"] > 0

    state = json.loads((ledger / "cloud.json").read_text())
    assert state["after"] == 6 and state["unreported"] == {}
    assert state["holdings_at"] > 0  # holdings had never been pushed before, so this run pushed it
    assert stat.S_IMODE(os.stat(ledger / "cloud.json").st_mode) == 0o600



def test_account_delete_from_the_phone(ledger, monkeypatch):
    w = Worker([op(1, "DELETE", "/api/accounts/kb%3Ademo-01")])  # the page's encodeURIComponent, as the Worker kept it
    run(monkeypatch, w)
    assert w.results[1]["status"] == "done"
    assert not rows("SELECT 1 FROM accounts WHERE id = 'kb:demo-01'")
    assert not rows("SELECT 1 FROM transactions WHERE account_id = 'kb:demo-01'")
    assert "kb:demo-01" not in {a["id"] for a in w.state["items"]["accounts"]}

def test_never_reapplied_and_lost_reports_retried(ledger, monkeypatch):
    ops = [op(7, "POST", "/api/transactions", {"account_id": "kb:demo-01", "date": "2026-09-24", "kind": "DEPOSIT",
                                              "amount": 5000})]
    manual = "SELECT COUNT(*) AS n FROM transactions WHERE source = 'manual' AND amount = 5000"
    with pytest.raises(RuntimeError, match="HTTP 503"):  # applied, then the report failed
        run(monkeypatch, Worker(ops, down=True))
    assert rows(manual)[0]["n"] == 1
    assert json.loads((ledger / "cloud.json").read_text()) == {"after": 7, "unreported": {"7": {"status": "done"}}}

    w = Worker(ops, stale=True)  # still offers op 7: the PC's own record keeps it from being booked twice
    report = run(monkeypatch, w)
    assert rows(manual)[0]["n"] == 1 and report["applied"] == 0
    assert w.results == {7: {"status": "done"}}  # the kept result went out first
    state = json.loads((ledger / "cloud.json").read_text())
    assert state["after"] == 7 and state["unreported"] == {}


def test_interrupted_op_reported_not_retried(ledger, monkeypatch):
    ops = [op(3, "POST", "/api/snapshots", {"account_id": "kb:demo-01", "date": "2026-09-24", "value": 9})]
    # the run dies mid-request, when whether the ledger committed is unknown
    with mock.patch.object(cloud, "_apply", side_effect=KeyboardInterrupt), pytest.raises(KeyboardInterrupt):
        run(monkeypatch, Worker(ops))
    assert json.loads((ledger / "cloud.json").read_text()) == {"after": 3, "unreported": {"3": cloud.INTERRUPTED}}

    w = Worker(ops, stale=True)
    run(monkeypatch, w)
    assert w.results == {3: cloud.INTERRUPTED}
    assert rows("SELECT 1 FROM snapshots WHERE value = 9") == []


def test_secret_headers_sent_and_never_reported(ledger, monkeypatch):
    w = Worker([])
    report = run(monkeypatch, w)
    puts = [("PUT", f"/pc/state/{k}") for k in w.state["items"]]
    assert [(m, p) for m, p, _ in w.calls] == [("GET", "/pc/ops")] + puts
    assert len(puts) == len(cloud.READS) - 1 + len(ledger_months())  # every read, transactions as its months
    for _, _, h in w.calls:
        assert h == {"X-Assetlog-Key": "pc-key-secret", "CF-Access-Client-Id": "id.access",
                     "CF-Access-Client-Secret": "access-secret"}
    assert not any(s in json.dumps(report) for s in ("pc-key-secret", "access-secret", "id.access"))

    w = Worker([])  # local `wrangler dev`: no Access service token
    run(monkeypatch, w, {"CLOUD_URL": "http://localhost:8787", "CLOUD_PC_KEY": "k"})
    assert all(h == {"X-Assetlog-Key": "k"} for _, _, h in w.calls)


def test_fit_drops_raw_then_oldest_rows_until_it_fits():
    rows = [{"id": i, "raw": "x" * 100} for i in range(50)]  # newest (id 0) first, as the page's own order is
    kept, dropped = cloud._fit(rows, 7000)
    assert dropped == 0 and kept == rows  # already fits: untouched

    kept, dropped = cloud._fit(rows, 2000)
    assert dropped == 0 and len(kept) == 50 and all(r["raw"] is None for r in kept)  # raw dropped, not rows

    kept, dropped = cloud._fit(rows, 500)
    assert 0 < dropped < 50 and len(kept) == 50 - dropped
    assert [r["id"] for r in kept] == list(range(len(kept)))  # the oldest (the list's tail) went first
    assert len(json.dumps(kept, ensure_ascii=False).encode()) <= 500


def test_oversized_list_items_shrink_but_a_dict_is_left_out_whole(ledger, monkeypatch):
    # demo sizes (measured): summary 8,849 B (dict), snapshots 12,012 B, transactions 37,500 B in 36 months of
    # 764-1,549 B (lists); one 6,000-character memo makes the oldest row of 2025-05 alone overflow its month
    big = rows("SELECT id FROM transactions WHERE date LIKE '2025-05%' ORDER BY date, id LIMIT 1")[0]["id"]
    conn = db.connect()
    with conn:
        conn.execute("UPDATE transactions SET memo = ? WHERE id = ?", ("x" * 6000, big))
    conn.close()
    monkeypatch.setattr(cloud, "MAX_ITEM", 5_000)
    w = Worker([])
    report = run(monkeypatch, w)
    assert "summary" not in w.state["items"] and report["left_out"]["summary"] == "over 5,000 bytes"
    for key in ("transactions-2025-05", "snapshots"):
        assert w.state["items"][key], key  # shrunk, not dropped whole
        assert len(json.dumps(w.state["items"][key], ensure_ascii=False).encode()) <= 5_000
        assert "rows left out" in report["left_out"][key]
    assert report["left_out"]["transactions-2025-05"].startswith("1 oldest rows")  # only the big one went
    assert set(report["left_out"]) == {"summary", "snapshots", "transactions-2025-05"}  # every other month whole
    assert len(txs(w.state["items"])) == len(ledger_ids()) - 1
    assert "accounts" in w.state["items"] and "holdings" in w.state["items"] and "formats" in w.state["items"]


def test_holdings_pushed_hourly_unless_applied_or_never_pushed(ledger, monkeypatch):
    def read(report):  # read from the app this run (the live broker call); PUT only if it changed
        return "holdings" in report["pushed"] + report["unchanged"]

    w1 = Worker([])
    run(monkeypatch, w1)
    assert "holdings" in w1.state["items"]  # never pushed before: push it

    w2 = Worker([])
    report2 = run(monkeypatch, w2)
    assert not read(report2) and "holdings" not in report2["left_out"]  # just pushed: too soon

    w3 = Worker([op(1, "POST", "/api/accounts", {"id": "toss:x", "institution": "toss", "name": "x"})])
    assert read(run(monkeypatch, w3))  # an op was applied this run: push it sooner

    state = cloud._read("cloud.json", {})
    state["holdings_at"] -= cloud.HOLDINGS_PERIOD + 1  # simulate over an hour since the last holdings push
    cloud._write("cloud.json", state)
    assert read(run(monkeypatch, Worker([])))  # an hour has passed: push it again


def test_unchanged_keys_skipped_but_summary_always_pushed(ledger, monkeypatch):
    run(monkeypatch, Worker([]))
    w = Worker([])
    report = run(monkeypatch, w)
    assert report["pushed"] == ["summary"] and set(w.state["items"]) == {"summary"}  # its time is the phone's clock
    n = len(ledger_months())  # counted, not listed
    assert report["unchanged"] == ["accounts", "formats", "snapshots", f"transactions-* ({n})"]  # holdings: too soon

    w = Worker([op(1, "POST", "/api/snapshots", {"account_id": "kb:demo-01", "date": "2026-09-24", "value": 7})])
    report = run(monkeypatch, w)
    assert "snapshots" in report["pushed"] and "formats" in report["unchanged"]
    assert set(w.state["items"]) == set(report["pushed"])
    assert any(s["account_id"] == "kb:demo-01" and s["value"] == 7 for s in w.state["items"]["snapshots"])

    class Tomorrow(datetime):  # a new day: every key goes once more, so a Worker that lost state heals by itself
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=1)
    monkeypatch.setattr(cloud, "datetime", Tomorrow)
    report = run(monkeypatch, Worker([]))
    assert {"accounts", "formats", "snapshots", f"transactions-* ({n})"} <= set(report["pushed"])


def test_only_a_changed_month_pushed_and_an_emptied_month_blanked_once(ledger, monkeypatch):
    cloud._write("cloud.json", {"after": 0, "unreported": {}, "sent": {"transactions": "0" * 64}})  # before months
    w = Worker([])
    run(monkeypatch, w)
    assert "transactions" not in w.state["items"]  # the old single key is never PUT: the Worker no longer takes it

    conn = db.connect()
    with conn:
        conn.execute("UPDATE transactions SET memo = 'edited' WHERE id = (SELECT MIN(id) FROM transactions "
                     "WHERE date LIKE '2024-03%')")
    conn.close()
    w = Worker([])
    report = run(monkeypatch, w)
    assert set(w.state["items"]) == {"summary", "transactions-2024-03"}  # only the month that changed
    assert report["pushed"] == ["summary", "transactions-* (1)"]
    assert "edited" in {t["memo"] for t in w.state["items"]["transactions-2024-03"]}

    conn = db.connect()
    with conn:
        conn.execute("DELETE FROM transactions WHERE date LIKE '2023-10%'")  # the oldest month, all of it
    conn.close()
    w = Worker([])
    run(monkeypatch, w)
    assert w.state["items"] == {"summary": w.state["items"]["summary"], "transactions-2023-10": []}  # emptied
    assert "transactions-2023-10" not in cloud._read("cloud.json", {})["sent"]

    w = Worker([])
    run(monkeypatch, w)
    assert set(w.state["items"]) == {"summary"}  # blanked once, then forgotten

    before, conn = ledger_months(), db.connect()
    with conn:
        conn.execute("DELETE FROM transactions")  # an empty ledger
    conn.close()
    w = Worker([])
    run(monkeypatch, w)
    this = cloud.MONTH + w.state["pushed_at"][:7]
    assert months(w.state["items"]) == before | {this} and not txs(w.state["items"])  # all blanked
    assert months(cloud._read("cloud.json", {})["sent"]) == {this}  # but this month stays: the Worker's sign of a push
    w = Worker([])
    assert "transactions-* (1)" in run(monkeypatch, w)["unchanged"] and set(w.state["items"]) == {"summary"}


def test_failed_put_not_recorded_so_it_is_retried(ledger, monkeypatch):
    tx_before = rows("SELECT COUNT(*) AS n FROM transactions")[0]["n"]
    run(monkeypatch, Worker([]))
    add = op(1, "POST", "/api/transactions", {"account_id": "kb:demo-01", "date": "2026-09-24", "kind": "DEPOSIT",
                                              "amount": 5000})
    with pytest.raises(RuntimeError, match="HTTP 503"):  # the op is applied, then its month's PUT fails
        run(monkeypatch, Worker([add], refuse={"transactions-2026-09"}))

    w = Worker([])
    report = run(monkeypatch, w)
    assert months(w.state["items"]) == {"transactions-2026-09"} and "transactions-* (1)" in report["pushed"]
    assert 5000 in {t["amount"] for t in w.state["items"]["transactions-2026-09"]}
    assert "accounts" in report["unchanged"]  # pushed before the failure, and kept as such
    assert len(ledger_ids()) == tx_before + 1


def test_insecure_cloud_url_refused_before_any_secret_is_sent(ledger, monkeypatch):
    monkeypatch.setattr(cloud.requests, "Session", lambda: pytest.fail("must not build a session before the check"))
    with pytest.raises(ValueError, match="https"):
        cloud.run({**CFG, "CLOUD_URL": "http://assetlog.example.workers.dev"})
    assert not (ledger / "cloud.json").exists()


def test_sync_broker_failure_reported_as_error_even_on_a_2xx(ledger, monkeypatch):
    """/api/sync answers 200 with a per-broker report; a broker's own "error: ..." value must not be swallowed as
    a plain "done", or the phone would never learn the sync failed."""
    import sys
    import types

    import assetlog
    fake = types.ModuleType("assetlog.sync")
    fake.sync = lambda conn, cfg, today: {"kis": "error: RuntimeError: kis down", "toss": "ok: 1 snapshots, 0 tx"}
    monkeypatch.setitem(sys.modules, "assetlog.sync", fake)
    monkeypatch.setattr(assetlog, "sync", fake, raising=False)

    w = Worker([op(1, "POST", "/api/sync")])
    report = run(monkeypatch, w)
    assert w.results[1]["status"] == "error"
    assert "kis" in w.results[1]["result"] and "error" in w.results[1]["result"]
    assert "toss" not in w.results[1]["result"]  # only the failed broker is named
    assert report["applied"] == 0 and report["errors"] == 1


def test_nothing_sent_without_cloud_url(ledger, monkeypatch):
    monkeypatch.setattr(cloud.requests, "Session", lambda: pytest.fail("no HTTP without CLOUD_URL"))
    assert cloud.run({**CFG, "CLOUD_URL": ""}) == {"skipped": "CLOUD_URL is not set"}
    assert not (ledger / "cloud.json").exists()


def test_file_parts_queued_by_the_phone_land_whole_in_the_inbox(ledger, monkeypatch):
    q = "upload=00112233aabbccdd&i={}&n=2"
    w = Worker([op(1, "POST", "/api/inbox/part", b"VIDEO-", query=q.format(1), content_type="application/octet-stream",
                   filename="%EB%85%B9%ED%99%94.mp4"),
                op(2, "POST", "/api/inbox/part", b"HEAD-", query=q.format(0), content_type="application/octet-stream",
                   filename="%EB%85%B9%ED%99%94.mp4")])
    report = run(monkeypatch, w)
    assert report["applied"] == 2 and {r["status"] for r in w.results.values()} == {"done"}
    saved = [p for p in (ledger.parent / "inbox").iterdir() if p.is_file()]
    assert len(saved) == 1 and saved[0].name.endswith("녹화.mp4") and saved[0].read_bytes() == b"HEAD-VIDEO-"
