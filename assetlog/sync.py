"""Pull balances and transactions from every configured broker into the ledger.

Broker module contract (assetlog/brokers/<name>.py):
    REQUIRED: tuple of config keys; the broker is skipped unless all are set.
    fetch(cfg, today, since) -> {
        "accounts":     [{"id", "institution", "name", "kind"}],
        "snapshots":    [{"account_id", "date", "value", "cash", "rate"?}],  # KRW; rate: KRW per USD it was priced at
        "transactions": [{"account_id", "date", "amount", "memo", "source_key", "raw", "kind"?}],
        "errors"?:      [str],  # parts that failed while the rest is still worth storing
        "fx"?:          [{"date", "currency", "krw"}],  # the broker's own rate, for display conversion
        "cash_obs"?:    [{"account_id", "date", "krw", "usd", "rp_usd", "rate", "at"?}],  # see infer.py
        "settlements"?: [{"account_id", "date", "usd", "krw", "at"?}],  # trade cash by KR day, or by fill time
        "settlements_from"?: "YYYY-MM-DD",                                          # ...complete from this date
    }
    `since` is the first date whose transactions should be (re)fetched, or None for full history.
"""
import json
import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import cloud, db, dcmodel, gold, infer, principal
from .brokers import kb, kis, toss

BROKERS = {"kb": kb, "kis": kis, "toss": toss}
REFETCH_DAYS = 7  # re-read the last week: late-posted rows and corrections
STATUS = "sync.json"  # the last run's outcome in data/, for the page's failure banner (web.summary)
HINTS = (  # (source, message pattern, what to do): known causes only
    ("toss", re.compile(r"HTTP 403|허용 IP|access_denied"),
     "PC 웹 설정 > Open API > 허용 IP 관리에 이 PC의 공인 IP를 등록하세요"),
    # token issuance refused (not EGW00133, the one-per-minute limit), invalid AppKey/AppSecret, a fresh token
    # still rejected (kis.STALE)
    ("kis", re.compile(r"tokenP: HTTP 4\d\d (?!EGW00133)|EGW0010[35]|EGW0012[13]"),
     "한투 Open API 신청이 만료됐는지 확인하세요 (홈페이지 서비스신청 > Open API > KIS Developers)"),
    # None: any source. No network (e.g. the boot catch-up ran before Wi-Fi was up); the unit retries on its own
    (None, TRANSIENT := re.compile(r"ConnectionError|NameResolution|ConnectTimeout|ReadTimeout|EGW00201|EGW00215"),
     "인터넷 연결이 안 된 상태라 실패했어요. 연결되면 자동으로 다시 시도해요"),
)

log = logging.getLogger(__name__)


def _since(conn, source):
    row = conn.execute("SELECT MAX(date) FROM transactions WHERE source = ?", (source,)).fetchone()
    return None if row[0] is None else date.fromisoformat(row[0]) - timedelta(days=REFETCH_DAYS)


def sync(conn, cfg, today):
    """_sync, with its outcome kept in data/sync.json. A run that raises is recorded as failed and still raises."""
    try:
        report = _sync(conn, cfg, today)
    except BaseException as e:
        _record({"sync": f"error: {type(e).__name__}: {e}"})
        raise
    _record(report)
    return report


def _record(report):
    """{"at", "ok_at" (the last run with no failure), "failed": [{"source", "message", "hint"}]}. A failure is an
    "error: ..." value or the errors of a partial "ok: ...; errors: ..."; skipped is not one. Messages are exception
    text (the brokers never put keys or tokens in them), cut to 200 characters."""
    failed = []
    for source, v in report.items():
        msg = v[len("error: "):] if v.startswith("error: ") else v.partition("; errors: ")[2]
        if msg:
            hint = next((h for s, p, h in HINTS if s in (source, None) and p.search(msg)), None)
            failed.append({"source": source, "message": msg[:200], "hint": hint})
    at = datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")
    try:
        cloud._write(STATUS, {"at": at, "ok_at": cloud._read(STATUS, {}).get("ok_at") if failed else at,
                              "failed": failed})
    except OSError:  # the banner is not worth failing a sync whose data is already stored
        log.exception("could not write data/%s", STATUS)


