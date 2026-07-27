#!/usr/bin/env bash
set -euo pipefail

# Creates/updates a systemd service that runs AISMM (dashboard + scheduler) under
# Gunicorn. Safe to re-run: an existing service is updated and restarted so it
# picks up the latest code.
#
#   sudo ./setup_service.sh
#   sudo SERVICE_NAME=aismm BIND_ADDR=0.0.0.0:8787 ./setup_service.sh
#   sudo SKIP_INSTALL=1 ./setup_service.sh      # don't touch the venv / pip
#
# ONE worker, on purpose: the APScheduler instance must live in the same process
# as the dashboard, because the dashboard re-syncs jobs in-process when you add
# or edit an instruction. Scale with --threads, not workers. See aismm/wsgi.py.

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Please run as root (e.g., sudo SERVICE_USER=$(whoami) ./setup_service.sh)" >&2
  exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl not found — this script targets a systemd Linux server." >&2
  echo "On macOS run AISMM directly instead:  python -m aismm.cli run" >&2
  exit 1
fi

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="${SERVICE_NAME:-aismm}"
# Default to the invoking sudo user when available; otherwise current user.
DEFAULT_USER="${SUDO_USER:-$(whoami)}"
SERVICE_USER="${SERVICE_USER:-$DEFAULT_USER}"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  echo "User '$SERVICE_USER' does not exist. Pass SERVICE_USER=<existing-user>." >&2
  exit 1
fi
SERVICE_GROUP="${SERVICE_GROUP:-$(id -gn "$SERVICE_USER")}"
WORKDIR="${WORKDIR:-$PROJECT_ROOT}"
VENV_PATH="${VENV_PATH:-$PROJECT_ROOT/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
GUNICORN_BIN="${GUNICORN_BIN:-$VENV_PATH/bin/gunicorn}"
BIND_ADDR="${BIND_ADDR:-0.0.0.0:8787}"
# Agent runs and video uploads are slow; don't let gunicorn kill a publish.
TIMEOUT="${TIMEOUT:-1800}"
THREADS="${THREADS:-8}"
SKIP_INSTALL="${SKIP_INSTALL:-0}"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

SERVICE_EXISTS=false
if systemctl list-unit-files | grep -q "^${SERVICE_NAME}.service"; then
  SERVICE_EXISTS=true
fi

run_as_service_user() {
  sudo -u "$SERVICE_USER" -H bash -lc "$1"
}

# --- venv + dependencies ----------------------------------------------------- #
if [[ "$SKIP_INSTALL" != "1" ]]; then
  if [[ ! -x "$VENV_PATH/bin/python" ]]; then
    echo "Creating virtualenv at $VENV_PATH ..."
    run_as_service_user "$PYTHON_BIN -m venv '$VENV_PATH'"
  fi
  echo "Installing dependencies (requirements.txt + gunicorn) ..."
  run_as_service_user "'$VENV_PATH/bin/pip' install --upgrade pip >/dev/null"
  run_as_service_user "'$VENV_PATH/bin/pip' install -r '$PROJECT_ROOT/requirements.txt'"
  run_as_service_user "'$VENV_PATH/bin/pip' install 'gunicorn>=21.2.0'"
fi

if [[ ! -x "$GUNICORN_BIN" ]]; then
  echo "Error: $GUNICORN_BIN not found. Install it first:" >&2
  echo "  $VENV_PATH/bin/pip install -r requirements.txt gunicorn" >&2
  echo "(or re-run without SKIP_INSTALL=1)" >&2
  exit 1
fi

# --- .env + data dir --------------------------------------------------------- #
if [[ ! -f "$PROJECT_ROOT/.env" ]]; then
  if [[ -f "$PROJECT_ROOT/.env.example" ]]; then
    install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 600 \
      "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
    echo "Created $PROJECT_ROOT/.env from .env.example." >&2
    echo "Fill in your Azure OpenAI creds + DASHBOARD_BASE_URL, then re-run this script." >&2
    exit 1
  else
    echo "Warning: no .env found at $PROJECT_ROOT/.env" >&2
  fi
fi

DATA_DIR="$PROJECT_ROOT/data"
mkdir -p "$DATA_DIR/assets"
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$DATA_DIR"
if [[ -f "$PROJECT_ROOT/tokens.key" ]]; then
  chown "$SERVICE_USER:$SERVICE_GROUP" "$PROJECT_ROOT/tokens.key"
  chmod 600 "$PROJECT_ROOT/tokens.key"
fi

# Instagram fetches media from a PUBLIC url, and every OAuth callback points at
# DASHBOARD_BASE_URL — a localhost value will break both on a server.
if grep -qE '^DASHBOARD_BASE_URL=.*(127\.0\.0\.1|localhost)' "$PROJECT_ROOT/.env" 2>/dev/null; then
  echo "Warning: DASHBOARD_BASE_URL in .env still points at localhost." >&2
  echo "         Set it to the public https URL of this server, or Instagram publishing" >&2
  echo "         and the OAuth callbacks will fail." >&2
fi

# --- systemd unit ------------------------------------------------------------ #
cat > "$UNIT_PATH" <<EOF
[Unit]
Description=AISMM — AI Social Media Manager (dashboard + scheduler, Gunicorn)
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${WORKDIR}
Environment="PATH=${VENV_PATH}/bin:/usr/local/bin:/usr/bin:/bin"
Environment="PYTHONUNBUFFERED=1"
Environment="AISMM_ENABLE_SCHEDULER=1"
ExecStart=${GUNICORN_BIN} --capture-output --log-level info \\
  --workers 1 --threads ${THREADS} --timeout ${TIMEOUT} --graceful-timeout 60 \\
  -b ${BIND_ADDR} 'aismm.wsgi:application'
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"

if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
  systemctl restart "${SERVICE_NAME}.service"
else
  systemctl start "${SERVICE_NAME}.service"
fi

if $SERVICE_EXISTS; then
  echo "Service existed; unit updated and restarted to pick up latest code."
else
  echo "Service ${SERVICE_NAME}.service created and started."
fi

sleep 2
if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
  echo "Running on ${BIND_ADDR} (user: ${SERVICE_USER})."
else
  echo "Service is NOT active — check the logs below." >&2
  systemctl --no-pager --lines=20 status "${SERVICE_NAME}.service" || true
fi
echo "Logs:    journalctl -u ${SERVICE_NAME}.service -f"
echo "Restart: sudo systemctl restart ${SERVICE_NAME}.service"
