import json
from datetime import date, datetime, timedelta

import pytest

from assetlog import config
from assetlog.brokers import toss

CFG = {"TOSS_CLIENT_ID": "c_test", "TOSS_CLIENT_SECRET": "s3cret-value"}
KIS_KEYS = {"KIS_APP_KEY": "x", "KIS_APP_SECRET": "x", "KIS_CANO": "x"}
TODAY = date(2026, 9, 25)

# Sample responses from docs/contracts/toss.json (openapi.json v1.2.17 examples), plus one non-brokerage account.
SAMPLES = {
    "/oauth2/token": {"access_token": "tok-1", "token_type": "Bearer", "expires_in": 86400},
    "/api/v1/accounts": {"result": [
        {"accountNo": "12345678901", "accountSeq": 1, "accountType": "BROKERAGE"},
        {"accountNo": "99999999999", "accountSeq": 2, "accountType": "PENSION_SAVINGS"},
    ]},
    "/api/v1/exchange-rate": {"result": {
        "baseCurrency": "USD", "quoteCurrency": "KRW", "rate": "1380.5", "midRate": "1375", "basisPoint": "40",
        "rateChangeType": "UP", "validFrom": "2026-03-25T09:30:00+09:00", "validUntil": "2026-03-25T09:31:00+09:00"}},
    "/api/v1/holdings": {"result": {
        "totalPurchaseAmount": {"krw": "6500000", "usd": "1553"},
        "marketValue": {"amount": {"krw": "7200000", "usd": "1785"}, "amountAfterCost": {"krw": "7050000", "usd": "1771.43"}},
        "profitLoss": {"amount": {"krw": "700000", "usd": "232"}, "amountAfterCost": {"krw": "550000", "usd": "218.43"},
                       "rate": "0.1179", "rateAfterCost": "0.0983"},
        "dailyProfitLoss": {"amount": {"krw": "100000", "usd": "25"}, "rate": "0.0141"},
        "items": []}},  # fetch() reads the summary above, never items (holdings() does: WITH_ITEMS)
    "/api/v1/buying-power?KRW": {"result": {"currency": "KRW", "cashBuyingPower": "5000000"}},
    "/api/v1/buying-power?USD": {"result": {"currency": "USD", "cashBuyingPower": "3500.5"}},
    "/api/v1/orders": {"result": {"orders": [], "nextCursor": None, "hasNext": False}},
}
EMPTY_HOLDINGS = {"result": {
    "totalPurchaseAmount": {"krw": "0", "usd": None},
    "marketValue": {"amount": {"krw": "0", "usd": None}, "amountAfterCost": {"krw": "0", "usd": None}},
    "profitLoss": {"amount": {"krw": "0", "usd": None}, "amountAfterCost": {"krw": "0", "usd": None},
                   "rate": "0", "rateAfterCost": "0"},
    "dailyProfitLoss": {"amount": {"krw": "0", "usd": None}, "rate": "0"}, "items": []}}


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.ok, self.text = status < 400, json.dumps(body)

    def json(self):
        return self._body


@pytest.fixture
def api(monkeypatch, tmp_path):
    """Serve SAMPLES; api.queue[key] = [Resp, ...] answers the next calls to key first."""
    monkeypatch.setattr(config, "DATA", tmp_path)  # token cache goes to tmp, not data/
    api = type("Api", (), {"calls": [], "sleeps": [], "queue": {}})()
    monkeypatch.setattr(toss.time, "sleep", api.sleeps.append)

    def request(method, url, headers=None, params=None, data=None, timeout=None):
        key = url.removeprefix(toss.BASE) + (f"?{params['currency']}" if params and "currency" in params else "")
        api.calls.append({"method": method, "key": key, "headers": headers, "params": params, "data": data})
        return api.queue[key].pop(0) if api.queue.get(key) else Resp(200, SAMPLES[key])

    monkeypatch.setattr(toss.requests, "request", request)
    return api


