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
PUBLIC_DOMAIN=discord-bridge.example.com
GATEWAY_UPSTREAM=http://127.0.0.1:3100
PYTHON_BRIDGE_UPSTREAM=http://127.0.0.1:8181
ENV
single_out="$TMP_DIR/single.conf"
ENV_FILE="$single_env" "$GATEWAY_DIR/nginx-setup.sh" --render > "$single_out"
assert_contains "$single_out" "server_name discord-bridge.example.com;"
assert_contains "$single_out" "location = /register"
assert_contains "$single_out" "location ^~ /auth/discord/"
assert_contains "$single_out" "location = /dashboard"
assert_contains "$single_out" "location ^~ /dashboard/"
assert_contains "$single_out" "proxy_pass http://127.0.0.1:8181;"
assert_contains "$single_out" "proxy_pass http://127.0.0.1:3100;"

legacy_env="$TMP_DIR/legacy.env"
cat > "$legacy_env" <<'ENV'
DEPLOYMENT_MODE=two-domain
GATEWAY_DOMAIN=discord-bridge.example.com
BRIDGE_DOMAIN=bridge.discord-bridge.example.com
ENV
if ENV_FILE="$legacy_env" "$GATEWAY_DIR/nginx-setup.sh" --render > "$TMP_DIR/legacy.out" 2>&1; then
    echo "Expected render to fail when legacy split-host settings are provided" >&2
    exit 1
fi
assert_contains "$TMP_DIR/legacy.out" "legacy split-host settings are no longer supported"

missing_single_env="$TMP_DIR/missing-single.env"
touch "$missing_single_env"
if ENV_FILE="$missing_single_env" "$GATEWAY_DIR/nginx-setup.sh" --render > "$TMP_DIR/missing-single.out" 2>&1; then
    echo "Expected render to fail without PUBLIC_DOMAIN" >&2
    exit 1
fi
assert_contains "$TMP_DIR/missing-single.out" "PUBLIC_DOMAIN must be set"

echo "verify:nginx-setup passed"
