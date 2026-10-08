"""Web UI + JSON API (python -m assetlog serve, run by hand on this PC). One SQLite connection per request.

There is no login: the phone uses the Worker in cloud/ behind Cloudflare Access, and cloud.py applies its queued
writes through this app in-process. local_only guards every request, and every response carries SECURITY_HEADERS:
- the Host must be localhost, 127.0.0.1 or ::1 (DNS rebinding);
- a client with an internet address is refused (the peer address; serve turns uvicorn's proxy headers off), so a
  port opened by mistake fails closed;
- a request with X-Forwarded-For or Forwarded is refused: a tunnel opened by mistake that rewrites Host to localhost
  arrives from 127.0.0.1, and nothing legitimate here sends them;
- writes need this app's Origin, and /api/* refuses Sec-Fetch-Site: cross-site (an <img> on another site can't
  start broker reads).
"""
import ipaddress
import logging
import math
import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager, suppress
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import AfterValidator, BaseModel, Field, model_validator
from starlette.concurrency import run_in_threadpool

from . import classes, cloud, config, db, dcmodel, gold, infer, metrics, payers, principal
from .__main__ import today

log = logging.getLogger(__name__)
app = FastAPI(title="assetlog")
STATIC = Path(__file__).resolve().parent / "static"
MAX_UPLOAD = 20 * 1024 * 1024
HOSTS = {"localhost", "127.0.0.1", "::1"}  # DNS-rebinding guard: only these Host names may talk to the app
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                               "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data:; "
                               "font-src 'self' https://cdn.jsdelivr.net; "
                               "connect-src 'self' https://cdn.jsdelivr.net; frame-ancestors 'none'; base-uri 'none'; "
                               "form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _internet(addr):
    try:
        return ipaddress.ip_address(addr).is_global  # False for loopback and LAN
    except ValueError:
        return True


def _refusal(request):
    host = request.url.hostname or ""
    if host not in HOSTS:
        return JSONResponse({"detail": f"host {host!r} not allowed"}, 403)
    if _internet(request.client.host if request.client else ""):
        return JSONResponse({"detail": "internet clients are not served; the phone uses the Cloudflare Worker"}, 403)
    if "x-forwarded-for" in request.headers or "forwarded" in request.headers:  # a tunnel or proxy opened by mistake
        return JSONResponse({"detail": "proxied requests are not served; the phone uses the Cloudflare Worker"}, 403)
    # CSRF guard: browsers send Origin on cross-site POST/PATCH/DELETE; it must be this app.
    origin, path = request.headers.get("origin"), request.url.path
    if request.method not in ("GET", "HEAD") and origin and urlsplit(origin).netloc != request.headers.get("host"):
        return JSONResponse({"detail": "cross-origin request refused"}, 403)
    if path.startswith("/api/") and request.headers.get("sec-fetch-site") == "cross-site":
        return JSONResponse({"detail": "cross-site request refused"}, 403)
    return None


@app.middleware("http")
async def local_only(request: Request, call_next):
    response = _refusal(request) or await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    return response


@contextmanager
def ledger():
    conn = db.connect()  # opened inside the handler's own thread: sqlite's check_same_thread holds
    try:
        with conn:  # commit on success, roll back on any error
            yield conn
    finally:
        conn.close()


def _past(d):
    if not date(1990, 1, 1) <= d <= today():
        raise ValueError("date must be between 1990-01-01 and today (KST)")
    return d


Day = Annotated[date, AfterValidator(_past)]
Kind = Literal[db.KINDS]
Won = Annotated[float, Field(allow_inf_nan=False, gt=-1e13, lt=1e13)]
AccountId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[\w.:@-]+$")]


def _check_sign(kind, amount):
    # same direction rule as db.classify: principal in is +, principal out is -
    if amount is not None and (kind == "DEPOSIT" and amount <= 0 or kind == "WITHDRAW" and amount >= 0):
        raise ValueError(f"{kind} amount must be {'positive' if kind == 'DEPOSIT' else 'negative'}")


