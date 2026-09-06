import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { fmtChartAxisDate } from "../../lib/chartRanges";

// Bright, distinct hues chosen against admin-spa's dark "ink" background
// (see tailwind.config.js's restyle comment) -- admin-site/index.html's
// original CHART_COLORS were tuned for a light page and would read as
// low-contrast here.
export const CHART_COLORS = ["#6366f1", "#4ade80", "#f59e0b", "#f472b6", "#22d3ee", "#a78bfa"];

export interface ChartSeries {
  key: string;
  label: string;
  color: string;
  points: { x: number; y: number }[];
  // Dashed rendering + hidden from the legend -- used for a forecast
  // projection (see SkuStockChart) so it doesn't double up the legend
  // with a solid+dashed entry per real series.
  dashed?: boolean;
}

interface TimeSeriesChartProps {
  series: ChartSeries[];
  domain: [number, number];
  height?: number;
  yDomainMin?: number;
  yTickFormat?: (v: number) => string;
  tooltipValueFormat?: (v: number) => string;
  showTodayLine?: boolean;
  emptyMessage: string;
}

// Generic multi-series time-series line chart shared by the SKU stock,
// price history, and catalog Daily Movement charts -- ports the visual
// design Al asked for on admin-site/index.html's Chart.js version
// ("upgrade the charts... date time selection... hover state for each
// of the data points... show pop overs for each values for each of the
// days all at once") onto recharts, per Al's ask to use recharts here.
//
// Recharts' <LineChart> takes ONE shared data array (unlike Chart.js,
// where each dataset carries its own independent [{x,y}] array) --
// different series here get checked at different, non-aligned
// timestamps (different SKUs/sources/days), so points are merged into
// sparse rows keyed by every distinct x value across all series, one
// column per series key, undefined where that series has no reading at
// that exact x. `connectNulls` on each <Line> then draws a continuous
// line across those gaps, the same visual result Chart.js got for free
// from each dataset's own independent array.
export default function TimeSeriesChart({
  series,
  domain,
  height = 220,
  yDomainMin,
  yTickFormat,
  tooltipValueFormat,
  showTodayLine = false,
  emptyMessage,
}: TimeSeriesChartProps) {
  const visible = series.filter((s) => s.points.length > 0);
  if (visible.length === 0) {
    return <p className="py-8 text-center text-sm text-ink-500">{emptyMessage}</p>;
  }

  const xValues = new Set<number>();
  for (const s of visible) {
    for (const p of s.points) xValues.add(p.x);
  }
  const sortedX = Array.from(xValues).sort((a, b) => a - b);
  const rows: Record<string, number>[] = sortedX.map((x) => ({ x }));
  const rowByX = new Map(rows.map((r) => [r.x, r]));
  for (const s of visible) {
    for (const p of s.points) {
      const row = rowByX.get(p.x);
      if (row) row[s.key] = p.y;
    }
  }

  const labelByKey = Object.fromEntries(visible.map((s) => [s.key, s.label]));
  const now = Date.now();

  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={rows} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
        <CartesianGrid stroke="#2c2d31" />
        <XAxis
          dataKey="x"
          type="number"
          domain={domain}
          tickFormatter={fmtChartAxisDate}
          stroke="#8b8d94"
          tick={{ fontSize: 10 }}
        />
        <YAxis domain={[yDomainMin ?? "auto", "auto"]} tickFormatter={yTickFormat} stroke="#8b8d94" tick={{ fontSize: 10 }} />
        <Tooltip
          contentStyle={{ background: "#212226", border: "1px solid #3a3b40", fontSize: 12 }}
          labelStyle={{ color: "#cfd1d6" }}
          labelFormatter={(x: number) => new Date(x).toLocaleString()}
          formatter={(value: number, key: string) => [tooltipValueFormat ? tooltipValueFormat(value) : value, labelByKey[key] ?? key]}
        />
        <Legend wrapperStyle={{ fontSize: 11, color: "#8b8d94" }} formatter={(key: string) => labelByKey[key] ?? key} />
        {showTodayLine && now >= domain[0] && now <= domain[1] && (
          <ReferenceLine x={now} stroke="#8b8d94" label={{ value: "Today", position: "top", fill: "#8b8d94", fontSize: 10 }} />
        )}
        {visible.map((s) => (
          <Line
            key={s.key}
            dataKey={s.key}
            name={s.key}
            stroke={s.color}
            strokeDasharray={s.dashed ? "6 4" : undefined}
            // Dashed forecast lines are hidden from the legend -- the
            // dash style already distinguishes them from the real
            // series, and a solid+dashed entry per series would just
            // double the legend for no benefit.
            legendType={s.dashed ? "none" : "line"}
            dot={{ r: 2.5 }}
            activeDot={{ r: 5 }}
            connectNulls
            isAnimationActive={false}
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}
