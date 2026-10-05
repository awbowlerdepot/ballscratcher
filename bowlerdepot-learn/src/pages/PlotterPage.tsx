import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { getLearnPlotter } from "../api/client";
import type { LearnPlotterPoint } from "../api/types";
import PlotterChart from "../components/plotter/PlotterChart";
import PlotterNeighborPanel, { PlotterNeighborCard } from "../components/plotter/PlotterNeighborPanel";
import { FULL_VIEW, ROLES, fitView, offsetForCard, type PlotterView } from "../components/plotter/plotterModel";

// /plotter -- the Learn site's ball motion plotter (runbook 6cc). Al: "the
// goal here is to move this to the learn site in some way with a more
// sophisticated version of the plotter. Some goals are to use this data to
// inform users of what balls would be similar across brand and what would
// be a bit more or less in all directions on the plotter". The consumer
// site's plotter (data.bowleriq.com/plotter) stays as is for now.
//
// ?ball=<product id> selects a ball and zooms to it and its suggestions --
// the article panel's "Open in the plotter" link and shared links use it.

// Desktop (lg+): full-width landscape chart with the details as a card
// floating in its top-left corner (runbook 6ce). Smaller screens keep the
// portrait chart with details stacked underneath.
const DESKTOP_QUERY = "(min-width: 1024px)";
const DESKTOP_CHART = { width: 1200, height: 740 };
const MOBILE_CHART = { width: 900, height: 980 };
const CARD_PX = 330; // card width (~20rem) in chart viewBox px at desktop width

