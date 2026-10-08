"""SQLite ledger: accounts, cash-flow transactions, valuation snapshots.

Estimates vs booked records: *_infer flows (infer.py), computed dividends (*_api rows keyed 'div:…') and dc_model rows
are estimates; a file statement, a recording or a manual row is booked, and wins over the estimates for its days.
"""
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from . import config

DB_PATH = Path(os.environ.get("ASSETLOG_DB", Path(__file__).resolve().parent.parent / "data" / "assetlog.db"))

# DEPOSIT/WITHDRAW: money crossing the tracked boundary (principal). DIVIDEND/INTEREST: income.
# INTERNAL: trades, FX, fees, taxes — already reflected in valuation snapshots, ignored by metrics.
# UNKNOWN: needs a human decision (shown in the review queue).
KINDS = ("DEPOSIT", "WITHDRAW", "DIVIDEND", "INTEREST", "INTERNAL", "UNKNOWN")

_KIND_CHECK = ", ".join(f"'{k}'" for k in KINDS)
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY,                      -- e.g. 'kb:12345678-01'
  institution TEXT NOT NULL,                -- kb | kis | toss | hana | manual
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'brokerage' CHECK (kind IN ('brokerage', 'pension'))
);
CREATE TABLE IF NOT EXISTS transactions (
  id INTEGER PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id),
  date TEXT NOT NULL,                       -- YYYY-MM-DD
  kind TEXT NOT NULL CHECK (kind IN ({_KIND_CHECK})),
  amount REAL NOT NULL,                     -- KRW, signed cash effect on the account (+ in, - out)
  memo TEXT NOT NULL DEFAULT '',            -- 적요
  source TEXT NOT NULL,                     -- kb_api | kis_xlsx | toss_pdf | csv | manual
  source_key TEXT NOT NULL,                 -- stable id within (account, source): re-imports dedupe
  kind_locked INTEGER NOT NULL DEFAULT 0,   -- 1 = kind set by the user; re-imports keep it
  raw TEXT,
  UNIQUE (account_id, source, source_key)
);
CREATE TABLE IF NOT EXISTS snapshots (
  account_id TEXT NOT NULL REFERENCES accounts(id),
  date TEXT NOT NULL,
  value REAL NOT NULL,                      -- KRW total: holdings market value + cash (end of day)
  cash REAL,                                -- KRW cash part, if known
  source TEXT NOT NULL,
  rate REAL,                                -- KRW per USD this value was priced at; NULL = fx of its date
  PRIMARY KEY (account_id, date)
);
CREATE TABLE IF NOT EXISTS fx (             -- rate by date: latest drives the ₩/$ toggle; history prices dollar returns
  date TEXT NOT NULL,
  currency TEXT NOT NULL,                   -- USD
  krw REAL NOT NULL CHECK (krw > 0),        -- KRW per 1 unit
  source TEXT NOT NULL,
  PRIMARY KEY (date, currency)
);
CREATE TABLE IF NOT EXISTS cash_obs (       -- KIS end-of-day cash legs; infer.py derives deposits/withdrawals from them
  account_id TEXT NOT NULL REFERENCES accounts(id),
  date TEXT NOT NULL,
  krw REAL NOT NULL,                        -- KRW 예수금 (settlement basis)
  usd REAL NOT NULL,                        -- USD 예수금 (settlement basis)
  rp_usd REAL NOT NULL,                     -- USD parked overnight in the auto-RP (not in the USD 예수금)
  rate REAL NOT NULL CHECK (rate > 0),      -- KRW per USD
  at TEXT,                                  -- when observed (ISO 8601, KST); splits fills between observations
  PRIMARY KEY (account_id, date)
);
CREATE TABLE IF NOT EXISTS gold_facts (     -- what the KB app showed for the gold account that day (gold.py)
  account_id TEXT NOT NULL REFERENCES accounts(id),
  date TEXT NOT NULL,
  grams REAL NOT NULL,                      -- held
  cost REAL NOT NULL,                       -- 매입금액 합계, KRW
  PRIMARY KEY (account_id, date)
);
CREATE TABLE IF NOT EXISTS coverage (       -- days a complete statement (toss_pdf, kis_xlsx) booked in full
  account_id TEXT NOT NULL REFERENCES accounts(id),
  source TEXT NOT NULL,
  first_day TEXT NOT NULL,                  -- its first and last dated row
  last_day TEXT NOT NULL,
  PRIMARY KEY (account_id, source, first_day, last_day)
);
"""

# First match wins. Order matters: '배당금입금' must hit 배당 before 입금, '환전정산입금' must hit 환전.
RULES = [
    ("배당", "DIVIDEND"), ("분배금", "DIVIDEND"),   # 배당소득세 rows land here too, netting the dividend
    ("이용료", "INTEREST"), ("이자", "INTEREST"), ("대여료", "INTEREST"),
    ("매수", "INTERNAL"), ("매도", "INTERNAL"), ("구매", "INTERNAL"), ("판매", "INTERNAL"),  # 토스: 구매/판매
    ("환전", "INTERNAL"), ("청약", "INTERNAL"),
    ("수수료", "INTERNAL"), ("세금", "INTERNAL"),
    ("공모주", "INTERNAL"), ("무상", "INTERNAL"),  # IPO allotment / bonus shares: paid from inside the account
    ("입고", "UNKNOWN"), ("출고", "UNKNOWN"),      # securities moved in/out: needs a market value
    ("입금", "DEPOSIT"), ("출금", "WITHDRAW"),
]
# Memo words meaning money went out: an income row carrying them must be negative (tax netting a dividend).
OUT_WORDS = ("출금", "세금", "소득세", "징수", "배당세")


def classify(memo, amount):
    for key, kind in RULES:
        if key in memo:
            if (kind == "DEPOSIT" and amount <= 0) or (kind == "WITHDRAW" and amount >= 0):
                return "UNKNOWN"  # direction contradicts the memo
            if kind in ("DIVIDEND", "INTEREST") and amount > 0 and any(w in memo for w in OUT_WORDS):
                return "UNKNOWN"  # unsigned tax row: counting it would add the tax to income
            return kind
    return "UNKNOWN"


def connect(path=None):
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    new = not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'coverage'").fetchone()
    conn.executescript(SCHEMA)
    if "at" not in {r[1] for r in conn.execute("PRAGMA table_info(cash_obs)")}:  # cash_obs predates `at` (2026-09-26)
        conn.execute("ALTER TABLE cash_obs ADD COLUMN at TEXT")
    if "rate" not in {r[1] for r in conn.execute("PRAGMA table_info(snapshots)")}:  # added 2026-09-26; backfill_fx fills it
        conn.execute("ALTER TABLE snapshots ADD COLUMN rate REAL")
    if new:  # coverage predates 2026-09-26: a statement imported before then covers the span of its rows
        with conn:
            conn.execute("INSERT INTO coverage SELECT account_id, source, MIN(date), MAX(date) FROM transactions "
                         "WHERE source IN ('toss_pdf', 'kis_xlsx') GROUP BY account_id, source")
    return conn


def coverage(conn, account_id):
    """[(first_day, last_day)] the account's complete statements cover, adjacent or overlapping ranges merged."""
    out = []
    for lo, hi in conn.execute("SELECT first_day, last_day FROM coverage WHERE account_id = ? ORDER BY first_day",
                               (account_id,)):
        if out and lo <= (date.fromisoformat(out[-1][1]) + timedelta(days=1)).isoformat():
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def _iso(d):
    return (d if isinstance(d, date) else date.fromisoformat(str(d))).isoformat()


