// Shared 7D/30D/90D/1Y/All range-picker machinery for every time-series
// chart in admin-spa (SKU stock, price history, catalog Daily Movement).
// Ported from admin-site/index.html's CHART_RANGE_PRESETS/
// filterHistoryByRange/rangeBounds -- Al's original ask there: "for the
// date axis can you include all the days for the range selected with or
// without data with the far right being today." Kept as one shared
// module (rather than duplicated per page, admin-spa's usual small-
// formatter convention) because three different pages now need the
// exact same preset list and bounds math, not just a one-line helper.

export interface ChartRangePreset {
  key: string;
  label: string;
  days: number | null;
}

export const CHART_RANGE_PRESETS: ChartRangePreset[] = [
  { key: "7d", label: "7D", days: 7 },
  { key: "30d", label: "30D", days: 30 },
  { key: "90d", label: "90D", days: 90 },
  { key: "1y", label: "1Y", days: 365 },
  { key: "all", label: "All", days: null },
];

// Matches the old fixed 90-day default window.
export const DEFAULT_CHART_RANGE = "90d";

function presetFor(rangeKey: string): ChartRangePreset {
  return CHART_RANGE_PRESETS.find((p) => p.key === rangeKey) ?? CHART_RANGE_PRESETS[2];
}

// Filters full (already-fetched) history to the selected range, client
// side -- no re-fetch per range-button click.
export function filterByRange<T>(rows: T[], getTimestampMs: (row: T) => number, rangeKey: string): T[] {
  const preset = presetFor(rangeKey);
  if (preset.days === null) return rows;
  const cutoff = Date.now() - preset.days * 24 * 60 * 60 * 1000;
  return rows.filter((r) => getTimestampMs(r) >= cutoff);
}

// Fixes the x-axis domain to the FULL selected window (today as the
// right edge) independent of which timestamps actually have data, so a
// source that hasn't been checked in a few days visibly shows the gap
// rather than making the chart's right edge look "on schedule". 'All'
// has no fixed lookback, so its min is the earliest real timestamp.
export function rangeBoundsMs(rangeKey: string, timestampsMs: number[]): { min: number; max: number } {
  const now = Date.now();
  const preset = presetFor(rangeKey);
  if (preset.days === null) {
    return { min: timestampsMs.length ? Math.min(...timestampsMs) : now, max: now };
  }
  return { min: now - preset.days * 24 * 60 * 60 * 1000, max: now };
}

// How far past "today" a dashed forecast line is allowed to extend --
// mirrors the selected range's own day span so the forecast doesn't
// dwarf or vanish relative to however much history is on screen. 'All'
// falls back to a flat 90 days (same as the old default window).
const FORECAST_HORIZON_FALLBACK_DAYS = 90;
export function forecastHorizonDays(rangeKey: string): number {
  const preset = presetFor(rangeKey);
  return preset.days === null ? FORECAST_HORIZON_FALLBACK_DAYS : preset.days;
}

// month/day only -- axis ticks are space-constrained, unlike a tooltip's
// full date.
export function fmtChartAxisDate(epochMs: number): string {
  try {
    return new Date(epochMs).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  } catch {
    return "";
  }
}
