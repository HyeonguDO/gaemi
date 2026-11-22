"""`python -m assetlog run`: the web app plus, in a background thread, what systemd timers or cron do elsewhere,
so one process (the Docker image's) does everything: the broker sync daily at 23:50 KST, retried every 10 minutes
up to 5 more times while it fails for a transient reason, and a missed one run at start (like Persistent=true);
and the phone copy every 15 minutes when CLOUD_URL is set. Each job reads .env afresh, so keys saved on the setup
page are used from the next run.
"""
import logging
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import cloud, config

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
SYNC_AT = (23, 50)
RETRY = timedelta(minutes=10)
TRIES = 6
CLOUD_EVERY = timedelta(minutes=15)
TICK = 30  # s between checks


def due(now):
    """The day whose 23:50 sync should have run by `now`: today from 23:50, else yesterday."""
    return now.date() if (now.hour, now.minute) >= SYNC_AT else now.date() - timedelta(days=1)


def last_sync():
    """The date of the last sync run, failed or not (data/sync.json's "at"), or None."""
    at = (cloud._read("sync.json", None) or {}).get("at")
    return date.fromisoformat(at[:10]) if at else None


def _sync():
    """True when the run should be retried (a transient failure)."""
    from . import db, sync
    from .__main__ import today
    out = sync.sync(db.connect(), config.load(), today())
    log.info("sync: %s", out)
    return any(sync.TRANSIENT.search(str(v)) for v in out.values())


def _cloud():
    if config.load().get("CLOUD_URL"):
        log.info("cloud: %s", cloud.run(config.load()))


def loop(stop, now=lambda: datetime.now(KST), sleep=time.sleep):
    retry_at, tries, cloud_at = None, 0, now()
    while not stop.is_set():
        t = now()
        last = last_sync()
        if retry_at and t >= retry_at or not retry_at and (last is None or last < due(t)):
            tries += 1
            try:
                again = _sync()
            except Exception:
                log.exception("sync failed")
                again = True
            retry_at = t + RETRY if again and tries < TRIES else None
            if not retry_at:
                tries = 0
        if t >= cloud_at:
            cloud_at = t + CLOUD_EVERY
            try:
                _cloud()
            except Exception:
                log.exception("cloud push failed")
        sleep(TICK)


def run(host, port):
    import uvicorn
    stop = threading.Event()
    threading.Thread(target=loop, args=(stop,), daemon=True, name="scheduler").start()
    try:
        uvicorn.run("assetlog.web:app", host=host, port=port, proxy_headers=False)
    finally:
        stop.set()
