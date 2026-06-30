/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// flexagg2++ is served under an un-stripped path prefix by nginx (like Streamlit's --server.baseUrlPath),
// so assets must resolve under it. In dev, /flexagg2++/api/* is proxied to the FastAPI cube on :8010 with
// the prefix stripped — the SAME shape nginx serves in prod (docs/vite-ui-plan.md §8), so app code can use
// one relative API base ("/flexagg2++/api") in both.
export default defineConfig({
  base: "/flexagg2++/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/flexagg2++/api": {
        target: "http://127.0.0.1:8010",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/flexagg2\+\+\/api/, ""),
        // LLM endpoints stream raw text/markdown — never buffer them in the dev proxy.
        configure: (proxy) => {
          proxy.on("proxyRes", (proxyRes) => {
            proxyRes.headers["x-accel-buffering"] = "no";
          });
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
