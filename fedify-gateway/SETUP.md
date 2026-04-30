# Fedify Gateway Setup

## Prerequisites

- Node.js 20+
- nginx
- certbot (for SSL certificates)
- Public domain with DNS configured
- Gateway running in background

## Step 1: Start the Gateway

```bash
cd fedify-gateway

FEDIFY_ORIGIN=https://bot-test.nachitima.com/ \
PYTHON_BRIDGE_EVENTS_URL=http://127.0.0.1:8080/internal/activitypub/events \
PYTHON_BRIDGE_SHARED_SECRET=your-secret-key \
npx tsx src/server.ts &
```

Or with `.env` file:
```bash
npm start &
```

## Step 2: Configure nginx

Run the setup script (requires sudo):

```bash
./nginx-setup.sh
```

This script will:
1. Create nginx configuration
2. Enable the site
3. Obtain SSL certificate from Let's Encrypt
4. Reload nginx with SSL

Alternatively, run manually:

```bash
sudo cp nginx.conf /etc/nginx/sites-available/bot-test.nachitima.com
sudo ln -s /etc/nginx/sites-available/bot-test.nachitima.com /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx

# Get SSL certificate
sudo certbot certonly --nginx -d bot-test.nachitima.com

# Update nginx config with SSL
# (uncomment SSL lines in nginx.conf)
sudo nginx -t
sudo systemctl reload nginx
```

## Step 3: Test the Gateway

Check health endpoint:
```bash
curl https://bot-test.nachitima.com/healthz
```

Expected response:
```json
{"status":"ok"}
```

## Step 4: Subscribe to a Community

```bash
curl -X POST https://bot-test.nachitima.com/follow-community \
  -H "Content-Type: application/json" \
  -d '{"communityActorUrl": "https://forum.nu31.space/c/discord_bridge_test"}'
```

Expected response:
```json
{"success":true}
```

## Troubleshooting

### nginx won't reload
```bash
sudo nginx -t  # Check for syntax errors
```

### SSL certificate issues
```bash
sudo certbot renew --dry-run  # Test renewal
```

### Gateway not accessible
```bash
curl http://localhost:3000/healthz  # Check if gateway is running
sudo systemctl status nginx  # Check nginx status
```

### Check logs
```bash
# nginx error log
sudo tail -f /var/log/nginx/error.log

# Gateway logs
tail -f /tmp/gateway.log
```
