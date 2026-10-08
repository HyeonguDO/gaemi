"""Deposits and withdrawals of the KIS account, inferred from its end-of-day cash, since no KIS API lists them.

Each sync stores the day's cash legs (db cash_obs: KRW and USD 예수금, the USD auto-RP, the rate). A window
between two stored days gives two estimates of the money that crossed the account boundary:
    krw_only = ΔKRW - KRW-settled trade cash (미니스탁 with its fee)
    combined = krw_only + rate × (Δ(USD + RP) - USD trade cash - USD dividends net of withholding)
krw_only is exact unless KRW and USD were exchanged in the window; combined is exchange-neutral but carries the
FX spread and one-day timing noise of the USD side. That noise cancels in the nearest non-quiet window, so a
window counts as "exchanged" when its USD residual is at least $60 (Toss: $1) or $20 with KRW moving the other way,
and neither such neighbour cancels it. Settlements that carry a fill time ("at", Toss) are split between observations
by their stored times, so a sync at any hour works; the others (KIS) by KR date.
Plain windows book krw_only; exchanged ones book combined (rounded to 10,000원 when within 3,000원), memo says so.
Both count as DEPOSIT/WITHDRAW at once: most big deposits arrive on days with an auto-exchange for a US buy, and a
review queue would leave them out of the returns until someone looked. The 검토 tab corrects (and locks) a row.

Booked records win: a window is not estimated, and its unedited estimate is removed, when a deposit or withdrawal
booked from another source (file, recording, manual) falls on one of its days, or a complete statement covers all of
them (db coverage). A window's days are the calendar days it holds at least an hour of, by the stored observation
times: after a 09:00 sync the rest of that day's money lands in the next window, after a 23:50 one it doesn't.

Backtest numbers (2026-01..09 against 47 flows from screen recordings) are in the README. Known blind spots:
domestic (KRX) trades and dividends are not subtracted (the account holds none); a KRW RP or 발행어음 would sit in
the RP row and read as USD; a flow is dated at the first sync that sees it.
"""
import json
import math
import re
from datetime import date, datetime, timedelta, timezone

from . import db

SOURCE = "kis_infer"  # default; the Toss account books "toss_infer"
MIN_FLOW = 5000     # KRW; quiet windows stay under ~5,000 (RP interest tax, rounding, small FX leftovers)
EXCHANGED = {"kis_infer": 60, "toss_infer": 1}  # USD residual that marks a KRW<->USD exchange. KIS: the overnight RP
                    # and settlement timing leave tens of dollars of noise. Toss has neither, and exchanges ~$10 a
                    # day for its auto-buys, so any residual is an exchange.
SMALL_EXCHANGE = 20 # ...or this much, when KRW moved the other way
CANCEL = 0.3        # ...unless the nearest non-quiet neighbour takes back all but this share of it
SNAP = 3000         # KRW distance to a multiple of 10,000 across which an exchanged estimate is rounded
RECHECK = 4         # windows re-estimated on each sync: the newest one changes once its successor exists
CONTEXT = 3         # earlier windows computed alongside, as neighbours for the cancel rule
SETTLE_LAG = 5      # days: a trade before the settlement fetch can still move cash this long after it
HOUR = timedelta(hours=1)  # a window holding less of a day than this (the minutes after a 23:50 sync) doesn't hold it
KST = timezone(timedelta(hours=9))
USD_TEXT = re.compile(r"\(\$\s*(-?[\d,]+(?:\.\d+)?)\)")  # Toss statement rows imported before 2026-09-26


def _at(x):  # an observation or fill time; observations stored without one count as the end of their day
    return datetime.fromisoformat(x.get("at") or f"{x['date']}T23:59:59+09:00")


def _days(p, c):
    """First and last calendar day that window (p, c] holds at least an HOUR of (ISO dates)."""
    return tuple(t.astimezone(KST).date().isoformat() for t in (_at(p) + HOUR, _at(c) - HOUR))


