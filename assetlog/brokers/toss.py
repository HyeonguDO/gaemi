"""토스증권 Open API (https://openapi.tossinvest.com): balance snapshots, computed dividends, inputs for infer.py.

The official API has no deposit/withdrawal/dividend/interest or cash-ledger endpoint (all 36 operations of
openapi.json v1.2.17 checked, docs/contracts/toss.json). So:
- Dividends are computed like KIS's (kis.us_dividends): per-share amounts from KIS × shares held on the US record
  date, rebuilt as current holdings minus the fills since (/orders). Against the 2026 거래 내역서: all 7 record-date
  share counts equal to 6 decimals and all 7 net amounts to the cent (15% withheld, as the statement shows).
  A fill counts on its US trade date: the overnight session (주간거래, 20:00-04:00 ET) trades for the next day's.
  Needs the KIS keys for the per-share data; without them there are no Toss dividends and no cash legs either
  (infer.py would read each dividend as a deposit). The same holds for a day the dividends fail.
- Deposits/withdrawals are inferred by infer.py from daily cash legs: cashBuyingPower + what open buy orders hold
  back = cash after every executed trade (live 2026-09-25: the statement's 잔액 less two unsettled $10 fills =
  buying power, to the cent), observed with the time buying power was read, and each fill's cash (filledAmount ±
  commission/tax) with its filledAt, so infer.py splits fills between observations by time whenever the sync runs.
- The 거래 내역서 PDF import stays the booked source: importing one replaces the estimates in its date range.
There is no history either: each sync stores today's value, so sync daily from day one.

Snapshot value = holdings market value (KR + US) + cash, USD converted at 매매기준율 (midRate; `rate` is
Toss's buy rate including spread). Cash is the cash legs' figure (KRW + USD): the API has no 예수금 or total-cash
figure. When a market buy by quantity hides its reservation it is APPROXIMATED by cashBuyingPower alone.
holdings.totalPurchaseAmount (투자원금) is the cost basis of current holdings, not money paid in, so it is never
emitted as a flow.

Setup: issue the client id/secret in WTS 설정 > Open API and register this machine's public IP under
허용 IP 관리; any other IP gets 403 (access_denied on the token call). Only one token per client is valid:
issuing one revokes the previous, so another program using the same client id kicks this one out and
vice versa. The token is cached in data/tokens.json per client id and re-issued once on 401 expired/revoked,
unless another caller already replaced the rejected token (re-issuing then would revoke that caller's).

Unverified against a live account:
- cashBuyingPower vs real cash: whether it includes unsettled sale proceeds, or folds the other currency in
  (통합증거금 / auto-FX), in which case KRW + USD double counts. Compare a snapshot's cash with the app's
  원화/달러 예수금 first.
- Whether holdings summaries include fractional (소수점) positions and KR ETFs/ETNs: compare the
  snapshot value with the app's 총자산.
- The error code of a REST 403 from an unlisted IP (edge-blocked or forbidden?) and of a bad accountSeq
  (account-not-found or stock-not-found?): both are raised verbatim with status, code and body.
- expires_in: whatever the token call returns is used as the cache lifetime (86400 is only the spec's example).

Holdings (holdings(), the 보유 tab): holdings.items, checked live (2026-09-25): quantity/lastPrice/
averagePurchasePrice and marketValue.purchaseAmount/amount are in the item's currency (fractional US shares
included), profitLoss.amount = amount - purchaseAmount. The summary profitLoss.rate is not that: it is taken in
KRW, FX moves included (live: a USD gain came with a negative rate), so per-item figures are converted at midRate and
the account rate is left to the reader. Items carry no KRW cost, so a USD item's pnl_krw leaves FX out (pnl_ex_fx). Total = fetch()'s snapshot value.
"""
import json
import logging
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import requests

from .. import config

REQUIRED = ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET")
BASE = "https://openapi.tossinvest.com"
PACE = 1.0  # s before every call: ACCOUNT group allows 1/s, and holdings has 429'd well below its 5/s
RENEW = ("expired-token", "token-revoked")
ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
ORDER_DAYS = 60          # fills fetched at least: infer.py's windows
DIVIDEND_DAYS = 45       # credited dividends looked for on a first sync
ORDER_SLACK = 5          # days an order (matched on orderedAt) may be placed before the fill that counts

log = logging.getLogger(__name__)


class TossError(RuntimeError):
    def __init__(self, msg, status, code):
        super().__init__(msg)
        self.status, self.code = status, code


