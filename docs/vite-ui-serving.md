# Serving the `flexagg2++` Vite SPA

This is the ops side of `docs/vite-ui-plan.md` §8. The SPA is **static files** (no server process of
its own) that reach the FastAPI cube (`risk_api.py`, loopback `:8010`) through a new same-origin nginx
proxy. Both locations sit under the existing basic-auth gate. `flexagg++` (Streamlit) is left untouched
and the two run side by side — deleting the two `flexagg2++` locations reverts cleanly.

## 1. Build

```bash
cd frontend
npm install          # first time only
npm run build        # tsc -b && vite build  ->  frontend/dist/
```

`vite.config.ts` sets `base: '/flexagg2++/'`, so asset URLs resolve under the un-stripped prefix
(nginx does **not** strip it — same reason Streamlit needs `--server.baseUrlPath`).

A convenience wrapper: `./frontend/deploy.sh` (build + an nginx-reload reminder).

## 2. Backend — restart to pick up `/views`

The only backend change is the saved-views CRUD wrapper (`views_api.py`, mounted in `risk_api.py`).
The running cube process must be **restarted once** for `GET/PUT/DELETE /views…` to exist:

```bash
cd python_src
BARRA_CUBE_PORT=9091 ../barra/bin/uvicorn risk_api:app --port 8010
```

Everything else (the 28 existing endpoints) already works against the current process — only the
Repository panel needs the restart. (`test_views_api.py` covers the wrapper without a cube.)

## 3. nginx — two locations

Production nginx config lives in `/etc/nginx/conf.d/flexagg-funnel.conf` (outside the repo, same as
`flexagg++`). Add:

```nginx
# built SPA — static files
location /flexagg2++/ {
    auth_basic "flexagg";
    auth_basic_user_file /etc/nginx/.htpasswd_flexagg;
    alias /home/abrennan/dev/barra_poc/frontend/dist/;
    try_files $uri $uri/ /flexagg2++/index.html;     # SPA fallback
}

# API, same-origin under the prefix (no CORS needed; auth inherited)
location /flexagg2++/api/ {
    auth_basic "flexagg";
    auth_basic_user_file /etc/nginx/.htpasswd_flexagg;
    proxy_pass http://127.0.0.1:8010/;               # trailing / strips the /flexagg2++/api/ prefix
    proxy_buffering off;                             # so the LLM token streams flush (/analysis,/ask)
    proxy_http_version 1.1;
    proxy_read_timeout 86400;
}
```

Then:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

`proxy_buffering off` is **required** — without it the raw `text/markdown` streams from `/analysis`,
`/ask`, `/whatchanged/analysis` buffer and the UI won't render tokens as they arrive (§7).

The Cloudflare → cloudflared → nginx:8090 chain is unchanged; the SPA is reachable at the same host
under `/flexagg2++/`.

## 4. Dev

```bash
cd frontend && npm run dev      # http://localhost:5173/flexagg2++/
```

`vite.config.ts` proxies `/flexagg2++/api/*` → `:8010` with the prefix stripped (the same shape nginx
serves), so app code uses one relative API base (`/flexagg2++/api`) in dev and prod alike. The dev
backend is just the local `uvicorn risk_api:app --port 8010`.

## 5. Static docs

The context-bar 📖 menu links the existing `flexagg++` copies of `guide.html` /
`barra_model_reference.html` (kept serving from the Streamlit app). To self-host them under the SPA
instead, copy them into `frontend/public/` and point the links at `/flexagg2++/...`.

## Reverting

Delete the two `location /flexagg2++/...` blocks and `nginx -s reload`. `flexagg++` is untouched;
nothing else depends on the SPA.