class AccountIn(BaseModel):
    id: AccountId
    institution: Annotated[str, Field(min_length=1, max_length=20, pattern=r"^[\w-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=60)]
    kind: Literal["brokerage", "pension"] = "brokerage"


class KindIn(BaseModel):
    kind: Kind
    amount: Won | None = None

    @model_validator(mode="after")
    def sign(self):
        _check_sign(self.kind, self.amount)
        return self


class TransactionIn(BaseModel):
    account_id: AccountId
    date: Day
    kind: Kind
    amount: Won
    memo: Annotated[str, Field(max_length=200)] = ""

    @model_validator(mode="after")
    def sign(self):
        _check_sign(self.kind, self.amount)
        return self


class SnapshotIn(BaseModel):
    account_id: AccountId
    date: Day
    value: Annotated[Won, Field(ge=0)]
    cash: Annotated[Won, Field(ge=0)] | None = None
    force: bool = False  # keep a value far off the DC model's (add_snapshot)


class GoldIn(BaseModel):
    date: Day
    grams: Annotated[float, Field(ge=0, lt=1e6, allow_inf_nan=False)]
    cost: Annotated[Won, Field(ge=0)]


def _account(conn, account_id):
    if not conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone():
        raise HTTPException(404, f"unknown account {account_id!r}")


def _json_safe(x):
    # ponytail: metrics can yield complex (negative base ** fractional power) or inf; one bad figure
    # must not 500 the whole home screen. Root fix belongs in metrics.period/compound.
    if isinstance(x, dict):
        return {k: _json_safe(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_json_safe(v) for v in x]
    return None if isinstance(x, complex) or isinstance(x, float) and not math.isfinite(x) else x


def _importers():
    try:
        from . import importers
    except ImportError as e:
        raise HTTPException(501, f"file import unavailable: {e}")
    return importers


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})  # phones kept serving an old UI


# "홈 화면에 추가" opens the app full screen (the phone gets cloud/'s copy of this manifest).
@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return JSONResponse({
        "name": "개미장부", "short_name": "개미장부", "start_url": "/", "display": "standalone",
        "background_color": "#f2f4f6", "theme_color": "#f2f4f6",
        "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
    }, media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
def service_worker():  # served from the root so it controls the whole app; no-cache so fixes reach phones
    return FileResponse(STATIC / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/icon.svg", include_in_schema=False)
def icon():
    return FileResponse(STATIC / "icon.svg", media_type="image/svg+xml")


@app.get("/api/summary")
def summary():
    with ledger() as conn:
        # sync: the last sync's outcome (sync.py), for the failure banner; it reaches the phone with the summary.
        # dividend_payers: the last holdings read's 보유 배당주 배당수익률 (payers.py), null before one
        return _json_safe({**metrics.summary(conn, today(), principal.accounts(config.load())), "sync": cloud._read("sync.json", None),
                           "dividend_payers": cloud._read(payers.STATE, None)})


@app.get("/api/accounts")
def accounts():
    with ledger() as conn:
        cfg = config.load()
        mode = dict.fromkeys(principal.accounts(cfg), "principal")
        if gold.account(cfg):
            mode[gold.account(cfg)] = "gold"
        return [{**dict(r), **({"mode": mode[r["id"]]} if r["id"] in mode else {})}  # principal | gold
                for r in conn.execute("SELECT id, institution, name, kind FROM accounts ORDER BY id")]


@app.post("/api/accounts")
def add_account(a: AccountIn):
    with ledger() as conn:
        try:  # not db.upsert_account: on an existing id that renames it and ignores institution and kind
            conn.execute("INSERT INTO accounts (id, institution, name, kind) VALUES (?, ?, ?, ?)",
                         (a.id, a.institution, a.name, a.kind))
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"account {a.id!r} already exists")
    return {"id": a.id}


@app.get("/api/transactions")
def transactions(kind: Kind | None = None, account_id: AccountId | None = None,
                 limit: Annotated[int, Query(ge=1, le=100_000)] = 200):  # cloud.py reads it all: ~130 years
    where = " AND ".join(c for c, v in (("kind = ?", kind), ("account_id = ?", account_id)) if v)
    args = [v for v in (kind, account_id) if v]
    with ledger() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, account_id, date, kind, amount, memo, source, kind_locked, raw FROM transactions "
            f"{'WHERE ' + where if where else ''} ORDER BY date DESC, id DESC LIMIT ?", (*args, limit))]


