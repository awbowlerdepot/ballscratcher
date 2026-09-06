// Industry-standard-ish inventory forecasting for SKU stock history --
// Al: "with the data for stock levels can we apply some industry
// standard inventory forecasting to that data." Ported verbatim (same
// methodology, same documented limits) from admin-site/index.html's
// computeSkuForecast/latestSkuReadings -- this project has no supplier
// lead-time or safety-stock policy data anywhere, so this stops at Avg
// Daily Movement/Days of Supply/an estimated stockout date rather than a
// full reorder-point recommendation.
//
// Daily Movement only counts DROPS between consecutive readings as
// "sold" (a rise is treated as a restock and excluded) -- same
// "drop=sold, rise=restock" reading get_sku_stock_history's own
// docstring documents as this project's chosen interpretation of the
// raw quantity-snapshot data. Daily Movement = total units sold in the
// window / elapsed days between the first and last reading in that
// window -- not simply "count of readings", since readings aren't
// guaranteed to land exactly once a day (rotation-based checking).

export interface SkuStockReading {
  product_sku_id: string;
  quantity: number | null;
  checked_at: string;
}

export interface SkuForecast {
  dailyMovement: number | null;
  daysOfSupply: number | null;
  stockoutDate: Date | null;
}

// Trailing window for Avg Daily Movement -- a standard, commonly-cited
// default lookback for a usage-rate calculation in inventory management.
// Fixed and independent of a chart's own display-range selector so the
// forecast doesn't jump around just because someone clicked a different
// range button.
export const FORECAST_LOOKBACK_DAYS = 30;

// Latest reading per SKU (by checked_at). Shared by the forecast summary
// table and a chart's forecast line -- both need "what do we currently
// have on hand, per SKU" from the same raw history.
export function latestSkuReadings(history: SkuStockReading[]): Record<string, SkuStockReading> {
  const latestBySku: Record<string, SkuStockReading> = {};
  for (const h of history) {
    const prev = latestBySku[h.product_sku_id];
    if (!prev || new Date(h.checked_at).getTime() > new Date(prev.checked_at).getTime()) {
      latestBySku[h.product_sku_id] = h;
    }
  }
  return latestBySku;
}

// Returns nulls across the board when there's not enough history (fewer
// than 2 readings in the lookback window) to compute a rate at all;
// daysOfSupply/stockoutDate are also null (not zero) when dailyMovement
// is 0 -- "no recent sales" isn't the same claim as "sold out", and
// conflating them would falsely flag a slow-moving weight as urgent.
export function computeSkuForecast(
  skuId: string,
  allHistory: SkuStockReading[],
  latestQuantity: number | null | undefined,
): SkuForecast {
  const cutoff = Date.now() - FORECAST_LOOKBACK_DAYS * 24 * 60 * 60 * 1000;
  const rows = allHistory
    .filter((h) => h.product_sku_id === skuId && h.quantity !== null && h.quantity !== undefined && new Date(h.checked_at).getTime() >= cutoff)
    .slice()
    .sort((a, b) => new Date(a.checked_at).getTime() - new Date(b.checked_at).getTime());

  if (rows.length < 2) return { dailyMovement: null, daysOfSupply: null, stockoutDate: null };

  let unitsSold = 0;
  for (let i = 1; i < rows.length; i++) {
    const drop = (rows[i - 1].quantity as number) - (rows[i].quantity as number);
    if (drop > 0) unitsSold += drop;
  }
  const elapsedDays =
    (new Date(rows[rows.length - 1].checked_at).getTime() - new Date(rows[0].checked_at).getTime()) / (24 * 60 * 60 * 1000);
  if (elapsedDays <= 0) return { dailyMovement: null, daysOfSupply: null, stockoutDate: null };

  const dailyMovement = unitsSold / elapsedDays;
  if (dailyMovement <= 0 || latestQuantity === null || latestQuantity === undefined) {
    return { dailyMovement, daysOfSupply: null, stockoutDate: null };
  }
  if (latestQuantity <= 0) {
    return { dailyMovement, daysOfSupply: 0, stockoutDate: new Date() };
  }
  const daysOfSupply = latestQuantity / dailyMovement;
  const stockoutDate = new Date(Date.now() + daysOfSupply * 24 * 60 * 60 * 1000);
  return { dailyMovement, daysOfSupply, stockoutDate };
}

export function fmtForecastDate(d: Date): string {
  try {
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  } catch {
    return "";
  }
}
