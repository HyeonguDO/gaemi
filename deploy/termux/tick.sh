#!/data/data/com.termux/files/usr/bin/sh
# Run by Android's JobScheduler about every 15 minutes (install.sh registers it), so it still runs after Android
# has killed Termux's own processes (on the phone this happened around 04:30 every few nights): it revives sshd and
# crond, and runs the day's 23:50 sync when that was missed. ~/gaemi-tick.log keeps only what it had to do.
log=$HOME/gaemi-tick.log
[ -f "$log" ] && [ "$(wc -c < "$log")" -gt 200000 ] && mv "$log" "$log.1"
termux-wake-lock
for p in crond sshd; do
  pgrep -x "$p" >/dev/null || { "$p"; echo "$(date '+%F %T') revived $p" >> "$log"; }
done
# the sync due: today's from 23:50, else yesterday's. data/sync.json's "at" is the last run, failed or not
for d in containers/debian/rootfs installed-rootfs/debian; do
  r=$PREFIX/var/lib/proot-distro/$d/root/assetlog; [ -d "$r" ] && break
done
last=$(sed -n 's/.*"at": "\([0-9-]*\)T.*/\1/p' "$r/data/sync.json" 2>/dev/null)
due=$(date +%F)
[ "$(date +%H%M)" -lt 2350 ] && due=$(date -d yesterday +%F)
if [ "$(echo "${last:-0}" | tr -d -)" -lt "$(echo "$due" | tr -d -)" ]; then  # YYYYMMDD as numbers
  echo "$(date '+%F %T') catch-up sync (last ${last:-none}, due $due)" >> "$log"
  nohup "$HOME/bin/gaemi.sh" sync >/dev/null 2>&1 &  # gaemi.sh's lock skips it if cron's is still running
fi
exit 0