def _displace_estimates(conn, account_id, d, kind):
    """A deposit or withdrawal booked by hand replaces the estimates for its days now, not at the next sync, so the
    two never count together."""
    if kind in ("DEPOSIT", "WITHDRAW"):
        infer.yield_to_booked(conn, account_id, d)
    if kind in ("DEPOSIT", "DIVIDEND"):
        dcmodel.yield_month(conn, account_id, d, kind)


@app.post("/api/transactions")
def add_transaction(t: TransactionIn):
    key = uuid.uuid4().hex
    with ledger() as conn:
        _account(conn, t.account_id)
        db.upsert_transaction(conn, t.account_id, t.date, t.amount, t.memo, "manual", key, t.kind)
        _displace_estimates(conn, t.account_id, t.date, t.kind)
        _principal(conn, t.account_id)
        (tx_id,) = conn.execute(
            "SELECT id FROM transactions WHERE account_id = ? AND source = 'manual' AND source_key = ?",
            (t.account_id, key)).fetchone()
    return {"id": tx_id}


@app.patch("/api/transactions/{tx_id}")
def patch_transaction(tx_id: int, k: KindIn):
    with ledger() as conn:
        row = conn.execute("SELECT account_id, date, amount, source FROM transactions WHERE id = ?",
                           (tx_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"no transaction {tx_id}")
        if k.amount is None:  # keeping the stored amount: it must fit the new kind too (입고 rows are 0)
            try:
                _check_sign(k.kind, row["amount"])
            except ValueError as e:
                raise HTTPException(422, f"{e}, stored amount is {row['amount']:,.0f}: enter the amount "
                                         "(for 입고/출고, the market value on that day)")
        db.set_kind(conn, tx_id, k.kind, k.amount)
        if not row["source"].endswith("_infer") and row["source"] != dcmodel.SOURCE:  # an edited estimate stays one
            _displace_estimates(conn, row["account_id"], row["date"], k.kind)
        _principal(conn, row["account_id"])
    return {"id": tx_id, "kind": k.kind}


@app.delete("/api/transactions/{tx_id}")
def delete_transaction(tx_id: int):
    with ledger() as conn:
        row = conn.execute("SELECT account_id, source FROM transactions WHERE id = ?", (tx_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"no transaction {tx_id}")
        if row["source"] != "manual":
            raise HTTPException(400, f"only manual rows can be deleted (source is {row['source']}); reclassify it")
        conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
        _principal(conn, row["account_id"])
    return {"deleted": tx_id}


@app.get("/api/snapshots")
def snapshots(account_id: AccountId | None = None, limit: Annotated[int, Query(ge=1, le=5000)] = 200):
    with ledger() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT account_id, date, value, cash, source FROM snapshots "
            f"{'WHERE account_id = ?' if account_id else ''} ORDER BY date DESC, account_id LIMIT ?",
            (account_id, limit) if account_id else (limit,))]


@app.post("/api/snapshots")
def add_snapshot(s: SnapshotIn):
    d = s.date.isoformat()
    with ledger() as conn:
        _account(conn, s.account_id)
        row = conn.execute("SELECT source FROM snapshots WHERE account_id = ? AND date = ?",
                           (s.account_id, d)).fetchone()
        # broker valuations can't be re-fetched for past dates; the DC model's are recomputed on every sync
        if row and row["source"] not in ("manual", dcmodel.SOURCE, principal.SOURCE):
            raise HTTPException(409, f"{s.date} already has a {row['source']} snapshot; it can't be overwritten")
        _check_dc(conn, s)
        db.upsert_snapshot(conn, s.account_id, s.date, s.value, s.cash, "manual")
        _principal(conn, s.account_id)
    return {"account_id": s.account_id, "date": d}


