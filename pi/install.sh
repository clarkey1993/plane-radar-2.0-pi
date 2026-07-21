#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_ROOT=/opt/plane-radar
VERSION="$(sed -n 's/^__version__ = "\([^"]*\)"/\1/p' "$SOURCE_DIR/plane_radar/__init__.py")"

if [[ -z "$VERSION" ]]; then
  echo "Could not determine Plane Radar version" >&2
  exit 1
fi

if [[ $EUID -ne 0 ]]; then
  echo "Run this installer with sudo" >&2
  exit 1
fi

install -d "$INSTALL_ROOT/releases/$VERSION" /etc/plane-radar
cp -a "$SOURCE_DIR/plane_radar" "$INSTALL_ROOT/releases/$VERSION/"
ln -sfn "$INSTALL_ROOT/releases/$VERSION" "$INSTALL_ROOT/current.new"
mv -Tf "$INSTALL_ROOT/current.new" "$INSTALL_ROOT/current"

if [[ ! -f /etc/plane-radar/config.json ]]; then
  install -m 0644 "$SOURCE_DIR/config.example.json" /etc/plane-radar/config.json
fi

install -m 0644 "$SOURCE_DIR/plane-radar.service" /etc/systemd/system/plane-radar.service
install -m 0644 "$SOURCE_DIR/plane-radar-update.service" /etc/systemd/system/plane-radar-update.service
install -m 0644 "$SOURCE_DIR/plane-radar-update.timer" /etc/systemd/system/plane-radar-update.timer
systemctl daemon-reload
systemctl enable plane-radar.service plane-radar-update.timer
systemctl restart plane-radar.service
systemctl start plane-radar-update.timer

echo "Plane Radar $VERSION installed"
