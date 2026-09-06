/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      // "Dense pro-tool" restyle (Al picked this direction from a set of
      // style mockups on 2026-09-05 -- see DEPLOY_RUNBOOK.md's admin-SPA
      // style-decision writeup). Replaces the earlier light slate/blue
      // palette that was deliberately continuous with admin-site/index.html;
      // that continuity goal is dropped in favor of a darker, tighter,
      // single-accent look suited to someone living in this tool all day
      // doing rapid review/approve work. `ink` is a from-scratch 10-step
      // dark-UI grayscale that mirrors slate's bucket meanings (50 = page
      // background/darkest, 900 = highest-contrast text/lightest) but with
      // the light/dark direction inverted -- every former `slate-N` class
      // in this app was mechanically renamed to `ink-N` and just works
      // because the bucket-per-number meaning didn't change, only the
      // literal color at each step.
      colors: {
        ink: {
          50: "#1a1b1e",
          100: "#212226",
          200: "#2c2d31",
          300: "#3a3b40",
          400: "#6b6d74",
          500: "#8b8d94",
          600: "#b4b6bc",
          700: "#cfd1d6",
          800: "#e4e5e8",
          900: "#f4f5f6",
        },
        primary: {
          DEFAULT: "#6366f1",
          hover: "#4f46e5",
          dark: "#a5b4fc",
          light: "#262b52",
        },
        danger: {
          DEFAULT: "#ef4444",
          dark: "#b91c1c",
          light: "#3f1d1f",
        },
        ok: {
          DEFAULT: "#4ade80",
          light: "#12291c",
        },
        warn: {
          DEFAULT: "#fbbf24",
          light: "#3a2a0a",
        },
      },
      fontFamily: {
        sans: [
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "Roboto",
          "Helvetica",
          "Arial",
          "sans-serif",
        ],
      },
    },
  },
  plugins: [],
};
