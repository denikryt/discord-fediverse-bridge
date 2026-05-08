#!/bin/bash
# Installs both nginx configs and issues SSL certs.
#   nginx.conf   -> GATEWAY_DOMAIN (fedify gateway, port 3000)
#   bridge.conf  -> BRIDGE_DOMAIN  (python bridge,  port 8081)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "Error: $ENV_FILE not found" >&2
    exit 1
fi

# shellcheck disable=SC2046
export $(grep -E '^(GATEWAY_DOMAIN|BRIDGE_DOMAIN)=' "$ENV_FILE" | xargs)

if [[ -z "$GATEWAY_DOMAIN" || -z "$BRIDGE_DOMAIN" ]]; then
    echo "Error: GATEWAY_DOMAIN and BRIDGE_DOMAIN must be set in .env" >&2
    exit 1
fi

EMAIL=$(git config user.email 2>/dev/null || echo "admin@example.com")

install_site() {
    local domain="$1"
    local conf_src="$2"
    local conf_dst="/etc/nginx/sites-available/${domain}"

    echo "--- Installing $domain ---"

    # Write HTTP-only stub so certbot can complete its challenge
    sudo tee "$conf_dst" > /dev/null << NGINX
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

    # Replace stub with the real config that has SSL directives
    sudo cp "$conf_src" "$conf_dst"
    sudo nginx -t
    sudo systemctl reload nginx
    echo "✓ $domain ready"
}

install_site "$GATEWAY_DOMAIN" "$SCRIPT_DIR/nginx.conf"
install_site "$BRIDGE_DOMAIN"  "$SCRIPT_DIR/bridge.conf"

echo ""
echo "Both sites are up. Update .env in the project root:"
echo "  PUBLIC_BRIDGE_BASE_URL=https://${BRIDGE_DOMAIN}"
echo "  DISCORD_OAUTH_REDIRECT_URI=https://${BRIDGE_DOMAIN}/auth/discord/callback"
