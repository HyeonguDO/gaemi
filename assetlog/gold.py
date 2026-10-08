"""Gold held at a broker whose API has no gold (KB증권 금현물): value = grams held x the KRX gold close.

The price is KRX 금 99.99_1kg (code CODE, KRW per gram), which KIS quotes like a stock (dcmodel.closes, FHKST03010100):
one call a day, cached in data/gold_closes.json. Buys are DEPOSIT rows of the account (amount = KRW paid) whose raw
JSON holds {"grams": g}; a sale is a WITHDRAW at cost with negative grams (its proceeds land outside the ledger, so a
realized gain is not tracked). The user's facts are gold_facts rows (POST /api/gold): grams held and 매입금액 합계 as
the KB app showed them that day. rebuild() turns a fact the booked rows don't explain into one derived buy (source
SOURCE) and writes a snapshot (source SOURCE) for every KRX trading day from the first buy, plus each fact date.
Account: ASSETLOG_GOLD_ACCOUNT in .env. rebuild() runs on every sync and after a page write to the account.
"""
import json
from datetime import date

from . import cloud, dcmodel

CODE = "M04020000"  # 금 99.99_1kg
SOURCE = "gold"
CACHE = "gold_closes.json"


def account(cfg):
    return cfg.get("ASSETLOG_GOLD_ACCOUNT", "").strip() or None


def closes(cfg, start, today):
    """{YYYY-MM-DD: KRW per gram} from start to today. Fetched from the last cached day on (it may have been cached
    mid-session, before its close), or from `start` when the cache doesn't reach back that far."""
    cached = cloud._read(CACHE, {})
    lo = max(start, date.fromisoformat(max(cached))) if cached and min(cached) <= start.isoformat() else start
    if lo <= today:
        cached.update(dcmodel.closes(cfg, CODE, lo, today))
        cloud._write(CACHE, cached)
    return cached


def rebuild(conn, cfg, account_id, today, prices=None):
    """Redo the derived buys and the snapshots of the gold account. `prices` ({date: close}) skips the KIS fetch."""
    conn.execute("DELETE FROM transactions WHERE account_id = ? AND source = ?", (account_id, SOURCE))
    conn.execute("DELETE FROM snapshots WHERE account_id = ? AND source = ?", (account_id, SOURCE))
    facts = {r["date"]: (r["grams"], r["cost"]) for r in conn.execute(
        "SELECT date, grams, cost FROM gold_facts WHERE account_id = ? ORDER BY date", (account_id,))}
    rows = [(r["date"], r["amount"], json.loads(r["raw"] or "{}").get("grams", 0.0)) for r in conn.execute(
        "SELECT date, amount, raw FROM transactions WHERE account_id = ? AND kind IN ('DEPOSIT', 'WITHDRAW') "
        "ORDER BY date", (account_id,))]
    if not facts and not rows:
        return
    first = min([*facts, *(d for d, _, _ in rows)])
    prices = prices if prices is not None else closes(cfg, date.fromisoformat(first), today)
    days = sorted({d for d in prices if first <= d <= today.isoformat()} | set(facts) | {d for d, _, _ in rows})
    grams = cost = 0.0
    held = {}  # date: grams at its close
    for d in days:
        for rd, amt, g in rows:
            if rd == d:
                grams, cost = grams + g, cost + amt
        if d in facts:
            fg, fc = facts[d]
            if abs(fc - cost) >= 1 or abs(fg - grams) > 1e-6:  # bought (or sold) since: one derived row for the gap
                conn.execute(
                    "INSERT INTO transactions (account_id, date, kind, amount, memo, source, source_key, raw) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (account_id, d, "DEPOSIT" if fc >= cost else "WITHDRAW", fc - cost,
                     "보유량·매입금액 변화로 계산한 금 매수", SOURCE, d, json.dumps({"grams": fg - grams})))
            grams, cost = fg, fc
        held[d] = grams
    ordered, moved = sorted(prices), {d for d, _, _ in rows}
    for d in days:
        price = prices.get(d) or next((prices[p] for p in reversed(ordered) if p <= d), None)
        if price and (held[d] > 0 or d in facts or d in moved) and not conn.execute(
                "SELECT 1 FROM snapshots WHERE account_id = ? AND date = ?", (account_id, d)).fetchone():
            conn.execute("INSERT INTO snapshots (account_id, date, value, cash, source) VALUES (?, ?, ?, NULL, ?)",
                         (account_id, d, held[d] * price, SOURCE))
