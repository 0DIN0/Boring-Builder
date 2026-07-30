#!/usr/bin/env bash
# Remove the Boring Builder systemd service. Leaves your app folder and data.
set -euo pipefail
SERVICE_NAME="boring-builder"
if [[ $EUID -ne 0 ]]; then echo "Run with sudo." >&2; exit 1; fi
systemctl stop "$SERVICE_NAME" 2>/dev/null || true
systemctl disable "$SERVICE_NAME" 2>/dev/null || true
rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
echo "==> Removed the $SERVICE_NAME service. Your files and data are untouched."
echo "    To also delete data: remove the 'instance/' folder in the app directory."
