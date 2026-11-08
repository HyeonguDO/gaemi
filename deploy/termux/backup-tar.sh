#!/bin/sh
# Runs inside the phone's Debian proot (deploy/phone.sh backup calls it over SSH): writes a tar of data/ to stdout
# for the PC to keep, with a consistent copy of the ledger (sqlite's backup API, safe while a sync writes) and
# without data/tokens.json (a live broker token). .env is never included: the keys stay on the phone.
set -eu
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/data"
cd /root/assetlog
/root/venv/bin/python -c "import sqlite3, sys; d = sqlite3.connect(sys.argv[2]); sqlite3.connect(sys.argv[1]).backup(d); d.close()" \
  data/assetlog.db "$tmp/data/assetlog.db"
for f in data/*.json; do [ "$f" = data/tokens.json ] || cp -p "$f" "$tmp/data/"; done
tar -C "$tmp" -cf - data
