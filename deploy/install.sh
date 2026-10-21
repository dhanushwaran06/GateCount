#!/usr/bin/env bash
# Install GateCount as systemd USER services (no sudo). They start at login and restart 3 s after a crash.
#
#   deploy/install.sh entry   # entry PC: counter (live window full screen) + hub/dashboard on port 8000
#   deploy/install.sh exit    # exit PC: counter only (live window), posts to hub_url in counter/config.yaml
#   deploy/install.sh remove  # stop and remove the services
#
# Logs:  journalctl --user -u gatecount-counter -f      journalctl --user -u gatecount-hub -f
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
UNITS=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
role=${1:-}

install_unit() {  # name, args
  sed -e "s|@ROOT@|$ROOT|g" -e "s|@ARGS@|$2|g" "$ROOT/deploy/$1.service" > "$UNITS/gatecount-$1.service"
}

case "$role" in
  entry|exit)
    [ -x "$ROOT/.venv/bin/python" ] || { echo "missing $ROOT/.venv — see README setup"; exit 1; }
    [ -f "$ROOT/counter/config.yaml" ] || { echo "missing counter/config.yaml — copy counter/config.example.yaml"; exit 1; }
    systemctl --user show-environment | grep -q '^DISPLAY=' \
      || echo "WARNING: no DISPLAY in the user session yet; the live window needs a logged-in desktop"
    mkdir -p "$UNITS"
    if [ "$role" = entry ]; then
      [ -f "$ROOT/hub/config.yaml" ] || cp "$ROOT/hub/config.example.yaml" "$ROOT/hub/config.yaml"
      [ -f "$ROOT/hub/.env" ] || { echo "missing hub/.env (device tokens) — see README"; exit 1; }
      install_unit counter "--debug --fullscreen"
      install_unit hub ""
      systemctl --user daemon-reload
      systemctl --user enable --now gatecount-hub gatecount-counter
    else
      install_unit counter "--debug"
      systemctl --user daemon-reload
      systemctl --user enable --now gatecount-counter
    fi
    systemctl --user --no-pager --lines=0 status 'gatecount-*' || true
    ;;
  remove)
    systemctl --user disable --now gatecount-counter gatecount-hub 2>/dev/null || true
    rm -f "$UNITS/gatecount-counter.service" "$UNITS/gatecount-hub.service"
    systemctl --user daemon-reload
    echo "removed"
    ;;
  *) sed -n '2,9p' "$0"; exit 1 ;;
esac
