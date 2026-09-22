#!/usr/bin/env bash
# Install Lamplight as a systemd service bound to the loopback interface.
set -euo pipefail

PREFIX="${PREFIX:-/opt/lamplight}"
HOST="${LAMPLIGHT_HOST:-127.0.0.1}"
PORT="${LAMPLIGHT_PORT:-3847}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "install.sh: $*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "run this with sudo."
[[ -f "$SRC/pyproject.toml" ]] || die "run this from a Lamplight checkout."

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  case " ${ID:-} ${ID_LIKE:-} " in
    *" debian "*|*" ubuntu "*) ;;
    *) echo "Warning: Lamplight targets Debian and Ubuntu. Detected ${PRETTY_NAME:-unknown}." >&2 ;;
  esac
fi

export DEBIAN_FRONTEND=noninteractive
echo "==> Installing Python prerequisites"
apt-get update
apt-get install -y --no-install-recommends python3 python3-venv

echo "==> Copying Lamplight to $PREFIX"
mkdir -p "$PREFIX" /var/lib/lamplight /etc/lamplight
if [[ "$SRC" != "$PREFIX" ]]; then
  find "$PREFIX" -mindepth 1 -maxdepth 1 ! -name .venv -exec rm -rf {} +
  tar -C "$SRC" --exclude=.git --exclude=.venv -cf - . | tar -C "$PREFIX" -xf -
fi

echo "==> Building the virtualenv"
rm -rf "$PREFIX/.venv"
python3 -m venv "$PREFIX/.venv"
"$PREFIX/.venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/.venv/bin/pip" install --quiet "$PREFIX"

echo "==> Writing configuration"
umask 077
cat >/etc/lamplight/lamplight.env <<EOF
LAMPLIGHT_HOST=${HOST}
LAMPLIGHT_PORT=${PORT}
LAMPLIGHT_DATA=/var/lib/lamplight
LAMPLIGHT_CONFIG=/etc/lamplight
EOF
umask 022

sed -e "s|@PREFIX@|${PREFIX}|g" -e "s|@HOST@|${HOST}|g" -e "s|@PORT@|${PORT}|g" \
  "$PREFIX/systemd/lamplight.service" >/etc/systemd/system/lamplight.service

echo "==> Starting the service"
systemctl daemon-reload
systemctl enable lamplight.service
# restart, not `enable --now`: on an upgrade the unit is already active, and
# `start` would leave the old process serving the old code.
systemctl restart lamplight.service
systemctl is-active --quiet lamplight.service ||
  die "the service did not start. Check: journalctl -u lamplight -n 40 --no-pager"

TOKEN="$("$PREFIX/.venv/bin/lamplight-token")"
cat <<EOF

Lamplight is running. Open this on this machine:

  http://${HOST}:${PORT}/?token=${TOKEN}

  Token again:   sudo ${PREFIX}/.venv/bin/lamplight-token
  Logs:          journalctl -u lamplight -f
  Change port:   edit /etc/lamplight/lamplight.env, then systemctl restart lamplight

Apache, MariaDB, PHP, phpMyAdmin, and Mailpit are NOT installed yet.
Install them one at a time from the panel.
EOF