def test_fetch_snapshot_is_holdings_plus_buying_power_at_mid_rate(api):
    out = toss.fetch(CFG, TODAY, None)
    cash = 5_000_000 + 3500.5 * 1375
    assert {k: out[k] for k in ("accounts", "snapshots", "transactions", "fx")} == {
        "accounts": [{"id": "toss:12345678901", "institution": "toss", "name": "토스증권", "kind": "brokerage"}],
        "snapshots": [{"account_id": "toss:12345678901", "date": TODAY, "cash": cash,
                       "value": 7_200_000 + 1785 * 1375 + cash,  # totalPurchaseAmount never shows up
                       "rate": 1375.0}],  # the midRate it was priced at, for the dollar return
        "transactions": [],  # no KIS keys in CFG: no per-share data, so no dividends
        "fx": [{"date": TODAY, "currency": "USD", "krw": 1375.0}],  # the same rate, for the ₩/$ toggle
    }
    # no dividends means no cash legs: infer.py would book each unsubtracted dividend as a deposit
    assert out["cash_obs"] == [] and out["settlements"] == []
    assert out["errors"] == ["toss:12345678901: dividends and deposit/withdrawal estimates need the KIS keys (per-share data)"]
    token, *rest = api.calls
    assert token["method"] == "POST" and token["data"]["grant_type"] == "client_credentials"
    assert "Authorization" not in token["headers"]
    assert all(c["headers"]["Authorization"] == "Bearer tok-1" for c in rest)
    per_account = [c for c in rest if c["key"].startswith(("/api/v1/holdings", "/api/v1/buying-power", "/api/v1/orders"))]
    assert len(per_account) == 4 and all(c["headers"]["X-Tossinvest-Account"] == "1" for c in per_account)
    assert per_account[-1]["params"] == {"status": "OPEN"}  # open orders only: no order history without the KIS keys
    assert all(s == toss.PACE for s in api.sleeps) and len(api.sleeps) == len(api.calls)


def test_empty_holdings_is_cash_only(api):
    api.queue["/api/v1/holdings"] = [Resp(200, EMPTY_HOLDINGS)]
    (snap,) = toss.fetch(CFG, TODAY, None)["snapshots"]
    assert snap["value"] == snap["cash"] == 5_000_000 + 3500.5 * 1375


def test_token_cached_and_reissued_once_on_revoke(api):
    toss.fetch(CFG, TODAY, None)
    api.queue["/api/v1/accounts"] = [Resp(401, {"error": {"requestId": "r1", "code": "token-revoked", "message": ""}})]
    api.queue["/oauth2/token"] = [Resp(200, {**SAMPLES["/oauth2/token"], "access_token": "tok-2"})]
    toss.fetch(CFG, TODAY, None)
    assert [c["key"] for c in api.calls].count("/oauth2/token") == 2  # 2nd fetch reused the cache until the 401
    assert api.calls[-1]["headers"]["Authorization"] == "Bearer tok-2"
    assert json.loads((config.DATA / "tokens.json").read_text())["toss:c_test"]["token"] == "tok-2"


def test_token_cache_is_per_client_id(api):
    toss.fetch(CFG, TODAY, None)
    api.queue["/oauth2/token"] = [Resp(200, {**SAMPLES["/oauth2/token"], "access_token": "tok-new"})]
    toss.fetch({**CFG, "TOSS_CLIENT_ID": "c_other"}, TODAY, None)
    assert api.calls[-1]["headers"]["Authorization"] == "Bearer tok-new"  # not the old client's cached tok-1


def test_a_token_another_caller_already_renewed_is_reused_not_revoked(api):
    assert toss._token(CFG) == "tok-1"
    api.queue["/oauth2/token"] = [Resp(200, {**SAMPLES["/oauth2/token"], "access_token": "tok-2"}),
                                  Resp(200, {**SAMPLES["/oauth2/token"], "access_token": "tok-3"})]
    # two callers both had tok-1 turned down (token-revoked); the first renews, the second takes its token
    assert toss._token(CFG, rejected="tok-1") == "tok-2"
    assert toss._token(CFG, rejected="tok-1") == "tok-2"
    assert [c["key"] for c in api.calls].count("/oauth2/token") == 2
    assert toss._token(CFG, rejected="tok-2") == "tok-3"  # the cached token itself rejected: renew


def test_short_token_lifetime_still_cached(api):
    api.queue["/oauth2/token"] = [Resp(200, {**SAMPLES["/oauth2/token"], "expires_in": 300})]
    toss.fetch(CFG, TODAY, None)
    assert [c["key"] for c in api.calls].count("/oauth2/token") == 1


