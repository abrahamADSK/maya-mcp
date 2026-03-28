#!/usr/bin/env bash
# setup_gpu_server.sh — Install and configure the GPU inference API server.
#
# Run this on the GPU machine (e.g. glorfindel):
#   bash setup_gpu_server.sh
#
# What it does:
#   1. Installs FastAPI + uvicorn into the existing venv
#   2. Generates a random API key (saved to .api_key)
#   3. Installs Caddy (if not present)
#   4. Creates a systemd service for the GPU server
#   5. Creates a systemd service for Caddy
#   6. Prints the config values to use on the Mac

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────

VISION_DIR="${GPU_VISION_DIR:-$HOME/ai-studio/vision}"
VENV_DIR="${GPU_VENV:-$VISION_DIR/.venv}"
WORK_DIR="${GPU_WORK_DIR:-$HOME/ai-studio/reference/3d_output}"
MODELS_DIR="${GPU_MODELS_DIR:-$VISION_DIR/hf_models}"
PORT=8000
CADDY_PORT=9443
HOSTNAME=$(hostname)

echo "═══════════════════════════════════════════════════════════"
echo " Hunyuan3D-2 GPU Server Setup"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo " Vision dir:  $VISION_DIR"
echo " Venv:        $VENV_DIR"
echo " Work dir:    $WORK_DIR"
echo " Models dir:  $MODELS_DIR"
echo " Hostname:    $HOSTNAME"
echo ""

# ── 1. Install Python dependencies ──────────────────────────────────────────

echo "[1/5] Installing FastAPI + uvicorn..."
if [ -f "$VENV_DIR/bin/pip" ]; then
    "$VENV_DIR/bin/pip" install --quiet fastapi uvicorn python-multipart
    echo "      Done."
else
    echo "ERROR: venv not found at $VENV_DIR"
    echo "Create it first: python3 -m venv $VENV_DIR && $VENV_DIR/bin/pip install -r requirements.txt"
    exit 1
fi

# ── 2. Generate API key ─────────────────────────────────────────────────────

echo "[2/5] Generating API key..."
API_KEY_FILE="$VISION_DIR/.api_key"
if [ -f "$API_KEY_FILE" ]; then
    API_KEY=$(cat "$API_KEY_FILE")
    echo "      Using existing key from $API_KEY_FILE"
else
    API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    echo "$API_KEY" > "$API_KEY_FILE"
    chmod 600 "$API_KEY_FILE"
    echo "      New key saved to $API_KEY_FILE"
fi

# ── 3. Install Caddy ────────────────────────────────────────────────────────

echo "[3/5] Checking Caddy..."
if command -v caddy &>/dev/null; then
    echo "      Caddy already installed: $(caddy version)"
else
    echo "      Installing Caddy..."
    if command -v dnf &>/dev/null; then
        sudo dnf install -y 'dnf-command(copr)'
        sudo dnf copr enable -y @caddy/caddy
        sudo dnf install -y caddy
    elif command -v apt-get &>/dev/null; then
        sudo apt-get update
        sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
        sudo apt-get update
        sudo apt-get install -y caddy
    else
        echo "ERROR: Could not detect package manager. Install Caddy manually:"
        echo "  https://caddyserver.com/docs/install"
        exit 1
    fi
    echo "      Caddy installed: $(caddy version)"
fi

# ── 4. Create systemd service for GPU server ─────────────────────────────────

echo "[4/5] Creating systemd service..."

SERVICE_FILE="/etc/systemd/system/gpu-server.service"
sudo tee "$SERVICE_FILE" > /dev/null <<UNIT
[Unit]
Description=Hunyuan3D-2 GPU Inference Server
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$VISION_DIR
Environment=GPU_API_KEY=$API_KEY
Environment=GPU_MODELS_DIR=$MODELS_DIR
Environment=GPU_WORK_DIR=$WORK_DIR
Environment=GPU_VISION_DIR=$VISION_DIR
ExecStart=$VENV_DIR/bin/python $VISION_DIR/gpu_server.py --host 127.0.0.1 --port $PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

echo "      Created $SERVICE_FILE"

# ── 5. Configure Caddy ──────────────────────────────────────────────────────

echo "[5/5] Configuring Caddy..."

CADDY_DIR="/etc/caddy"
sudo mkdir -p "$CADDY_DIR" /var/log/caddy

sudo tee "$CADDY_DIR/Caddyfile" > /dev/null <<CADDY
https://$HOSTNAME:$CADDY_PORT {
    tls internal
    reverse_proxy localhost:$PORT
    log {
        output file /var/log/caddy/gpu-server.log
        format console
    }
}
CADDY

echo "      Caddyfile written to $CADDY_DIR/Caddyfile"

# ── Enable and start ─────────────────────────────────────────────────────────

sudo systemctl daemon-reload
sudo systemctl enable --now gpu-server.service
sudo systemctl enable --now caddy.service

# Wait a moment for services to start
sleep 2

echo ""
echo "═══════════════════════════════════════════════════════════"
echo " Setup complete!"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo " GPU Server:  http://localhost:$PORT/api/health"
echo " Caddy HTTPS: https://$HOSTNAME:$CADDY_PORT/api/health"
echo ""
echo " API Key:     $API_KEY"
echo ""
echo " ── Mac configuration (~/.claude.json) ──"
echo " Set these env vars in maya-mcp:"
echo ""
echo "   GPU_API_URL=https://$HOSTNAME:$CADDY_PORT"
echo "   GPU_API_KEY=$API_KEY"
echo ""
echo " Or for direct HTTP (no TLS, LAN only):"
echo ""
echo "   GPU_API_URL=http://$HOSTNAME:$PORT"
echo "   GPU_API_KEY=$API_KEY"
echo ""
echo " ── Quick test ──"
echo "   curl -k https://$HOSTNAME:$CADDY_PORT/api/health"
echo ""

# Verify health
echo "Verifying..."
if curl -sf "http://localhost:$PORT/api/health" > /dev/null 2>&1; then
    echo "✓ GPU server is running"
else
    echo "⚠ GPU server may still be starting — check: systemctl status gpu-server"
fi
