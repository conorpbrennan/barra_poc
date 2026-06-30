#!/usr/bin/env bash
# Build the flexagg2++ SPA and remind to reload nginx (which serves frontend/dist/ directly).
# See docs/vite-ui-serving.md. There is no systemd unit — the SPA is static files.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> npm install (if needed)"
[ -d node_modules ] || npm install --no-audit --no-fund

echo "==> vite build -> frontend/dist/"
npm run build

echo
echo "Built. Next:"
echo "  • If /views (saved-views Repository) is new on this host, restart the cube:"
echo "      cd ../python_src && BARRA_CUBE_PORT=9091 ../barra/bin/uvicorn risk_api:app --port 8010"
echo "  • Reload nginx to serve the fresh dist/:"
echo "      sudo nginx -t && sudo systemctl reload nginx"
