#!/data/data/com.termux/files/usr/bin/sh
# Runs `python3 -m assetlog sync|cloud` inside the Debian proot (proot-distro) for Termux's cron, doing what the
# systemd units do on a PC: wait for DNS, retry a sync that failed for a transient reason (exit 75) every 10 minutes
# up to 5 more times, and keep a log in the Termux home (~/gaemi-<cmd>.log, rotated at 1 MB).
cmd=${1:?usage: gaemi.sh sync|cloud}
case $cmd in
  sync) host=openapi.koreainvestment.com tries=6 ;;
  cloud) host=cloudflare.com tries=1 ;;
  *) echo "usage: gaemi.sh sync|cloud" >&2; exit 2 ;;
esac
log=$HOME/gaemi-$cmd.log
[ -f "$log" ] && [ "$(wc -c < "$log")" -gt 1000000 ] && mv "$log" "$log.1"
exec >> "$log" 2>&1
i=1
while :; do
  echo "== $(date '+%F %T') $cmd (try $i)"
  proot-distro login debian -- sh -c "for n in \$(seq 60); do getent hosts $host >/dev/null && break; sleep 2; done
    cd /root/assetlog && exec /root/venv/bin/python -m assetlog $cmd"
  rc=$?
  { [ "$rc" -ne 75 ] || [ "$i" -ge "$tries" ]; } && break
  i=$((i + 1))
  sleep 600
done
echo "== exit $rc"
exit "$rc"