def _usd(raw, amount):
    """Net USD of a dividend row. Toss statement rows imported before 2026-09-26 hold the '($ …)' text instead: its
    second value is the 정산금액, signed like the row."""
    u = json.loads(raw).get("usd") or 0
    if isinstance(u, str) and "($" in u:
        return math.copysign(float(USD_TEXT.findall(u)[1].replace(",", "")), amount)
    return float(u)


def estimates(conn, account_id):
    """[(row, first day, last day)] for the account's *_infer rows, with the days of the window each one stands for."""
    obs = [dict(r) for r in conn.execute("SELECT date, at FROM cash_obs WHERE account_id = ? ORDER BY date",
                                         (account_id,))]
    span = {c["date"]: _days(p, c) for p, c in zip(obs, obs[1:])}
    return [(r, *span.get(r["date"], (r["date"], r["date"]))) for r in conn.execute(
        "SELECT id, date, kind, amount, memo, source, kind_locked FROM transactions WHERE account_id = ? "
        "AND source LIKE '%\\_infer' ESCAPE '\\'", (account_id,))]


def yield_to_booked(conn, account_id, day):
    """A deposit or withdrawal booked by hand for `day` replaces the estimates whose window holds that day at once,
    not at the next sync. Estimates the user edited stay. Returns how many were removed."""
    gone = [(r["id"],) for r, lo, hi in estimates(conn, account_id) if not r["kind_locked"] and lo <= str(day) <= hi]
    conn.executemany("DELETE FROM transactions WHERE id = ?", gone)
    return len(gone)


def _booked(conn, account_id):
    """Predicate on a window's (first, last) day: booked records stand for it (see the module docstring)."""
    flows = {r[0] for r in conn.execute(
        "SELECT date FROM transactions WHERE account_id = ? AND kind IN ('DEPOSIT', 'WITHDRAW') "
        "AND source NOT LIKE '%\\_infer' ESCAPE '\\'", (account_id,))}
    ranges = db.coverage(conn, account_id)
    return lambda lo, hi: any(lo <= d <= hi for d in flows) or any(f <= lo and hi <= t for f, t in ranges)


def windows(obs, settlements, dividends, exchanged_usd=EXCHANGED[SOURCE]):
    """obs: [{date, krw, usd, rp_usd, rate, at?}] by date; settlements: [{date, usd, krw, at?}];
    dividends: [(date, net usd)]. Returns [{date, amount, kind, exchanged, krw_only, combined, usd_residual}]
    for every window with a flow."""
    rows = []
    for p, c in zip(obs, obs[1:]):
        day = lambda d: p["date"] < d <= c["date"]  # noqa: E731
        inside = lambda s: _at(p) < _at(s) <= _at(c) if s.get("at") else day(s["date"])  # noqa: E731
        moved = [s for s in settlements if inside(s)]
        krw_only = c["krw"] - p["krw"] - sum(s["krw"] for s in moved)
        usd = (c["usd"] + c["rp_usd"] - p["usd"] - p["rp_usd"] - sum(s["usd"] for s in moved)
               - sum(a for d, a in dividends if day(d)))
        rows.append({"date": c["date"], "krw_only": round(krw_only), "combined": round(krw_only + c["rate"] * usd),
                     "usd_residual": round(usd, 2)})
    busy = [i for i, r in enumerate(rows) if abs(r["usd_residual"]) >= 1 or abs(r["krw_only"]) >= MIN_FLOW]
    out = []
    for i, r in enumerate(rows):
        u = r["usd_residual"]
        k = busy.index(i) if i in busy else None
        near = [] if k is None else [busy[j] for j in (k - 1, k + 1) if 0 <= j < len(busy)]  # quiet weekends skipped
        # an exchange moves KRW and USD by the same value in opposite directions; a big USD residual alone also counts
        big = abs(u) >= exchanged_usd or (abs(u) >= SMALL_EXCHANGE and u * r["krw_only"] < 0)
        exchanged = big and all(abs(u + rows[j]["usd_residual"]) >= CANCEL * abs(u) for j in near)
        amount = r["combined"] if exchanged else r["krw_only"]
        if exchanged and abs(amount - round(amount, -4)) <= SNAP:
            amount = round(amount, -4)
        if abs(amount) >= MIN_FLOW:
            out.append({**r, "amount": amount, "exchanged": exchanged, "kind": "DEPOSIT" if amount > 0 else "WITHDRAW"})
    return out


