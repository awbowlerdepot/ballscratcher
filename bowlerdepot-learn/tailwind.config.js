/** @type {import('tailwindcss').Config} */
export default {
  // scripts/**/*.ts included alongside src -- scripts/prerender.ts
  // hand-builds its own HTML string with literal class="..." attributes
  // (see index.css's @layer components block for why those classes
  // exist at all) rather than JSX, so Tailwind's content scanner needs
  // to see that file too or it purges those component classes entirely.
  content: ["./index.html", "./src/**/*.{ts,tsx}", "./scripts/**/*.ts"],
  theme: {
    extend: {
      // Editorial Magazine theme (Al's pick, 2026-09-07) -- real
      // bowlerdepot.com brand values researched into DEPLOY_RUNBOOK.md
      // (header/text navy, blue accent, red reserved for alert-style
      // callouts), plus the theme's own warm paper background/border/
      // muted-text palette that isn't on the live storefront today.
      colors: {
        paper: "#faf8f4", // warm-white page background
        "paper-border": "#e8e2d6",
        ink: "#0f0f2d", // real bowlerdepot.com heading/text navy
        muted: "#6b6b63",
        accent: "#1f439e", // real bowlerdepot.com primary link/accent blue
        secondary: "#212152", // real bowlerdepot.com deep-indigo, used sparingly
        alert: "#d14343", // real bowlerdepot.com red, reserved for sale/alert callouts
      },
      fontFamily: {
        // Body copy stays Cabin, matching the real bowlerdepot.com site
        // (see Nav's brand link, footer, etc. that stay off Tailwind).
        // Headlines use Space Grotesk (Al: "a touch more modern font" --
        // picked over Fraunces/Instrument Serif after seeing all three
        // rendered) -- a clean modern grotesk, no serif.
        sans: ["Cabin", "Arial", "Helvetica", "sans-serif"],
        display: ["Space Grotesk", "Arial", "Helvetica", "sans-serif"],
      },
    },
  },
  plugins: [],
};
