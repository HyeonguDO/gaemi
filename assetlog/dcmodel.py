"""A DC pension that buys one KRX ETF with each month's employer contribution, tracked without any API.

The model, checked on the user's 하나은행 DC in 2026:
- the 부담금 arrives on the 25th, or the last business day before it when that's a weekend or holiday (16 of 16
  monthly contributions since 2025-06: Friday for a weekend 25th, 12-24 for Christmas, 05-22 before a substitute
  holiday, 09-23 before 추석). The current month's day comes from the KIS holiday calendar (영업일), so a contribution
  that lands early is counted from that day;
- a few trading days later the whole cash balance buys as many whole shares as it can at about that day's close
  (all 9 share counts equal floor(cash / close));
- the ETF's distributions (KSD per-share data, HHKDB669102C0) are paid in cash, untaxed inside a pension.
Each sync replays these events from a known state (DC_START) to today, pricing shares at KIS daily closes, and books
the value of every trading day and of today (snapshots, source dc_model), the scheduled contributions and
distributions, and the buys (INTERNAL, for the record). Every sync rebuilds the model's rows after DC_START, so a
day it no longer books (a weekend or 12-31 booked as "today") falls back to the last trading day's fresh value.
What the user enters wins:
- a value entered for the account (입력 > 평가액 직접 입력) re-anchors the replay. With the price known it fixes the
  share count, choosing between "contribution already invested" and "still in cash" by the modelled count. One
  that implies a share count more than 5% off the model's is taken for a typo: not used, and named in the report;
- a deposit the user (or a recording) books replaces that month's scheduled one, and its amount becomes the monthly
  contribution from then on (a raise needs one such row);
- a dividend the user (or a recording) books within MATCH_DAYS of a distribution's pay date replaces the model's
  row for it; the modelled cash still receives it.
Config (.env): DC_ACCOUNT=<ledger id>, DC_SYMBOL=<6-digit KRX code>, DC_START=YYYY-MM-DD,<shares>,<cash>.
Cash earns a little interest (a few hundred won a month) that the model leaves out; a re-anchor absorbs it.
"""
import json
from datetime import date, timedelta

from . import db
from .brokers import kis

SOURCE = "dc_model"
DAY = 25      # contribution day of the month
ANCHOR_JUMP = 0.05  # an entered value implying a share count this far off the model's is taken for a typo
MATCH_DAYS = 7  # a booked dividend this close to a pay date is that distribution (the bank may credit a few days late)
BUY_LAG = 5   # KRX trading days from the contribution to the buy: 2026 had 3,3,3,4,5,6,8,10. Replaying 2026 with no
              # re-anchor: 2 shares more than the real count on 09-25, month-end values 0.16% off on average (max 0.19%)


