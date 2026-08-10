#!/bin/sh
# ── Rolarr entrypoint ────────────────────────────────────────────────────────
# Generates a self-signed TLS cert on first run (persisted in /config so the
# browser only needs to accept it once), then starts Gunicorn over HTTPS.

CONFIG_DIR="${CONFIG_DIR:-/config}"
CERT="$CONFIG_DIR/cert.pem"
KEY="$CONFIG_DIR/key.pem"

# Create config dir if running outside Docker (local dev fallback)
mkdir -p "$CONFIG_DIR"

if [ ! -f "$CERT" ] || [ ! -f "$KEY" ]; then
    echo "[entrypoint] Generating self-signed TLS certificate..."
    openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout "$KEY" \
        -out "$CERT" \
        -days 3650 \
        -subj "/CN=rolarr" \
        -addext "subjectAltName=DNS:rolarr,DNS:localhost,IP:127.0.0.1" \
        2>/dev/null
    echo "[entrypoint] Certificate written to $CERT"
fi

exec gunicorn \
    --bind "0.0.0.0:${PORT:-5000}" \
    --workers 1 \
    --threads 4 \
    --timeout 120 \
    --certfile "$CERT" \
    --keyfile "$KEY" \
    app:app
