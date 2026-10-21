/** @type {import('tailwindcss').Config} */
// In-store kiosk theme (runbook 6eh). Al: "we also want the experience to
// match instore not online". A dark, high-contrast floor-display look in
// BowlerDepot's own navy/blue (the Learn site's ink #0f0f2d and accent
// #1f439e, lifted for a lit screen), with big type for a 27" touchscreen
// read from a step or two back.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        night: "#08081c", // page background
        panel: "#12123a", // cards
        "panel-2": "#1a1a4a",
        edge: "#2a2a66", // borders
        ink: "#f4f5ff", // primary text
        mute: "#a3a6c9", // secondary text
        dim: "#6e7199",
        brand: "#3d6bff", // BowlerDepot blue, brightened for a dark screen
        "brand-deep": "#1f439e",
        hot: "#ff5a5f", // BowlerDepot red, sparingly
        gold: "#ffc857",
      },
      fontFamily: {
        sans: ["Cabin", "Arial", "Helvetica", "sans-serif"],
        display: ["Space Grotesk", "Arial", "Helvetica", "sans-serif"],
      },
    },
  },
  plugins: [],
};
