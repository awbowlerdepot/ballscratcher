import { useEffect, useState } from "react";

// Two-handle min/max slider (runbook 6ew). Al: "can we improve the
// filtering to be sliders with min an max values". Two native range inputs
// on one track (styles in index.css .range-dual), so it works with mouse,
// touch and the keyboard without a dependency. `value` holds null for an
// open end (handle at the edge = no limit); onChange fires on release,
// so dragging doesn't refetch on every pixel.
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
  value: [number | null, number | null];
  onChange: (v: [number | null, number | null]) => void;
  format?: (v: number) => string;
}) {
  const [lo, setLo] = useState(value[0] ?? min);
  const [hi, setHi] = useState(value[1] ?? max);
  useEffect(() => {
    setLo(value[0] ?? min);
    setHi(value[1] ?? max);
  }, [value[0], value[1], min, max]); // eslint-disable-line react-hooks/exhaustive-deps

  const commit = (a: number, b: number) => onChange([a <= min ? null : a, b >= max ? null : b]);
  const pct = (v: number) => ((v - min) / (max - min || 1)) * 100;
  const active = lo > min || hi < max;

  return (
    <div className="flex min-w-[13rem] flex-1 flex-col gap-1">
      <div className="flex items-baseline justify-between text-xs">
        <span className={`font-medium ${active ? "text-primary-dark" : "text-ink-600"}`}>{label}</span>
        <span className="tabular-nums text-ink-500">
          {active ? `${format(lo)} – ${format(hi)}` : "any"}
        </span>
      </div>
      <div className="range-dual relative h-5">
        <div className="absolute inset-x-0 top-1/2 h-1 -translate-y-1/2 rounded bg-ink-200" />
        <div className="absolute top-1/2 h-1 -translate-y-1/2 rounded bg-primary" style={{ left: `${pct(lo)}%`, right: `${100 - pct(hi)}%` }} />
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={lo}
          aria-label={`${label} minimum`}
          onChange={(e) => setLo(Math.min(Number(e.target.value), hi))}
          onPointerUp={() => commit(lo, hi)}
          onKeyUp={() => commit(lo, hi)}
        />
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={hi}
          aria-label={`${label} maximum`}
          onChange={(e) => setHi(Math.max(Number(e.target.value), lo))}
          onPointerUp={() => commit(lo, hi)}
          onKeyUp={() => commit(lo, hi)}
        />
      </div>
    </div>
  );
}
