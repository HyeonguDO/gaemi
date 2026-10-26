"""The PC half of the phone's cloud copy: `python3 -m assetlog cloud`, every 15 min from deploy/assetlog-cloud.timer.

A Cloudflare Worker (cloud/) serves the same page from workers.dev behind Cloudflare Access. It answers reads from
what this PC last pushed and queues the phone's writes as ops. run(cfg) does nothing without CLOUD_URL; otherwise
(1) it pulls the queued ops and applies each to this app in-process, as the request the page would have sent the PC
(method, path, query, body, Content-Type, X-Filename, Origin http://localhost), so every check and side effect of
that route applies; then (2) it PUTs what the page reads to the Worker, one key per request (PUT /pc/state/<key>),
so the Worker's cost per push stays flat as the ledger grows: it stores the body text as given and never parses it.
transactions go as one key per month of their date (transactions-YYYY-MM), each in the app's order (date DESC, id DESC),
which the Worker's reads put back together.

- An op is applied at most once: data/cloud.json (0600) records its id before the request is made, so a run killed
  mid-request reports that op as an error instead of risking booking it twice. Each result stays in that file until
  the Worker took it; a report lost with the connection goes out with the next run.
- Only the writes the Worker queues are applied (WRITES); anything else in the queue is reported as an error.
- A 2xx /api/sync can still carry a failed broker in its per-broker report (sync.sync); that is reported as an
  error too, or the phone would never learn the sync failed.
- holdings is the one read that makes a live broker call, so it is pushed at most once an hour (data/cloud.json
  remembers when), sooner if this run applied an op or holdings have never been pushed. That keeps live broker
  reads to about 24 a day instead of one per 15-minute run (96).
- A key is PUT only when its JSON changed since the last PUT the Worker took (a sha256 per key in data/cloud.json),
  which saves ~470 identical PUTs (~24 MB) a day. summary goes every run anyway: its time is the phone's
  "PC 동기화 기준" (the Worker's /api/meta pushed_at). Deleting data/cloud.json forces a full push.
- A month whose rows are all gone is PUT as [] once, then dropped from data/cloud.json's sha256s (the current month
  only once it is over: an empty ledger still needs one month key, or the Worker answers "not pushed yet"). Only that
  record names the months pushed: a month emptied while the file is missing stays on the Worker.
- A recreated D1 database restarts op ids at 1 and starts empty: delete data/cloud.json then, or its first ops are
  taken as done and unchanged keys are never pushed to it.
- CLOUD_URL must be https unless its host is localhost, 127.0.0.1 or ::1 (checked before any secret goes into a
  header): a typo'd http:// would otherwise send X-Assetlog-Key and the CF-Access-Client-Secret in the clear before
  the redirect is refused.
- Secrets go only in headers (X-Assetlog-Key, CF-Access-Client-Id/Secret, the latter only when set) and are never
  printed. Measured 2026-09-26 on the real ledger: 579 transactions = 253 KB of JSON over 16 months (the
  biggest 43 KB), every other item under 5 KB. As one key, that PUT cost the Worker 3-9 ms of its 10 ms CPU per
  request, hence the months.
"""
import base64
import fcntl
import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests

from . import config

TIMEOUT = (10, 60)  # s: connect, read (an answer can carry ~2 MB of uploads)
MAX_ITEM = 1_900_000  # bytes of JSON the Worker takes per state item (a D1 row tops out near 2 MB)
HOLDINGS_PERIOD = 3600  # s between holdings pushes, absent a reason to push it sooner
WRITES = re.compile(r"POST /api/(transactions|snapshots|accounts|import|sync|inbox/part|gold)|(PATCH|DELETE) /api/transactions/\d+"
                    r"|DELETE /api/snapshots|DELETE /api/accounts/[\w.:@%-]+")  # the writes web.py has, as the Worker queues them
READS = {"summary": "/api/summary", "accounts": "/api/accounts", "holdings": "/api/holdings",
         "transactions": "/api/transactions?limit=100000", "snapshots": "/api/snapshots?limit=5000",
         "formats": "/api/import/formats"}  # each its own state key, but transactions: one per month (MONTH)
MONTH = "transactions-"  # + YYYY-MM: the key of a month of transactions
INTERRUPTED = {"status": "error", "result": "PC가 이 요청을 반영하다 멈췄어요. 반영됐는지 확인하고 필요하면 다시 입력해 주세요"}


def _write(name, obj):
    """Owner-only (0600) JSON in data/, written atomically."""
    config.DATA.mkdir(parents=True, exist_ok=True)
    tmp = config.DATA / f"{name}.{os.getpid()}.tmp"  # the serve process and the cloud timer may write one name at once
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, config.DATA / name)


def _read(name, default):
    try:
        return json.loads((config.DATA / name).read_text())
    except (FileNotFoundError, ValueError):
        return default


