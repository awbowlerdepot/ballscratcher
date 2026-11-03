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
      // Bowler Depot design direction (approved by Al 2026-11-01,
      // https://claude.ai/artifact/9pDXa8M2M4MXLsirP3k4vu), the same
      // tokens as the new store theme's assets/tailwind/tailwind.css
      // (awbowlerdepot/bowlerdepot-theme). Learn was built as an extension
      // of the old bowlerdepot.com (warm paper, navy, Cabin); the brief
      // settles that "Learn moves to the new look too ... both get this
      // design system: the store first, then Learn as a restyle of its
      // tokens", and nothing from the old site carries over but the logo.
      //
      // The old token NAMES (paper, paper-border, ink, muted, accent,
      // secondary, alert) are kept and repointed so every existing class
      // takes the new palette; the theme's own names sit beside them for
      // new markup. Rules from the brief: pin red only for the one main
      // action on a screen, oil blue for oil/plotter graphics, stock green
      // only for "in stock at a store".
      colors: {
        ink: "#141c2e",
        pin: "#d21f3c",
        "pin-dark": "#a8172f",
        maple: "#ddae6e",
        "maple-soft": "#f4e6d0",
        oil: "#2b76a3",
        go: "#17804a",
        paper: "#f5f6f8",
        slate: "#586276",
        line: "#dce1ea",
        // Old names, repointed
        "paper-border": "#dce1ea", // = line
        muted: "#586276", // = slate
        accent: "#141c2e", // links are ink with a maple underline now (see index.css)
        secondary: "#141c2e", // = ink
        alert: "#a8172f", // = pin-dark
      },
      fontFamily: {
        // Google Fonts, loaded in index.css: Big Shoulders Display 700-900
        // for headings (usually uppercase), Instrument Sans for reading,
        // Spline Sans Mono for specs and numbers.
        sans: ["Instrument Sans", "Helvetica Neue", "Arial", "sans-serif"],
        display: ["Big Shoulders Display", "Arial Narrow", "Helvetica Neue", "Arial", "sans-serif"],
        mono: ["Spline Sans Mono", "ui-monospace", "SF Mono", "Menlo", "monospace"],
      },
      // Square-ish, like lane signage: 4px for fields and buttons, 6px for
      // boxes. Tailwind's bigger radii are capped at the box size so older
      // rounded-lg/xl/2xl cards don't come out pillowy.
      borderRadius: {
        DEFAULT: "4px",
        md: "4px",
        lg: "6px",
        xl: "6px",
        "2xl": "6px",
        "3xl": "6px",
      },
    },
  },
  plugins: [],
};
