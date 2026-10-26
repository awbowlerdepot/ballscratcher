// Two-handle min/max slider for the kiosk (runbook 6ew). Al: "can we
// improve the filtering to be sliders with min an max values". Two native
// range inputs on one track (index.css .range-dual) with 44px thumbs for a
// finger; filters live as it moves (the kiosk filters patterns on the
// device). A handle at the end means "no limit".
export default function RangeSlider({
  label,
  min,
  max,
  step,
  value,
  onChange,
  format = (v) => String(v),
}: {
  label: string;
  min: number;
  max: number;
  step: number;
  value: [number, number];
  onChange: (v: [number, number]) => void;
  format?: (v: number) => string;
}) {
  const [lo, hi] = value;
  const pct = (v: number) => ((v - min) / (max - min || 1)) * 100;
  const active = lo > min || hi < max;
  return (
    <div className="flex items-center gap-6">
      <span className="w-24 shrink-0 text-lg font-semibold text-mute">{label}</span>
      <div className="range-dual relative h-14 flex-1">
        <div className="absolute inset-x-0 top-1/2 h-2 -translate-y-1/2 rounded-full bg-edge" />
        <div
          className="absolute top-1/2 h-2 -translate-y-1/2 rounded-full bg-brand"
          style={{ left: `${pct(lo)}%`, right: `${100 - pct(hi)}%` }}
        />
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={lo}
          aria-label={`${label} minimum`}
          onChange={(e) => onChange([Math.min(Number(e.target.value), hi), hi])}
        />
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={hi}
          aria-label={`${label} maximum`}
          onChange={(e) => onChange([lo, Math.max(Number(e.target.value), lo)])}
        />
      </div>
      <span className={`w-48 shrink-0 text-right text-xl font-semibold tabular-nums ${active ? "text-ink" : "text-dim"}`}>
        {active ? `${format(lo)} – ${format(hi)}` : "Any"}
      </span>
    </div>
  );
}
