import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { getLearnPlotter, getPlotterBall } from "../../api/client";
import type { LearnPlotterPoint } from "../../api/types";
import PlotterChart from "./PlotterChart";
import PlotterNeighborPanel, { PlotterNeighborCard } from "./PlotterNeighborPanel";
import {
  FULL_VIEW,
  ROLES,
  TENTHS_ZOOM,
  fitView,
  formatPosition,
  offsetForCard,
  sourceLabel,
  type PlotterView,
} from "./plotterModel";
import { useIsDesktop } from "./useIsDesktop";

// "Where this ball sits" on a ball review article (runbook 6cc -- Al
// picked a full /plotter page PLUS a panel in each article). A zoomed-in
// slice of the plotter around this ball with its suggestions. Wheel zoom
// is off here so scrolling the article never gets trapped by the chart;
// the buttons and drag still work. Renders nothing for a ball that isn't
// on the plotter (retired balls -- the plotter is current balls only).
//
// Same layout as the /plotter page (runbook 6ci -- Al: "the plotter inline
// in articles didn't get the styling updates"): on desktop a full-width
// chart with the floating details card (Hide / Show details; no Clear,
// since this article's ball is always the selection), the view shifted so
// the ball clears the card; on smaller screens the list stacks underneath.

const DESKTOP_CHART = { width: 1200, height: 620 };
const MOBILE_CHART = { width: 640, height: 520 };
const CARD_PX = 330;

export default function PlotterArticlePanel({ productId }: { productId: string }) {
  const navigate = useNavigate();
  const isDesktop = useIsDesktop();
  const [points, setPoints] = useState<LearnPlotterPoint[] | null>(null);
  const [view, setView] = useState<PlotterView>(FULL_VIEW);
  const [cardCollapsed, setCardCollapsed] = useState(false);
  const chart = isDesktop ? DESKTOP_CHART : MOBILE_CHART;

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

  // Retired ball (runbook 6cl): not in the current-ball list, so fetch it
  // with its current replacements and merge it in.
  const [extraBall, setExtraBall] = useState<LearnPlotterPoint | null>(null);
  useEffect(() => {
    if (!points || points.some((p) => p.id === productId)) return;
    let cancelled = false;
    getPlotterBall(productId)
      .then((b) => {
        if (!cancelled) setExtraBall(b);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [points, productId]);
  const byId = useMemo(() => {
    const m = new Map((points ?? []).map((p) => [p.id, p]));
    if (extraBall && !m.has(extraBall.id)) m.set(extraBall.id, extraBall);
    return m;
  }, [points, extraBall]);
  const ball = byId.get(productId) ?? null;
  const retired = ball?.status === "retired";

  useEffect(() => {
    if (!ball) return;
    const related = ROLES.flatMap((r) => ball.neighbors[r.key] ?? []).map((n) => byId.get(n.id)).filter(Boolean) as LearnPlotterPoint[];
    const fitted = fitView([ball, ...related]);
    setView(isDesktop && !cardCollapsed ? offsetForCard(fitted, chart.width, CARD_PX) : fitted);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ball, byId, isDesktop]);

  if (!ball) return null;

  const openInPlotter = (id: string) => navigate(`/plotter?ball=${encodeURIComponent(id)}`);

  return (
    <section className="mb-10 rounded-xl border border-paper-border bg-white p-5">
      <div className="mb-4 flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 className="font-display text-xl font-semibold text-ink">
            {retired ? "Current replacements" : "Where this ball sits"}
          </h2>
          <p className="text-sm text-muted">
            {retired
              ? `This ball is retired. It sat at ${formatPosition(ball, TENTHS_ZOOM)} on the ball motion chart; these current balls are closest.`
              : `${formatPosition(ball, TENTHS_ZOOM)} on the ball motion chart · ${sourceLabel(ball.oil_motion_source).toLowerCase()}`}
          </p>
        </div>
        <Link to={`/plotter?ball=${encodeURIComponent(ball.id)}`} className="text-sm font-semibold">
          Open in the plotter &rarr;
        </Link>
      </div>

      <div className="relative">
        <PlotterChart
          points={points ?? []}
          byId={byId}
          selectedId={ball.id}
          onSelect={(id) => (id === ball.id ? undefined : openInPlotter(id))}
          view={view}
          onViewChange={setView}
          width={chart.width}
          height={chart.height}
          ariaLabel={`Ball motion plotter around the ${ball.brand_name} ${ball.name}`}
        />
        {isDesktop ? (
          <div className="pointer-events-none absolute bottom-12 left-12 top-3 flex w-[20rem] flex-col items-start">
            {!cardCollapsed ? (
              <div className="pointer-events-auto flex max-h-full w-full flex-col">
                <PlotterNeighborCard
                  selected={ball}
                  byId={byId}
                  onSelect={openInPlotter}
                  onCollapse={() => setCardCollapsed(true)}
                  showOwnReview={false}
                />
              </div>
            ) : (
              <button
                type="button"
                onClick={() => setCardCollapsed(false)}
                title="Show similar balls and a step in each direction"
                className="pointer-events-auto flex h-10 items-center gap-2 rounded-lg border border-paper-border bg-white/95 px-3 text-sm font-semibold text-ink shadow hover:border-ink"
              >
                <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M4 6l4 4 4-4" />
                </svg>
                Show details
              </button>
            )}
          </div>
        ) : null}
      </div>

      {!isDesktop ? (
        <div className="mt-5">
          <PlotterNeighborPanel selected={ball} byId={byId} onSelect={openInPlotter} compact />
        </div>
      ) : null}
    </section>
  );
}