def apply(conn, account_id, settlements, settlements_from, source=SOURCE):
    """Re-estimate the newest windows of `account_id` and book them as source rows.
    Returns (rows inserted, changed or removed; notes for the sync report). A window booked records stand for is left
    to them, and its estimate is removed unless the user edited it. Windows opening before the trade data is complete
    (`settlements_from` + SETTLE_LAG) are not touched. `settlements` may hold other accounts' (sync passes all)."""
    settlements = [s for s in settlements if s.get("account_id", account_id) == account_id]
    obs = [dict(r) for r in conn.execute(
        "SELECT date, krw, usd, rp_usd, rate, at FROM cash_obs WHERE account_id = ? ORDER BY date", (account_id,))]
    booked = _booked(conn, account_id)
    changed, notes = 0, []
    for r, first, last in estimates(conn, account_id):
        if not r["kind_locked"] and booked(first, last):
            conn.execute("DELETE FROM transactions WHERE id = ?", (r["id"],))
            changed += 1
    safe = (date.fromisoformat(settlements_from) + timedelta(days=SETTLE_LAG)).isoformat() if settlements_from else ""
    ends = [i for i in range(1, len(obs))][-RECHECK:]
    stale = [i for i in ends if obs[i - 1]["date"] < safe]
    if stale and stale[-1] == len(obs) - 1:
        notes.append(f"infer: no deposit/withdrawal estimate for {obs[-2]['date']}..{obs[-1]['date']} "
                     "(the sync gap is longer than the trade history fetched)")
    ends = [i for i in ends if i not in stale and not booked(*_days(obs[i - 1], obs[i]))]
    if not ends:
        return changed, notes
    lo = ends[0] - 1
    while lo > 0 and ends[0] - 1 - lo < CONTEXT and obs[lo - 1]["date"] >= safe:
        lo -= 1
    span = obs[lo:]
    dividends = []
    for d, raw, amount in conn.execute("SELECT date, raw, amount FROM transactions WHERE account_id = ? "
                                       "AND kind = 'DIVIDEND' AND date > ? AND raw IS NOT NULL",
                                       (account_id, span[0]["date"])):
        try:
            dividends.append((d, _usd(raw, amount)))
        except (ValueError, TypeError, AttributeError, IndexError):
            pass  # not a USD dividend row
    found = {w["date"]: w for w in windows(span, settlements, dividends, EXCHANGED.get(source, EXCHANGED[SOURCE]))}
    for i in ends:
        d, key = obs[i]["date"], f"infer:{obs[i]['date']}"
        old = conn.execute("SELECT kind, amount, kind_locked FROM transactions WHERE account_id = ? AND source = ? "
                           "AND source_key = ?", (account_id, source, key)).fetchone()
        if old and old[2]:
            continue  # the user's word stands
        w = found.get(d)
        if not w:
            if old:
                conn.execute("DELETE FROM transactions WHERE account_id = ? AND source = ? AND source_key = ?",
                             (account_id, source, key))
                changed += 1
            continue
        memo = ("입금" if w["kind"] == "DEPOSIT" else "출금") + (" (잔고로 추정 · 환전 섞임)" if w["exchanged"] else " (잔고로 추정)")
        detail = json.dumps({k: w[k] for k in ("krw_only", "combined", "usd_residual")}, ensure_ascii=False)
        db.upsert_transaction(conn, account_id, d, w["amount"], memo, source, key, w["kind"], detail)
        changed += not old or (old[0], old[1]) != (w["kind"], float(w["amount"]))
    return changed, notes
