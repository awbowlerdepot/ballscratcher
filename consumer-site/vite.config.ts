import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Straightforward static-build config -- output goes to dist/, which is
// what gets synced to the ConsumerSiteBucket (see template.yaml) behind
// CloudFront. No SSR, no server -- this is a pure client-side SPA
// talking to PublicApiFunction, per Al's ask for a "single page like
// site for quick navigation and not a ton of full page reloads".
export default defineConfig({
  plugins: [react()],
});
