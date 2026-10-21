import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// BowlerDepot in-store kiosk (runbook 6eh) -- a plain client-side SPA,
// output to dist/ and synced to KioskSiteBucket behind CloudFront, same
// shape as bowlerdepot-learn minus the prerender step (a kiosk has no SEO).
export default defineConfig({
  plugins: [react()],
});