def _principal(conn, account_id):
    """Redo a derived account's rows after a write to it: principal-only savings (principal.py) or gold (gold.py)."""
    cfg = config.load()
    if account_id in principal.accounts(cfg):
        principal.rebuild(conn, account_id, today())
    elif account_id == gold.account(cfg):
        gold.rebuild(conn, cfg, account_id, today())


@app.post("/api/gold")
def add_gold(g: GoldIn):
    """What the KB app shows for the gold account (ASSETLOG_GOLD_ACCOUNT): grams held and 매입금액 합계 on a day."""
    account_id = gold.account(config.load())
    if not account_id:
        raise HTTPException(404, "ASSETLOG_GOLD_ACCOUNT is not set")
    with ledger() as conn:
        _account(conn, account_id)
        conn.execute("INSERT INTO gold_facts (account_id, date, grams, cost) VALUES (?, ?, ?, ?) ON CONFLICT "
                     "(account_id, date) DO UPDATE SET grams = excluded.grams, cost = excluded.cost",
                     (account_id, g.date.isoformat(), g.grams, g.cost))
        _principal(conn, account_id)
    return {"account_id": account_id, "date": g.date.isoformat()}


def _check_dc(conn, s):
    """A DC value this far off the model's latest value on or before its date (a weekend, holiday or 12-31 has none of
    its own) is refused unless forced: dcmodel won't re-anchor on it, yet totals and returns would use it."""
    m = conn.execute("SELECT date, value FROM snapshots WHERE account_id = ? AND source = ? AND date <= ? "
                     "ORDER BY date DESC LIMIT 1", (s.account_id, dcmodel.SOURCE, s.date.isoformat())).fetchone()
    off = m and m["value"] > 0 and abs(s.value / m["value"] - 1)
    if off and off > dcmodel.ANCHOR_JUMP and not s.force:
        raise HTTPException(409, {"code": "dc_mismatch", "model": m["value"], "message": (
            f"{m['date']} 모델 평가액 {m['value']:,.0f}원과 {off:.0%} 차이 나요. 자릿수를 확인하세요. 맞는 값이면 그대로 "
            "저장할 수 있지만, 모델은 이 값을 오타로 보고 주식 수를 다시 맞추지 않을 수 있어요(그럴 땐 .env의 DC_START를 고치세요).")})


@app.delete("/api/snapshots")
def delete_snapshot(account_id: AccountId, date: date):
    with ledger() as conn:
        row = conn.execute("SELECT source FROM snapshots WHERE account_id = ? AND date = ?",
                           (account_id, date.isoformat())).fetchone()
        if not row:
            raise HTTPException(404, "no such snapshot")
        if row["source"] != "manual":
            raise HTTPException(400, f"only manual snapshots can be deleted (source is {row['source']})")
        conn.execute("DELETE FROM snapshots WHERE account_id = ? AND date = ?", (account_id, date.isoformat()))
        _principal(conn, account_id)
    return {"deleted": [account_id, date.isoformat()]}


@app.get("/api/import/formats")
def import_formats():
    return _importers().FORMATS


@app.post("/api/import")
async def import_file(request: Request, account_id: AccountId, fmt: Annotated[str, Query(max_length=40)]):
    importers = _importers()
    if fmt not in importers.FORMATS:
        raise HTTPException(422, f"unknown format {fmt!r}; one of {sorted(importers.FORMATS)}")
    data = b"".join([chunk async for chunk in _chunks(request, MAX_UPLOAD)])
    if not data:
        raise HTTPException(400, "file is empty")
    # headers are latin-1 only, so the page sends encodeURIComponent(file.name)
    filename = Path(unquote(request.headers.get("x-filename", ""))).name[:200]

    def run():  # sync DB work off the event loop, in one thread
        with ledger() as conn:
            _account(conn, account_id)
            try:
                return importers.import_bytes(conn, account_id, fmt, data, filename)
            except Exception as e:  # show the parser's message in the UI; ledger() rolled back
                log.exception("import %s failed", fmt)
                # keep the file: fixing a parser needs the real thing, and the phone may not have it handy again
                note = ""
                with suppress(HTTPException, OSError):  # best effort: the parser's message matters more
                    kept = _inbox_path(filename or f"import.{fmt}", prefix=f"failed-{fmt}_")
                    kept.write_bytes(data)
                    note = f" (파일은 inbox/{kept.name}에 보관했어요)"
                raise HTTPException(400, f"{type(e).__name__}: {e}{note}")

    return await run_in_threadpool(run)


