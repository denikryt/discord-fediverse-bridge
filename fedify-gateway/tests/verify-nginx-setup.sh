#!/bin/bash
# Verify nginx-setup.sh rendering paths without requiring nginx, certbot, or root.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

assert_contains() {
    local file="$1"
    local expected="$2"
    if ! grep -Fq "$expected" "$file"; then
        echo "Expected to find '$expected' in $file" >&2
        echo "--- rendered ---" >&2
        cat "$file" >&2
        exit 1
    fi
}

single_env="$TMP_DIR/single.env"
cat > "$single_env" <<'ENV'
DEPLOYMENT_MODE=single-domain
PUBLIC_DOMAIN=bot.example.com
ENV
single_out="$TMP_DIR/single.conf"
ENV_FILE="$single_env" "$GATEWAY_DIR/nginx-setup.sh" --render > "$single_out"
assert_contains "$single_out" "server_name bot.example.com;"
assert_contains "$single_out" "location = /register"
assert_contains "$single_out" "location ^~ /auth/discord/"
assert_contains "$single_out" "proxy_pass http://127.0.0.1:8081;"
assert_contains "$single_out" "proxy_pass http://127.0.0.1:3000;"

two_env="$TMP_DIR/two.env"
cat > "$two_env" <<'ENV'
DEPLOYMENT_MODE=two-domain
GATEWAY_DOMAIN=bot.example.com
BRIDGE_DOMAIN=bridge.bot.example.com
ENV
two_out="$TMP_DIR/two.conf"
ENV_FILE="$two_env" "$GATEWAY_DIR/nginx-setup.sh" --render > "$two_out"
assert_contains "$two_out" "# gateway:bot.example.com"
assert_contains "$two_out" "server_name bot.example.com;"
assert_contains "$two_out" "# bridge:bridge.bot.example.com"
assert_contains "$two_out" "server_name bridge.bot.example.com;"

missing_single_env="$TMP_DIR/missing-single.env"
cat > "$missing_single_env" <<'ENV'
DEPLOYMENT_MODE=single-domain
ENV
if ENV_FILE="$missing_single_env" "$GATEWAY_DIR/nginx-setup.sh" --render > "$TMP_DIR/missing-single.out" 2>&1; then
    echo "Expected single-domain render to fail without PUBLIC_DOMAIN" >&2
    exit 1
fi
assert_contains "$TMP_DIR/missing-single.out" "PUBLIC_DOMAIN must be set"

missing_two_env="$TMP_DIR/missing-two.env"
cat > "$missing_two_env" <<'ENV'
DEPLOYMENT_MODE=two-domain
GATEWAY_DOMAIN=bot.example.com
ENV
if ENV_FILE="$missing_two_env" "$GATEWAY_DIR/nginx-setup.sh" --render > "$TMP_DIR/missing-two.out" 2>&1; then
    echo "Expected two-domain render to fail without BRIDGE_DOMAIN" >&2
    exit 1
fi
assert_contains "$TMP_DIR/missing-two.out" "GATEWAY_DOMAIN and BRIDGE_DOMAIN must be set"

echo "verify:nginx-setup passed"
