import { CHART_RANGE_PRESETS } from "../../lib/chartRanges";

interface ChartRangePickerProps {
  value: string;
  onChange: (rangeKey: string) => void;
}

// 7D/30D/90D/1Y/All -- the same range picker admin-site/index.html
// established for the price/SKU-stock charts, now shared across every
// time-series chart in admin-spa (see TimeSeriesChart's own comment).
export default function ChartRangePicker({ value, onChange }: ChartRangePickerProps) {
  return (
    <div className="flex gap-1">
      {CHART_RANGE_PRESETS.map((p) => (
        <button
          key={p.key}
          type="button"
          onClick={() => onChange(p.key)}
          className={`rounded-md px-2 py-0.5 text-xs font-medium ${
            p.key === value ? "bg-primary text-white" : "text-ink-500 hover:bg-ink-200"
          }`}
        >
          {p.label}
        </button>
      ))}
    </div>
  );
}
