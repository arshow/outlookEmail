#!/usr/bin/env bash
set -Eeuo pipefail

# One-time VPS bootstrap. Future releases are deployed with: git push vps main
APP_DIR="/opt/outlook-email"
GIT_DIR="/opt/git/outlook-email.git"
SERVICE_NAME="outlook-email"
SERVICE_USER="outlook-email"
ENV_DIR="/etc/outlook-email"
ENV_FILE="${ENV_DIR}/outlook-email.env"
STATE_DIR="/var/lib/outlook-email"
BRANCH="main"
PORT="5001"
HOST="0.0.0.0"

log() {
  printf '[OutlookEmail bootstrap] %s\n' "$*"
}

fail() {
  printf '[OutlookEmail bootstrap] ERROR: %s\n' "$*" >&2
  exit 1
}

require_root() {
  [[ "${EUID}" -eq 0 ]] || fail "Run as root."
}

install_packages() {
  log "Installing required system packages"
  apt-get update
  apt-get install -y git python3 python3-venv python3-pip
}

ensure_service_user() {
  if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    log "Creating service user ${SERVICE_USER}"
    useradd --system --home-dir "${STATE_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
  fi
}

ensure_environment() {
  install -d -m 0750 "${ENV_DIR}"
  install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${STATE_DIR}" "${STATE_DIR}/data"

  if [[ -f "${ENV_FILE}" ]]; then
    log "Keeping existing environment file: ${ENV_FILE}"
    return
  fi

  local secret_key
  secret_key="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  log "Creating environment file: ${ENV_FILE}"
  umask 077
  cat > "${ENV_FILE}" <<ENV
# This file is intentionally outside the Git working tree.
SECRET_KEY=${secret_key}
LOGIN_PASSWORD=admin123
FLASK_ENV=production
HOST=${HOST}
PORT=${PORT}
DATABASE_PATH=${STATE_DIR}/data/outlook_accounts.db
LOG_LEVEL=INFO
ENV
  chown root:"${SERVICE_USER}" "${ENV_FILE}"
  chmod 0640 "${ENV_FILE}"
}

ensure_bare_repository() {
  install -d -m 0755 "$(dirname "${GIT_DIR}")" "${APP_DIR}"
  if [[ ! -d "${GIT_DIR}/objects" ]]; then
    log "Creating bare repository: ${GIT_DIR}"
    git init --bare "${GIT_DIR}"
  fi
  git --git-dir="${GIT_DIR}" symbolic-ref HEAD "refs/heads/${BRANCH}"
}

write_post_receive_hook() {
  log "Writing post-receive hook"
  cat > "${GIT_DIR}/hooks/post-receive" <<HOOK
#!/usr/bin/env bash
set -Eeuo pipefail

BRANCH="${BRANCH}"
WORK_TREE="${APP_DIR}"
GIT_DIR="${GIT_DIR}"

while read -r old_revision new_revision ref_name; do
  if [[ "\${ref_name}" != "refs/heads/\${BRANCH}" ]]; then
    continue
  fi
  if [[ "\${new_revision}" =~ ^0+\$ ]]; then
    echo "Branch deletion does not change the deployed service."
    continue
  fi

  echo "Deploying \${BRANCH} to \${WORK_TREE} ..."
  git --git-dir="\${GIT_DIR}" --work-tree="\${WORK_TREE}" checkout -f "\${BRANCH}"
  chmod +x "\${WORK_TREE}/scripts/deploy_debian.sh"
  APP_DIR="\${WORK_TREE}" \\
  SERVICE_NAME="${SERVICE_NAME}" \\
  SERVICE_USER="${SERVICE_USER}" \\
  ENV_FILE="${ENV_FILE}" \\
  STATE_DIR="${STATE_DIR}" \\
  HOST="${HOST}" \\
  PORT="${PORT}" \\
  bash "\${WORK_TREE}/scripts/deploy_debian.sh"
done
HOOK
  chmod 0755 "${GIT_DIR}/hooks/post-receive"
}

print_summary() {
  cat <<SUMMARY

Bootstrap complete.

Bare repository: ${GIT_DIR}
Working tree:    ${APP_DIR}
Service:         ${SERVICE_NAME}
Web port:        ${PORT}
State directory: ${STATE_DIR}
Environment:     ${ENV_FILE}

Next, from the local repository:
  git remote add vps ssh://root@23.145.120.84:55231${GIT_DIR}
  git push vps ${BRANCH}

Subsequent deployments:
  git push vps ${BRANCH}

Server diagnostics:
  systemctl status ${SERVICE_NAME}
  journalctl -u ${SERVICE_NAME} -f
SUMMARY
}

main() {
  require_root
  install_packages
  ensure_service_user
  ensure_environment
  ensure_bare_repository
  write_post_receive_hook
  print_summary
}

main "$@"
