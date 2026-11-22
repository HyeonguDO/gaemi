#!/data/data/com.termux/files/usr/bin/sh
# Termux side of running 개미장부 on an always-on Android phone instead of a PC (README: 안 쓰는 안드로이드 폰을 서버로 쓰기).
# Expects the app in the Debian proot at /root/assetlog with a venv at /root/venv. Installs the cron entries
# (sync 23:50 by the phone's clock, the phone copy every 15 minutes) and a boot script that keeps the phone awake for
# Termux and starts sshd and crond after a reboot: in ~/.config/termux/boot for the Google Play Termux (boot support
# built in) and ~/.termux/boot for the F-Droid one (with the Termux:Boot app). It also registers tick.sh with
# Android's JobScheduler (needs `pkg install termux-api`), which revives them when Android kills Termux's processes.
# Re-running it is safe.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$HOME/bin"
cp "$here/gaemi.sh" "$here/tick.sh" "$HOME/bin/"
chmod 700 "$HOME/bin/gaemi.sh" "$HOME/bin/tick.sh"
{ crontab -l 2>/dev/null | grep -v 'bin/gaemi.sh' || true
  echo "50 23 * * * $HOME/bin/gaemi.sh sync"
  echo "*/15 * * * * $HOME/bin/gaemi.sh cloud"; } | crontab -
for dir in "$HOME/.config/termux/boot" "$HOME/.termux/boot"; do
  mkdir -p "$dir"
  printf '#!/data/data/com.termux/files/usr/bin/sh\ntermux-wake-lock\nsshd\ncrond\n' > "$dir/gaemi"
  chmod 700 "$dir/gaemi"
done
# fallback when a boot is missed: opening the Termux app starts them again
sed -i '/gaemi: keep sshd and crond up/d' "$HOME/.bashrc" 2>/dev/null || true  # an older line without the wake lock
printf '%s\n' 'termux-wake-lock; pgrep -x sshd >/dev/null || sshd; pgrep -x crond >/dev/null || crond; (termux-job-scheduler --pending | grep -q tick.sh || ~/bin/gaemi-schedule) >/dev/null 2>&1 &  # gaemi: keep sshd and crond up' >> "$HOME/.bashrc"
# the watchdog: a force stop cancels it, so the .bashrc line above registers it again when Termux is opened
printf '#!/data/data/com.termux/files/usr/bin/sh\nexec termux-job-scheduler --job-id 7 --script "$HOME/bin/tick.sh" --period-ms 900000 --persisted true --battery-not-low false\n' \
  > "$HOME/bin/gaemi-schedule"
chmod 700 "$HOME/bin/gaemi-schedule"
"$HOME/bin/gaemi-schedule"
pgrep -x crond >/dev/null || crond
termux-wake-lock
crontab -l | grep gaemi
echo "F-Droid Termux only: install the Termux:Boot app and open it once so Android lets it run at boot."
