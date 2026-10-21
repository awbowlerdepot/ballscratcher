import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { getPlotter, img, type LearnPlotterPoint } from "../lib/api";
import PlotterChart from "./plotter/PlotterChart";
import { ROLES, fitView, sourceLabel, type PlotterRole, type PlotterView } from "./plotter/plotterModel";

// Plotter pieces shared by the ball page, "What's next for my bag" and "My
// ball was discontinued" (runbook 6eh). The chart is the Learn site's
// (copied), drawn on a white panel; tapping a ball opens its kiosk page.

export function usePlotterIndex() {
  const [points, setPoints] = useState<LearnPlotterPoint[] | null>(null);
  useEffect(() => {
    getPlotter().then(setPoints).catch(() => setPoints([]));
  }, []);
  const byId = useMemo(() => new Map((points ?? []).map((p) => [p.id, p])), [points]);
  return { points, byId };
}

const CHART = { width: 1200, height: 520 }; // ~780px tall at full kiosk width: fits on screen with its caption

export function FocusPlotter({ ball, points, byId }: { ball: LearnPlotterPoint; points: LearnPlotterPoint[]; byId: Map<string, LearnPlotterPoint> }) {
  const navigate = useNavigate();
  const allById = useMemo(() => {
    if (byId.has(ball.id)) return byId;
    const m = new Map(byId);
    m.set(ball.id, ball); // a retired ball isn't in the current list
    return m;
  }, [byId, ball]);
  const related = useMemo(
    () => ROLES.flatMap((r) => ball.neighbors[r.key] ?? []).map((n) => allById.get(n.id)).filter(Boolean) as LearnPlotterPoint[],
    [ball, allById],
  );
  const [view, setView] = useState<PlotterView>(() => fitView([ball, ...related]));
  useEffect(() => setView(fitView([ball, ...related])), [ball, related]);
  const drawn = useMemo(() => (byId.has(ball.id) ? points : [...points, ball]), [points, byId, ball]);
  return (
    <div className="overflow-hidden rounded-3xl bg-white p-3">
      <PlotterChart
        points={drawn}
        byId={allById}
        selectedId={ball.id}
        onSelect={(id) => id !== ball.id && navigate(`/ball/${id}`)}
        view={view}
        onViewChange={setView}
        width={CHART.width}
        height={CHART.height}
        ariaLabel={`Ball motion plotter centered on ${ball.name}`}
      />
      <div className="flex flex-wrap items-center justify-between gap-3 px-3 pb-1 pt-3 text-lg text-[#45453f]">
        <span>
          <strong className="text-[#0f0f2d]">{ball.name}</strong>: oil {ball.oil.toFixed(1)} · motion {ball.motion.toFixed(1)} ·{" "}
          {sourceLabel(ball.oil_motion_source)}
        </span>
        <span className="text-base">Drag to move · pinch to zoom · tap a ball to open it</span>
      </div>
    </div>
  );
}

export function Suggestions({
  ball,
  byId,
  roles,
  title,
}: {
  ball: LearnPlotterPoint;
  byId: Map<string, LearnPlotterPoint>;
  roles?: PlotterRole[];
  title?: string;
}) {
  const groups = ROLES.filter((r) => (!roles || roles.includes(r.key)) && (ball.neighbors[r.key]?.length ?? 0) > 0);
  if (!groups.length) return null;
  return (
    <div>
      {title && <h2 className="mb-5 font-display text-4xl font-bold text-ink">{title}</h2>}
      <div className="grid grid-cols-2 gap-6">
        {groups.map((r) => (
          <div key={r.key} className="rounded-3xl border-2 border-edge bg-panel p-6">
            <div className="mb-1 flex items-center gap-3">
              <span className="h-4 w-4 rounded-full" style={{ background: r.color }} />
              <span className="font-display text-2xl font-bold text-ink">{r.label}</span>
            </div>
            <p className="mb-4 text-lg text-mute">{r.blurb}</p>
            <div className="grid grid-cols-3 gap-3">
              {(ball.neighbors[r.key] ?? [])
                .slice(0, 3)
                .map((n) => byId.get(n.id))
                .filter(Boolean)
                .map((p) => (
                  <Link key={p!.id} to={`/ball/${p!.id}`} className="rounded-2xl border-2 border-edge bg-panel-2 p-3 active:border-brand">
                    <div className="aspect-square overflow-hidden rounded-xl bg-white/95">
                      {p!.primary_image_url && <img src={img(p!.primary_image_url, 220, 220) ?? ""} alt="" className="h-full w-full object-contain p-2" />}
                    </div>
                    <div className="mt-2 text-xs font-semibold uppercase tracking-wider text-mute">{p!.brand_name}</div>
                    <div className="font-display text-lg font-bold leading-tight text-ink">{p!.name}</div>
                  </Link>
                ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
