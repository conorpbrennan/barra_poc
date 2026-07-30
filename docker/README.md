# Hardened notebook deployment

The public `/jupyter/` surface (`risk.barra-poc.com/jupyter/`) runs as a locked-down rootless
Podman container, not host Python. This dir holds everything needed to rebuild it.

## Pieces

| File | Goes to | Purpose |
|---|---|---|
| `Dockerfile` | build context = repo root | image: python:3.12-slim + `requirements.txt`, non-root `appuser` |
| `entrypoint.sh` | baked into image | launches JupyterLab at `/jupyter/`, root_dir `/work`. Its `cp /app/notebooks/*.ipynb` is now a no-op — `/app/notebooks` is deliberately not mounted, see the unit |
| `flexagg-jupyter.container` | `/home/flexnb/.config/containers/systemd/` | Quadlet unit: read-only rootfs, cap-drop all, internal (no-egress) network, our licence; `notebooks/` mounted rw at `/work` |
| `barra-jail.nft` | `/etc/nftables.d/` | backstop firewall: blocks the container (flexnb uid) from LAN/host/tailnet |
| `barra-jail.service` | `/etc/systemd/system/` | loads `barra-jail.nft` at boot |

## Request path

```
internet -> Cloudflare (TLS) -> cloudflared -> nginx :8090
              /jupyter/  (no nginx auth)  -> 127.0.0.1:8888 (container) -> JupyterLab token
```

**One gate: the Jupyter token** (in `/home/flexnb/.config/flexagg-jupyter.env`, flexnb-only —
share out of band). Open `/jupyter/lab?token=…` once; the session cookie then lasts 30 days.

nginx basic-auth used to sit in front of this and was **removed on 2026-07-30**: iOS Safari does
not reliably attach Basic credentials to `fetch`/XHR subrequests, and JupyterLab fires ~15 of them
per page load, so each one 401'd and re-prompted — the page was unusable from a phone
(`access.log` 02:56 shows the alternating `flexagg 200` / `- 401` pattern). Cookies do not have
that problem, hence token-only. `flexagg++` and `flexagg2++` keep their basic-auth gate.
Re-adding a second gate here means something cookie-based (e.g. Cloudflare Access), not
`auth_basic`.

## Security posture

- Runs as service user `flexnb` (no sudo, no login). In-container `appuser` maps to host uid 210000.
- Read-only rootfs. `python_src/`, `data/` and the licence are mounted read-only. **`notebooks/` is
  mounted read-WRITE** at `/work` so saves survive a restart (it was tmpfs/RAM until 2026-07-30 and
  every edit was lost). That is the one relaxation of the read-only rule; code and frames stay
  untouchable from inside. Write access needs a host ACL, since uid 210000 has no write bit on an
  abrennan-owned dir — and `abrennan` needs one too, or files Lab creates (owned by 210000) become
  unwritable to git:
  ```bash
  setfacl -R -m u:210000:rwX -m u:abrennan:rwX notebooks
  setfacl -R -d -m u:210000:rwx -m u:abrennan:rwx notebooks   # so new files inherit it
  ```
  Miss the `.ipynb_checkpoints/` subdir and every save 500s on the checkpoint copy.
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
`apt install podman uidmap acl`, set the `notebooks/` ACLs above, install + enable `barra-jail`.
The token is rotated by writing a new `flexagg-jupyter.env` and restarting — note that rotating it
invalidates any `?token=` bookmark, which is now the only way in.

## Notebook dependencies

The image is air-gapped with a read-only rootfs, so `pip install` cannot work at runtime. Pure-python
libs the notebook needs but the image lacks are staged on the host in `data/_pylibs` (gitignored) and
put on `PYTHONPATH` by the unit as `/app/data/_pylibs`. Currently altair + narwhals:

```bash
cp -r barra/lib/python3.12/site-packages/{altair,narwhals} data/_pylibs/
```

Anything with compiled extensions needs adding to `requirements.txt` and an image rebuild instead.
