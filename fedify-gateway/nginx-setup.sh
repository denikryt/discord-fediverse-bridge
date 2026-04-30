#!/bin/bash
# nginx setup script for bot-test.nachitima.com

set -e

DOMAIN="bot-test.nachitima.com"
NGINX_CONF="/etc/nginx/sites-available/${DOMAIN}"
NGINX_ENABLED="/etc/nginx/sites-enabled/${DOMAIN}"

echo "Setting up nginx for $DOMAIN..."

# Step 1: Create nginx config with HTTP only (for certbot validation)
echo "Step 1: Creating nginx config..."
sudo tee "$NGINX_CONF" > /dev/null << 'EOF'
server {
    listen 80;
    server_name bot-test.nachitima.com;

    location / {
        proxy_pass http://localhost:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
    }
}
EOF

# Step 2: Enable site
echo "Step 2: Enabling nginx site..."
sudo ln -sf "$NGINX_CONF" "$NGINX_ENABLED"

# Step 3: Test nginx config
echo "Step 3: Testing nginx config..."
sudo nginx -t

# Step 4: Reload nginx
echo "Step 4: Reloading nginx..."
sudo systemctl reload nginx

# Step 5: Get SSL certificate with certbot
echo "Step 5: Getting SSL certificate..."
sudo certbot certonly --nginx -d "$DOMAIN" --non-interactive --agree-tos -m $(git config user.email || echo "admin@example.com")

# Step 6: Update nginx config with SSL
echo "Step 6: Updating nginx config with SSL..."
sudo tee "$NGINX_CONF" > /dev/null << 'EOF'
server {
    listen 80;
    server_name bot-test.nachitima.com;

    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name bot-test.nachitima.com;

    ssl_certificate /etc/letsencrypt/live/bot-test.nachitima.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bot-test.nachitima.com/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    location / {
        proxy_pass http://localhost:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_request_buffering off;
    }
}
EOF

# Step 7: Test and reload nginx
echo "Step 7: Testing and reloading nginx with SSL..."
sudo nginx -t
sudo systemctl reload nginx

echo "✓ nginx setup complete!"
echo "Gateway is now accessible at https://$DOMAIN"
