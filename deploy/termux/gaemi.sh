#!/data/data/com.termux/files/usr/bin/sh
# Runs `python3 -m assetlog sync|cloud` inside the Debian proot (proot-distro) for Termux's cron, doing what the
# systemd units do on a PC: wait for DNS, retry a sync that failed for a transient reason (exit 75) every 10 minutes
# up to 5 more times, keep a log in the Termux home (~/gaemi-<cmd>.log, rotated at 1 MB), one run at a time.
cmd=${1:?usage: gaemi.sh sync|cloud}
case $cmd in
  sync) host=openapi.koreainvestment.com tries=6 ;;
  cloud) host=cloudflare.com tries=1 ;;
  *) echo "usage: gaemi.sh sync|cloud" >&2; exit 2 ;;
esac
log=$HOME/gaemi-$cmd.log
[ -f "$log" ] && [ "$(wc -c < "$log")" -gt 1000000 ] && mv "$log" "$log.1"
exec >> "$log" 2>&1
# one run per command at a time: cron's and the catch-up from tick.sh may meet
lock=$HOME/.gaemi-$cmd.lock
if ! mkdir "$lock" 2>/dev/null; then
  pid=$(cat "$lock/pid" 2>/dev/null)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then echo "== $(date '+%F %T') $cmd already running ($pid)"; exit 0; fi
  rm -rf "$lock"; mkdir "$lock" || exit 0  # left by a run Android killed
fi
echo $$ > "$lock/pid"
trap 'rm -rf "$lock"' EXIT
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
