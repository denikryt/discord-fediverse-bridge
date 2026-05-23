#!/bin/bash
# Install nginx reverse proxy sites for either single-domain or legacy
# two-domain deployments. The script renders final nginx configs from env values
# so checked-in files never need project-specific hostnames.

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

    if [[ -z "${DEPLOYMENT_MODE}" ]]; then
        if [[ -n "${PUBLIC_DOMAIN}" ]]; then
            DEPLOYMENT_MODE="single-domain"
        elif [[ -n "${GATEWAY_DOMAIN}" && -n "${BRIDGE_DOMAIN}" ]]; then
            DEPLOYMENT_MODE="two-domain"
        else
            echo "Error: set PUBLIC_DOMAIN for single-domain mode or GATEWAY_DOMAIN and BRIDGE_DOMAIN for two-domain mode" >&2
            exit 1
        fi
    fi

    case "$DEPLOYMENT_MODE" in
        single-domain)
            if [[ -z "${PUBLIC_DOMAIN}" ]]; then
                echo "Error: PUBLIC_DOMAIN must be set when DEPLOYMENT_MODE=single-domain" >&2
                exit 1
            fi
            ;;
        two-domain)
            if [[ -z "${GATEWAY_DOMAIN}" || -z "${BRIDGE_DOMAIN}" ]]; then
                echo "Error: GATEWAY_DOMAIN and BRIDGE_DOMAIN must be set when DEPLOYMENT_MODE=two-domain" >&2
                exit 1
            fi
            ;;
        *)
            echo "Error: DEPLOYMENT_MODE must be single-domain or two-domain" >&2
            exit 1
            ;;
    esac
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

render_gateway_site() {
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

render_bridge_site() {
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

    location / {
        proxy_pass ${PYTHON_BRIDGE_UPSTREAM};
$(proxy_headers)
    }
}
NGINX
}

render_selected_sites() {
    load_configuration
    if [[ "$DEPLOYMENT_MODE" == "single-domain" ]]; then
        render_single_domain_site "$PUBLIC_DOMAIN"
    else
        echo "# gateway:${GATEWAY_DOMAIN}"
        render_gateway_site "$GATEWAY_DOMAIN"
        echo "# bridge:${BRIDGE_DOMAIN}"
        render_bridge_site "$BRIDGE_DOMAIN"
    fi
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
    if [[ "$DEPLOYMENT_MODE" == "single-domain" ]]; then
        install_site "$PUBLIC_DOMAIN" "$(render_single_domain_site "$PUBLIC_DOMAIN")"
        echo ""
        echo "Single-domain site is up. Update .env in the project root:"
        echo "  FEDIFY_ORIGIN=https://${PUBLIC_DOMAIN}"
        echo "  PUBLIC_BRIDGE_BASE_URL=https://${PUBLIC_DOMAIN}"
        echo "  DISCORD_OAUTH_REDIRECT_URI=https://${PUBLIC_DOMAIN}/auth/discord/callback"
    else
        install_site "$GATEWAY_DOMAIN" "$(render_gateway_site "$GATEWAY_DOMAIN")"
        install_site "$BRIDGE_DOMAIN" "$(render_bridge_site "$BRIDGE_DOMAIN")"
        echo ""
        echo "Both sites are up. Update .env in the project root:"
        echo "  FEDIFY_ORIGIN=https://${GATEWAY_DOMAIN}"
        echo "  PUBLIC_BRIDGE_BASE_URL=https://${BRIDGE_DOMAIN}"
        echo "  DISCORD_OAUTH_REDIRECT_URI=https://${BRIDGE_DOMAIN}/auth/discord/callback"
    fi
}

if [[ "${1:-}" == "--render" ]]; then
    render_selected_sites
else
    main
fi
