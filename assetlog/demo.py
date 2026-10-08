"""Deterministic demo ledger. python -m assetlog.demo -> data/demo.db, then
ASSETLOG_DB=data/demo.db python3 -m assetlog serve"""
import random
from datetime import date, timedelta

from . import config, db, metrics
from .__main__ import today as kst_today

MONTHS = 36
ACCOUNTS = {
    # id: institution, name, kind, first point tracked, opening value, monthly drift, monthly volatility
    "kb:demo-01": ("kb", "KB증권 종합", "brokerage", 0, 48_000_000, 0.006, 0.045),
    "kis:demo-01": ("kis", "한국투자 ISA", "brokerage", 0, 21_000_000, 0.005, 0.03),
    "toss:demo-01": ("toss", "토스증권 해외주식", "brokerage", 12, 8_000_000, 0.011, 0.055),
    "hana:dc": ("hana", "하나은행 DC 퇴직연금", "pension", 0, 32_000_000, 0.004, 0.012),
}


def _points(today):
    """The last MONTHS month-ends before today, oldest first, then today."""
    out, d = [], today.replace(day=1) - timedelta(days=1)
    for _ in range(MONTHS):
        out.append(d)
        d = d.replace(day=1) - timedelta(days=1)
    return out[::-1] + [today]


def _events(aid, i, month, v):
    """(day, amount, memo[, value change if not the amount]) during point i; v = value after the market move."""
    if aid == "kb:demo-01":
        ev = [(25, 1_000_000, "이체입금")]
        if month in (4, 5, 8, 11):  # KR: annual dividend in April, quarterly in May/Aug/Nov (already net of 15.4%)
            ev.append((20, round(v * (0.006 if month == 4 else 0.0025)), "배당금입금 삼성전자"))
        if i == 17:
            ev.append((8, -3_000_000, "이체출금"))
        if i == 30:  # shares moved in from another broker: no cash, value jumps -> review queue
            ev.append((14, 0, "타사대체입고 NAVER", 3_500_000))
    elif aid == "kis:demo-01":
        ev = [(10, 500_000, "HTS타사이체입금"),
              (3, round(v * 0.0033), "ETF분배금입금 TIGER 미국배당다우존스")]  # monthly-distribution ETF
        if i == 20:
            ev.append((12, -5_000_000, "HTS타사이체출금"))  # to the own Toss account below
        if i == 26:
            ev.append((5, -2_000_000, "HTS타사이체출금"))
        if i == 33:
            ev.append((18, 12_340, "기타정산"))  # no rule matches -> review queue
    elif aid == "toss:demo-01":
        ev = [(26, 700_000, "이체입금(카카오뱅크)")]
        if month in (3, 6, 9, 12):  # US: credited net of 15% US withholding
            ev.append((16, round(v * 0.0024), "외화배당금입금 SCHD"))
        if month == 12:  # KR top-up tax as its own row; classify() nets it into DIVIDEND
            ev.append((17, -round(v * 0.0001), "배당세출금"))
        if i == 20:
            ev.append((12, 5_000_000, "이체입금(한국투자증권)"))
    else:
        ev = [(15, 420_000, "사용자부담금입금")]  # DC employer contribution
    return ev


def seed(conn, today):
    rng = random.Random(42)
    points = _points(today)
    n = 0
    for aid, (inst, name, kind, first, v, mu, sd) in ACCOUNTS.items():
        db.upsert_account(conn, aid, inst, name, kind)
        source = "manual" if inst == "hana" else f"{inst}_api"
        prev = None
        for i, end in enumerate(points[first:], first):
            if prev:
                f = (end - prev).days / 30.4  # the last point is a partial month
                v *= 1 + rng.gauss(mu * f, sd * f ** 0.5)
                for day, amount, memo, *jump in _events(aid, i, end.month, v):
                    d = date(end.year, end.month, day)
                    if prev < d <= end:
                        n += 1
                        db.upsert_transaction(conn, aid, d, amount, memo, source, f"demo-{n}")
                        v += jump[0] if jump else amount
            # DC: typed in quarterly, and the latest one is always stale (also just after a quarter end)
            if inst != "hana" or (end.month % 3 == 0 and (today - end).days > metrics.STALE_DAYS):
                db.upsert_snapshot(conn, aid, end, round(v), None, source)
            prev = end
    # A row per point (not just today's), back to the ledger's own start, so return_usd/fx_effect have fx history
    # to convert every period against (metrics.in_usd looks up each snapshot's own date; see docs/fx-usd-return.md).
    fx = 1300.0
    for d in points:
        fx *= 1 + rng.gauss(0.001, 0.02)  # gentle deterministic drift (seed 42), not tied to any account's path
        db.upsert_fx(conn, d, "USD", round(fx, 1), "demo")


def main():
    path = config.DATA / "demo.db"
    path.unlink(missing_ok=True)
    conn = db.connect(path)
    with conn:
        seed(conn, kst_today())
    conn.close()
    print(path)


if __name__ == "__main__":
    main()
