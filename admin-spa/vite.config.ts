import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Straightforward static-build config -- output goes to dist/, which is
// what gets synced to AdminSiteBucket (see template.yaml) behind
// CloudFront. Pure client-side SPA talking to AdminApiFunction, same
// hosting shape as consumer-site/bowlerdepot-learn (see those two
// vite.config.ts files for the identical precedent).
export default defineConfig({
  plugins: [react()],
  // amazon-cognito-identity-js (see src/auth/cognito.ts) pulls in
  // Node's `buffer` package as a transitive dependency, which assumes a
  // global `global` object exists -- true in Node, not in a browser.
  // Older bundlers (webpack) polyfilled this automatically; Vite
  // doesn't. Aliasing `global` to `globalThis` here is the standard fix
  // for this exact "Uncaught ReferenceError: global is not defined"
  // error with this library + Vite (confirmed against a real `npm run
  // dev` failure, not a guess). If a similar `process is not defined`
  // or `Buffer is not defined` error shows up next from the same
  // dependency chain, that needs `vite-plugin-node-polyfills` (or a
  // manual process/Buffer shim) added here too -- this fix only covers
  // the `global` symbol itself.
  define: {
    global: "globalThis",
  },
});
