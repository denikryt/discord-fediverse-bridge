#!/usr/bin/env bash

# Deploy one bridge instance from an explicit env file.
# The script prepares the backup directory, starts the Compose stack, and can
# optionally install the matching nginx site for the same env file.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

ENV_FILE=""
PROJECT_NAME=""
INSTANCE_NAME=""
USE_BUILD_STACK=false
INSTALL_NGINX=false
SKIP_PULL=false
BACKUP_DIR_UID="${BACKUP_DIR_UID:-10001}"
BACKUP_DIR_GID="${BACKUP_DIR_GID:-10001}"

usage() {
  cat <<'USAGE'
Usage: scripts/deploy.sh --env-file PATH [options]

Required:
  --env-file PATH   Root env file for this instance, for example .env.dev

Options:
  --name NAME       Instance name for nginx setup, defaults to the env-file name
  --build           Rebuild the local working tree with compose.build.yaml
  --with-nginx      Install the matching nginx site after starting Compose
  --no-pull         Skip docker compose pull before starting
  -h, --help        Show this help text
USAGE
}

die() {
  echo "Error: $*" >&2
  exit 1
}

read_env_value() {
  local key="$1"
  local line value
  line="$(grep -E "^[[:space:]]*${key}=" "$ENV_FILE" | tail -n 1 || true)"
  [[ -z "$line" ]] && return 0
  value="${line#*=}"
  value="${value%%#*}"
  value="${value%$'\r'}"
  value="$(printf '%s' "$value" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  printf '%s' "$value"
}

derive_instance_name() {
  local file_name
  file_name="$(basename "$ENV_FILE")"
  case "$file_name" in
    .env)
      printf '%s' "prod"
      ;;
    .env.*)
      printf '%s' "${file_name#.env.}"
      ;;
    *)
      printf '%s' "${file_name%%.*}"
      ;;
  esac
}

read_compose_project_name() {
  PROJECT_NAME="$(read_env_value COMPOSE_PROJECT_NAME)"
  [[ -n "$PROJECT_NAME" ]] || die "COMPOSE_PROJECT_NAME must be set in $ENV_FILE"
}

sanitize_compose_environment() {
  local key
  while IFS= read -r line; do
    key="${line%%=*}"
    [[ -n "$key" ]] || continue
    case "$key" in
      \#*|"")
        continue
        ;;
      COMPOSE_PROJECT_NAME)
        continue
        ;;
    esac
    unset "$key" 2>/dev/null || true
  done < <(grep -E '^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE")
}

prepare_backup_directory() {
  local backup_host_dir
  backup_host_dir="$(read_env_value BACKUP_HOST_DIR)"
  [[ -n "$backup_host_dir" ]] || backup_host_dir="./backups"

  mkdir -p "$backup_host_dir"

  if [[ $EUID -eq 0 ]]; then
    chown "$BACKUP_DIR_UID":"$BACKUP_DIR_GID" "$backup_host_dir"
  elif command -v sudo >/dev/null 2>&1; then
    sudo chown "$BACKUP_DIR_UID":"$BACKUP_DIR_GID" "$backup_host_dir"
  else
    chown "$BACKUP_DIR_UID":"$BACKUP_DIR_GID" "$backup_host_dir"
  fi

  echo "Prepared backup directory: $backup_host_dir"
}

run_compose() {
  local -a compose_args
  sanitize_compose_environment
  compose_args=(docker compose -p "$PROJECT_NAME" --env-file "$ENV_FILE" -f "$REPO_ROOT/compose.yaml")

  if [[ "$USE_BUILD_STACK" == true ]]; then
    compose_args+=(-f "$REPO_ROOT/compose.build.yaml")
  fi

  if [[ "$SKIP_PULL" == false && "$USE_BUILD_STACK" == false ]]; then
    "${compose_args[@]}" pull
  fi

  if [[ "$USE_BUILD_STACK" == true ]]; then
    "${compose_args[@]}" up -d --build
  else
    "${compose_args[@]}" up -d
  fi
}

install_nginx() {
  local nginx_script
  nginx_script="$REPO_ROOT/fedify-gateway/nginx-setup.sh"
  if [[ ! -x "$nginx_script" ]]; then
    die "nginx setup script not found or not executable: $nginx_script"
  fi

  sudo "$nginx_script" --name "$INSTANCE_NAME" --env-file "$ENV_FILE"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --env-file)
        [[ $# -ge 2 ]] || die "--env-file requires a path"
        ENV_FILE="$2"
        shift 2
        ;;
      --name)
        [[ $# -ge 2 ]] || die "--name requires a value"
        INSTANCE_NAME="$2"
        shift 2
        ;;
      --build)
        USE_BUILD_STACK=true
        shift
        ;;
      --with-nginx)
        INSTALL_NGINX=true
        shift
        ;;
      --no-pull)
        SKIP_PULL=true
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "unknown argument: $1"
        ;;
    esac
  done

  [[ -n "$ENV_FILE" ]] || die "--env-file is required"
  [[ -f "$ENV_FILE" ]] || die "env file not found: $ENV_FILE"

  if [[ -z "$INSTANCE_NAME" ]]; then
    INSTANCE_NAME="$(derive_instance_name)"
  fi
}

main() {
  parse_args "$@"
  cd "$REPO_ROOT"
  read_compose_project_name
  prepare_backup_directory
  run_compose
  if [[ "$INSTALL_NGINX" == true ]]; then
    install_nginx
  fi
}

main "$@"
