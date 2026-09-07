#!/usr/bin/env bash
#
# Install corsair-fanctl on a Proxmox VE (or any Debian/Ubuntu) host.
#
#   ./install.sh                  install and start the service
#   ./install.sh --with-liquidctl also set up the USB HID fallback backend
#   ./install.sh --port 9000      listen on a different port
#
set -euo pipefail

PREFIX=/opt/corsair-fanctl
CONFIG_DIR=/etc/corsair-fanctl
CONFIG=$CONFIG_DIR/config.json
UNIT=/etc/systemd/system/corsair-fanctl.service
SERVICE=corsair-fanctl
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PORT=8899
PORT_GIVEN=0
WITH_LIQUIDCTL=0

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m warn\033[0m %s\n' "$*"; }
die()   { printf '\033[1;31merror\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-liquidctl) WITH_LIQUIDCTL=1; shift ;;
    --port) PORT="${2:?--port needs a value}"; PORT_GIVEN=1; shift 2 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "run as root (sudo ./install.sh)"

# ---------------------------------------------------------------- prerequisites

command -v python3 >/dev/null || die "python3 is required (apt install python3)"
python3 - <<'PY' || die "python 3.9 or newer is required"
import sys
sys.exit(0 if sys.version_info >= (3, 9) else 1)
PY
info "python: $(python3 --version)"

command -v systemctl >/dev/null || die "systemd is required"

# ------------------------------------------------------------------- kernel driver

info "checking for the corsair-cpro kernel driver"
if ! lsmod 2>/dev/null | grep -q '^corsair_cpro'; then
  modprobe corsair-cpro 2>/dev/null || warn "could not modprobe corsair-cpro"
fi

HWMON_FOUND=0
for dir in /sys/class/hwmon/hwmon*; do
  [[ -r "$dir/name" ]] || continue
  if [[ "$(cat "$dir/name")" == "corsair-cpro" ]]; then
    info "found Commander Pro at $dir"
    HWMON_FOUND=1
    break
  fi
done

if [[ $HWMON_FOUND -eq 1 ]]; then
  # Make sure the module is back after a reboot.
  echo corsair-cpro > /etc/modules-load.d/corsair-cpro.conf
else
  warn "no corsair-cpro hwmon device found."
  warn "Either the Commander Pro is unplugged, or this kernel lacks the driver."
  warn "The service will keep retrying, and will use liquidctl if installed."
  if [[ $WITH_LIQUIDCTL -eq 0 ]]; then
    warn "Consider re-running with --with-liquidctl for the USB HID fallback."
  fi
fi

# ---------------------------------------------------------------- storage tool

if command -v arcconf >/dev/null; then
  info "found arcconf at $(command -v arcconf) - drive temperatures will be available"
else
  warn "arcconf not found; the Storage panel will stay empty."
  warn "Install the Microsemi/Adaptec CLI if you want SAS drive temperatures,"
  warn "or set storage.enabled=false in the config to hide the panel."
fi

# ------------------------------------------------------------------------ files

info "installing to $PREFIX"
install -d -m 0755 "$PREFIX"
rm -rf "$PREFIX/fanctl" "$PREFIX/web" "$PREFIX/tools"
cp -r "$SOURCE_DIR/fanctl" "$SOURCE_DIR/web" "$SOURCE_DIR/tools" "$PREFIX/"
cp "$SOURCE_DIR/README.md" "$PREFIX/" 2>/dev/null || true
find "$PREFIX" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

install -d -m 0755 "$CONFIG_DIR"

PYTHON=/usr/bin/python3
if [[ $WITH_LIQUIDCTL -eq 1 ]]; then
  info "creating a virtualenv for liquidctl"
  command -v python3 >/dev/null
  python3 -m venv "$PREFIX/venv" || die "python3-venv missing (apt install python3-venv)"
  "$PREFIX/venv/bin/pip" install --quiet --upgrade pip
  "$PREFIX/venv/bin/pip" install --quiet liquidctl || die "failed to install liquidctl"
  PYTHON="$PREFIX/venv/bin/python"
  info "liquidctl: $("$PREFIX/venv/bin/python" -c 'import liquidctl; print(liquidctl.__version__)')"
fi

# ----------------------------------------------------------------------- config

if [[ -f "$CONFIG" ]]; then
  info "keeping the existing config at $CONFIG"
  # An explicit --port must still apply on an upgrade, otherwise the flag would
  # silently do nothing for everyone who already has a config.
  if [[ $PORT_GIVEN -eq 1 ]]; then
    python3 -c "
import json, sys
path, port = sys.argv[1], int(sys.argv[2])
cfg = json.load(open(path))
cfg.setdefault('http', {})['port'] = port
json.dump(cfg, open(path, 'w'), indent=2)
" "$CONFIG" "$PORT"
    info "set the listen port to $PORT"
  fi
else
  info "writing a default config to $CONFIG"
  PYTHONPATH="$PREFIX" "$PYTHON" - "$CONFIG" "$PORT" <<'PY'
import sys
from fanctl import config
cfg = config.default_config()
cfg["http"]["port"] = int(sys.argv[2])
config.save(sys.argv[1], cfg)
PY
  chmod 0640 "$CONFIG"
fi

# ---------------------------------------------------------------------- service

info "installing the systemd unit"
sed "s|__PYTHON__|$PYTHON|" "$SOURCE_DIR/systemd/corsair-fanctl.service" > "$UNIT"
chmod 0644 "$UNIT"

systemctl daemon-reload
systemctl enable "$SERVICE"
# `enable --now` only *starts* a stopped unit, so on an upgrade it would leave
# the old process running against the new files. restart covers both cases.
systemctl restart "$SERVICE"
sleep 2

if systemctl is-active --quiet "$SERVICE"; then
  BIND=$(python3 -c "import json;print(json.load(open('$CONFIG'))['http']['bind'])")
  LISTEN_PORT=$(python3 -c "import json;print(json.load(open('$CONFIG'))['http']['port'])")
  ADDR=$(hostname -I 2>/dev/null | awk '{print $1}')
  [[ -n "$ADDR" ]] || ADDR=$BIND
  RUNNING=$(curl -fsS "http://127.0.0.1:${LISTEN_PORT}/api/state" 2>/dev/null \
            | python3 -c "import json,sys; print(json.load(sys.stdin).get('version','?'))" 2>/dev/null || echo "?")
  info "service is running (version $RUNNING)"
  echo
  echo "  Web UI:  http://${ADDR}:${LISTEN_PORT}/"
  echo "  Config:  $CONFIG"
  echo "  Logs:    journalctl -u $SERVICE -f"
  echo "  Sensors: $PYTHON -m fanctl --config $CONFIG --list-sensors"
  echo
  echo "  Next: open the web UI and assign a temperature source to each fan."
  echo "  Until you do, fans run at the failsafe duty (80%) by design."
else
  warn "the service did not start; recent log:"
  journalctl -u "$SERVICE" -n 25 --no-pager || true
  exit 1
fi
