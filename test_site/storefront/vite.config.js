import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built into dist/ and served by test_site/app.py at /test-site. In dev, proxy the market data to a test site on 4100.
export default defineConfig({
  base: "/test-site/",
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    proxy: {
      "/test-site/market.json": "http://localhost:4100",
    },
  },
});
