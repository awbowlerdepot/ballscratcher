import { useState, type ReactNode } from "react";
import type { SkuStockHistoryResult } from "../../api/types";
import { DEFAULT_CHART_RANGE, filterByRange, forecastHorizonDays, rangeBoundsMs } from "../../lib/chartRanges";
import { FORECAST_LOOKBACK_DAYS, computeSkuForecast, fmtForecastDate, latestSkuReadings } from "../../lib/skuForecast";
import Badge from "../Badge";
import ChartRangePicker from "./ChartRangePicker";
import TimeSeriesChart, { CHART_COLORS, type ChartSeries } from "./TimeSeriesChart";

function fmtDateTime(iso: string): string {
  return new Date(iso).toLocaleString();
}

// Restores the SKU stock forecasting feature (Al's original ask: "with
// the data for stock levels can we apply some industry standard
// inventory forecasting to that data", plus the later chart-upgrade ask)
// that existed in admin-site/index.html's buildSkuStockSection/
// renderSkuStockChart but never got ported when the SKUs & Stock tab
// moved to admin-spa -- both the summary table AND the chart were
// dropped, not just the chart Al noticed missing. See
// lib/skuForecast.ts's own comment for the Daily Movement/DOS
// methodology.
export default function SkuStockChart({ data }: { data: SkuStockHistoryResult }) {
  const [rangeKey, setRangeKey] = useState(DEFAULT_CHART_RANGE);

  const skus = data.skus;
  // Readings with a null quantity (BigCommerce not tracking that
  // variant's inventory) are skipped everywhere below, same "skip what
  // we don't know" stance the original chart took.
  const rawHistory = data.history.filter((h) => h.quantity !== null && h.quantity !== undefined);
  const latestBySku = latestSkuReadings(rawHistory);
  const rangeFiltered = filterByRange(rawHistory, (h) => new Date(h.checked_at).getTime(), rangeKey);

  const now = Date.now();
  const horizonMs = forecastHorizonDays(rangeKey) * 24 * 60 * 60 * 1000;
  let forecastMaxX = now;

  const series: ChartSeries[] = [];
  skus.forEach((s, i) => {
    const color = CHART_COLORS[i % CHART_COLORS.length];
    const actualPoints = rangeFiltered
      .filter((h) => h.product_sku_id === s.id)
      .slice()
      .sort((a, b) => new Date(a.checked_at).getTime() - new Date(b.checked_at).getTime())
      .map((h) => ({ x: new Date(h.checked_at).getTime(), y: h.quantity as number }));
    series.push({ key: `actual_${s.id}`, label: `${s.weight_lbs}lb`, color, points: actualPoints });

    // Dashed forecast projection past "today", reusing the SAME full
    // unfiltered history the forecast rate is always computed from
    // (a fixed 30-day trailing figure, independent of the display
    // range) -- not the range-filtered actualPoints above.
    const latest = latestBySku[s.id];
    if (latest) {
      const forecast = computeSkuForecast(s.id, rawHistory, latest.quantity);
      if (forecast.daysOfSupply !== null && forecast.stockoutDate) {
        const startX = new Date(latest.checked_at).getTime();
        const startY = latest.quantity as number;
        const stockoutX = forecast.stockoutDate.getTime();
        const endX = Math.min(stockoutX, now + horizonMs);
        const fraction = stockoutX > startX ? (endX - startX) / (stockoutX - startX) : 1;
        const endY = Math.max(0, startY * (1 - fraction));
        forecastMaxX = Math.max(forecastMaxX, endX);
        series.push({
          key: `forecast_${s.id}`,
          label: `${s.weight_lbs}lb (forecast)`,
          color,
          dashed: true,
          points: [
            { x: startX, y: startY },
            { x: endX, y: endY },
          ],
        });
      }
    }
  });

  const bounds = rangeBoundsMs(rangeKey, rangeFiltered.map((h) => new Date(h.checked_at).getTime()));
  const domain: [number, number] = [bounds.min, Math.max(bounds.max, forecastMaxX)];

  return (
    <div className="flex flex-col gap-3">
      <div>
        <p className="mb-1 text-sm font-semibold text-ink-800">SKU stock (BowlerDepot, per weight)</p>
        {skus.length === 0 ? (
          <p className="text-sm text-ink-500">No SKU stock data yet -- recorded alongside a BowlerDepot price check once a SKU's weight matches a variant.</p>
        ) : (
          <>
            <div className="overflow-x-auto rounded-md border border-ink-200">
              <table className="w-full text-left text-xs">
                <thead className="bg-ink-50 text-ink-500">
                  <tr>
                    <th className="px-2 py-1">Weight</th>
                    <th className="px-2 py-1">Quantity</th>
                    <th className="px-2 py-1">Avg Daily Movement</th>
                    <th className="px-2 py-1">Days of supply</th>
                    <th className="px-2 py-1">Est. stockout</th>
                    <th className="px-2 py-1">Last checked</th>
                  </tr>
                </thead>
                <tbody>
                  {skus.map((s) => {
                    const latest = latestBySku[s.id];
                    const forecast = computeSkuForecast(s.id, rawHistory, latest ? latest.quantity : null);
                    let dosCell: ReactNode = <span className="text-ink-400">—</span>;
                    if (forecast.daysOfSupply === 0) {
                      dosCell = <Badge tone="danger">out of stock</Badge>;
                    } else if (forecast.daysOfSupply !== null) {
                      const days = Math.round(forecast.daysOfSupply);
                      const tone = days <= 14 ? "danger" : days <= 30 ? "muted" : "ok";
                      dosCell = <Badge tone={tone}>{days}d</Badge>;
                    }
                    return (
                      <tr key={s.id} className="border-t border-ink-200">
                        <td className="px-2 py-1">{s.weight_lbs}lb</td>
                        <td className="px-2 py-1">{latest && latest.quantity !== null ? latest.quantity : <span className="text-ink-400">unknown</span>}</td>
                        <td className="px-2 py-1">
                          {forecast.dailyMovement === null ? (
                            <span className="text-ink-400">—</span>
                          ) : forecast.dailyMovement > 0 ? (
                            `${forecast.dailyMovement.toFixed(2)}/day`
                          ) : (
                            <span className="text-ink-400">no recent sales</span>
                          )}
                        </td>
                        <td className="px-2 py-1">{dosCell}</td>
                        <td className="px-2 py-1 text-ink-500">{forecast.stockoutDate ? fmtForecastDate(forecast.stockoutDate) : "—"}</td>
                        <td className="px-2 py-1 text-ink-500">{latest ? fmtDateTime(latest.checked_at) : "never checked"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <p className="mt-1 text-xs text-ink-500">
              Forecast columns use a trailing {FORECAST_LOOKBACK_DAYS}-day Avg Daily Movement -- see DEPLOY_RUNBOOK.md for the full methodology.
            </p>
          </>
        )}
      </div>

      {skus.length > 0 && (
        <div>
          <div className="mb-1 flex items-center justify-between">
            <span className="text-xs font-medium text-ink-600">Quantity over time</span>
            <ChartRangePicker value={rangeKey} onChange={setRangeKey} />
          </div>
          <TimeSeriesChart
            series={series}
            domain={domain}
            yDomainMin={0}
            yTickFormat={(v) => v.toFixed(0)}
            tooltipValueFormat={(v) => v.toFixed(0)}
            showTodayLine={forecastMaxX > now}
            emptyMessage="No SKU stock quantities recorded yet in this range -- once a BowlerDepot check matches a SKU's weight, its quantity charts here."
          />
        </div>
      )}
    </div>
  );
}
