import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { getLearnPlotter } from "../../api/client";
import type { LearnPlotterPoint } from "../../api/types";
import PlotterChart from "./PlotterChart";
import PlotterNeighborPanel from "./PlotterNeighborPanel";
import { FULL_VIEW, ROLES, TENTHS_ZOOM, fitView, formatPosition, sourceLabel, type PlotterView } from "./plotterModel";

// "Where this ball sits" on a ball review article (runbook 6cc -- Al
// picked a full /plotter page PLUS a panel in each article). A zoomed-in
// slice of the plotter around this ball with its suggestions, next to the
// same suggestion list the /plotter page shows. Wheel zoom is off here so
// scrolling the article never gets trapped by the chart; the buttons and
// drag still work. Renders nothing for a ball that isn't on the plotter
// (retired balls -- the plotter is current balls only).

export default function PlotterArticlePanel({ productId }: { productId: string }) {
  const navigate = useNavigate();
  const [points, setPoints] = useState<LearnPlotterPoint[] | null>(null);
  const [view, setView] = useState<PlotterView>(FULL_VIEW);

  useEffect(() => {
    let cancelled = false;
    getLearnPlotter()
      .then((items) => {
        if (!cancelled) setPoints(items);
      })
      .catch(() => {
        if (!cancelled) setPoints([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const byId = useMemo(() => new Map((points ?? []).map((p) => [p.id, p])), [points]);
  const ball = byId.get(productId) ?? null;

  useEffect(() => {
    if (!ball) return;
    const related = ROLES.flatMap((r) => ball.neighbors[r.key]).map((n) => byId.get(n.id)).filter(Boolean) as LearnPlotterPoint[];
    setView(fitView([ball, ...related]));
  }, [ball, byId]);

  if (!ball) return null;

  const openInPlotter = (id: string) => navigate(`/plotter?ball=${encodeURIComponent(id)}`);

  return (
    <section className="mb-10 rounded-xl border border-paper-border bg-white p-5">
      <div className="mb-4 flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 className="font-display text-xl font-semibold text-ink">Where this ball sits</h2>
          <p className="text-sm text-muted">
            {formatPosition(ball, TENTHS_ZOOM)} on the ball motion chart · {sourceLabel(ball.oil_motion_source).toLowerCase()}
          </p>
        </div>
        <Link to={`/plotter?ball=${encodeURIComponent(ball.id)}`} className="text-sm font-semibold">
          Open in the plotter &rarr;
        </Link>
      </div>
      <div className="grid gap-6 md:grid-cols-[minmax(0,1fr)_17rem]">
        <PlotterChart
          points={points ?? []}
          byId={byId}
          selectedId={ball.id}
          onSelect={(id) => (id === ball.id ? undefined : openInPlotter(id))}
          view={view}
          onViewChange={setView}
          width={640}
          height={460}
          ariaLabel={`Ball motion plotter around the ${ball.brand_name} ${ball.name}`}
        />
        <PlotterNeighborPanel selected={ball} byId={byId} onSelect={openInPlotter} compact />
      </div>
    </section>
  );
}
