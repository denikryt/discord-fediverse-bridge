#!/bin/bash
# Verify nginx setup rendering and argument parsing without root or nginx.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

assert_contains() {
    local file="$1"
    local expected="$2"
    if ! grep -Fq -- "$expected" "$file"; then
        echo "Expected to find '$expected' in $file" >&2
        cat "$file" >&2
        exit 1
    fi
}

single_env="$TMP_DIR/single.env"
cat > "$single_env" <<'ENV'
PUBLIC_BASE_URL=https://discord-bridge-dev.example.com
GATEWAY_PUBLISHED_PORT=3100
BRIDGE_PUBLISHED_PORT=8181
ENV
single_out="$TMP_DIR/single.conf"
"$GATEWAY_DIR/nginx-setup.sh" --env-file "$single_env" --name dev --render > "$single_out"
assert_contains "$single_out" "server_name discord-bridge-dev.example.com;"
assert_contains "$single_out" "proxy_pass http://127.0.0.1:8181;"
assert_contains "$single_out" "proxy_pass http://127.0.0.1:3100;"

legacy_env="$TMP_DIR/legacy.env"
cat > "$legacy_env" <<'ENV'
DEPLOYMENT_MODE=two-domain
GATEWAY_DOMAIN=discord-bridge.example.com
BRIDGE_DOMAIN=bridge.discord-bridge.example.com
ENV
if "$GATEWAY_DIR/nginx-setup.sh" --env-file "$legacy_env" --render > "$TMP_DIR/legacy.out" 2>&1; then
    echo "Expected render to fail when legacy split-host settings are provided" >&2
    exit 1
fi
assert_contains "$TMP_DIR/legacy.out" "legacy split-host settings are no longer supported"

missing_env="$TMP_DIR/missing.env"
touch "$missing_env"
if "$GATEWAY_DIR/nginx-setup.sh" --env-file "$missing_env" --render > "$TMP_DIR/missing.out" 2>&1; then
    echo "Expected render to fail without PUBLIC_BASE_URL" >&2
    exit 1
fi
assert_contains "$TMP_DIR/missing.out" "PUBLIC_BASE_URL must be set"

if "$GATEWAY_DIR/nginx-setup.sh" --env-file "$single_env" --name 'bad/name' --render > "$TMP_DIR/name.out" 2>&1; then
    echo "Expected invalid instance name to fail" >&2
    exit 1
fi
assert_contains "$TMP_DIR/name.out" "--name may contain only"

echo "verify:nginx-setup passed"