def _sync(conn, cfg, today):
    report = {}
    for name, mod in BROKERS.items():
        if not all(cfg.get(k) for k in mod.REQUIRED):
            report[name] = "skipped (not configured)"
            continue
        source = f"{name}_api"
        try:
            out = mod.fetch(cfg, today, _since(conn, source))
            with conn:  # one broker = one transaction: all or nothing
                for a in out["accounts"]:
                    db.upsert_account(conn, a["id"], a["institution"], a["name"], a.get("kind", "brokerage"))
                for s in out["snapshots"]:
                    db.upsert_snapshot(conn, s["account_id"], s["date"], s["value"], s.get("cash"), source, s.get("rate"))
                for f in out.get("fx", []):
                    db.upsert_fx(conn, f["date"], f["currency"], f["krw"], source)
                # Booked records win over computed dividends ('div:' keys): skip the days a complete statement
                # (toss_pdf, kis_xlsx) covers, where any API row would double, and days with a booked dividend.
                covered = {a: db.coverage(conn, a) for a in {t["account_id"] for t in out["transactions"]}}
                paid = {tuple(r) for r in conn.execute(
                    "SELECT account_id, date FROM transactions WHERE kind = 'DIVIDEND' "
                    "AND source NOT LIKE '%\\_api' ESCAPE '\\' AND source != 'dc_model'")}
                for t in out["transactions"]:
                    d = str(t["date"])
                    if any(lo <= d <= hi for lo, hi in covered[t["account_id"]]) or (
                            str(t["source_key"]).startswith("div:") and (t["account_id"], d) in paid):
                        continue
                    raw = t.get("raw")
                    db.upsert_transaction(
                        conn, t["account_id"], t["date"], t["amount"], t["memo"], source, t["source_key"],
                        t.get("kind"), raw if raw is None or isinstance(raw, str) else json.dumps(raw, ensure_ascii=False),
                    )
                inferred = 0
                for c in out.get("cash_obs", []):
                    db.upsert_cash_obs(conn, c["account_id"], c["date"], c["krw"], c["usd"], c["rp_usd"], c["rate"], c.get("at"))
                    conn.execute("SAVEPOINT infer")  # an estimate failing must not cost the day's snapshot
                    try:
                        n, notes = infer.apply(conn, c["account_id"], out.get("settlements", []), out.get("settlements_from", ""),
                                               f"{name}_infer")
                        conn.execute("RELEASE infer")
                        inferred += n
                        out.setdefault("errors", []).extend(notes)
                    except Exception as e:
                        conn.execute("ROLLBACK TO infer")
                        conn.execute("RELEASE infer")
                        out.setdefault("errors", []).append(f"infer: {type(e).__name__}: {e}")
            report[name] = f"ok: {len(out['snapshots'])} snapshots, {len(out['transactions'])} transactions"
            if inferred:
                report[name] += f", {inferred} inferred deposits/withdrawals changed"
            if out.get("errors"):  # partial success: e.g. valuation stored, ledger rejected
                report[name] += "; errors: " + " | ".join(out["errors"])
        except Exception as e:  # one broker failing must not block the others
            log.exception("%s sync failed", name)
            report[name] = f"error: {type(e).__name__}: {e}"
    if cfg.get("DC_ACCOUNT"):  # a pension with no API, modelled from its contributions and KIS prices
        try:
            report["dc"] = dcmodel.sync(conn, cfg, today)
        except Exception as e:
            log.exception("dc model failed")
            report["dc"] = f"error: {type(e).__name__}: {e}"
    for aid in principal.accounts(cfg):  # brings the derived snapshot up to today (principal.py)
        with conn:
            principal.rebuild(conn, aid, today)
    if gold.account(cfg):  # the day's KRX gold close (gold.py)
        try:
            with conn:
                gold.rebuild(conn, cfg, gold.account(cfg), today)
            report["gold"] = "ok"
        except Exception as e:
            log.exception("gold failed")
            report["gold"] = f"error: {type(e).__name__}: {e}"
    return report
