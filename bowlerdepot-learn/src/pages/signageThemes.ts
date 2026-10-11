// Per-brand look for the in-store signage overlay (runbook 6cs). Al: "for
// the signage videos can you analyse the current marketing site for each
// brand and pull in the current styling and color schemes to use for the
// fonts and colors you use for the overlays".
//
// Every value below was read off the brand's live site on 2026-10-11 --
// computed styles of its headings/buttons in a real browser, plus the
// logo colors for the three Storm-family brands, whose pages share
// stormbowling.com's template (Oswald/Roboto, #191C1B) and carry their
// color only in the logo. Storm's own homepage was mid "Paint the Lane
// Pink" campaign, so its pink is deliberately NOT used; Storm's sage
// green is from its brand page's banner and logo.
//
// Fonts: where the brand's own face is a free Google Font it's used as-is
// (Oswald, Roboto, Open Sans, Mohave, Montserrat, Assistant, Nunito Sans,
// Anton). The paid faces can't be loaded from our page, so the closest
// Google Font stands in:
//   Brunswick DIN          -> Barlow Condensed
//   Brunswick/DV8 Usual    -> Inter
//   Radical Proxima Nova   -> Montserrat
//   DV8 DDC Hardware       -> Bungee (picked side by side against
//                             dv8bowling.com's own "TONIGHT WE BOWL.")
//
// The overlay sits on a dark video, so a brand color that's too dark to
// read there (Brunswick blue, Ebonite navy, Storm sage) is used for FILLS
// -- the "Drilled" tag, label borders, panels -- with white text and
// lines, never as small text on the video.

export type SignageTheme = {
  accent: string; // brand color: "Drilled" tag, label borders, chart dot
  onAccent: string; // text on an accent fill
  line: string; // callout lines + dots, must read on the video
  keyText: string; // brand name + label keys
  price: string; // the big drilled price
  panel: string; // label / badge background
  shade: string; // "r,g,b" for the top/bottom readability gradients
  heading: { family: string; weight: number; italic?: boolean; tracking: string };
  body: { family: string; weight: number };
  radius: number; // label + badge corners (Hammer/Radical/DV8 are square on their sites)
  fonts: string; // Google Fonts css2 family query
};

const DEFAULT: SignageTheme = {
  accent: "#fbbf24",
  onAccent: "#111827",
  line: "#fbbf24",
  keyText: "#fbbf24",
  price: "#fbbf24",
  panel: "rgba(8,10,30,0.72)",
  shade: "5,8,25",
  heading: { family: "Space Grotesk", weight: 700, tracking: "0px" },
  body: { family: "Space Grotesk", weight: 600 },
  radius: 18,
  fonts: "family=Space+Grotesk:wght@500;600;700",
};