def test_429_waits_retry_after_then_retries_once(api):
    limited = {"error": {"requestId": "r", "code": "rate-limit-exceeded", "message": ""}}
    api.queue["/api/v1/exchange-rate"] = [Resp(429, limited, {"Retry-After": "2"})]
    assert toss.fetch(CFG, TODAY, None)["snapshots"]
    assert 2.0 in api.sleeps
    api.queue["/api/v1/exchange-rate"] = [Resp(429, limited), Resp(429, limited)]
    with pytest.raises(toss.TossError, match="HTTP 429 rate-limit-exceeded"):
        toss.fetch(CFG, TODAY, None)


def test_ip_not_allowed_is_loud_and_secret_free(api):
    api.queue["/oauth2/token"] = [Resp(403, {"error": "access_denied", "error_description": "IP address not allowed"})]
    with pytest.raises(toss.TossError, match="허용 IP") as e:
        toss.fetch(CFG, TODAY, None)
    assert e.value.code == "access_denied" and CFG["TOSS_CLIENT_SECRET"] not in str(e.value)


def test_unexpected_shape_raises_with_field_name(api):
    api.queue["/api/v1/exchange-rate"] = [Resp(200, {"result": {"rate": "1380.5"}})]
    with pytest.raises(ValueError, match="midRate"):
        toss.fetch(CFG, TODAY, None)


@pytest.mark.parametrize("accounts, field", [
    ({"result": None}, "NoneType"),
    ({"result": [{"accountNo": "1", "accountType": "BROKERAGE"}]}, "accountSeq"),
    ({"result": [{"accountSeq": 1, "accountType": "BROKERAGE"}]}, "accountNo"),  # no silent switch to toss:1
])
def test_bad_accounts_shape_raises_with_excerpt(api, accounts, field):
    api.queue["/api/v1/accounts"] = [Resp(200, accounts)]
    with pytest.raises(ValueError, match=field) as e:
        toss.fetch(CFG, TODAY, None)
    assert '"result": ' in str(e.value)


def test_probe_is_raw_and_has_no_secrets(api):
    out = toss.probe(CFG)
    assert out["balances"][0]["holdings"] == SAMPLES["/api/v1/holdings"]
    dumped = json.dumps(out)
    assert "tok-1" not in dumped and CFG["TOSS_CLIENT_SECRET"] not in dumped


# the spec's withHoldings example (docs/contracts/toss.json): the same summary as SAMPLES, with its two items
ITEMS = [
    {"symbol": "005930", "name": "삼성전자", "marketCountry": "KR", "currency": "KRW", "quantity": "100",
     "lastPrice": "72000", "averagePurchasePrice": "65000",
     "marketValue": {"purchaseAmount": "6500000", "amount": "7200000", "amountAfterCost": "7050000"},
     "profitLoss": {"amount": "700000", "amountAfterCost": "550000", "rate": "0.1077", "rateAfterCost": "0.0846"},
     "dailyProfitLoss": {"amount": "100000", "rate": "0.0141"}, "cost": {"commission": "14400", "tax": "135600"}},
    {"symbol": "AAPL", "name": "Apple Inc.", "marketCountry": "US", "currency": "USD", "quantity": "10",
     "lastPrice": "178.5", "averagePurchasePrice": "155.3",
     "marketValue": {"purchaseAmount": "1553", "amount": "1785", "amountAfterCost": "1771.43"},
     "profitLoss": {"amount": "232", "amountAfterCost": "218.43", "rate": "0.1494", "rateAfterCost": "0.1406"},
     "dailyProfitLoss": {"amount": "25", "rate": "0.0142"}, "cost": {"commission": "3.57", "tax": "10"}},
]
WITH_ITEMS = {"result": {**SAMPLES["/api/v1/holdings"]["result"], "items": ITEMS}}


