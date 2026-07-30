#!/usr/bin/env bash
# Run Boring Builder directly (no systemd). Good for a quick try or development.
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

if [[ ! -d .venv ]]; then
  echo "==> Creating virtual environment"
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

# Load .env if present, otherwise use sensible defaults.
if [[ -f .env ]]; then set -a; source .env; set +a; fi
export PORT="${PORT:-5001}"
export HOST="${HOST:-0.0.0.0}"
export ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
export ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"

echo "==> Boring Builder on http://localhost:${PORT}  (admin: ${ADMIN_USERNAME})"
exec .venv/bin/gunicorn --config gunicorn_conf.py wsgi:app