def upsert_account(conn, id, institution, name, kind="brokerage"):
    conn.execute(
        "INSERT INTO accounts (id, institution, name, kind) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (id) DO UPDATE SET name = excluded.name",
        (id, institution, name, kind),
    )


def upsert_snapshot(conn, account_id, d, value, cash=None, source="manual", rate=None):
    conn.execute(
        "INSERT INTO snapshots (account_id, date, value, cash, source, rate) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (account_id, date) DO UPDATE SET value = excluded.value, cash = excluded.cash, "
        "source = excluded.source, rate = excluded.rate",
        (account_id, _iso(d), float(value), None if cash is None else float(cash), source,
         None if not rate else float(rate)),
    )


def upsert_fx(conn, d, currency, krw, source):
    conn.execute(
        "INSERT INTO fx (date, currency, krw, source) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (date, currency) DO UPDATE SET krw = excluded.krw, source = excluded.source",
        (_iso(d), currency, float(krw), source),
    )


def upsert_transaction(conn, account_id, d, amount, memo, source, source_key, kind=None, raw=None):
    amount = float(amount)
    kind = kind or classify(memo, amount)
    conn.execute(
        "INSERT INTO transactions (account_id, date, kind, amount, memo, source, source_key, raw) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (account_id, source, source_key) DO UPDATE SET "
        "date = excluded.date, memo = excluded.memo, raw = excluded.raw, "
        "amount = CASE WHEN transactions.kind_locked THEN transactions.amount ELSE excluded.amount END, "
        "kind = CASE WHEN transactions.kind_locked THEN transactions.kind ELSE excluded.kind END",
        (account_id, _iso(d), kind, amount, memo, source, str(source_key), raw),
    )


