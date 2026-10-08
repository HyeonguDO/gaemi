"""python -m assetlog serve [--host 127.0.0.1] [--port 8000] | sync | cloud | probe {kb,kis,toss} | demo | backfill-fx"""
import argparse
import json
import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo


def today():
    return datetime.now(ZoneInfo("Asia/Seoul")).date()


def main():
    p = argparse.ArgumentParser(prog="assetlog")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")  # this PC only; the phone uses the Cloudflare Worker (cloud/)
    s.add_argument("--port", type=int, default=8000)
    sub.add_parser("sync", help="pull balances and transactions from configured brokers")
    sub.add_parser("cloud", help="apply the phone's writes queued in the Worker, then push the app's data to it")
    pr = sub.add_parser("probe", help="print raw API responses (secrets redacted) to check field mappings")
    pr.add_argument("broker", choices=("kb", "kis", "toss"))
    sub.add_parser("demo", help="write a demo ledger to data/demo.db")
    sub.add_parser("backfill-fx", help="one-off, no API calls: past USD rates and snapshots.rate from the ledger")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO)

    if args.cmd == "serve":
        import uvicorn
        uvicorn.run("assetlog.web:app", host=args.host, port=args.port, proxy_headers=False)  # web.py: the peer address
    elif args.cmd == "demo":
        from . import demo
        demo.main()
    elif args.cmd == "backfill-fx":
        from . import db
        print(json.dumps(db.backfill_fx(db.connect()), ensure_ascii=False, indent=2))
    elif args.cmd == "cloud":
        from . import cloud, config
        print(json.dumps(cloud.run(config.load()), ensure_ascii=False, indent=2))
    else:
        from . import config, db, sync
        if args.cmd == "sync":
            out = sync.sync(db.connect(), config.load(), today())
        else:
            out = sync.BROKERS[args.broker].probe(config.load())
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        if args.cmd == "sync" and any(sync.TRANSIENT.search(str(v)) for v in out.values()):
            sys.exit(75)  # EX_TEMPFAIL: no network; deploy/assetlog-sync.service restarts it later


if __name__ == "__main__":
    main()
