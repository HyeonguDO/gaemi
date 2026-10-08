"""보유 배당주 기준 배당수익률: what the dividend payers held now paid per share over the last 12 months, over
the value of those payers only, so a position paying nothing does not dilute it. metrics.py's dividend_return is
another figure: the dividends a period booked over the whole portfolio's average capital (their share of its return).

- Per share: cash over a year of record dates, one per date: those within 360 days of the latest one since a year
  and a week ago (record dates drift by days: 52 weekly, 12 monthly, 4 quarterly, 1 annual). USD from KIS CTRGT011R
  (kis.rights_of, two half-year asks: it has no next page), less the 15% US withholding as the ledger books it; KRW
  from the KSD schedule (dcmodel.distributions), less KR_WITHHOLDING except in the DC pension, which is paid untaxed.
  Another currency is reported missing.
- annual_div_krw = per share x quantity x the position's KRW rate (quantity x rate = value_krw / price);
  yield = annual_div_krw / value_krw. Payers: annual_div_krw > 0. The block's yield = their annual sum over
  their value sum.
- Positions: every live one (web.py's holdings read), plus the DC ETF, which no API reports: the model's shares on
  its latest day (value - cash over that day's close, both booked by dcmodel.sync) at the latest KIS close.
- Cost: each symbol's per-share figure is kept for the day in data/div_ttm.json, so only the first holdings read of a
  day asks KIS for it (1 call a KRX symbol, 2 a US one, up to 6 for a Toss one, whose exchange is unknown). Later
  reads make only the DC's price call. A failed lookup is not kept, so the next read tries it again.
- A failure is named in "missing" ({"symbol", "account", "error"}; symbol null and account the broker for a broker
  whose holdings failed; a payer held at a price or rate of 0 is one too). What it covers keeps its payers from the
  last block, so a hiccup does not change which payers the yield is over. A position that pays nothing needs no price.
  The block goes to data/dividend_payers.json, which /api/summary serves as dividend_payers (null before the first
  holdings read with the KIS keys set).
"""
from contextlib import closing
from datetime import date, timedelta

from . import cloud, db, dcmodel, metrics
from .brokers import kis

CACHE = "div_ttm.json"          # {"date": today, "per_share": {symbol: gross cash per share, own currency}}
STATE = "dividend_payers.json"  # the last block, for /api/summary
KR_WITHHOLDING = 0.154          # 배당소득세 14% + 지방소득세 1.4%, in a taxable account
TAX = {"USD": kis.US_WITHHOLDING, "KRW": KR_WITHHOLDING}


def _per_share(cfg, p, today):
    """Gross cash per share over the last year of record dates, in the position's currency."""
    start, mid = metrics.year_ago(today) - timedelta(days=7), today - timedelta(days=183)
    if p["currency"] == "USD":  # CTRGT011R has no next page: two half-year asks, so a weekly payer's 52 rows fit
        paid = {kis._ymd(x["acpl_bass_dt"]): float(x["alct_frcr_unpr"]) for a, b in ((start, mid), (mid, today))
                for x in kis.rights_of(cfg, p["symbol"], p["market"], a, b)}
    elif p["currency"] == "KRW":
        paid = {date.fromisoformat(r): amount for r, amount, _ in dcmodel.distributions(cfg, p["symbol"], start, today)}
    else:
        raise ValueError(f"no per-share source for {p['currency']}")
    last = max(paid, default=None)
    return sum(amount for d, amount in paid.items() if (last - d).days <= 360)


def _dc(cfg, today):
    """The DC ETF as a position: the model's shares on its latest day, at the latest close."""
    code = cfg.get("DC_SYMBOL") or "438100"
    with closing(db.connect()) as conn:
        s = conn.execute("SELECT s.date, s.value, s.cash, a.name FROM snapshots s JOIN accounts a "
                         "ON a.id = s.account_id WHERE s.account_id = ? AND s.source = ? ORDER BY s.date DESC LIMIT 1",
                         (cfg["DC_ACCOUNT"], dcmodel.SOURCE)).fetchone()
    if not s:
        raise RuntimeError("no DC model value yet: run a sync")
    closes = dcmodel.closes(cfg, code, date.fromisoformat(s["date"]) - timedelta(days=20), today)
    shares = round((s["value"] - s["cash"]) / closes[max(d for d in closes if d <= s["date"])])
    price = closes[max(closes)]
    return {"symbol": code, "name": s["name"], "currency": "KRW", "quantity": shares, "price": price,
            "value_krw": shares * price}


def annotate(cfg, live, errors, today, as_of):
    """Add annual_div_krw and div_yield (None where the lookup failed) to every position in `live`
    ({account_id: holdings}), and save the payers block. errors: {broker: why its holdings failed, or None}."""
    if not all(cfg.get(k) for k in kis.REQUIRED):
        return  # every per-share figure comes from KIS
    cache = cloud._read(CACHE, {})
    known = cache.get("per_share", {}) if cache.get("date") == today.isoformat() else {}
    held = [(aid, p, TAX.get(p["currency"], 0.0)) for aid, h in live.items() for p in h["positions"]]
    missing = [{"symbol": None, "account": name, "error": e} for name, e in errors.items() if e]
    if cfg.get("DC_ACCOUNT"):
        try:
            held.append((cfg["DC_ACCOUNT"], _dc(cfg, today), 0.0))  # a pension: untaxed
        except Exception as e:
            missing.append({"symbol": cfg.get("DC_SYMBOL") or "438100", "account": cfg["DC_ACCOUNT"],
                            "error": f"{type(e).__name__}: {e}"[:300]})
    payers = []
    for account, p, tax in held:
        p["annual_div_krw"] = p["div_yield"] = None
        try:
            if p["symbol"] not in known:
                known[p["symbol"]] = _per_share(cfg, p, today)
            if known[p["symbol"]]:  # a price or rate of 0 raises here: a payer that can't be weighed
                annual = known[p["symbol"]] * (1 - tax) * p["value_krw"] / p["price"]
                p["annual_div_krw"], p["div_yield"] = annual, annual / p["value_krw"]
            else:
                p["annual_div_krw"] = p["div_yield"] = annual = 0.0
        except Exception as e:  # one symbol failing must not hide the others
            missing.append({"symbol": p["symbol"], "account": account, "error": f"{type(e).__name__}: {e}"[:300]})
            continue
        if annual > 0:
            payers.append({"symbol": p["symbol"], "name": p["name"], "account": account, "value_krw": p["value_krw"],
                           "annual_div_krw": annual, "yield": p["div_yield"]})
    prev = (cloud._read(STATE, None) or {}).get("positions", [])  # what failed keeps its payers from the last block
    payers += [x for x in prev if any(x["account"].split(":")[0] == m["account"] if m["symbol"] is None
                                      else (x["symbol"], x["account"]) == (m["symbol"], m["account"]) for m in missing)]
    cloud._write(CACHE, {"date": today.isoformat(), "per_share": known})
    value, annual = sum(x["value_krw"] for x in payers), sum(x["annual_div_krw"] for x in payers)
    cloud._write(STATE, {"as_of": as_of, "yield": annual / value if value else None, "annual_krw": annual,
                         "value_krw": value, "positions": sorted(payers, key=lambda x: -x["annual_div_krw"]),
                         "missing": missing})
