#!/usr/bin/env bash

# Manage the two systemd units that make up the bridge deployment.
# The defaults are intentionally overridable so different hosts can keep their
# own unit naming without editing this script.

set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BRIDGE_SERVICE="${PYTHON_BRIDGE_SERVICE:-discord-lemmy-bridge.service}"
FEDIFY_GATEWAY_SERVICE="${FEDIFY_GATEWAY_SERVICE:-discord-lemmy-bridge-fedify-gateway.service}"
PYTHON_BRIDGE_UNIT_FILE="${PYTHON_BRIDGE_UNIT_FILE:-}"
FEDIFY_GATEWAY_UNIT_FILE="${FEDIFY_GATEWAY_UNIT_FILE:-}"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$USER}}"
SERVICE_GROUP="${SERVICE_GROUP:-$SERVICE_USER}"
PYTHON_BRIDGE_ENV_FILE="${PYTHON_BRIDGE_ENV_FILE:-$PROJECT_DIR/.env}"
FEDIFY_GATEWAY_ENV_FILE="${FEDIFY_GATEWAY_ENV_FILE:-$PROJECT_DIR/fedify-gateway/.env}"
PYTHON_BRIDGE_WORKDIR="${PYTHON_BRIDGE_WORKDIR:-$PROJECT_DIR}"
FEDIFY_GATEWAY_WORKDIR="${FEDIFY_GATEWAY_WORKDIR:-$PROJECT_DIR/fedify-gateway}"
PYTHON_BRIDGE_EXEC="${PYTHON_BRIDGE_EXEC:-$PROJECT_DIR/.venv/bin/python -m src.app}"
FEDIFY_GATEWAY_EXEC="${FEDIFY_GATEWAY_EXEC:-/usr/bin/env npm run start}"

usage() {
  cat <<'EOF'
Usage: ./systemd-services.sh <command> [options]

Manage the two systemd units that run the bridge deployment:
  - Python bridge service
  - fedify-gateway service

Commands:
  install   Create both unit files, reload systemd, and run `start`
  start     Enable autostart by default and start both services now
  stop      Stop both services
  restart   Restart both services
  status    Show status for both services
  enable    Enable both services for boot
  disable   Disable both services for boot
  remove    Stop, disable, and remove both unit files
  help      Show this help text

Options:
  --no-autostart   With `install` or `start`, override the default and start without enabling autostart

Optional environment overrides:
  PYTHON_BRIDGE_SERVICE   Default: discord-lemmy-bridge.service
  FEDIFY_GATEWAY_SERVICE  Default: discord-lemmy-bridge-fedify-gateway.service
  PYTHON_BRIDGE_UNIT_FILE Default: /etc/systemd/system/<PYTHON_BRIDGE_SERVICE>
  FEDIFY_GATEWAY_UNIT_FILE Default: /etc/systemd/system/<FEDIFY_GATEWAY_SERVICE>
  SERVICE_USER            Default: current shell user (or SUDO_USER)
  SERVICE_GROUP           Default: same as SERVICE_USER
  PYTHON_BRIDGE_ENV_FILE  Default: <repo>/.env
  FEDIFY_GATEWAY_ENV_FILE Default: <repo>/fedify-gateway/.env
  PYTHON_BRIDGE_WORKDIR   Default: <repo>
  FEDIFY_GATEWAY_WORKDIR  Default: <repo>/fedify-gateway
  PYTHON_BRIDGE_EXEC      Default: <repo>/.venv/bin/python -m src.app
  FEDIFY_GATEWAY_EXEC     Default: /usr/bin/env npm run start

Examples:
  ./systemd-services.sh install
  ./systemd-services.sh install --no-autostart
  ./systemd-services.sh start
  ./systemd-services.sh start --no-autostart
  ./systemd-services.sh status
  PYTHON_BRIDGE_SERVICE=bridge.service \
  FEDIFY_GATEWAY_SERVICE=gateway.service \
  ./systemd-services.sh restart
  PYTHON_BRIDGE_UNIT_FILE=/etc/systemd/system/bridge.service \
  FEDIFY_GATEWAY_UNIT_FILE=/etc/systemd/system/gateway.service \
  ./systemd-services.sh remove
EOF
}

