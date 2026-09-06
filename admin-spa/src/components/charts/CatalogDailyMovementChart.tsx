import { useState } from "react";
import type { CatalogDailyMovementHistoryPoint } from "../../api/types";
import { DEFAULT_CHART_RANGE, filterByRange, rangeBoundsMs } from "../../lib/chartRanges";
import ChartRangePicker from "./ChartRangePicker";
import TimeSeriesChart, { CHART_COLORS } from "./TimeSeriesChart";

// Al, same session as the SKU stock chart: "can we add some data over
// time charts to the dashboard, maybe total catalog adu over time
// similar to what we have per product 7d, 30d, 90d, 1y and all picker"
// (predates the ADU->Daily Movement rename). Backend
// (get_catalog_daily_movement_history) has existed since that ask; this
// is the chart that was designed for it but never actually got built
// into admin-spa's Dashboard.
export default function CatalogDailyMovementChart({ items }: { items: CatalogDailyMovementHistoryPoint[] }) {
  const [rangeKey, setRangeKey] = useState(DEFAULT_CHART_RANGE);

  const rows = items.map((it) => ({ x: new Date(it.day).getTime(), y: it.total_daily_movement }));
  const filtered = filterByRange(rows, (r) => r.x, rangeKey);
  const bounds = rangeBoundsMs(rangeKey, filtered.map((r) => r.x));

  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <span className="text-xs font-medium text-ink-600">Total Avg Daily Movement over time</span>
        <ChartRangePicker value={rangeKey} onChange={setRangeKey} />
      </div>
      <TimeSeriesChart
        series={[{ key: "total_daily_movement", label: "Total Avg Daily Movement", color: CHART_COLORS[0], points: filtered }]}
        domain={[bounds.min, bounds.max]}
        yDomainMin={0}
        yTickFormat={(v) => v.toFixed(0)}
        tooltipValueFormat={(v) => v.toFixed(1)}
        emptyMessage="No SKU stock history recorded yet in this range -- this fills in once BowlerDepot price checks start recording quantities."
      />
    </div>
  );
}
