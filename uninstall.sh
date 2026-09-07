#!/usr/bin/env bash
#
# Remove corsair-fanctl.  The config is kept unless --purge is given.
#
#   ./uninstall.sh            remove the service and program files
#   ./uninstall.sh --purge    also delete /etc/corsair-fanctl
#
set -euo pipefail

PREFIX=/opt/corsair-fanctl
CONFIG_DIR=/etc/corsair-fanctl
UNIT=/etc/systemd/system/corsair-fanctl.service
SERVICE=corsair-fanctl
PURGE=0

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

[[ "${1:-}" == "--purge" ]] && PURGE=1
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE}.service"; then
  # Stopping the service applies the failsafe duty, so the fans are left in a
  # safe state rather than wherever the last curve happened to put them.
  info "stopping $SERVICE (fans go to the failsafe duty)"
  systemctl disable --now "$SERVICE" || true
fi

info "removing $UNIT"
rm -f "$UNIT"
systemctl daemon-reload

info "removing $PREFIX"
rm -rf "$PREFIX"
rm -f /etc/modules-load.d/corsair-cpro.conf

if [[ $PURGE -eq 1 ]]; then
  info "removing $CONFIG_DIR"
  rm -rf "$CONFIG_DIR"
else
  info "keeping your config at $CONFIG_DIR (use --purge to delete it)"
fi

echo
echo "Done. The fans are now at the failsafe duty and stay there until"
echo "something else takes over -- the motherboard does not control these"
echo "channels, the Commander Pro does."