def upsert_cash_obs(conn, account_id, d, krw, usd, rp_usd, rate, at=None):
    conn.execute(
        "INSERT INTO cash_obs (account_id, date, krw, usd, rp_usd, rate, at) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (account_id, date) DO UPDATE SET krw = excluded.krw, usd = excluded.usd, "
        "rp_usd = excluded.rp_usd, rate = excluded.rate, at = excluded.at",
        (account_id, _iso(d), float(krw), float(usd), float(rp_usd), float(rate), at),
    )


def set_kind(conn, tx_id, kind, amount=None):
    """User override from the review queue; locked so re-imports don't undo it."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    cur = conn.execute(
        "UPDATE transactions SET kind = ?, kind_locked = 1, amount = COALESCE(?, amount) WHERE id = ?",
        (kind, None if amount is None else float(amount), tx_id),
    )
    if cur.rowcount == 0:
        raise KeyError(tx_id)


# Your own month-end USD rates (KIS CTRP6010R bass_exrt), for hand-entered month-end snapshots priced at them:
# data/fx_month_end.json, {"YYYY-MM-DD": krw}. Optional and personal, so it lives in data/, not here.
MONTH_END_FILE = "fx_month_end.json"


def backfill_fx(conn):
    """One-off, no API calls (`python3 -m assetlog backfill-fx`, docs/fx-usd-return.md §2): past USD rates into fx
    from what the ledger already holds, and snapshots.rate where the rate a value was priced at is known. Idempotent:
    fx rows only fill empty dates (a sync's own row wins) and only NULL rates are set. Returns what it wrote."""
    import json
    import re
    from .importers import NUM, USD_TAIL  # importers imports db: late
    row = re.compile(rf"\s*(\d{{4}}\.\d\d\.\d\d)\s+(\S.*?)((?:\s+{NUM}){{{len(USD_TAIL)},{len(USD_TAIL) + 1}}})\s*$")
    month_ends = config.DATA / MONTH_END_FILE
    rates = [(d, r, "kis_bass") for d, r in (json.loads(month_ends.read_text()) if month_ends.exists() else {}).items()]
    for (raw,) in conn.execute("SELECT raw FROM transactions WHERE source = 'kis_api' AND kind = 'DIVIDEND' ORDER BY id"):
        x = json.loads(raw)  # us_dividends(): KIS bass_exrt on the KR base date
        rates.append((f"{x['kr_base_dt'][:4]}-{x['kr_base_dt'][4:6]}-{x['kr_base_dt'][6:]}", float(x["rate"]), "kis_div"))
    for d, memo, raw in conn.execute("SELECT date, memo, raw FROM transactions WHERE source = 'toss_pdf' ORDER BY id"):
        x = json.loads(raw or "{}")  # 환전 rows carry Toss's customer rate (spread up to 2%): not a 매매기준율
        nums = "usd" in x and "환전" not in memo and (m := row.match(x["line"])) and m[3].split()
        if nums and len(nums) == len(USD_TAIL) + 1 and 900 < (r := float(nums[0].replace(",", ""))) < 2500:
            rates.append((d, r, "toss_pdf"))  # the statement's optional 환율 column
    out = {}
    with conn:
        for d, r, source in rates:  # first wins: month-ends, then dividends, then the statement
            n = conn.execute("INSERT OR IGNORE INTO fx (date, currency, krw, source) VALUES (?, 'USD', ?, ?)",
                             (d, r, source)).rowcount
            out[f"fx {source}"] = out.get(f"fx {source}", 0) + n
        out["snapshots.rate month-end"] = sum(conn.execute(
            "UPDATE snapshots SET rate = ? WHERE date = ? AND source = 'manual' AND rate IS NULL AND account_id IN "
            "(SELECT id FROM accounts WHERE institution IN ('kis', 'toss'))", (r, d)).rowcount
            for d, r, source in rates if source == "kis_bass")
        out["snapshots.rate sync"] = conn.execute(  # the day's cash legs were read at the snapshot's rate
            "UPDATE snapshots SET rate = (SELECT rate FROM cash_obs o WHERE o.account_id = snapshots.account_id "
            "AND o.date = snapshots.date) WHERE source IN ('kis_api', 'toss_api') AND rate IS NULL AND EXISTS "
            "(SELECT 1 FROM cash_obs o WHERE o.account_id = snapshots.account_id AND o.date = snapshots.date)").rowcount
    return out
