#!/bin/bash
# Install nginx for the public bridge host.
# The script renders the checked-in nginx template from env values so route
# ownership stays defined in one place instead of being duplicated in shell.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"
TEMPLATE_FILE="${TEMPLATE_FILE:-$SCRIPT_DIR/nginx.conf}"
EMAIL="${EMAIL:-$(git config user.email 2>/dev/null || echo "admin@example.com")}"

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

    # Derive the nginx hostname from the same public URL used by the bridge and
    # gateway. Operators no longer repeat the public identity as PUBLIC_DOMAIN.
    PUBLIC_DOMAIN="$(python3 - "$PUBLIC_BASE_URL" <<'PYURL'
from sys import argv
from urllib.parse import urlparse

parsed = urlparse(argv[1])
if parsed.scheme not in {"http", "https"} or not parsed.hostname:
    raise SystemExit("PUBLIC_BASE_URL must be an absolute http(s) URL")
print(parsed.hostname)
PYURL
)"

    # The external nginx setup proxies to the host-published Compose ports.
    # Explicit shell overrides remain available for unusual installations but
    # are not duplicated in the shared .env contract.
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

render_selected_sites() {
    load_configuration
    render_site
}

install_site() {
    local domain="$1"
    local rendered_config="$2"
    local conf_dst="/etc/nginx/sites-available/${domain}"

    echo "--- Installing $domain ---"
    sudo tee "$conf_dst" > /dev/null <<NGINX
server {
    listen 80;
    server_name ${domain};
    location / {
        proxy_pass http://127.0.0.1:1;
    }
}
NGINX
    sudo ln -sf "$conf_dst" "/etc/nginx/sites-enabled/${domain}"
    sudo nginx -t
    sudo systemctl reload nginx

    sudo certbot certonly --nginx -d "$domain" --non-interactive --agree-tos -m "$EMAIL"

    printf '%s\n' "$rendered_config" | sudo tee "$conf_dst" > /dev/null
    sudo nginx -t
    sudo systemctl reload nginx
    echo "✓ $domain ready"
}

main() {
    load_configuration
    install_site "$PUBLIC_DOMAIN" "$(render_site)"
    echo ""
    echo "Public site is up at ${PUBLIC_BASE_URL}."
}

if [[ "${1:-}" == "--render" ]]; then
    render_selected_sites
else
    main
fi
