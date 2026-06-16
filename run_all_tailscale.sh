#!/usr/bin/env bash
# run_all_tailscale.sh — serve BOTH front doors to tailnet users over HTTPS, and launch the stack.
#
#   Risk UI (Streamlit) : https://ajb1ubuntu.tail062fc3.ts.net          (-> 127.0.0.1:8502)
#   Notebook (JupyterLab): https://ajb1ubuntu.tail062fc3.ts.net:8443    (-> 127.0.0.1:8888)
#
# Only these two front doors are exposed. The FastAPI risk API (:8010) and every Atoti cube port
# (:9095 API cube, :9096 notebook cube) stay bound to localhost — never served on the tailnet.
#
#   ./run_all_tailscale.sh           # step 2 (tailscale serve) + step 3 (launch); Ctrl-C tears down
#
# Prereqs (one-time):
#   * deps:   barra/bin/pip install -r requirements.txt   (jupyterlab/ipykernel/streamlit incl.)
#   * certs:  enable MagicDNS + HTTPS in the Tailscale admin console
#   * sudo-less serve:  sudo tailscale set --operator="$USER"
#   * invite the external user to the tailnet (admin console -> Users -> Invite / Share node)
set -euo pipefail
cd "$(dirname "$0")"                                   # repo root
export PYTHONPATH="${PWD}/python_src"                  # absolute: kernels/children inherit it

UI_PORT=8502; API_PORT=8010; NB_PORT=8888; CUBE_PORT=9095
PY=barra/bin

# ── step 2 — expose the two front doors over Tailscale (HTTPS, each at root on its own port) ──
echo "[serve] mapping tailnet HTTPS -> localhost"
tailscale serve --bg --https=443  "http://127.0.0.1:${UI_PORT}"     # Risk UI  -> :443  (root)
tailscale serve --bg --https=8443 "http://127.0.0.1:${NB_PORT}"     # Notebook -> :8443
tailscale serve status || true

# ── step 3 — launch the stack, all bound to 127.0.0.1 (only Tailscale can reach them) ────────
pids=()
cleanup(){ echo; echo "[stop] shutting down…"; kill "${pids[@]}" 2>/dev/null || true;
           tailscale serve --https=443 off 2>/dev/null || true
           tailscale serve --https=8443 off 2>/dev/null || true; }
trap cleanup INT TERM EXIT

echo "[api] building cube + FastAPI on :${API_PORT} (cube :${CUBE_PORT}) — ~1–2 min…"
( cd python_src && BARRA_CUBE_PORT=${CUBE_PORT} exec ../${PY}/uvicorn risk_api:app \
    --host 127.0.0.1 --port ${API_PORT} ) &
pids+=($!)

# wait for the API to answer before starting the UI that depends on it. /meta is cube-backed, so a
# 200 means the cube has finished building (not just that uvicorn booted).
for i in $(seq 1 150); do
  curl -fsS "http://127.0.0.1:${API_PORT}/meta" >/dev/null 2>&1 && { echo "[api] cube ready"; break; }
  sleep 2
done

echo "[ui] Streamlit risk dashboard on :${UI_PORT}"
BARRA_API="http://127.0.0.1:${API_PORT}" ${PY}/streamlit run python_src/risk_pivot_app.py \
    --server.address 127.0.0.1 --server.port ${UI_PORT} --server.headless true \
    --server.enableCORS false --server.enableXsrfProtection false &
pids+=($!)

echo "[nb] JupyterLab on :${NB_PORT} (token in the log below)"
${PY}/jupyter lab notebooks/soros_13f_risk.ipynb \
    --no-browser --ip=127.0.0.1 --port=${NB_PORT} --ServerApp.allow_remote_access=True &
pids+=($!)

cat <<EOF

  ───────────────────────────────────────────────────────────────
   Risk UI   : https://ajb1ubuntu.tail062fc3.ts.net
   Notebook  : https://ajb1ubuntu.tail062fc3.ts.net:8443   (use the Jupyter token printed above)
  ───────────────────────────────────────────────────────────────
   Send tailnet users docs/tailscale-connect.html + the notebook token.
   Ctrl-C to stop everything (also turns the tailscale serve mappings off).

EOF
wait
