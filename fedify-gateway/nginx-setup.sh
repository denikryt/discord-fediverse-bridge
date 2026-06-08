#!/bin/bash
# Render or install one nginx site for a bridge deployment instance.
# The selected env file is the single source for public URL and published ports.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"
INSTANCE_NAME="${INSTANCE_NAME:-prod}"
TEMPLATE_FILE="${TEMPLATE_FILE:-$SCRIPT_DIR/nginx.conf}"
EMAIL="${EMAIL:-$(git config user.email 2>/dev/null || echo "admin@example.com")}"
RENDER_ONLY=false

usage() {
    cat <<'USAGE'
Usage: nginx-setup.sh [--env-file PATH] [--name INSTANCE] [--render]

  --env-file PATH  Read PUBLIC_BASE_URL and published ports from PATH.
  --name INSTANCE  Use an independent nginx site name, for example prod or dev.
  --render         Print the rendered nginx config without installing it.
USAGE
}

parse_arguments() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --env-file)
                [[ $# -ge 2 ]] || { echo "Error: --env-file requires a path" >&2; exit 2; }
                ENV_FILE="$2"
                shift 2
                ;;
            --name)
                [[ $# -ge 2 ]] || { echo "Error: --name requires a value" >&2; exit 2; }
                INSTANCE_NAME="$2"
                shift 2
                ;;
            --render)
                RENDER_ONLY=true
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                echo "Error: unknown argument: $1" >&2
                usage >&2
                exit 2
                ;;
        esac
    done

    if [[ ! "$INSTANCE_NAME" =~ ^[a-zA-Z0-9._-]+$ ]]; then
        echo "Error: --name may contain only letters, numbers, dots, underscores, and dashes" >&2
        exit 2
    fi
}

read_env_value() {
    local key="$1"
    if [[ ! -f "$ENV_FILE" ]]; then
        return 0
    fi
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

load_configuration() {
    if [[ ! -f "$ENV_FILE" ]]; then
        echo "Error: $ENV_FILE not found" >&2
        exit 1
    fi

    PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-$(read_env_value PUBLIC_BASE_URL)}"
    BRIDGE_PUBLISHED_PORT="${BRIDGE_PUBLISHED_PORT:-$(read_env_value BRIDGE_PUBLISHED_PORT)}"
    GATEWAY_PUBLISHED_PORT="${GATEWAY_PUBLISHED_PORT:-$(read_env_value GATEWAY_PUBLISHED_PORT)}"

    DEPLOYMENT_MODE="${DEPLOYMENT_MODE:-$(read_env_value DEPLOYMENT_MODE)}"
    GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-$(read_env_value GATEWAY_DOMAIN)}"
    BRIDGE_DOMAIN="${BRIDGE_DOMAIN:-$(read_env_value BRIDGE_DOMAIN)}"
    if [[ -n "${DEPLOYMENT_MODE}" || -n "${GATEWAY_DOMAIN}" || -n "${BRIDGE_DOMAIN}" ]]; then
        echo "Error: legacy split-host settings are no longer supported" >&2
        exit 1
    fi

    if [[ ! -f "$TEMPLATE_FILE" ]]; then
        echo "Error: nginx template not found: $TEMPLATE_FILE" >&2
        exit 1
    fi

    if [[ -z "${PUBLIC_BASE_URL}" ]]; then
        echo "Error: PUBLIC_BASE_URL must be set" >&2
        exit 1
    fi

    # Derive the hostname from the same public URL used by ActivityPub actors.
    PUBLIC_DOMAIN="$(python3 - "$PUBLIC_BASE_URL" <<'PYURL'
from sys import argv
from urllib.parse import urlparse

parsed = urlparse(argv[1])
if parsed.scheme not in {"http", "https"} or not parsed.hostname:
    raise SystemExit("PUBLIC_BASE_URL must be an absolute http(s) URL")
print(parsed.hostname)
PYURL
)"

    # External nginx proxies to the loopback-only ports published by Compose.
    BRIDGE_PUBLISHED_PORT="${BRIDGE_PUBLISHED_PORT:-8080}"
    GATEWAY_PUBLISHED_PORT="${GATEWAY_PUBLISHED_PORT:-3000}"
    GATEWAY_UPSTREAM_URL="${GATEWAY_UPSTREAM_URL:-http://127.0.0.1:${GATEWAY_PUBLISHED_PORT}}"
    BRIDGE_UPSTREAM_URL="${BRIDGE_UPSTREAM_URL:-http://127.0.0.1:${BRIDGE_PUBLISHED_PORT}}"
}

escape_sed_replacement() {
    printf '%s' "$1" | sed -e 's/[\/&]/\\&/g'
}

render_site() {
    local public_domain gateway_upstream bridge_upstream
    public_domain="$(escape_sed_replacement "$PUBLIC_DOMAIN")"
    gateway_upstream="$(escape_sed_replacement "$GATEWAY_UPSTREAM_URL")"
    bridge_upstream="$(escape_sed_replacement "$BRIDGE_UPSTREAM_URL")"

    sed \
        -e "s/__PUBLIC_DOMAIN__/${public_domain}/g" \
        -e "s/__GATEWAY_UPSTREAM__/${gateway_upstream}/g" \
        -e "s/__PYTHON_BRIDGE_UPSTREAM__/${bridge_upstream}/g" \
        "$TEMPLATE_FILE"
}

install_site() {
    local rendered_config="$1"
    local site_name="discord-fediverse-bridge-${INSTANCE_NAME}"
    local conf_dst="/etc/nginx/sites-available/${site_name}.conf"
    local enabled_dst="/etc/nginx/sites-enabled/${site_name}.conf"

    echo "--- Installing ${site_name} for ${PUBLIC_DOMAIN} ---"
    sudo tee "$conf_dst" > /dev/null <<NGINX
server {
    listen 80;
    server_name ${PUBLIC_DOMAIN};
    location / {
        proxy_pass http://127.0.0.1:1;
    }
}
NGINX
    sudo ln -sf "$conf_dst" "$enabled_dst"
    sudo nginx -t
    sudo systemctl reload nginx

    sudo certbot certonly --nginx -d "$PUBLIC_DOMAIN" --non-interactive --agree-tos -m "$EMAIL"

    printf '%s\n' "$rendered_config" | sudo tee "$conf_dst" > /dev/null
    sudo nginx -t
    sudo systemctl reload nginx
    echo "✓ ${site_name} ready at ${PUBLIC_BASE_URL}"
}

main() {
    parse_arguments "$@"
    load_configuration
    if [[ "$RENDER_ONLY" == true ]]; then
        render_site
        return
    fi
    install_site "$(render_site)"
}

main "$@"
