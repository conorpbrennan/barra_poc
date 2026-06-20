#!/usr/bin/env bash
# Copy the read-only mounted notebook(s) into a writable tmpfs workdir, then serve JupyterLab.
# Nothing the user does persists: /work is tmpfs, and python_src/ data/ notebooks/ are mounted
# read-only. The cube resolves data as <python_src>/../data, so /app/python_src + /app/data must
# sit side by side (they do — both mounted under /app).
set -euo pipefail

WORK=/work
mkdir -p "$WORK"
cp -f /app/notebooks/*.ipynb "$WORK"/ 2>/dev/null || true

# Token comes from the JUPYTER_TOKEN env var (set by the Quadlet unit, rotated, file-backed).
# jupyter_server reads JUPYTER_TOKEN natively, so it never appears in the process argv.
: "${JUPYTER_TOKEN:?JUPYTER_TOKEN must be set}"

exec jupyter lab \
  --no-browser --ip=0.0.0.0 --port=8888 \
  --ServerApp.root_dir="$WORK" \
  --ServerApp.base_url="${JUPYTER_BASE_URL:-/jupyter/}" \
  --ServerApp.allow_remote_access=True \
  --ServerApp.allow_origin="${JUPYTER_ALLOW_ORIGIN:-https://risk.barra-poc.com}"
