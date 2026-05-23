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

    PUBLIC_DOMAIN="${PUBLIC_DOMAIN:-$(read_env_value PUBLIC_DOMAIN)}"
    GATEWAY_UPSTREAM="${GATEWAY_UPSTREAM:-$(read_env_value GATEWAY_UPSTREAM)}"
    PYTHON_BRIDGE_UPSTREAM="${PYTHON_BRIDGE_UPSTREAM:-$(read_env_value PYTHON_BRIDGE_UPSTREAM)}"
    DEPLOYMENT_MODE="${DEPLOYMENT_MODE:-$(read_env_value DEPLOYMENT_MODE)}"
    GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-$(read_env_value GATEWAY_DOMAIN)}"
    BRIDGE_DOMAIN="${BRIDGE_DOMAIN:-$(read_env_value BRIDGE_DOMAIN)}"

    # Reject the old split-host settings explicitly so operators migrate to the
    # single public host contract instead of getting a silently wrong config.
    if [[ -n "${DEPLOYMENT_MODE}" || -n "${GATEWAY_DOMAIN}" || -n "${BRIDGE_DOMAIN}" ]]; then
        echo "Error: legacy split-host settings are no longer supported; use PUBLIC_DOMAIN only" >&2
        exit 1
    fi

    if [[ ! -f "$TEMPLATE_FILE" ]]; then
        echo "Error: nginx template not found: $TEMPLATE_FILE" >&2
        exit 1
    fi

    if [[ -z "${PUBLIC_DOMAIN}" ]]; then
        echo "Error: PUBLIC_DOMAIN must be set" >&2
        exit 1
    fi

    # Nginx upstreams default to the local bridge binds but can still be
    # overridden from the root env file or from shell exports for one-off runs.
    GATEWAY_UPSTREAM="${GATEWAY_UPSTREAM:-http://127.0.0.1:3000}"
    PYTHON_BRIDGE_UPSTREAM="${PYTHON_BRIDGE_UPSTREAM:-http://127.0.0.1:8081}"
}

escape_sed_replacement() {
    printf '%s' "$1" | sed -e 's/[\/&]/\\&/g'
}

render_site() {
    local public_domain gateway_upstream python_bridge_upstream
    public_domain="$(escape_sed_replacement "$PUBLIC_DOMAIN")"
    gateway_upstream="$(escape_sed_replacement "$GATEWAY_UPSTREAM")"
    python_bridge_upstream="$(escape_sed_replacement "$PYTHON_BRIDGE_UPSTREAM")"

    sed \
        -e "s/__PUBLIC_DOMAIN__/${public_domain}/g" \
        -e "s/__GATEWAY_UPSTREAM__/${gateway_upstream}/g" \
        -e "s/__PYTHON_BRIDGE_UPSTREAM__/${python_bridge_upstream}/g" \
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
    echo "Public site is up. Update the root .env:"
    echo "  FEDIFY_ORIGIN=https://${PUBLIC_DOMAIN}"
    echo "  PUBLIC_BRIDGE_BASE_URL=https://${PUBLIC_DOMAIN}"
    echo "  DISCORD_OAUTH_REDIRECT_URI=https://${PUBLIC_DOMAIN}/auth/discord/callback"
}

if [[ "${1:-}" == "--render" ]]; then
    render_selected_sites
else
    main
fi