const THEMES: Record<string, SignageTheme> = {
  brunswick: {
    accent: "#0000f0", onAccent: "#ffffff", line: "#ffffff", keyText: "#ffffff", price: "#ffffff",
    panel: "rgba(6,27,43,0.8)", shade: "6,27,43",
    heading: { family: "Barlow Condensed", weight: 600, tracking: "1px" },
    body: { family: "Inter", weight: 500 }, radius: 4,
    fonts: "family=Barlow+Condensed:wght@500;600;700&family=Inter:wght@400;500;600",
  },
  storm: {
    accent: "#548277", onAccent: "#ffffff", line: "#a9cbc2", keyText: "#a9cbc2", price: "#ffffff",
    panel: "rgba(25,28,27,0.8)", shade: "25,28,27",
    heading: { family: "Oswald", weight: 700, italic: true, tracking: "0px" },
    body: { family: "Roboto", weight: 500 }, radius: 8,
    fonts: "family=Oswald:wght@500;700&family=Roboto:wght@400;500;700",
  },
  "roto grip": {
    accent: "#e21836", onAccent: "#ffffff", line: "#e21836", keyText: "#ff4d63", price: "#ffffff",
    panel: "rgba(25,28,27,0.8)", shade: "25,28,27",
    heading: { family: "Oswald", weight: 700, tracking: "0px" },
    body: { family: "Roboto", weight: 500 }, radius: 8,
    fonts: "family=Oswald:wght@500;700&family=Roboto:wght@400;500;700",
  },
  "900 global": {
    accent: "#f5a81c", onAccent: "#111111", line: "#f5a81c", keyText: "#f5a81c", price: "#f5a81c",
    panel: "rgba(17,17,17,0.8)", shade: "17,17,17",
    heading: { family: "Oswald", weight: 700, tracking: "0px" },
    body: { family: "Roboto", weight: 500 }, radius: 8,
    fonts: "family=Oswald:wght@500;700&family=Roboto:wght@400;500;700",
  },
  hammer: {
    accent: "#fe5000", onAccent: "#ffffff", line: "#fe5000", keyText: "#fe5000", price: "#ffffff",
    panel: "rgba(0,0,0,0.8)", shade: "0,0,0",
    heading: { family: "Open Sans", weight: 800, tracking: "0px" },
    body: { family: "Open Sans", weight: 600 }, radius: 0,
    fonts: "family=Open+Sans:wght@600;700;800",
  },
  motiv: {
    accent: "#e63201", onAccent: "#ffffff", line: "#e63201", keyText: "#ff5a2e", price: "#ffffff",
    panel: "rgba(16,16,16,0.8)", shade: "16,16,16",
    heading: { family: "Mohave", weight: 600, tracking: "2px" },
    body: { family: "Montserrat", weight: 500 }, radius: 4,
    fonts: "family=Mohave:wght@500;600;700&family=Montserrat:wght@400;500;600",
  },
  radical: {
    accent: "#fff200", onAccent: "#000000", line: "#fff200", keyText: "#fff200", price: "#fff200",
    panel: "rgba(0,0,0,0.8)", shade: "0,0,0",
    heading: { family: "Montserrat", weight: 800, tracking: "0px" },
    body: { family: "Montserrat", weight: 500 }, radius: 0,
    fonts: "family=Montserrat:wght@500;600;800",
  },
  ebonite: {
    accent: "#002d72", onAccent: "#ffffff", line: "#ffffff", keyText: "#ffffff", price: "#ffffff",
    panel: "rgba(0,45,114,0.82)", shade: "0,20,55",
    heading: { family: "Open Sans", weight: 800, tracking: "0px" },
    body: { family: "Assistant", weight: 600 }, radius: 4,
    fonts: "family=Open+Sans:wght@700;800&family=Assistant:wght@500;600",
  },
  track: {
    accent: "#ffd700", onAccent: "#000000", line: "#ffd700", keyText: "#ffd700", price: "#ffd700",
    panel: "rgba(0,0,0,0.8)", shade: "0,0,0",
    heading: { family: "Nunito Sans", weight: 900, tracking: "0px" },
    body: { family: "Nunito Sans", weight: 600 }, radius: 6,
    fonts: "family=Nunito+Sans:wght@600;700;900",
  },
  dv8: {
    accent: "#ffffff", onAccent: "#000000", line: "#ffffff", keyText: "#ffffff", price: "#ffffff",
    panel: "rgba(0,0,0,0.82)", shade: "0,0,0",
    heading: { family: "Bungee", weight: 400, tracking: "1px" },
    body: { family: "Inter", weight: 500 }, radius: 0,
    fonts: "family=Bungee&family=Inter:wght@400;500;600",
  },
  swag: {
    accent: "#e8202a", onAccent: "#ffffff", line: "#e8202a", keyText: "#ff4a52", price: "#ffffff",
    panel: "rgba(17,19,24,0.82)", shade: "17,19,24",
    heading: { family: "Anton", weight: 400, tracking: "1px" },
    body: { family: "Noto Sans", weight: 500 }, radius: 4,
    fonts: "family=Anton&family=Noto+Sans:wght@400;500;600",
  },
};

export function signageTheme(brand?: string | null): SignageTheme {
  return THEMES[(brand ?? "").trim().toLowerCase()] ?? DEFAULT;
}

// CSS custom properties for .hx-canvas -- HERO_CSS reads only these.
export function themeVars(t: SignageTheme): Record<string, string> {
  return {
    "--sg-accent": t.accent,
    "--sg-on-accent": t.onAccent,
    "--sg-line": t.line,
    "--sg-key": t.keyText,
    "--sg-price": t.price,
    "--sg-panel": t.panel,
    "--sg-shade": t.shade,
    "--sg-head-font": `"${t.heading.family}", "Space Grotesk", Arial, sans-serif`,
    "--sg-head-weight": String(t.heading.weight),
    "--sg-head-style": t.heading.italic ? "italic" : "normal",
    "--sg-head-tracking": t.heading.tracking,
    "--sg-body-font": `"${t.body.family}", "Space Grotesk", Arial, sans-serif`,
    "--sg-body-weight": String(t.body.weight),
    "--sg-radius": `${t.radius}px`,
  };
}