async def _chunks(request, cap):
    """The request body, chunk by chunk; 413 once it (or its Content-Length) passes `cap` bytes. request.body()
    would hold all of it, twice over, before any length check."""
    if int(request.headers.get("content-length") or 0) > cap:
        raise HTTPException(413, f"file larger than {cap >> 20}MB")
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > cap:
            raise HTTPException(413, f"file larger than {cap >> 20}MB")
        yield chunk


INBOX = config.ROOT / "inbox"
MAX_INBOX = 500 * 1024 * 1024  # screen recordings are big
MIN_FREE = 2 * 1024 ** 3  # disk left for the ledger, which shares it: a full disk loses that night's snapshot


def _inbox_path(filename, prefix=""):
    if shutil.disk_usage(INBOX.parent).free < MIN_FREE:
        raise HTTPException(507, f"less than {MIN_FREE >> 30}GB of disk left: files are no longer kept")
    # bytes, not characters: a Hangul name 3 bytes a character passed ext4's 255-byte limit (ENAMETOOLONG, a 500)
    name = re.sub(r"[^\w.\-]", "_", Path(filename).name).encode()[-150:].decode(errors="ignore") or "upload"
    INBOX.mkdir(mode=0o700, exist_ok=True)
    return INBOX / f"{today():%Y%m%d}_{uuid.uuid4().hex[:8]}_{prefix}{name}"


@app.post("/api/inbox")
async def to_inbox(request: Request):
    """Phone screenshots/videos/statements kept as-is for transcription later (README: 화면으로 옮기기)."""
    path, size = _inbox_path(unquote(request.headers.get("x-filename", ""))), 0
    try:
        with open(path, "xb") as f:
            async for chunk in _chunks(request, MAX_INBOX):
                size += len(chunk)
                f.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)  # never leave half a file behind
        raise
    if not size:
        path.unlink()
        raise HTTPException(400, "empty file")
    return {"saved": path.name, "bytes": size}


MAX_PART = 1_400_000  # bytes: the Worker's MAX_BODY, one op each


@app.post("/api/inbox/part")
async def inbox_part(request: Request, upload: Annotated[str, Query(pattern=r"^[0-9a-f]{16}$")],
                     i: Annotated[int, Query(ge=0)], n: Annotated[int, Query(ge=1, le=600)]):
    """One part of a file the phone sent through the Worker, which caps a request at MAX_PART (index.html cuts files
    into 1 MB parts). Parts wait in inbox/.parts/<upload>/ in any order, a repeat replacing its twin; the last one
    to arrive puts the file together in inbox/ as /api/inbox would have saved it. Parts of an upload that never
    completes are dropped after a week."""
    if i >= n:
        raise HTTPException(422, "i must be below n")
    parts = INBOX / ".parts"
    parts.mkdir(parents=True, mode=0o700, exist_ok=True)
    for old in parts.iterdir():  # ponytail: a scan per part; only abandoned uploads ever sit here
        if time.time() - old.stat().st_mtime > 7 * 86400:
            shutil.rmtree(old, ignore_errors=True)
    folder = parts / upload
    folder.mkdir(mode=0o700, exist_ok=True)
    tmp = folder / f"{i:05d}.tmp"
    with open(tmp, "wb") as f:
        async for chunk in _chunks(request, MAX_PART):
            f.write(chunk)
    tmp.replace(folder / f"{i:05d}")
    have = sorted(p for p in folder.iterdir() if p.suffix != ".tmp")
    if len(have) < n:
        return {"part": i, "of": n, "have": len(have)}
    path, size = _inbox_path(unquote(request.headers.get("x-filename", ""))), 0
    with open(path, "xb") as out:
        for p in have[:n]:
            size += out.write(p.read_bytes())
    shutil.rmtree(folder, ignore_errors=True)
    return {"saved": path.name, "bytes": size}


