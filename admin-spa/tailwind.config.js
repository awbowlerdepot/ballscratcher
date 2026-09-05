/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      // Palette is deliberately continuous with admin-site/index.html's
      // CSS custom properties (--primary: #2563eb etc.) -- the old
      // vanilla-JS admin UI and this new SPA will likely run side by
      // side for a while (see DEPLOY_RUNBOOK.md's admin-SPA section),
      // so keeping the same blue-primary/slate look avoids two
      // visually-unrelated "admin tools" for the same team.
      colors: {
        primary: {
          DEFAULT: "#2563eb",
          dark: "#1d4ed8",
          light: "#eff6ff",
        },
        danger: {
          DEFAULT: "#dc2626",
          dark: "#b91c1c",
          light: "#fee2e2",
        },
        ok: {
          DEFAULT: "#16a34a",
          light: "#dcfce7",
        },
        warn: {
          DEFAULT: "#854d0e",
          light: "#fef9c3",
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
