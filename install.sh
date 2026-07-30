#!/usr/bin/env bash
#
# Install Boring Builder as a systemd service.
#
# Run from the project directory:
#     sudo ./install.sh
#
# Re-running is safe: it rebuilds the venv, refreshes the unit file, and
# restarts the service. Your data (database, secret key) is left untouched.
#
# Runs on port 5001 by default so it can coexist with the chat app on 5000.
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="boring-builder"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ $EUID -ne 0 ]]; then
  echo "This installer needs root. Re-run with: sudo ./install.sh" >&2
  exit 1
fi

RUN_USER="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"
RUN_GROUP="$(id -gn "$RUN_USER")"
DATA_DIR="${DATA_DIR:-$APP_DIR/instance}"

echo "==> App directory : $APP_DIR"
echo "==> Service user  : $RUN_USER:$RUN_GROUP"
echo "==> Data directory: $DATA_DIR"

# --- system dependency: python venv --------------------------------------
if ! python3 -c "import venv" 2>/dev/null; then
  echo "==> Installing python3-venv"
  if command -v apt >/dev/null; then
    apt update -qq && apt install -y python3-venv python3-pip
  fi
fi

# --- ensure the service user owns the app directory ----------------------
CURRENT_OWNER="$(stat -c '%U' "$APP_DIR")"
if [[ "$CURRENT_OWNER" != "$RUN_USER" ]]; then
  echo "==> Fixing ownership: $APP_DIR is owned by '$CURRENT_OWNER', giving it to '$RUN_USER'"
  chown -R "$RUN_USER:$RUN_GROUP" "$APP_DIR"
fi

# --- build the virtual environment (as the service user) -----------------
echo "==> Creating virtual environment"
rm -rf "$APP_DIR/.venv"
sudo -u "$RUN_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$RUN_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$RUN_USER" "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# --- data directory + .env -----------------------------------------------
mkdir -p "$DATA_DIR"
chown -R "$RUN_USER:$RUN_GROUP" "$DATA_DIR"

if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "==> Writing .env with a generated SECRET_KEY"
  SECRET="$(sudo -u "$RUN_USER" "$APP_DIR/.venv/bin/python" -c 'import secrets;print(secrets.token_hex(32))')"

  ADMIN_USER="${ADMIN_USERNAME:-}"
  ADMIN_PASS="${ADMIN_PASSWORD:-}"

  if [[ -z "$ADMIN_USER" || -z "$ADMIN_PASS" ]] && [[ -t 0 ]]; then
    echo
    echo "==> Set up your administrator account"
    if [[ -z "$ADMIN_USER" ]]; then
      read -rp "    Admin username [admin]: " ADMIN_USER
      ADMIN_USER="${ADMIN_USER:-admin}"
    fi
    if [[ -z "$ADMIN_PASS" ]]; then
      while true; do
        read -rsp "    Admin password (min 6 chars): " ADMIN_PASS; echo
        if [[ ${#ADMIN_PASS} -lt 6 ]]; then
          echo "    Password too short, try again."; continue
        fi
        read -rsp "    Confirm password: " ADMIN_PASS2; echo
        [[ "$ADMIN_PASS" == "$ADMIN_PASS2" ]] && break
        echo "    Passwords did not match, try again."
      done
    fi
    echo
  fi

  if [[ -z "$ADMIN_USER" ]]; then ADMIN_USER="admin"; fi
  if [[ -z "$ADMIN_PASS" ]]; then
    ADMIN_PASS="admin"
    echo "!!  No admin password given and no terminal to prompt on."
    echo "!!  Using the default admin/admin — change it immediately after first login."
  fi

  cat > "$APP_DIR/.env" <<ENV
SECRET_KEY=${SECRET}
DATA_DIR=${DATA_DIR}
OLLAMA_HOST=http://localhost:11434
OLLAMA_TIMEOUT=600
HOST=0.0.0.0
PORT=5001
# Where finished builds are written (blank => a folder inside DATA_DIR).
BUILDER_OUTPUT_DIR=
# Default builder model (blank => pick one in the UI). Example: builder-web
BUILDER_MODEL=
# Bootstrap admin, applied on first run only (when no users exist yet).
ADMIN_USERNAME=${ADMIN_USER}
ADMIN_PASSWORD=${ADMIN_PASS}
ENV
  chown "$RUN_USER:$RUN_GROUP" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo "==> Admin account '${ADMIN_USER}' will be created on first launch."
else
  echo "==> Keeping existing .env"
  echo "    (To change the admin password on an existing install, run:"
  echo "     sudo -u $RUN_USER $APP_DIR/.venv/bin/python $APP_DIR/manage.py set-password <user> <newpass>)"
fi

# --- render + install the systemd unit -----------------------------------
echo "==> Installing systemd unit at $UNIT_PATH"
sed -e "s|__USER__|$RUN_USER|g" \
    -e "s|__GROUP__|$RUN_GROUP|g" \
    -e "s|__APP_DIR__|$APP_DIR|g" \
    -e "s|__DATA_DIR__|$DATA_DIR|g" \
    "$APP_DIR/deploy/boring-builder.service" > "$UNIT_PATH"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

sleep 2
echo
systemctl --no-pager --full status "$SERVICE_NAME" | head -n 12 || true
echo
echo "==> Done. Boring Builder is running on http://localhost:$(grep -E '^PORT=' "$APP_DIR/.env" | cut -d= -f2 || echo 5001)"
echo "    Logs:    journalctl -u $SERVICE_NAME -f"
echo "    Restart: sudo systemctl restart $SERVICE_NAME"
echo "    Stop:    sudo systemctl stop $SERVICE_NAME"
