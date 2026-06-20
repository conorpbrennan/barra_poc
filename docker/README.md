# Hardened notebook deployment

The public `/jupyter/` surface (`risk.barra-poc.com/jupyter/`) runs as a locked-down rootless
Podman container, not host Python. This dir holds everything needed to rebuild it.

## Pieces

| File | Goes to | Purpose |
|---|---|---|
| `Dockerfile` | build context = repo root | image: python:3.12-slim + `requirements.txt`, non-root `appuser` |
| `entrypoint.sh` | baked into image | copies the read-only notebook into tmpfs `/work`, launches JupyterLab at `/jupyter/` |
| `flexagg-jupyter.container` | `/home/flexnb/.config/containers/systemd/` | Quadlet unit: ro mounts, read-only rootfs, cap-drop all, internal (no-egress) network, our licence |
| `barra-jail.nft` | `/etc/nftables.d/` | backstop firewall: blocks the container (flexnb uid) from LAN/host/tailnet |
| `barra-jail.service` | `/etc/systemd/system/` | loads `barra-jail.nft` at boot |

## Request path

```
internet -> Cloudflare (TLS) -> cloudflared -> nginx :8090
              /jupyter/  (nginx basic-auth)  -> 127.0.0.1:8888 (container) -> JupyterLab token
```

Two independent gates: nginx basic-auth (`/etc/nginx/.htpasswd_flexagg`) then the Jupyter token
(in `/home/flexnb/.config/flexagg-jupyter.env`, flexnb-only — share out of band).

## Security posture

- Runs as service user `flexnb` (no sudo, no login). In-container `appuser` maps to host uid ~210000.
- Read-only rootfs; only `python_src/`, `data/`, `notebooks/`, and the licence are mounted, read-only.
- All capabilities dropped, `no-new-privileges`, seccomp on, mem/cpu/pids limits.
- **Air-gapped**: internal Podman network = zero egress. The Atoti licence (`ATOTI_LICENSE`)
  disables the telemetry that would otherwise force an outbound call. See the `atoti-license`
  note — **if the licence expires the notebook breaks.**

## Rebuild / redeploy

```bash
FLEXUID=$(id -u flexnb); RUN="env XDG_RUNTIME_DIR=/run/user/$FLEXUID"
# image
sudo -u flexnb $RUN podman build -t localhost/flexagg-jupyter:latest -f docker/Dockerfile .
# unit + restart
sudo install -o flexnb -g flexnb -m644 docker/flexagg-jupyter.container \
  /home/flexnb/.config/containers/systemd/flexagg-jupyter.container
sudo -u flexnb $RUN systemctl --user daemon-reload
sudo -u flexnb $RUN systemctl --user restart flexagg-jupyter.service
```

One-time host setup (not scripted): create `flexnb` (system user, nologin, subuid range, linger),
`apt install podman uidmap`, add nginx basic-auth to the `/jupyter/` location, install + enable
`barra-jail`. The token is rotated by writing a new `flexagg-jupyter.env` and restarting.