def test_holdings_items_at_mid_rate_and_total_matches_fetch(api):
    api.queue["/api/v1/holdings"] = [Resp(200, WITH_ITEMS)]
    (acct,) = toss.holdings(CFG)
    snap = toss.fetch(CFG, TODAY, None)["snapshots"][0]
    assert (acct["account_id"], acct["total_krw"]) == (snap["account_id"], snap["value"])
    sam, aapl = acct["positions"]  # biggest KRW value first
    assert sam == {"symbol": "005930", "name": "삼성전자", "market": "KR", "currency": "KRW", "quantity": 100.0,
                   "avg_price": 65000.0, "price": 72000.0, "value_krw": 7_200_000.0, "cost_krw": 6_500_000.0,
                   "pnl_krw": 700_000.0, "pnl_rate": pytest.approx(0.1077, abs=5e-5), "pnl_ex_fx": False,
                   "cost_local": 6_500_000.0, "pnl_local": 700_000.0, "pnl_local_rate": pytest.approx(0.1077, abs=5e-5)}
    assert (aapl["currency"], aapl["price"], aapl["avg_price"], aapl["quantity"]) == ("USD", 178.5, 155.3, 10.0)
    assert (aapl["value_krw"], aapl["cost_krw"], aapl["pnl_krw"]) == (1785 * 1375, 1553 * 1375, 232 * 1375)
    assert aapl["pnl_rate"] == pytest.approx(0.1494, abs=5e-5)  # native-currency rate, as the item reports it
    assert aapl["pnl_ex_fx"] is True  # no KRW cost per item: the UI labels it
    assert (aapl["cost_local"], aapl["pnl_local"], aapl["pnl_local_rate"]) == (1553.0, 232.0, aapl["pnl_rate"])


def test_holdings_unknown_currency_is_a_shape_error(api):
    odd = {**ITEMS[1], "currency": "JPY"}
    api.queue["/api/v1/holdings"] = [Resp(200, {"result": {**WITH_ITEMS["result"], "items": [odd]}})]
    with pytest.raises(ValueError, match="unexpected response shape"):
        toss.holdings(CFG)


def _order(sym, side, at, qty, amount, cur="USD", status="FILLED", **kw):
    return {"symbol": sym, "side": side, "status": status, "currency": cur, "orderAmount": kw.get("order_amount"),
            "price": kw.get("price"), "quantity": kw.get("quantity"),
            "execution": {"filledQuantity": qty, "filledAmount": amount, "commission": kw.get("fee", "0"), "tax": "0",
                          "filledAt": at, "settlementDate": None}}


OPENED = [_order("DGRO", "BUY", None, "0", "0", status="PENDING", order_amount="10"),
          _order("TSLA", "BUY", None, "0", "0", status="PENDING", price="380", quantity="0.5")]


def test_cash_adds_back_what_open_buys_hold(api):
    bal = {"buying_power_krw": {"result": {"cashBuyingPower": "5"}}, "buying_power_usd": {"result": {"cashBuyingPower": "183.27"}}}
    assert toss._cash({**bal, "open_orders": {"result": {"orders": OPENED}}}) == (5, toss.Decimal("383.27"), True)  # + $10 + 0.5 × $380
    by_qty = _order("QQQ", "BUY", None, "0", "0", status="PENDING", quantity="1")  # market buy by quantity: unknown
    assert toss._cash({**bal, "open_orders": {"result": {"orders": [by_qty]}}}) == (5, toss.Decimal("183.27"), False)


def _closed(*orders):
    return Resp(200, {"result": {"orders": list(orders), "nextCursor": None, "hasNext": False}})


