import { useState } from "react";
import type { PriceHistoryResult } from "../../api/types";
import { DEFAULT_CHART_RANGE, filterByRange, rangeBoundsMs } from "../../lib/chartRanges";
import ChartRangePicker from "./ChartRangePicker";
import TimeSeriesChart, { CHART_COLORS, type ChartSeries } from "./TimeSeriesChart";

// Same gap as SkuStockChart -- admin-site/index.html's renderPriceChart
// never made it into admin-spa's Pricing tab, which currently only shows
// a raw "Recent price checks" list. No forecast line here (price has no
// analogous "days of supply" concept), just the per-source price-over-
// time series with the same range picker.
export default function PriceHistoryChart({ data }: { data: PriceHistoryResult }) {
  const [rangeKey, setRangeKey] = useState(DEFAULT_CHART_RANGE);

  const withPrice = data.history.filter((h) => h.price !== null && h.price !== undefined);
  const filtered = filterByRange(withPrice, (h) => new Date(h.checked_at).getTime(), rangeKey);

  const series: ChartSeries[] = data.sources.map((s, i) => ({
    key: s.id,
    label: s.site_name,
    color: CHART_COLORS[i % CHART_COLORS.length],
    points: filtered
      .filter((h) => h.price_source_id === s.id)
      .slice()
      .sort((a, b) => new Date(a.checked_at).getTime() - new Date(b.checked_at).getTime())
      .map((h) => ({ x: new Date(h.checked_at).getTime(), y: h.price as number })),
  }));

  const bounds = rangeBoundsMs(rangeKey, filtered.map((h) => new Date(h.checked_at).getTime()));

  if (data.sources.length === 0) return null;

  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <span className="text-xs font-medium text-ink-600">Price over time</span>
        <ChartRangePicker value={rangeKey} onChange={setRangeKey} />
      </div>
      <TimeSeriesChart
        series={series}
        domain={[bounds.min, bounds.max]}
        yDomainMin={0}
        yTickFormat={(v) => `$${v.toFixed(2)}`}
        tooltipValueFormat={(v) => `$${v.toFixed(2)}`}
        emptyMessage="No successful price checks yet in this range -- once an approved source is checked, its price history charts here."
      />
    </div>
  );
}