def _send(method, path, token=None, seq=None, **kw):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if seq is not None:
        headers["X-Tossinvest-Account"] = str(seq)
    for retry in (False, True):
        time.sleep(PACE)
        r = requests.request(method, BASE + path, headers=headers, timeout=15, **kw)
        if r.status_code != 429 or retry:
            break
        try:
            wait = float(r.headers.get("Retry-After", 1))
        except ValueError:  # HTTP-date form
            wait = 1
        time.sleep(min(wait, 30))
    try:
        body = r.json()
    except ValueError:
        body = None
    if r.ok and isinstance(body, dict):
        return body
    err = body.get("error") if isinstance(body, dict) else None
    code = err.get("code") if isinstance(err, dict) else err  # common envelope vs bare OAuth2 (token 400/401/403)
    detail = r.text[:300] if body is None else json.dumps(body, ensure_ascii=False)[:500]
    hint = " (is this machine's public IP registered in WTS 설정 > Open API > 허용 IP 관리?)" if r.status_code == 403 else ""
    raise TossError(f"toss {method} {path}: HTTP {r.status_code} {code}: {detail}{hint}", r.status_code, code)


def _token(cfg, rejected=None):
    """The cached token, or a new one when the cache still holds `rejected` (the token a 401 turned down)."""
    name = f"toss:{cfg['TOSS_CLIENT_ID']}"

    def issue():  # runs under cached_token's file lock
        if rejected:  # another caller may have renewed since our 401: issuing again would revoke its token
            # ponytail: reads cached_token's file directly; config.cached_token taking `rejected` would hide that
            hit = json.loads((config.DATA / "tokens.json").read_text() or "{}").get(name)
            if hit and hit["token"] != rejected and hit["expires"] > time.time():
                return hit["token"], hit["expires"] - time.time()
        body = _send("POST", "/oauth2/token", data={
            "grant_type": "client_credentials",
            "client_id": cfg["TOSS_CLIENT_ID"], "client_secret": cfg["TOSS_CLIENT_SECRET"],
        })
        return body["access_token"], int(body["expires_in"])
    # margin=0: expiry is caught by the 401 expired-token re-issue, and a fixed margin >= expires_in would disable
    # the cache (each call then revokes the last token). margin=inf makes the cached token count as expired.
    # Keyed per client id so switching clients never replays the old client's token.
    return config.cached_token(name, issue, margin=float("inf") if rejected else 0)


def _get(cfg, path, seq=None, **params):
    token = _token(cfg)
    try:
        return _send("GET", path, token, seq, params=params)
    except TossError as e:
        if e.status != 401 or e.code not in RENEW:
            raise
        log.warning("toss token %s, re-issuing", e.code)
        return _send("GET", path, _token(cfg, rejected=token), seq, params=params)


def _shape(e, raw):
    return ValueError(f"toss: unexpected response shape ({e!r}): {json.dumps(raw, ensure_ascii=False)[:1500]}")


def probe(cfg):
    """Raw bodies of the calls fetch() reads (all but the order history), and when buying power was read.
    None carries a secret; the token never leaves _token()."""
    out = {
        "accounts": _get(cfg, "/api/v1/accounts"),
        "exchange_rate": _get(cfg, "/api/v1/exchange-rate", baseCurrency="USD", quoteCurrency="KRW"),
        "balances": [],
    }
    try:
        listed = out["accounts"]["result"]
        brokerage = [(a, a["accountSeq"]) for a in listed if a["accountType"] == "BROKERAGE"]
        skipped = sorted({a["accountType"] for a in listed} - {"BROKERAGE"})
    except (KeyError, TypeError) as e:
        raise _shape(e, out["accounts"]) from e
    if skipped:  # BROKERAGE is the only type the API returns today
        log.warning("toss: skipping unsupported account types %s", skipped)
    for a, seq in brokerage:
        b = {"account": a, "holdings": _get(cfg, "/api/v1/holdings", seq),
             "buying_power_krw": _get(cfg, "/api/v1/buying-power", seq, currency="KRW"),
             "buying_power_usd": _get(cfg, "/api/v1/buying-power", seq, currency="USD")}
        # the cash legs' time: a fill after it is not in this buying power. Open orders right after, so an order
        # placed or filled between the two reads (about PACE apart) is the only mismatch left
        b["observed_at"] = datetime.now(KST).isoformat(timespec="seconds")
        b["open_orders"] = _get(cfg, "/api/v1/orders", seq, status="OPEN")
        out["balances"].append(b)
    return out


