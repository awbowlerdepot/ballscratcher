import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Same shape as consumer-site/vite.config.ts -- pure client-side SPA
// build, output to dist/, synced to LearnSiteBucket (see template.yaml)
// behind CloudFront. The one difference from consumer-site: this
// project's `npm run build` script (see package.json) runs a
// post-build `prerender` step on top of `vite build` -- see
// scripts/prerender.ts's own header comment for why a pure client-
// rendered SPA isn't good enough here (SEO -- BigCommerce's native blog
// gave real server-rendered HTML per post for free, this is how that's
// replaced without giving up the SPA itself for logged-in/JS
// navigation).
export default defineConfig({
  plugins: [react()],
});
