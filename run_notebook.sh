#!/usr/bin/env bash
# run_notebook.sh — serve the direct-Atoti demo notebook to CRO / Risk over Tailscale (HTTPS).
#
# Surface is the NOTEBOOK ONLY. JupyterLab binds to localhost; Tailscale terminates TLS and
# reverse-proxies it onto the tailnet at the node's MagicDNS name. The notebook's cube session
# binds :9096 but is deliberately NOT exposed (the Atoti web app is not part of this demo).
#
#   ./run_notebook.sh            # launches JupyterLab + sets up `tailscale serve`
#
# Prereqs (one-time):
#   * deps:        barra/bin/pip install jupyterlab ipykernel   (already in requirements.txt)
#   * HTTPS certs: enable MagicDNS + HTTPS in the Tailscale admin console (Settings -> Keys/DNS),
#                  so `tailscale serve --https` can provision a real cert for *.ts.net.
#   * operator:    `sudo tailscale set --operator="$USER"`      (lets `tailscale serve` run sans sudo)
set -euo pipefail
cd "$(dirname "$0")"                                          # repo root

PORT=8888
NB="notebooks/soros_13f_risk.ipynb"

# Expose localhost:8888 over Tailscale with a real HTTPS cert on this node's MagicDNS name.
# -> CRO/Risk open  https://<this-node>.<tailnet>.ts.net/  (find the exact URL via `tailscale serve status`).
tailscale serve --bg --https=443 "http://127.0.0.1:${PORT}"
echo "Tailscale serve mapping:"
tailscale serve status || true

# JupyterLab on localhost only; token auth (printed below) is the access control. PYTHONPATH so the
# notebook's bare imports (notebook_helpers, barra_factor_risk_cube) resolve — never sys.path-inject.
# It MUST be absolute: the kernel's cwd is the notebook's folder, so a relative `python_src` would
# not resolve. The kernel inherits this env. If WebSocket origin checks bite behind the proxy, add:
#   --ServerApp.allow_origin='https://<this-node>.<tailnet>.ts.net'
export PYTHONPATH="${PWD}/python_src"
exec barra/bin/jupyter lab "$NB" \
  --no-browser --ip=127.0.0.1 --port="${PORT}" \
  --ServerApp.allow_remote_access=True