def holdings(cfg):
    """[{account_id, total_krw, positions}] per brokerage account, same ids and total as fetch()."""
    raw = probe(cfg)
    out = []
    try:
        fx = Decimal(raw["exchange_rate"]["result"]["midRate"])
        for b in raw["balances"]:
            h = b["holdings"]["result"]
            mv = h["marketValue"]["amount"]
            krw, usd, _ = _cash(b)
            # ponytail: fetch()'s value formula, repeated so the snapshot code stays untouched; a test pins them equal
            total = Decimal(mv["krw"]) + Decimal(mv["usd"] or 0) * fx + krw + usd * fx
            positions = []
            for it in h["items"]:
                rate = {"KRW": 1, "USD": fx}[it["currency"]]  # the spec's only two currencies; anything else is a shape error
                cost, value = Decimal(it["marketValue"]["purchaseAmount"]), Decimal(it["marketValue"]["amount"])
                local_rate = float((value - cost) / cost) if cost else None
                positions.append({
                    "symbol": it["symbol"], "name": it["name"], "market": it["marketCountry"], "currency": it["currency"],
                    "quantity": float(it["quantity"]), "avg_price": float(it["averagePurchasePrice"]),
                    "price": float(it["lastPrice"]), "value_krw": float(value * rate), "cost_krw": float(cost * rate),
                    "pnl_krw": float((value - cost) * rate), "pnl_rate": local_rate,
                    "pnl_ex_fx": it["currency"] != "KRW",  # no KRW cost per item: a USD item's P&L leaves FX out
                    "cost_local": float(cost), "pnl_local": float(value - cost), "pnl_local_rate": local_rate})
            out.append({"account_id": f"toss:{b['account']['accountNo']}", "total_krw": float(total),
                        "positions": sorted(positions, key=lambda p: -p["value_krw"])})
    except (KeyError, TypeError, InvalidOperation) as e:
        raise _shape(e, raw) from e
    return out


def _orders(cfg, seq, start, end):
    """Every CLOSED order placed (orderedAt, KST) in [start, end]."""
    out, cursor = [], None
    while True:
        params = {"status": "CLOSED", "from": start.isoformat(), "to": end.isoformat(), "limit": 100}
        r = _get(cfg, "/api/v1/orders", seq, **params, **({"cursor": cursor} if cursor else {}))["result"]
        out += r["orders"]
        if not r.get("hasNext"):
            return out
        cursor = r["nextCursor"]


def _fills(orders):
    """[(symbol, currency, signed quantity, signed cash incl. fees, filled at)] for every executed order."""
    out = []
    for o in orders:
        e = o.get("execution") or {}
        q = Decimal(e.get("filledQuantity") or 0)
        if q <= 0:
            continue
        buy = o["side"] == "BUY"
        fees = Decimal(e.get("commission") or 0) + Decimal(e.get("tax") or 0)
        cash = -(Decimal(e["filledAmount"]) + fees) if buy else Decimal(e["filledAmount"]) - fees
        out.append((o["symbol"], o["currency"], q if buy else -q, cash, datetime.fromisoformat(e["filledAt"])))
    return out


def _held_back(open_orders, currency):
    """What open buy orders keep out of cashBuyingPower; None when an order's amount can't be known."""
    total = Decimal(0)
    for o in open_orders:
        if o["side"] != "BUY" or o["currency"] != currency:
            continue
        filled = Decimal((o.get("execution") or {}).get("filledQuantity") or 0)
        if o.get("orderAmount"):  # amount-based US market buy
            total += Decimal(o["orderAmount"]) - Decimal((o.get("execution") or {}).get("filledAmount") or 0)
        elif o.get("price"):
            total += (Decimal(o["quantity"]) - filled) * Decimal(o["price"])
        else:  # a market buy by quantity: its reservation isn't in the response
            return None
    return total


def _cash(b):
    """(KRW, USD, known) for a probe() balance: cashBuyingPower plus what open buy orders hold back, i.e. cash after
    every executed trade. Buying power alone (known False) when a market buy by quantity hides its reservation."""
    bp = [Decimal(b[f"buying_power_{c}"]["result"]["cashBuyingPower"]) for c in ("krw", "usd")]
    held = [_held_back(b["open_orders"]["result"]["orders"], c) for c in ("KRW", "USD")]
    return (*bp, False) if None in held else (bp[0] + held[0], bp[1] + held[1], True)