def _require_https(url):
    """A plaintext CLOUD_URL would send X-Assetlog-Key and the CF-Access-Client-Secret in the clear before a
    redirect (allow_redirects=False) is even followed. Local `wrangler dev` is the one allowed exception."""
    u = urlsplit(url)
    if u.scheme != "https" and u.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError(f"CLOUD_URL must be https (got {url!r})")


def run(cfg):
    """Apply the phone's queued writes, then push the page's reads. Returns a small report. Raises when the Worker
    can't be reached or refuses (Access, X-Assetlog-Key), keeping what was already done."""
    url = cfg.get("CLOUD_URL", "").rstrip("/")
    if not url:
        return {"skipped": "CLOUD_URL is not set"}
    _require_https(url)
    s = requests.Session()
    s.headers["X-Assetlog-Key"] = cfg.get("CLOUD_PC_KEY", "")
    s.headers.update({h: cfg[k] for h, k in (("CF-Access-Client-Id", "CF_ACCESS_CLIENT_ID"),
                                              ("CF-Access-Client-Secret", "CF_ACCESS_CLIENT_SECRET")) if cfg.get(k)})
    config.DATA.mkdir(parents=True, exist_ok=True)
    with open(config.DATA / "cloud.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # the timer and a manual run would pull the same ops
        with _app() as app:
            counts = _apply_queue(s, url, app)
            return {**counts, **_push(s, url, app, counts["applied"])}


@contextmanager
def _app():
    """This app in-process, reached as the page on this PC reaches it: a loopback client on http://localhost."""
    from fastapi.testclient import TestClient
    from . import web
    with TestClient(web.app, base_url="http://localhost", client=("127.0.0.1", 0),
                    raise_server_exceptions=False) as app:
        yield app


def _apply_queue(s, url, app):
    state = _read("cloud.json", {"after": 0, "unreported": {}})
    counts = {"applied": 0, "errors": 0}
    while True:
        _report(s, url, state)
        ops = [op for op in _call(s, "GET", f"{url}/pc/ops", params={"after": state["after"]})["ops"]
               if op["id"] > state["after"]]  # never re-apply, whatever the Worker sends
        if not ops:
            return counts
        for op in ops:
            key = str(op["id"])
            state["after"], state["unreported"][key] = op["id"], INTERRUPTED
            _write("cloud.json", state)
            state["unreported"][key] = result = _apply(app, op)
            _write("cloud.json", state)
            counts["applied" if result["status"] == "done" else "errors"] += 1
            _report(s, url, state)


def _report(s, url, state):
    """Hand the Worker each kept result; it leaves data/cloud.json once the Worker took it."""
    for key, result in list(state["unreported"].items()):
        _call(s, "POST", f"{url}/pc/ops/{key}", json=result)
        del state["unreported"][key]
        _write("cloud.json", state)


def _apply(app, op):
    """{"status": "done"} for a 2xx answer, else {"status": "error", "result": what the page would have shown}.
    A 2xx /api/sync can still carry a failed broker in its per-broker report; that counts as an error too, or the
    phone would never learn the sync failed."""
    if not WRITES.fullmatch(f"{op.get('method')} {op.get('path')}"):
        return {"status": "error", "result": f"PC가 받지 않는 요청이에요: {op.get('method')} {op.get('path')}"[:1000]}
    headers = {"Origin": "http://localhost"}
    headers.update({h: op[k] for h, k in (("Content-Type", "content_type"), ("X-Filename", "filename")) if op.get(k)})
    query = (op.get("query") or "").lstrip("?")
    try:
        r = app.request(op["method"], op["path"] + ("?" + query if query else ""), headers=headers,
                        content=base64.b64decode(op.get("body_b64") or ""))
    except Exception as e:  # a malformed op (bad base64, a non-latin-1 header) must not hold up the ones after it
        return {"status": "error", "result": f"{type(e).__name__}: {e}"[:1000]}
    if not r.is_success:
        return {"status": "error", "result": _detail(r)[:1000]}
    if op.get("path") == "/api/sync":
        failed = [f"{k}: {v}" for k, v in (r.json() or {}).items() if isinstance(v, str) and v.startswith("error")]
        if failed:
            return {"status": "error", "result": " | ".join(failed)[:1000]}
    return {"status": "done"}


def _detail(r):
    """The error text index.html's api() shows for this answer: detail is a string, a validation list or
    {code, message, ...}."""
    try:
        d = r.json()["detail"]
    except (ValueError, KeyError, TypeError):
        d = None
    if isinstance(d, list):
        return ", ".join(f"{(e.get('loc') or [''])[-1]}: {e.get('msg')}" for e in d if isinstance(e, dict))
    if isinstance(d, dict):
        d = d.get("message")
    return d if isinstance(d, str) and d else f"{r.status_code} {r.reason_phrase}"


def _fit(rows, max_bytes):
    """Shrink a list (a month of transactions, or snapshots; newest first) to fit `max_bytes` of JSON: drop every
    row's `raw` first (only transactions carry one), then the oldest rows (the list end) until it fits.
    (kept, rows_dropped)."""
    def nbytes(xs):
        return len(json.dumps(xs, ensure_ascii=False).encode())

    if nbytes(rows) <= max_bytes:
        return rows, 0
    if rows and "raw" in rows[0]:
        rows = [{**r, "raw": None} for r in rows]
    dropped = 0
    while rows and nbytes(rows) > max_bytes:
        # ponytail: one row (and one re-serialize) per iteration, O(n^2) worst case; this only runs for a month
        # (or snapshots) over MAX_ITEM, ~45 times today's biggest month, and it's this 15-minute background job,
        # not the Worker's CPU-limited request
        rows = rows[:-1]
        dropped += 1
    return rows, dropped


def _push(s, url, app, applied):
    """PUT the page's reads to the Worker, one key per request (the Worker's cost must not grow with the ledger),
    transactions one key per month. holdings is skipped most runs (see HOLDINGS_PERIOD in the module docstring);
    every other read is made every run and each key PUT when it changed (summary always). A month of transactions,
    or snapshots, still over MAX_ITEM after dropping `raw` loses its oldest rows until it fits, reported as left_out;
    anything else oversized, or that fails to read, is left out whole."""
    pushed_at = datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")
    state = _read("cloud.json", {"after": 0, "unreported": {}})
    sent = state.setdefault("sent", {})  # key: sha256 of the body the Worker last took
    push_holdings = applied > 0 or not state.get("holdings_at") or time.time() - state["holdings_at"] >= HOLDINGS_PERIOD
    pushed, unchanged, left_out = [], [], {}
    for name, path in READS.items():
        if name == "holdings" and not push_holdings:
            continue
        r = app.get(path)
        if not r.is_success:
            left_out[name] = _detail(r)
            continue
        items = {name: r.json()}
        if name == "transactions":  # split by month, keeping the order; a month sent before and now rowless goes as []
            rows, items = items[name], {k: [] for k in sent if k.startswith(MONTH)}
            if not rows:  # an empty ledger still gets a month key, this one: with none the Worker answers "not pushed"
                items[MONTH + pushed_at[:7]] = []
            for row in rows:
                row["raw"] = row["raw"] and row["raw"][:2000]
                items.setdefault(MONTH + row["date"][:7], []).append(row)
        for key, body in items.items():
            if isinstance(body, list):
                body, dropped = _fit(body, MAX_ITEM)
                if dropped:
                    left_out[key] = f"{dropped} oldest rows left out to fit {MAX_ITEM:,} bytes"
            elif len(json.dumps(body, ensure_ascii=False).encode()) > MAX_ITEM:
                left_out[key] = f"over {MAX_ITEM:,} bytes"
                continue
            data = json.dumps(body, ensure_ascii=False).encode()
            # the date in the hash: every key goes once a day anyway, so a Worker that lost or changed state heals
            digest = hashlib.sha256(pushed_at[:10].encode() + data).hexdigest()
            if key != "summary" and sent.get(key) == digest:
                unchanged.append(key)
            else:
                _call(s, "PUT", f"{url}/pc/state/{key}",
                      headers={"Content-Type": "application/json", "X-Pushed-At": pushed_at}, data=data)
                # only once the Worker took it: a failed PUT raises and goes again next run
                # no row left in the ledger (not just shrunk by _fit), and not the month an empty ledger keeps
                if key.startswith(MONTH) and not items[key] and key != MONTH + pushed_at[:7]:
                    del sent[key]  # emptied on the Worker: nothing left to send for that month
                else:
                    sent[key] = digest
                pushed.append(key)
            if key == "holdings":
                state["holdings_at"] = time.time()
            _write("cloud.json", state)
    return {"pushed_at": pushed_at, "pushed": _brief(pushed), "unchanged": _brief(unchanged), "left_out": left_out}


def _brief(keys):
    """Sorted, with the month keys counted rather than listed (there is one per month of the ledger)."""
    months = sum(k.startswith(MONTH) for k in keys)
    return sorted(k for k in keys if not k.startswith(MONTH)) + ([f"{MONTH}* ({months})"] if months else [])


def _call(s, method, url, **kw):
    """The Worker's JSON answer; raises on anything but 2xx. Redirects are not followed: an Access login page must not
    pass for an answer."""
    r = s.request(method, url, timeout=TIMEOUT, allow_redirects=False, **kw)
    if not 200 <= r.status_code < 300:
        raise RuntimeError(f"{method} {urlsplit(url).path}: HTTP {r.status_code} {r.text[:200]}")
    return r.json()