@app.post("/api/sync")
def sync_all():
    from . import sync  # imports every broker module; keep the rest of the app usable without them
    with ledger() as conn:
        return sync.sync(conn, config.load(), today())


HOLDINGS_TTL = 60  # s; one Toss read is ~5 calls at 1/s, and the phone may reopen the tab often
_held = {}  # {"at": monotonic, "live": {account_id: holdings}, "errors": {institution: str}, "fetched_at": iso}
_held_lock = threading.Lock()  # one broker read at a time; a caller that waited on it takes that read's result


def _live_holdings():
    from . import sync  # BROKERS; imported late like /api/sync
    cfg, live, errors = config.load(), {}, {}
    for name, mod in sync.BROKERS.items():
        if not hasattr(mod, "holdings"):
            continue  # KB: not configured here and never checked against a live account
        if not all(cfg.get(k) for k in mod.REQUIRED):
            errors[name] = "API 키가 설정되지 않았어요"
            continue
        try:
            live.update((a["account_id"], a) for a in mod.holdings(cfg))
        except Exception as e:  # one broker failing must not hide the others
            log.exception("%s holdings failed", name)
            errors[name] = f"{type(e).__name__}: {e}"[:300]
        else:
            errors[name] = None  # answered: an account it left out is reported per account below
    fetched_at = datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")
    try:
        payers.annotate(cfg, live, errors, today(), fetched_at)
    except Exception:  # the dividend figures are extra: holdings still answer without them
        log.exception("dividend payers failed")
    return {"at": time.monotonic(), "live": live, "errors": errors, "fetched_at": fetched_at}


@app.get("/api/holdings")
def holdings(refresh: bool = False):
    """Every ledger account: live positions where a broker API answers, else the latest snapshot value
    ("source": "snapshot", "error": why, or null for an account with no API). Live positions also carry
    annual_div_krw and div_yield (payers.py). Broker reads are cached HOLDINGS_TTL seconds; ?refresh=1 re-reads them.
    The ledger part is read fresh every time."""
    global _held
    asked = time.monotonic()
    with _held_lock:
        at = _held.get("at", -math.inf)  # a refresh takes a read that ended after it asked: queued ones share it
        if refresh and at < asked or time.monotonic() - at > HOLDINGS_TTL:
            _held = _live_holdings()
        held = _held
    with ledger() as conn:
        accounts = conn.execute("SELECT id, institution, name, kind FROM accounts ORDER BY id").fetchall()
        latest = {r["account_id"]: r for r in conn.execute(  # sqlite: bare columns come from the MAX(date) row
            "SELECT account_id, MAX(date) AS date, value, cash FROM snapshots GROUP BY account_id")}
    out = []
    for a in map(dict, accounts):
        if a["id"] in held["live"]:
            h = held["live"][a["id"]]
            out.append({**a, "source": "live", "error": None, "as_of": held["fetched_at"],
                        "total_krw": h["total_krw"], "positions": h["positions"]})
            continue
        s, inst = latest.get(a["id"]), a["institution"]
        error = held["errors"].get(inst)
        if inst in held["errors"] and error is None:
            error = "증권사 API 응답에 이 계좌가 없어요"
        out.append({**a, "source": "snapshot", "error": error, "as_of": s and s["date"],
                    "total_krw": s and s["value"], "positions": []})
    cfg = config.load()
    dc = latest.get(cfg.get("DC_ACCOUNT"))
    return _json_safe({"fetched_at": held["fetched_at"], "accounts": out,
                       "classes": classes.breakdown(out, cfg, dc and dc["cash"])})
