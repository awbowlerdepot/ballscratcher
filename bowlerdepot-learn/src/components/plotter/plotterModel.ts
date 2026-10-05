import type { LearnPlotterPoint, PlotterNeighbors } from "../../api/types";

// Ball motion plotter geometry + recommendation roles, shared by the
// /plotter page and the article "Where this ball sits" panel (runbook
// 6cc). Same axes as Brunswick's Ball Motion Comparison Chart: oil 1
// (light) -> 16 (heavy) across, motion 1 (smooth) -> 18 (angular) up.

export const OIL_MIN = 1;
export const OIL_MAX = 16;
export const MOTION_MIN = 1;
export const MOTION_MAX = 18;
// Room around the outermost grid lines so an edge ball isn't clipped.
const PAD = 0.75;
export const DOMAIN = {
  x0: OIL_MIN - PAD,
  x1: OIL_MAX + PAD,
  y0: MOTION_MIN - PAD,
  y1: MOTION_MAX + PAD,
};

export const ZOOM_MIN = 1;
export const ZOOM_MAX = 10;
// Al: "can we make these accurate down to on tenth so that we can support
// zooming in on the motion plotter? default is to round to the whole
// number". Below this zoom every ball snaps to the whole-number grid like
// the printed chart; at or above it, balls sit at their real tenths.
export const TENTHS_ZOOM = 2.5;

export interface PlotterView {
  cx: number; // data-space center (oil)
  cy: number; // data-space center (motion)
  k: number; // zoom factor, 1 = whole chart
}

export const FULL_VIEW: PlotterView = {
  cx: (DOMAIN.x0 + DOMAIN.x1) / 2,
  cy: (DOMAIN.y0 + DOMAIN.y1) / 2,
  k: 1,
};

export function spans(k: number) {
  return { sx: (DOMAIN.x1 - DOMAIN.x0) / k, sy: (DOMAIN.y1 - DOMAIN.y0) / k };
}

// Keeps the view inside the chart: zoom within limits, and the visible
// window never slides past an edge.
export function clampView(view: PlotterView): PlotterView {
  const k = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, view.k));
  const { sx, sy } = spans(k);
  const clampAxis = (c: number, lo: number, hi: number, span: number) =>
    span >= hi - lo ? (lo + hi) / 2 : Math.min(hi - span / 2, Math.max(lo + span / 2, c));
  return {
    k,
    cx: clampAxis(view.cx, DOMAIN.x0, DOMAIN.x1, sx),
    cy: clampAxis(view.cy, DOMAIN.y0, DOMAIN.y1, sy),
  };
}

// Zoom by `factor`, keeping data point (px, py) fixed under the cursor.
export function zoomAbout(view: PlotterView, factor: number, px: number, py: number): PlotterView {
  const k = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, view.k * factor));
  const ratio = view.k / k;
  return clampView({ k, cx: px + (view.cx - px) * ratio, cy: py + (view.cy - py) * ratio });
}

// A view centered on one ball, zoomed in far enough to show tenths and
// its nearby recommendations.
export function focusView(oil: number, motion: number, k = 3): PlotterView {
  return clampView({ cx: oil, cy: motion, k });
}

// The smallest view that fits a ball and all its recommendations -- never
// below TENTHS_ZOOM, so the selected ball and its suggestions sit at their
// real tenths instead of stacked on one whole-number spot.
export function fitView(points: { oil: number; motion: number }[], minK = TENTHS_ZOOM, maxK = 4): PlotterView {
  if (!points.length) return FULL_VIEW;
  const xs = points.map((p) => p.oil);
  const ys = points.map((p) => p.motion);
  const minX = Math.min(...xs) - 1.25;
  const maxX = Math.max(...xs) + 1.25;
  const minY = Math.min(...ys) - 1.25;
  const maxY = Math.max(...ys) + 1.25;
  const full = spans(1);
  const k = Math.max(minK, Math.min(maxK, full.sx / (maxX - minX), full.sy / (maxY - minY)));
  return clampView({ cx: (minX + maxX) / 2, cy: (minY + maxY) / 2, k });
}

