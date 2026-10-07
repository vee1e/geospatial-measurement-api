import { defineConfig } from "vite";

export default defineConfig({
  server: {
    port: 5173,
    // Local development talks to the API started with `uv run uvicorn app.main:app`.
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
  // `vite preview` serves the production build; give it the same /api proxy.
  preview: {
    port: 4173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
