#!/data/data/com.termux/files/usr/bin/sh
# Termux side of running 개미장부 on an always-on Android phone instead of a PC (README: 안드로이드 폰에서 돌리기).
# Expects the app in the Debian proot at /root/assetlog with a venv at /root/venv. Installs the cron entries
# (sync 23:50 by the phone's clock, the phone copy every 15 minutes) and a boot script that keeps the phone awake for
# Termux and starts sshd and crond after a reboot: in ~/.config/termux/boot for the Google Play Termux (boot support
# built in) and ~/.termux/boot for the F-Droid one (with the Termux:Boot app). Re-running it is safe.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$HOME/bin"
cp "$here/gaemi.sh" "$HOME/bin/gaemi.sh"
chmod 700 "$HOME/bin/gaemi.sh"
{ crontab -l 2>/dev/null | grep -v 'bin/gaemi.sh' || true
  echo "50 23 * * * $HOME/bin/gaemi.sh sync"
  echo "*/15 * * * * $HOME/bin/gaemi.sh cloud"; } | crontab -
for dir in "$HOME/.config/termux/boot" "$HOME/.termux/boot"; do
  mkdir -p "$dir"
  printf '#!/data/data/com.termux/files/usr/bin/sh\ntermux-wake-lock\nsshd\ncrond\n' > "$dir/gaemi"
  chmod 700 "$dir/gaemi"
done
# fallback when a boot is missed: opening the Termux app starts them again
grep -q 'gaemi: keep sshd and crond up' "$HOME/.bashrc" 2>/dev/null ||
  printf '%s\n' 'pgrep -x sshd >/dev/null || sshd; pgrep -x crond >/dev/null || crond  # gaemi: keep sshd and crond up' >> "$HOME/.bashrc"
pgrep -x crond >/dev/null || crond
termux-wake-lock
crontab -l | grep gaemi
echo "F-Droid Termux only: install the Termux:Boot app and open it once so Android lets it run at boot."
