#!/bin/sh
# From the PC, for an Android phone that runs the sync (README: 안드로이드 폰에서 돌리기). Needs an SSH host alias
# `gaemi-phone` in ~/.ssh/config (or GAEMI_PHONE=<alias>).
#   deploy/phone.sh apply    the phone takes the pushed code: git pull, pip install, cron and boot script
#   deploy/phone.sh backup   keep a copy of the phone's data/ in ~/gaemi-backups/<time>/ (no keys, nothing deleted)
#   deploy/phone.sh timer    run that backup from a systemd user timer: 5 min after boot, then every 12 hours
#   deploy/phone.sh status   the last runs of the phone's sync and phone-copy push
set -eu
host=${GAEMI_PHONE:-gaemi-phone}
# the app's folder as Termux sees it (proot-distro moved the Debian root in a newer version)
termux_root='for d in containers/debian/rootfs installed-rootfs/debian; do
  r=$PREFIX/var/lib/proot-distro/$d/root/assetlog; [ -d "$r" ] && break; done'
case ${1:-} in
  apply)
    ssh "$host" 'proot-distro login debian -- sh -c "cd /root/assetlog && git pull --ff-only -q && git log --oneline -1 &&
      /root/venv/bin/pip install -q -r requirements.txt"' 2>&1 | grep -v 'proot warning'
    ssh "$host" "$termux_root; sh \"\$r/deploy/termux/install.sh\""
    ;;
  backup)
    if ! ssh -o ConnectTimeout=5 "$host" true 2>/dev/null; then
      echo "phone not reachable (not on the same Wi-Fi?): no backup this time"; exit 0
    fi
    dest=${GAEMI_BACKUPS:-$HOME/gaemi-backups}/$(date +%Y%m%d-%H%M)
    mkdir -p "$dest" && chmod 700 "$(dirname "$dest")"
    ssh "$host" 'proot-distro login debian -- sh /root/assetlog/deploy/termux/backup-tar.sh' 2>/dev/null | tar -C "$dest" -xf -
    [ -s "$dest/data/assetlog.db" ] || { echo "backup failed: no ledger in $dest" >&2; exit 1; }
    echo "backup: $dest ($(du -sh "$dest" | cut -f1))"
    ;;
  timer)
    dir=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
    mkdir -p "$dir"
    printf '[Unit]\nDescription=개미장부: copy the phone ledger to this PC\n\n[Service]\nType=oneshot\nExecStart=%s backup\n' \
      "$(cd "$(dirname "$0")" && pwd)/phone.sh" > "$dir/gaemi-phone-backup.service"
    printf '[Unit]\nDescription=개미장부: phone ledger backup\n\n[Timer]\nOnBootSec=5min\nOnUnitActiveSec=12h\n\n[Install]\nWantedBy=timers.target\n' \
      > "$dir/gaemi-phone-backup.timer"
    systemctl --user daemon-reload
    systemctl --user enable --now gaemi-phone-backup.timer
    systemctl --user list-timers gaemi-phone-backup.timer --no-pager
    ;;
  status)
    ssh "$host" 'for f in sync cloud; do echo "-- $f"; grep -E "^== " "$HOME/gaemi-$f.log" 2>/dev/null | tail -4; done
      echo "-- watchdog (revivals and catch-up syncs)"; tail -5 "$HOME/gaemi-tick.log" 2>/dev/null'
    ;;
  *) echo "usage: deploy/phone.sh apply|backup|timer|status" >&2; exit 2 ;;
esac