// Chart margins in viewBox px (axis labels live in them).
export const CHART_MARGIN = { left: 40, right: 12, top: 12, bottom: 34 };

// Desktop floating details card (runbook 6ce): shift the view left by half
// the card's width so a focused ball and its suggestions land in the open
// part of the chart instead of under the card.
export function offsetForCard(view: PlotterView, chartWidth: number, cardPx: number): PlotterView {
  const plotW = chartWidth - CHART_MARGIN.left - CHART_MARGIN.right;
  return clampView({ ...view, cx: view.cx - (cardPx / 2) * (spans(view.k).sx / plotW) });
}

export function snap(value: number, decimals: number) {
  const f = 10 ** decimals;
  return Math.round(value * f) / f;
}

// Where a ball is drawn at this zoom: whole numbers at default zoom,
// real tenths once zoomed in.
export function displayPosition(p: { oil: number; motion: number }, k: number) {
  const d = k >= TENTHS_ZOOM ? 1 : 0;
  return { oil: snap(p.oil, d), motion: snap(p.motion, d) };
}

export function formatPosition(p: { oil: number; motion: number }, k: number) {
  const d = k >= TENTHS_ZOOM ? 1 : 0;
  return `oil ${snap(p.oil, d).toFixed(d)} · motion ${snap(p.motion, d).toFixed(d)}`;
}

// --- Recommendation roles ---------------------------------------------
// Al: "use this data to inform users of what balls would be similar
// across brand and what would be a bit more or less in all directions on
// the plotter".

export type PlotterRole = keyof PlotterNeighbors;

export const ROLES: { key: PlotterRole; label: string; short: string; blurb: string; color: string }[] = [
  {
    // Retired balls only (runbook 6cl).
    key: "closest",
    label: "Closest current balls",
    short: "Closest",
    blurb: "Current balls BowlerDepot sells that sit closest to where this retired ball was.",
    color: "#9d174d",
  },
  {
    key: "twins",
    label: "Similar from other brands",
    short: "Similar",
    blurb: "Close to the same spot on the chart from a different manufacturer.",
    color: "#1f439e",
  },
  {
    key: "more_oil",
    label: "Handles more oil",
    short: "More oil",
    blurb: "A step stronger: reads the lane earlier for heavier or longer patterns.",
    color: "#b45309",
  },
  {
    key: "less_oil",
    label: "For less oil",
    short: "Less oil",
    blurb: "A step weaker: more length and less hook for drier lanes.",
    color: "#0f766e",
  },
  {
    key: "more_angular",
    label: "More angular",
    short: "Angular",
    blurb: "Similar oil range with a sharper move off the dry backends.",
    color: "#7e22ce",
  },
  {
    key: "smoother",
    label: "Smoother",
    short: "Smoother",
    blurb: "Similar oil range with a more controllable, arcing motion.",
    color: "#0369a1",
  },
];

// The roles a ball actually has: current balls get twins + steps, retired
// balls get closest + steps.
export function rolesPresent(p: LearnPlotterPoint) {
  return ROLES.filter((r) => p.neighbors[r.key] !== undefined);
}

export const ROLE_BY_KEY = Object.fromEntries(ROLES.map((r) => [r.key, r])) as Record<
  PlotterRole,
  (typeof ROLES)[number]
>;

// id -> role for everything recommended for `selected` (first role wins
// if a ball somehow appears twice).
export function rolesFor(selected: LearnPlotterPoint | null | undefined): Map<string, PlotterRole> {
  const out = new Map<string, PlotterRole>();
  if (!selected) return out;
  for (const role of ROLES) {
    for (const n of selected.neighbors[role.key] ?? []) {
      if (!out.has(n.id)) out.set(n.id, role.key);
    }
  }
  return out;
}

export function sourceLabel(source: LearnPlotterPoint["oil_motion_source"]) {
  return source === "chart" ? "From the manufacturer's chart" : source === "manual" ? "Checked by our staff" : "Our estimate";
}
