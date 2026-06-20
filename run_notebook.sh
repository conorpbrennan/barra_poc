#!/usr/bin/env bash
# run_notebook.sh — expose the hardened notebook container to CRO / Risk over Tailscale (HTTPS).
#
# The notebook no longer runs as host Python. It runs as a locked-down rootless Podman container
# (systemd service `flexagg-jupyter` under the `flexnb` service user), published on 127.0.0.1:8888.
# This script does NOT launch JupyterLab — it makes sure that container is up, then points
# `tailscale serve` at the same :8888. Access control is nginx basic-auth + the Jupyter token.
# The container is air-gapped (internal network + nft jail) and mounts only read-only code/data.
#
#   ./run_notebook.sh
#
# The container is boot-enabled (linger), so it is normally already running. The image, Quadlet
# unit, and firewall live in docker/.
#
# Prereqs (one-time):
#   * HTTPS certs: enable MagicDNS + HTTPS in the Tailscale admin console.
#   * operator:    sudo tailscale set --operator="$USER"   (lets `tailscale serve` run sans sudo)
set -euo pipefail
cd "$(dirname "$0")"                                          # repo root

PORT=8888
FLEXUID="$(id -u flexnb)"
NB_SVC="env XDG_RUNTIME_DIR=/run/user/${FLEXUID} systemctl --user"

# Make sure the hardened notebook container is running (normally already up at boot).
sudo -u flexnb $NB_SVC start flexagg-jupyter.service
sudo -u flexnb $NB_SVC --no-pager --lines=0 status flexagg-jupyter.service | sed -n '1,3p' || true

# Expose localhost:8888 over Tailscale with a real HTTPS cert on this node's MagicDNS name.
# -> users open  https://<this-node>.<tailnet>.ts.net/  (exact URL via `tailscale serve status`).
tailscale serve --bg --https=443 "http://127.0.0.1:${PORT}"
echo "Tailscale serve mapping:"
tailscale serve status || true

cat <<EOF

  Notebook is served by the flexagg-jupyter container on 127.0.0.1:${PORT}.
  Access control: nginx basic-auth + Jupyter token. The token is in
  /home/flexnb/.config/flexagg-jupyter.env (flexnb-only) — share it out of band.
EOF