def _trade_day(at):
    """A fill's US trade date: the overnight session (20:00-04:00 ET, Sun-Thu) trades for the next day, so +4 h moves
    20:00-24:00 there and leaves pre-market to after-hours (04:00-20:00) on their own day."""
    return (at.astimezone(ET) + timedelta(hours=4)).date().isoformat()


def _inputs(cfg, aid, seq, today, since, items, fx, cash, observed_at):
    """Cash legs, trade cash by fill time and computed dividends for one account (see the module docstring).
    cash: (KRW, USD) after every executed trade as of observed_at, or None when unknown."""
    from . import kis  # per-share dividend data lives at KIS
    if not all(cfg.get(k) for k in kis.REQUIRED):
        return [], [], [], ["dividends and deposit/withdrawal estimates need the KIS keys (per-share data)"]
    start = since or today - timedelta(days=DIVIDEND_DAYS)
    # record dates go back to start - RIGHTS_BACK: rebuilding holdings there needs every fill since
    orders = _orders(cfg, seq, min(today - timedelta(days=ORDER_DAYS),
                                   start - timedelta(days=kis.RIGHTS_BACK + ORDER_SLACK)), today)
    fills = _fills(orders)
    legs, notes = [], []
    if cash is None:
        notes.append("open market order by quantity: today's cash legs skipped")
    else:
        legs.append({"account_id": aid, "date": today.isoformat(), "krw": float(cash[0]), "usd": float(cash[1]),
                     "rp_usd": 0.0, "rate": float(fx), "at": observed_at})
    moved = [{"account_id": aid, "date": at.astimezone(KST).date().isoformat(), "at": at.isoformat(),
              "usd": float(c) if cur == "USD" else 0.0, "krw": 0.0 if cur == "USD" else float(c)}
             for sym, cur, q, c, at in sorted(fills, key=lambda f: f[4])]
    qty = {i["symbol"]: Decimal(i["quantity"]) for i in items if i.get("currency") == "USD"}
    us = {s for s, cur, *_ in fills if cur == "USD"} | set(qty)

    def shares_at(pdno, record, bass):  # holders of record: trades before the record (= ex) date under T+1
        rec = f"{record[:4]}-{record[4:6]}-{record[6:]}"
        return float(qty.get(pdno, 0) - sum(q for s, cur, q, _, at in fills if s == pdno and _trade_day(at) >= rec))

    dividends = kis.us_dividends(cfg, aid, dict.fromkeys(us), start, today, shares_at, lambda bass: float(fx), credit_lag=0)
    return legs, moved, dividends, notes


def fetch(cfg, today, since):
    """Today's snapshot per account, plus computed dividends and infer.py's inputs (optional: failures are errors)."""
    raw = probe(cfg)
    accounts, snapshots, txs, cash_obs, settlements, errors = [], [], [], [], [], []
    try:
        fx = Decimal(raw["exchange_rate"]["result"]["midRate"])
        for b in raw["balances"]:
            mv = b["holdings"]["result"]["marketValue"]["amount"]
            krw, usd, known = _cash(b)
            cash = krw + usd * fx
            value = Decimal(mv["krw"]) + Decimal(mv["usd"] or 0) * fx + cash  # usd is null without US holdings
            aid = f"toss:{b['account']['accountNo']}"  # no accountSeq fallback: a changed id would double count
            accounts.append({"id": aid, "institution": "toss", "name": "토스증권", "kind": "brokerage"})
            snapshots.append({"account_id": aid, "date": today, "value": float(value), "cash": float(cash),
                              "rate": float(fx)})  # the midRate it was priced at
            try:
                legs, sett, divs, notes = _inputs(cfg, aid, b["account"]["accountSeq"], today, since,
                                                  b["holdings"]["result"]["items"], fx, (krw, usd) if known else None,
                                                  b["observed_at"])
                cash_obs += legs
                settlements += sett
                txs += divs
                errors += [f"{aid}: {n}" for n in notes]
            except Exception as e:  # the day's valuation must not depend on them; no legs without the dividends
                errors.append(f"{aid}: dividends/cash legs: {type(e).__name__}: {e}")
    except (KeyError, TypeError, InvalidOperation) as e:
        raise _shape(e, raw) from e
    return {"accounts": accounts, "snapshots": snapshots, "transactions": txs, "errors": errors,
            "fx": [{"date": today, "currency": "USD", "krw": float(fx)}],
            "cash_obs": cash_obs, "settlements": settlements,
            "settlements_from": (today - timedelta(days=ORDER_DAYS)).isoformat()}
