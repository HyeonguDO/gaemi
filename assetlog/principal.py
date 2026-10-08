"""Principal-only savings accounts (e.g. a 청년도약계좌 at a bank with no API): the value is the money paid in, nothing else.

The user enters the account's 납입원금 합계 now and then (an ordinary manual snapshot), and may book individual
deposits (from transfer screenshots). rebuild() turns that into a ledger metrics can read without special cases:
- every manual snapshot is a fact; the gap between it and the running principal (previous fact + booked flows since)
  becomes one DEPOSIT (or WITHDRAW) dated that day, source SOURCE, so the account never shows a gain;
- a snapshot (source SOURCE) is kept on every booked-flow date and on `today`, so the latest value always counts the
  money paid in and the page never calls it stale.
Interest and government contributions (정부기여금) are deliberately left out. Accounts: ASSETLOG_PRINCIPAL_ONLY in .env,
comma-separated account ids. rebuild() runs after every page write to such an account and on every sync.
"""
from datetime import date

SOURCE = "principal"


def accounts(cfg):
    return [a.strip() for a in cfg.get("ASSETLOG_PRINCIPAL_ONLY", "").split(",") if a.strip()]


def rebuild(conn, account_id, today=None):
    """Redo the derived rows of one account from its manual snapshots and booked DEPOSIT/WITHDRAW rows."""
    today = (today or date.today()).isoformat()
    conn.execute("DELETE FROM transactions WHERE account_id = ? AND source = ?", (account_id, SOURCE))
    conn.execute("DELETE FROM snapshots WHERE account_id = ? AND source = ?", (account_id, SOURCE))
    facts = conn.execute("SELECT date, value FROM snapshots WHERE account_id = ? ORDER BY date", (account_id,)).fetchall()
    flows = conn.execute("SELECT date, amount FROM transactions WHERE account_id = ? "
                         "AND kind IN ('DEPOSIT', 'WITHDRAW') ORDER BY date", (account_id,)).fetchall()
    if not facts and not flows:  # nothing entered yet: no value to carry forward
        return
    events = sorted({r["date"] for r in facts} | {r["date"] for r in flows} | {today})
    fact = {r["date"]: r["value"] for r in facts}
    p = 0.0
    for d in events:
        p += sum(r["amount"] for r in flows if r["date"] == d)
        if d in fact:
            gap = fact[d] - p
            if abs(gap) >= 1:  # what the booked rows don't explain: paid in (or out) since the last fact
                conn.execute(
                    "INSERT INTO transactions (account_id, date, kind, amount, memo, source, source_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (account_id, d, "DEPOSIT" if gap > 0 else "WITHDRAW", gap, "납입원금 변화로 계산한 입출금", SOURCE, d))
            p = fact[d]
        elif d <= today:
            conn.execute("INSERT INTO snapshots (account_id, date, value, cash, source) VALUES (?, ?, ?, ?, ?)",
                         (account_id, d, p, p, SOURCE))