function useIsDesktop() {
  const [desktop, setDesktop] = useState(() => typeof window !== "undefined" && window.matchMedia(DESKTOP_QUERY).matches);
  useEffect(() => {
    const mq = window.matchMedia(DESKTOP_QUERY);
    const onChange = () => setDesktop(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return desktop;
}

export default function PlotterPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [points, setPoints] = useState<LearnPlotterPoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<PlotterView>(FULL_VIEW);
  const [hiddenBrands, setHiddenBrands] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState("");
  const isDesktop = useIsDesktop();
  const [cardCollapsed, setCardCollapsed] = useState(false);
  const chart = isDesktop ? DESKTOP_CHART : MOBILE_CHART;

  const selectedId = searchParams.get("ball");
  const byId = useMemo(() => new Map(points.map((p) => [p.id, p])), [points]);
  const selected = selectedId ? (byId.get(selectedId) ?? null) : null;
  const brands = useMemo(() => [...new Set(points.map((p) => p.brand_name))].sort(), [points]);
  const visible = useMemo(() => points.filter((p) => !hiddenBrands.has(p.brand_name)), [points, hiddenBrands]);

  useEffect(() => {
    document.title = "Ball Motion Plotter | BowlerDepot Learn";
    getLearnPlotter()
      .then(setPoints)
      .catch((err) => setError(err instanceof Error ? err.message : "Couldn't load the plotter."))
      .finally(() => setLoading(false));
  }, []);

  // Zoom to a ball + its suggestions whenever the selection changes.
  function focusOn(p: LearnPlotterPoint) {
    const related = ROLES.flatMap((r) => p.neighbors[r.key]).map((n) => byId.get(n.id)).filter(Boolean) as LearnPlotterPoint[];
    const fitted = fitView([p, ...related]);
    setView(isDesktop && !cardCollapsed ? offsetForCard(fitted, chart.width, CARD_PX) : fitted);
  }

  useEffect(() => {
    if (selected) focusOn(selected);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.id, isDesktop]);

  function select(id: string) {
    const next = new URLSearchParams(searchParams);
    next.set("ball", id);
    setSearchParams(next, { replace: false });
    setQuery("");
  }

  function clearSelection() {
    const next = new URLSearchParams(searchParams);
    next.delete("ball");
    setSearchParams(next);
  }

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (q.length < 2) return [];
    return points.filter((p) => `${p.brand_name} ${p.name}`.toLowerCase().includes(q)).slice(0, 8);
  }, [query, points]);

  function toggleBrand(brand: string) {
    const next = new Set(hiddenBrands);
    if (next.has(brand)) next.delete(brand);
    else next.add(brand);
    setHiddenBrands(next);
  }

  return (
    <div>
      <header className="mb-6 max-w-3xl">
        <p className="text-xs font-semibold uppercase tracking-wide text-muted">Tools</p>
        <h1 className="font-display text-3xl font-semibold text-ink">Ball Motion Plotter</h1>
        <p className="mt-2 text-muted">
          Every current ball on one chart: how much oil it handles (across) and how sharply it moves on the backend (up).
          Pick a ball to see similar balls from other brands and what to try if you want a bit more or less in any
          direction. Scroll or pinch to zoom; drag to move around.
        </p>
      </header>

      <div className="mb-4 flex flex-wrap items-start gap-3">
        <div className="relative w-full sm:w-80">
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Find a ball (e.g. Phaze II)"
            aria-label="Find a ball"
            className="w-full rounded-lg border border-paper-border bg-white px-3 py-2 text-sm"
          />
          {matches.length ? (
            <ul className="absolute z-20 mt-1 w-full list-none overflow-hidden rounded-lg border border-paper-border bg-white p-0 shadow-lg">
              {matches.map((p) => (
                <li key={p.id}>
                  <button
                    type="button"
                    onClick={() => select(p.id)}
                    className="block w-full px-3 py-2 text-left text-sm hover:bg-paper"
                  >
                    <span className="text-muted">{p.brand_name}</span> {p.name}
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
        <div className="flex flex-wrap gap-1.5" role="group" aria-label="Brands shown">
          {brands.map((b) => {
            const on = !hiddenBrands.has(b);
            return (
              <button
                key={b}
                type="button"
                aria-pressed={on}
                onClick={() => toggleBrand(b)}
                className={`rounded-full border px-2.5 py-1 text-xs font-semibold ${
                  on ? "border-ink bg-ink text-white" : "border-paper-border bg-white text-muted"
                }`}
              >
                {b}
              </button>
            );
          })}
        </div>
      </div>

      {loading ? <p className="py-16 text-center text-muted">Loading the plotter…</p> : null}
      {error ? <p className="py-16 text-center text-alert">{error}</p> : null}

      {!loading && !error ? (
        <div>
          <div className="relative">
            <PlotterChart
              points={visible}
              byId={byId}
              selectedId={selected?.id ?? null}
              onSelect={select}
              view={view}
              onViewChange={setView}
              width={chart.width}
              height={chart.height}
              wheelZoom
            />
            {isDesktop ? (
              <div className="pointer-events-none absolute bottom-12 left-12 top-3 flex w-[20rem] flex-col items-start">
                {selected && !cardCollapsed ? (
                  <div className="pointer-events-auto flex max-h-full w-full flex-col">
                    <PlotterNeighborCard
                      selected={selected}
                      byId={byId}
                      onSelect={select}
                      onCollapse={() => setCardCollapsed(true)}
                      onClear={clearSelection}
                    />
                  </div>
                ) : selected ? (
                  <button
                    type="button"
                    onClick={() => setCardCollapsed(false)}
                    className="pointer-events-auto flex items-center gap-2 rounded-full border border-paper-border bg-white/95 px-3 py-1.5 text-xs font-semibold text-ink shadow"
                  >
                    {selected.brand_name} {selected.name} <span className="text-muted">&#9662; details</span>
                  </button>
                ) : (
                  <div className="pointer-events-auto rounded-lg border border-paper-border bg-white/95 px-3 py-2 text-xs text-muted shadow-sm">
                    <span className="font-semibold text-ink">Pick a ball</span> to see similar balls and a step in each
                    direction.
                  </div>
                )}
              </div>
            ) : null}
          </div>
          <p className="mt-2 text-xs text-muted">
            Solid ring: position read off the manufacturer&rsquo;s published ball motion chart. Dashed ring: our estimate
            from the ball&rsquo;s cover, finish, core specs and price. A number on a ball means several balls share that
            spot &mdash; click it to zoom in.
          </p>
          <ul className="mt-2 flex list-none flex-wrap gap-x-4 gap-y-1 p-0">
            {ROLES.map((r) => (
              <li key={r.key} className="flex items-center gap-1.5 text-xs" title={r.blurb}>
                <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: r.color }} />
                <span className="font-semibold text-ink">{r.label}</span>
              </li>
            ))}
          </ul>

          {!isDesktop ? (
            <div className="mt-6">
              {selected ? (
                <div className="rounded-xl border border-paper-border bg-white p-5">
                  <div className="mb-3 flex justify-end">
                    <button type="button" onClick={clearSelection} className="text-xs font-semibold text-muted hover:text-ink">
                      Clear &times;
                    </button>
                  </div>
                  <PlotterNeighborPanel selected={selected} byId={byId} onSelect={select} />
                </div>
              ) : (
                <div className="rounded-xl border border-dashed border-paper-border p-5 text-sm text-muted">
                  <p className="font-display text-base font-semibold text-ink">Pick a ball</p>
                  <p className="mt-1">
                    Tap any ball on the chart, or search above, to see similar balls from other brands and the nearest
                    step toward more oil, less oil, a sharper move, or a smoother one.
                  </p>
                </div>
              )}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
