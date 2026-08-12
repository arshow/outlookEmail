#!/usr/bin/env bash
set -Eeuo pipefail

# This script is intentionally kept in the repository. The bare-repository
# hook runs it after every successful push to the deployment branch.
APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SERVICE_NAME="${SERVICE_NAME:-outlook-email}"
SERVICE_USER="${SERVICE_USER:-outlook-email}"
ENV_FILE="${ENV_FILE:-/etc/outlook-email/outlook-email.env}"
STATE_DIR="${STATE_DIR:-/var/lib/outlook-email}"
PORT="${PORT:-5001}"
HOST="${HOST:-0.0.0.0}"
# One worker is required because SSE and short-lived task state are in-process.
# Reserve enough threads for one long-lived SSE connection per active user plus
# ordinary UI and remote-mail requests.
GUNICORN_THREADS="${GUNICORN_THREADS:-16}"
VENV_DIR="${APP_DIR}/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"
DEPENDENCY_STAMP="${STATE_DIR}/.requirements.sha256"

log() {
  printf '[OutlookEmail deploy] %s\n' "$*"
}

fail() {
  printf '[OutlookEmail deploy] ERROR: %s\n' "$*" >&2
  exit 1
}

require_root() {
  [[ "${EUID}" -eq 0 ]] || fail "Run this deployment script as root."
}

validate_project() {
  [[ -f "${APP_DIR}/requirements.txt" ]] || fail "requirements.txt is missing from ${APP_DIR}"
  [[ -f "${APP_DIR}/web_outlook_app.py" ]] || fail "web_outlook_app.py is missing from ${APP_DIR}"
  [[ -r "${ENV_FILE}" ]] || fail "Environment file is missing or unreadable: ${ENV_FILE}"
}

ensure_runtime() {
  if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    log "Creating service user ${SERVICE_USER}"
    useradd --system --home-dir "${STATE_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
  fi

  install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${STATE_DIR}" "${STATE_DIR}/data"

  if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    log "Creating virtual environment"
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
  fi

  local requirements_hash
  requirements_hash="$(sha256sum "${APP_DIR}/requirements.txt" | awk '{print $1}')"
  if [[ -x "${VENV_DIR}/bin/gunicorn" ]] \
    && [[ -f "${DEPENDENCY_STAMP}" ]] \
    && [[ "$(<"${DEPENDENCY_STAMP}")" == "${requirements_hash}" ]]; then
    log "Python dependencies unchanged; skipping installation"
    return
  fi

  log "Installing Python dependencies"
  "${VENV_DIR}/bin/python" -m pip install --disable-pip-version-check --quiet --upgrade pip
  "${VENV_DIR}/bin/python" -m pip install --disable-pip-version-check --quiet -r "${APP_DIR}/requirements.txt" gunicorn
  printf '%s\n' "${requirements_hash}" > "${DEPENDENCY_STAMP}"
  chown "${SERVICE_USER}:${SERVICE_USER}" "${DEPENDENCY_STAMP}"
}

write_service() {
  log "Writing systemd service ${SERVICE_NAME}"
  cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<SERVICE
[Unit]
Description=OutlookEmail web service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
Environment=PYTHONUTF8=1
Environment=PYTHONUNBUFFERED=1
Environment=PATH=${VENV_DIR}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
ExecStart=${VENV_DIR}/bin/gunicorn -k gthread -w 1 --threads ${GUNICORN_THREADS} --bind ${HOST}:${PORT} --timeout 300 --graceful-timeout 10 --access-logfile - --error-logfile - --capture-output web_outlook_app:app
Restart=always
RestartSec=5
TimeoutStartSec=30
TimeoutStopSec=15
UMask=0077

[Install]
WantedBy=multi-user.target
SERVICE
}

verify_service() {
  local health_url="http://127.0.0.1:${PORT}/"
  local attempt
  for attempt in $(seq 1 20); do
    if "${VENV_DIR}/bin/python" - "${health_url}" <<'PY'
import sys
import urllib.request

try:
    with urllib.request.urlopen(sys.argv[1], timeout=3) as response:
        if response.status < 500:
            raise SystemExit(0)
except Exception:
    raise SystemExit(1)
PY
    then
      log "Health check passed: ${health_url}"
      return 0
    fi
    sleep 1
  done
  fail "Health check failed: ${health_url}"
}

main() {
  require_root
  validate_project
  ensure_runtime
  write_service
  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}" >/dev/null
  log "Restarting ${SERVICE_NAME} (old worker has at most 15 seconds to exit)"
  systemctl restart "${SERVICE_NAME}"
  systemctl is-active --quiet "${SERVICE_NAME}" || fail "${SERVICE_NAME} did not start"
  log "Waiting for health check"
  verify_service
  log "Deployment completed"
}

main "$@"
