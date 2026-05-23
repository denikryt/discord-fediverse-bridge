#!/bin/bash
# Install nginx for the single-domain deployment model.
# The script renders the final public site from env values so checked-in files
# never need project-specific hostnames.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/.env}"
EMAIL="${EMAIL:-$(git config user.email 2>/dev/null || echo "admin@example.com")}"
GATEWAY_UPSTREAM="${GATEWAY_UPSTREAM:-http://127.0.0.1:3000}"
PYTHON_BRIDGE_UPSTREAM="${PYTHON_BRIDGE_UPSTREAM:-http://127.0.0.1:8081}"

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

    DEPLOYMENT_MODE="${DEPLOYMENT_MODE:-$(read_env_value DEPLOYMENT_MODE)}"
    PUBLIC_DOMAIN="${PUBLIC_DOMAIN:-$(read_env_value PUBLIC_DOMAIN)}"
    GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-$(read_env_value GATEWAY_DOMAIN)}"
    BRIDGE_DOMAIN="${BRIDGE_DOMAIN:-$(read_env_value BRIDGE_DOMAIN)}"

    # Reject old split-host configuration explicitly so operators do not think
    # two-domain mode still exists after the deployment model was simplified.
    if [[ -n "${GATEWAY_DOMAIN}" || -n "${BRIDGE_DOMAIN}" || "${DEPLOYMENT_MODE}" == "two-domain" ]]; then
        echo "Error: only single-domain deployments are supported; use PUBLIC_DOMAIN and remove GATEWAY_DOMAIN/BRIDGE_DOMAIN" >&2
        exit 1
    fi

    if [[ -n "${DEPLOYMENT_MODE}" && "${DEPLOYMENT_MODE}" != "single-domain" ]]; then
        echo "Error: DEPLOYMENT_MODE may only be omitted or set to single-domain" >&2
        exit 1
    fi

    if [[ -z "${PUBLIC_DOMAIN}" ]]; then
        echo "Error: PUBLIC_DOMAIN must be set" >&2
        exit 1
    fi
}

proxy_headers() {
    cat <<'NGINX'
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
NGINX
}

render_single_domain_site() {
    local domain="$1"
    cat <<NGINX
server {
    listen 80;
    server_name ${domain};

    return 301 https://\$server_name\$request_uri;
}

server {
    listen 443 ssl http2;
    server_name ${domain};

    ssl_certificate /etc/letsencrypt/live/${domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${domain}/privkey.pem;

    location = /register {
        proxy_pass ${PYTHON_BRIDGE_UPSTREAM};
$(proxy_headers)
    }

    location ^~ /register/ {
        proxy_pass ${PYTHON_BRIDGE_UPSTREAM};
$(proxy_headers)
    }

    location ^~ /auth/discord/ {
        proxy_pass ${PYTHON_BRIDGE_UPSTREAM};
$(proxy_headers)
    }

    location = /dashboard {
        proxy_pass ${PYTHON_BRIDGE_UPSTREAM};
$(proxy_headers)
    }

    location ^~ /dashboard/ {
        proxy_pass ${PYTHON_BRIDGE_UPSTREAM};
$(proxy_headers)
    }

    location / {
        proxy_pass ${GATEWAY_UPSTREAM};
$(proxy_headers)
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_request_buffering off;
    }
}
NGINX
}

render_selected_sites() {
    load_configuration
    render_single_domain_site "$PUBLIC_DOMAIN"
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
    install_site "$PUBLIC_DOMAIN" "$(render_single_domain_site "$PUBLIC_DOMAIN")"
    echo ""
    echo "Single-domain site is up. Update .env in the project root:"
    echo "  FEDIFY_ORIGIN=https://${PUBLIC_DOMAIN}"
    echo "  PUBLIC_BRIDGE_BASE_URL=https://${PUBLIC_DOMAIN}"
    echo "  DISCORD_OAUTH_REDIRECT_URI=https://${PUBLIC_DOMAIN}/auth/discord/callback"
}

if [[ "${1:-}" == "--render" ]]; then
    render_selected_sites
else
    main
fi
