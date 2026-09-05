import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Straightforward static-build config -- output goes to dist/, which is
// what gets synced to AdminSiteBucket (see template.yaml) behind
// CloudFront. Pure client-side SPA talking to AdminApiFunction, same
// hosting shape as consumer-site/bowlerdepot-learn (see those two
// vite.config.ts files for the identical precedent).
export default defineConfig({
  plugins: [react()],
});
