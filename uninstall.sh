#!/usr/bin/env bash
# Remove the Lamplight panel. Never touches Apache, MariaDB, PHP, or phpMyAdmin.
set -euo pipefail

PREFIX="${PREFIX:-/opt/lamplight}"

[[ "$(id -u)" -eq 0 ]] || { echo "uninstall.sh: run this with sudo." >&2; exit 1; }

systemctl disable --now lamplight.service 2>/dev/null || true
rm -f /etc/systemd/system/lamplight.service
systemctl daemon-reload

if [[ "${PURGE:-0}" == "1" ]]; then
  rm -rf "$PREFIX" /var/lib/lamplight /etc/lamplight
  echo "Removed the panel, its data, and its config."
else
  rm -rf "$PREFIX"
  echo "Removed the panel. Kept /var/lib/lamplight and /etc/lamplight."
  echo "Re-run with PURGE=1 to delete those too."
fi

cat <<'EOF'

Left in place, remove them yourself if you want to:
  apt-get remove apache2 mariadb-server php phpmyadmin
  systemctl disable --now lamplight-mailpit
  rm -f /etc/systemd/system/lamplight-mailpit.service /usr/local/bin/mailpit
  rm -rf /var/lib/lamplight-mailpit   # captured mail
EOF
