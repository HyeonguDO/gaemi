#!/bin/sh
# Installs 개미장부's systemd user timers for this checkout: the daily broker sync (23:50 KST) and, when CLOUD_URL is
# set in .env, the 15-minute exchange with the Cloudflare Worker. Re-running it is safe (it rewrites the units).
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DEST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$DEST"
for f in assetlog-sync.service assetlog-sync.timer assetlog-cloud.service assetlog-cloud.timer; do
  sed "s#@ROOT@#$ROOT#g" "$ROOT/deploy/$f" > "$DEST/$f"
done
systemctl --user daemon-reload
systemctl --user enable --now assetlog-sync.timer
if grep -q '^CLOUD_URL=..*' "$ROOT/.env" 2>/dev/null; then
  systemctl --user enable --now assetlog-cloud.timer
else
  echo "CLOUD_URL is empty in .env: the cloud timer is not enabled (the phone copy is optional)"
fi
# Without linger, user timers run only while you are logged in.
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  echo "To run the timers without a login: sudo loginctl enable-linger $USER"
fi
systemctl --user list-timers --all | grep assetlog || true
