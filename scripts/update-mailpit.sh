#!/usr/bin/env bash
# Print the pin block for a Mailpit release so lamplight/installer.py can be
# updated by hand. Usage: scripts/update-mailpit.sh [vX.Y.Z]   (default: latest)
set -euo pipefail

VERSION="${1:-}"
if [[ -z "$VERSION" ]]; then
  VERSION="$(curl -fsSL https://api.github.com/repos/axllent/mailpit/releases/latest |
    sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)"
fi
[[ -n "$VERSION" ]] || { echo "could not determine a version" >&2; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "MAILPIT_VERSION = \"${VERSION}\""
echo "MAILPIT_SHA256 = {"
for arch in amd64 arm64; do
  url="https://github.com/axllent/mailpit/releases/download/${VERSION}/mailpit-linux-${arch}.tar.gz"
  curl -fsSL -o "$tmp/$arch.tar.gz" "$url"
  echo "    \"${arch}\": \"$(sha256sum "$tmp/$arch.tar.gz" | cut -d' ' -f1)\","
done
echo "}"