def closes(cfg, code, start, end):
    """{YYYY-MM-DD: close} for KRX trading days in [start, end]; FHKST03010100 returns at most 100 days a call."""
    out, lo = {}, start
    while lo <= end:
        hi = min(end, lo + timedelta(days=120))
        for page in kis._get(cfg, "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", "FHKST03010100", {
                "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": f"{lo:%Y%m%d}",
                "FID_INPUT_DATE_2": f"{hi:%Y%m%d}", "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "1"}):
            for x in page.get("output2") or []:
                if x.get("stck_bsop_date") and float(x.get("stck_clpr") or 0) > 0:
                    d = x["stck_bsop_date"]
                    out[f"{d[:4]}-{d[4:6]}-{d[6:]}"] = float(x["stck_clpr"])
        lo = hi + timedelta(days=1)
    return out


def distributions(cfg, code, start, end):
    """[(record date, KRW per share, pay date)] from the KSD schedule."""
    rows = kis._rows(cfg, ("/uapi/domestic-stock/v1/ksdinfo/dividend", "HHKDB669102C0"), {
        "CTS": "", "GB1": "0", "F_DT": f"{start:%Y%m%d}", "T_DT": f"{end:%Y%m%d}", "SHT_CD": code, "HIGH_GB": ""}, "output1")
    iso = lambda s: s.replace("/", "-") if "-" in s or "/" in s else f"{s[:4]}-{s[4:6]}-{s[6:]}"  # noqa: E731
    return [(iso(x["record_date"]), float(x["per_sto_divi_amt"]), iso(x["divi_pay_dt"]))
            for x in rows if float(x.get("per_sto_divi_amt") or 0) > 0 and x.get("divi_pay_dt")]


def contribution_days(prices, today, calendar=None, day=None):
    """`day` (DC_DAY, default the 25th) or the last business day before it, per month. Past months come from KRX trading days (`prices`).
    The current month is settled by `calendar`, the business days from today to its 25th; without one, only once the
    25th has passed."""
    day, days = day or DAY, set(prices) | set(calendar or ())
    out = set()
    for month in sorted({d[:7] for d in days}):
        before = [d for d in days if d[:7] == month and int(d[8:]) <= day]
        if before and (today >= f"{month}-{day:02d}" or (calendar is not None and month == today[:7])):
            out.add(max(before))
    return out


def replay(start, shares, cash, prices, today, monthly, last_deposit, deposits=(), payouts=(), values=(),
           deposit_days=None, paid=(), buy_lag=None):
    """Replay from the end of `start` to `today` (ISO dates). prices: {trading day: close}; monthly: contribution;
    last_deposit: the latest contribution on or before `start` (a buy is due after it while cash covers a share);
    deposits: [(date, amount)] booked by the user after `start`; payouts: [(record, per share, pay)];
    values: {date: value} entered by the user; deposit_days: scheduled contribution dates (default: from `prices`);
    paid: dates of dividends booked by the user. Returns (events, {day: (shares, cash, value)})."""
    buy_lag = buy_lag or BUY_LAG  # read now, not at def time: DC_BUY_LAG, or the module default
    if deposit_days is None:
        deposit_days = contribution_days(prices, today)
    days = sorted(d for d in prices if d <= today)
    trading = days  # the buy countdown may start before `start` (a contribution not invested yet)
    user_months = {d[:7] for d, _ in deposits}
    held, events, states = {start: shares}, [], {}
    pending = last_deposit
    d = date.fromisoformat(start)
    end = date.fromisoformat(today)
    last_price = next((prices[x] for x in reversed(days) if x <= start), None)
    while d < end:
        d += timedelta(days=1)
        k = d.isoformat()
        for when, amount in deposits:
            if when == k:
                cash += amount
                monthly, pending = amount, k
        if k in deposit_days and k[:7] not in user_months:
            cash += monthly
            pending = k
            events.append(("DEPOSIT", k, monthly, {}))
        for record, per_share, pay in payouts:
            on_record = held.get(record, held[start] if record <= start else None)
            if pay == k and on_record:
                amount = round(on_record * per_share)
                cash += amount
                if not any(abs((date.fromisoformat(p) - d).days) <= MATCH_DAYS for p in paid):
                    events.append(("DIVIDEND", k, amount, {"record": record, "shares": on_record, "per_share": per_share}))
        if k in prices:
            last_price = prices[k]
            due = [x for x in trading if pending and x > pending]
            if pending and len(due) >= buy_lag and due[buy_lag - 1] <= k and cash >= last_price:
                qty = int(cash // last_price)
                shares += qty
                cash -= qty * last_price
                pending = None
                events.append(("INTERNAL", k, -qty * last_price, {"qty": qty, "price": last_price}))
        if k in values and last_price:
            v = values[k]
            candidates = {int(v // last_price), int((v - monthly) // last_price)}  # invested, or still in cash
            n = min(candidates, key=lambda c: (abs(c - shares), -c))
            if abs(n - shares) > max(3, ANCHOR_JUMP * shares):  # a missing or extra digit: keep the model, say so
                events.append(("IGNORED", k, v, {"model_shares": shares, "implied_shares": n}))
            else:
                shares, cash = n, v - n * last_price
                pending = (pending or k) if cash >= last_price else None
                events.append(("ANCHOR", k, v, {"shares": shares, "cash": cash}))
        held[k] = shares
        if last_price:
            states[k] = (shares, cash, shares * last_price + cash)
    return events, states


def sync(conn, cfg, today):
    account, code = cfg["DC_ACCOUNT"], cfg.get("DC_SYMBOL") or "438100"
    day, lag = int(cfg.get("DC_DAY") or DAY), int(cfg.get("DC_BUY_LAG") or BUY_LAG)  # the plan's own rules
    try:
        start, shares, cash = cfg["DC_START"].split(",")
        shares, cash = int(shares), float(cash)
    except (KeyError, ValueError):
        return "error: DC_START must be YYYY-MM-DD,shares,cash"
    if not all(cfg.get(k) for k in kis.REQUIRED):
        return "skipped (prices come from the KIS API: set the KIS keys)"
    t0 = date.fromisoformat(start)
    prices = closes(cfg, code, t0 - timedelta(days=40), today)
    payouts = distributions(cfg, code, t0 - timedelta(days=40), today + timedelta(days=40))
    booked = [(r[0], r[1]) for r in conn.execute(
        "SELECT date, amount FROM transactions WHERE account_id = ? AND kind = 'DEPOSIT' AND source != ? ORDER BY date",
        (account, SOURCE))]
    paid = [r[0] for r in conn.execute(
        "SELECT date FROM transactions WHERE account_id = ? AND kind = 'DIVIDEND' AND amount > 0 AND source != ?",
        (account, SOURCE))]
    before = [(d, a) for d, a in booked if d <= start]
    if not before:
        return "error: no contribution booked on or before DC_START to take the monthly amount from"
    values = {r[0]: r[1] for r in conn.execute(
        "SELECT date, value FROM snapshots WHERE account_id = ? AND source = 'manual' AND date > ?", (account, start))}
    upcoming = None
    if today.day <= day:  # this month's contribution day may still be ahead, or be today: ask the calendar
        upcoming = [d.isoformat() for d in kis._business_days(cfg, today, today.replace(day=day), flag="bzdy_yn")]
    days = contribution_days(prices, today.isoformat(), upcoming, day)
    events, states = replay(start, shares, cash, prices, today.isoformat(), before[-1][1], before[-1][0],
                            [(d, a) for d, a in booked if d > start], payouts, values, days, paid, lag)
    with conn:
        conn.execute("DELETE FROM transactions WHERE account_id = ? AND source = ? AND kind_locked = 0 AND date > ?",
                     (account, SOURCE, start))
        for kind, d, amount, raw in events:
            if kind in ("ANCHOR", "IGNORED"):
                continue
            memo = {"DEPOSIT": "부담금 입금 (예정액으로 추정)", "DIVIDEND": "분배금 (추정)",
                    "INTERNAL": f"매수 {raw.get('qty')}좌 (추정)"}[kind]
            key = {"DEPOSIT": f"dep:{d[:7]}", "DIVIDEND": f"div:{raw.get('record')}", "INTERNAL": f"buy:{d}"}[kind]
            db.upsert_transaction(conn, account, d, amount, memo, SOURCE, key, kind, json.dumps(raw, ensure_ascii=False))
        conn.execute("DELETE FROM snapshots WHERE account_id = ? AND source = ? AND date > ?", (account, SOURCE, start))
        for d, (n, c, v) in states.items():
            if d not in values and (d in prices or d == today.isoformat()):
                db.upsert_snapshot(conn, account, d, round(v), round(c), SOURCE)
    n, c, v = states[max(states)] if states else (shares, cash, None)
    out = f"ok: {n} shares + {round(c):,}원 cash = {round(v or 0):,}원 on {max(states) if states else start}"
    for kind, d, amount, raw in events:
        if kind == "IGNORED":
            out += (f"; the value entered for {d} ({amount:,.0f}원) implies {raw['implied_shares']} shares against the"
                    f" model's {raw['model_shares']}: not used (typo?). Correct it, or set DC_START if it is right")
    return out


def yield_month(conn, account_id, d, kind="DEPOSIT"):
    """A deposit or dividend booked by hand or from a file replaces the model's own at once, so the two never count
    together until the next sync: a deposit takes its month's contribution, a dividend the distribution within
    MATCH_DAYS of it. A booked record wins even over a model row the user edited, unless the edit made it something
    else than that kind (the kind filter leaves such a row alone)."""
    if kind == "DEPOSIT":
        conn.execute("DELETE FROM transactions WHERE account_id = ? AND source = ? AND kind = 'DEPOSIT' "
                     "AND substr(date, 1, 7) = ?", (account_id, SOURCE, str(d)[:7]))
    elif kind == "DIVIDEND":
        day = date.fromisoformat(str(d)[:10])
        conn.execute("DELETE FROM transactions WHERE account_id = ? AND source = ? AND kind = 'DIVIDEND' "
                     "AND date BETWEEN ? AND ?", (account_id, SOURCE, (day - timedelta(days=MATCH_DAYS)).isoformat(),
                                                  (day + timedelta(days=MATCH_DAYS)).isoformat()))