def test_inputs_time_fills_and_rebuild_record_date_shares(api, monkeypatch):
    api.queue["/api/v1/orders"] = [_closed(
        _order("DGRO", "BUY", "2026-09-24T23:14:07+09:00", "0.129871", "9.999911"),
        _order("DGRO", "BUY", "2026-09-25T23:57:12+09:00", "0.132046", "9.999893"),
        _order("DGRO", "BUY", "2026-09-15T10:00:00+09:00", "0.5", "38"),  # 주간거래: 21:00 ET 09-14, trade date 09-15
        _order("XQQI", "SELL", "2026-09-16T00:10:00+09:00", "1", "50.10", fee="0.10"),
        _order("005880", "BUY", "2026-09-17T10:00:00+09:00", "3", "30000", cur="KRW", fee="15"))]
    seen = {}

    def fake(cfg, aid, symbols, start, today, shares_at, rate_at, credit_lag):
        seen.update(symbols=set(symbols), lag=credit_lag, dgro=shares_at("DGRO", "20260915", "20260916"),
                    xqqi=shares_at("XQQI", "20260916", "20260917"), rate=rate_at("20260916"))
        return []
    from assetlog.brokers import kis
    monkeypatch.setattr(kis, "us_dividends", fake)
    items = [{"symbol": "DGRO", "currency": "USD", "quantity": "21.574316"}, {"symbol": "XQQI", "currency": "USD", "quantity": "9"}]
    legs, sett, divs, notes = toss._inputs({**CFG, **KIS_KEYS}, "toss:1", 1, date(2026, 9, 25), None, items,
                                           toss.Decimal("1360"), (toss.Decimal("5"), toss.Decimal("383.27")), "2026-09-25T23:50:04+09:00")
    assert notes == [] and divs == []
    assert legs == [{"account_id": "toss:1", "date": "2026-09-25", "krw": 5.0, "usd": 383.27,
                     "rp_usd": 0.0, "rate": 1360.0, "at": "2026-09-25T23:50:04+09:00"}]
    assert [(s["at"][:16], s["usd"], s["krw"]) for s in sett] == [  # one per fill, with its time
        ("2026-09-15T10:00", -38.0, 0.0), ("2026-09-16T00:10", 50.0, 0.0), ("2026-09-17T10:00", 0.0, -30015.0),
        ("2026-09-24T23:14", -9.999911, 0.0), ("2026-09-25T23:57", -9.999893, 0.0)]
    # US trade dates decide holdings of record
    assert seen["symbols"] == {"DGRO", "XQQI"} and seen["rate"] == 1360.0 and seen["lag"] == 0  # Toss: on the pay date
    assert seen["dgro"] == pytest.approx(21.574316 - 0.5 - 0.129871 - 0.132046)  # the overnight buy settles after 09-15
    assert seen["xqqi"] == 9  # sold 09-15 in New York, before the 09-16 record date: not added back
    assert toss._trade_day(datetime.fromisoformat("2026-09-15T08:30:00+09:00")) == "2026-09-14"  # after-hours 19:30 ET


def test_fills_reach_back_to_every_record_date_priced(api, monkeypatch):
    # a sync after a long gap: dividends from `since` on, holdings rebuilt from fills back to its record dates
    from assetlog.brokers import kis
    seen = {}
    monkeypatch.setattr(kis, "us_dividends", lambda cfg, aid, symbols, start, today, *a, **k: seen.update(start=start) or [])
    toss._inputs({**CFG, **KIS_KEYS}, "toss:1", 1, date(2026, 10, 27), date(2026, 8, 1), [], toss.Decimal("1360"),
                 None, "2026-10-27T23:50:00+09:00")
    assert seen["start"] == date(2026, 8, 1)  # not clamped to the last 55 days: 08-01..09-02 dividends kept
    [closed] = [c["params"] for c in api.calls if c["key"] == "/api/v1/orders"]
    assert closed["from"] == (date(2026, 8, 1) - timedelta(days=kis.RIGHTS_BACK + toss.ORDER_SLACK)).isoformat()


def test_snapshot_counts_reserved_cash_and_legs_are_timed_at_the_buying_power_read(api, monkeypatch):
    from assetlog.brokers import kis
    monkeypatch.setattr(kis, "us_dividends", lambda *a, **k: [])
    t0 = datetime(2026, 9, 25, 23, 50, tzinfo=toss.KST)

    class Clock(datetime):  # one second per API call
        @classmethod
        def now(cls, tz=None):
            return t0 + timedelta(seconds=len(api.calls))
    monkeypatch.setattr(toss, "datetime", Clock)
    api.queue["/api/v1/orders"] = [Resp(200, {"result": {"orders": OPENED}})]  # the open orders, then SAMPLES' history
    out = toss.fetch({**CFG, **KIS_KEYS}, TODAY, None)
    cash = 5_000_000 + (3500.5 + 10 + 190) * 1375  # buying power + what the open buys hold back
    assert out["snapshots"][0]["cash"] == cash and out["snapshots"][0]["value"] == 7_200_000 + 1785 * 1375 + cash
    [obs] = out["cash_obs"]
    assert (obs["krw"], obs["usd"]) == (5_000_000, 3700.5)
    # token, accounts, rate, holdings, 2 × buying power: stamped then, before the order calls
    assert obs["at"] == (t0 + timedelta(seconds=6)).isoformat(timespec="seconds")
    api.queue["/api/v1/orders"] = [Resp(200, {"result": {"orders": OPENED}})]
    assert toss.holdings(CFG)[0]["total_krw"] == out["snapshots"][0]["value"]