sudo_if_needed() {
  if [[ $EUID -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

unit_file_path() {
  local service_name="$1"
  local explicit_path="$2"

  if [[ -n "$explicit_path" ]]; then
    printf '%s\n' "$explicit_path"
    return 0
  fi

  printf '/etc/systemd/system/%s\n' "$service_name"
}

write_unit_file() {
  local unit_path="$1"
  local unit_body="$2"

  sudo_if_needed mkdir -p "$(dirname "$unit_path")"
  printf '%s\n' "$unit_body" | sudo_if_needed tee "$unit_path" >/dev/null
}

install_units() {
  local python_unit_path
  local gateway_unit_path
  local python_unit_body
  local gateway_unit_body

  python_unit_path="$(unit_file_path "$PYTHON_BRIDGE_SERVICE" "$PYTHON_BRIDGE_UNIT_FILE")"
  gateway_unit_path="$(unit_file_path "$FEDIFY_GATEWAY_SERVICE" "$FEDIFY_GATEWAY_UNIT_FILE")"

  # These units intentionally point at the checked-out repo and local env files
  # so one install command can materialize a working deployment from this tree.
  python_unit_body="[Unit]
Description=Discord Lemmy Bridge Python service
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$PYTHON_BRIDGE_WORKDIR
EnvironmentFile=$PYTHON_BRIDGE_ENV_FILE
ExecStart=$PYTHON_BRIDGE_EXEC
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target"

  gateway_unit_body="[Unit]
Description=Discord Lemmy Bridge Fedify gateway
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$FEDIFY_GATEWAY_WORKDIR
EnvironmentFile=$FEDIFY_GATEWAY_ENV_FILE
ExecStart=$FEDIFY_GATEWAY_EXEC
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target"

  write_unit_file "$python_unit_path" "$python_unit_body"
  write_unit_file "$gateway_unit_path" "$gateway_unit_body"
  sudo_if_needed systemctl daemon-reload
}

remove_unit_file() {
  local unit_file="$1"

  if [[ -z "$unit_file" ]]; then
    return 0
  fi

  if [[ -f "$unit_file" ]]; then
    rm -f "$unit_file"
  fi
}

run_start() {
  local option="$1"

  case "$option" in
    "")
      # The default startup path enables boot autostart so the bridge comes
      # back automatically after host reboots.
      sudo_if_needed systemctl enable --now "$PYTHON_BRIDGE_SERVICE" "$FEDIFY_GATEWAY_SERVICE"
      ;;
    --no-autostart)
      # Some operators may want a manual session-only start without changing
      # the boot-time policy for the units.
      sudo_if_needed systemctl start "$PYTHON_BRIDGE_SERVICE" "$FEDIFY_GATEWAY_SERVICE"
      ;;
    *)
      usage
      exit 1
      ;;
  esac
}

main() {
  if [[ $# -lt 1 || $# -gt 2 ]]; then
    usage
    exit 1
  fi

  local action="$1"
  local option="${2:-}"
  case "$action" in
    help|-h|--help)
      usage
      ;;
    install)
      install_units
      run_start "$option"
      ;;
    start)
      run_start "$option"
      ;;
    stop|restart|status|enable|disable)
      if [[ -n "$option" ]]; then
        usage
        exit 1
      fi
      # Run one systemctl command against both units so operators do not have
      # to remember or type two separate service names.
      sudo_if_needed systemctl "$action" "$PYTHON_BRIDGE_SERVICE" "$FEDIFY_GATEWAY_SERVICE"
      ;;
    remove)
      if [[ -n "$option" ]]; then
        usage
        exit 1
      fi

      # Full removal means stopping the units, disabling boot autostart,
      # deleting the unit files when explicit paths are provided, and then
      # asking systemd to reload its unit registry.
      sudo_if_needed systemctl stop "$PYTHON_BRIDGE_SERVICE" "$FEDIFY_GATEWAY_SERVICE" || true
      sudo_if_needed systemctl disable "$PYTHON_BRIDGE_SERVICE" "$FEDIFY_GATEWAY_SERVICE" || true
      remove_unit_file "$(unit_file_path "$PYTHON_BRIDGE_SERVICE" "$PYTHON_BRIDGE_UNIT_FILE")"
      remove_unit_file "$(unit_file_path "$FEDIFY_GATEWAY_SERVICE" "$FEDIFY_GATEWAY_UNIT_FILE")"
      sudo_if_needed systemctl daemon-reload
      sudo_if_needed systemctl reset-failed "$PYTHON_BRIDGE_SERVICE" "$FEDIFY_GATEWAY_SERVICE" || true
      ;;
    *)
      usage
      exit 1
      ;;
  esac
}

main "$@"
