import { useEffect, useState } from "react";
import { lookupBalls, type PlotterLookupItem } from "../lib/api";
import { useKiosk } from "../lib/kiosk";
import { Keyboard, SearchField } from "./ui";

// Name search with the on-screen keyboard (runbook 6eh), over the plotter's
// ball lookup (current and retired balls). `status` narrows the results.
export default function BallSearch({
  placeholder,
  status,
  onPick,
}: {
  placeholder: string;
  status?: "current" | "retired";
  onPick: (item: PlotterLookupItem) => void;
}) {
  const { sessionKey } = useKiosk();
  const [q, setQ] = useState("");
  const [results, setResults] = useState<PlotterLookupItem[]>([]);
  useEffect(() => setQ(""), [sessionKey]);
  useEffect(() => {
    const term = q.trim();
    if (term.length < 2) {
      setResults([]);
      return;
    }
    let cancelled = false;
    const t = window.setTimeout(() => {
      lookupBalls(term)
        .then((items) => !cancelled && setResults(items.filter((i) => !status || i.status === status).slice(0, 12)))
        .catch(() => !cancelled && setResults([]));
    }, 250);
    return () => {
      cancelled = true;
      window.clearTimeout(t);
    };
  }, [q, status]);

  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-8">
      <div className="flex flex-col gap-4">
        <SearchField value={q} placeholder={placeholder} />
        {q.trim().length >= 2 && !results.length && <p className="text-xl text-mute">No balls match “{q}” yet. Keep typing or try the brand name.</p>}
        <div className="grid grid-cols-2 gap-3">
          {results.map((r) => (
            <button
              key={r.id}
              type="button"
              onClick={() => onPick(r)}
              className="flex min-h-[72px] items-center justify-between rounded-2xl border-2 border-edge bg-panel px-5 text-left active:border-brand"
            >
              <span>
                <span className="block text-sm font-semibold uppercase tracking-wider text-mute">{r.brand_name}</span>
                <span className="font-display text-2xl font-bold text-ink">{r.name}</span>
              </span>
              {r.status === "retired" && <span className="rounded-full bg-hot/20 px-3 py-1 text-sm font-semibold text-hot">Retired</span>}
            </button>
          ))}
        </div>
      </div>
      <Keyboard value={q} onChange={setQ} />
    </div>
  );
}
