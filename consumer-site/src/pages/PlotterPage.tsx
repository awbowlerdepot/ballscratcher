import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { getPlotterPositions } from "../api/client";
import type { PlotterPoint, ProductStatus } from "../api/types";

// Standalone page, per Al's own decision when asked how the plotter
// should fit into the site (not the main Browse view, not the
// comparison-page picker). Data comes straight off GET /products/
// plotter -- oil (1 light -> 16 heavy) on X, motion (1 smooth -> 18
// angular) on Y, chart-sourced positions rendered as filled circles and
// algorithmic-estimate positions as outlined ones so a visitor isn't
// given false precision on the estimated majority (see public_api.
// list_plotter_positions' own docstring).
//
// This is a functional first pass, not the final design -- Al's
// original plotter (see reference/plotter_reference.html in the main
// repo) has real polish (brand-filter chips, a size slider, label
// toggle, hover tooltips styled to match) that's worth porting here as
// a follow-up rather than blocking this page on that work.
const WIDTH = 900;
const HEIGHT = 640;
const MARGIN = { top: 24, right: 24, bottom: 56, left: 56 };
const OIL_MIN = 1;
const OIL_MAX = 16;
const MOTION_MIN = 1;
const MOTION_MAX = 18;

function xFor(oil: number) {
  const t = (oil - OIL_MIN) / (OIL_MAX - OIL_MIN);
  return MARGIN.left + t * (WIDTH - MARGIN.left - MARGIN.right);
}

function yFor(motion: number) {
  const t = (motion - MOTION_MIN) / (MOTION_MAX - MOTION_MIN);
  // Angular (high motion) at the top, matching Al's original chart.
  return HEIGHT - MARGIN.bottom - t * (HEIGHT - MARGIN.top - MARGIN.bottom);
}

export default function PlotterPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const status = (searchParams.get("status") as ProductStatus) || "current";
  const [points, setPoints] = useState<PlotterPoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [hovered, setHovered] = useState<PlotterPoint | null>(null);
  const [brandFilter, setBrandFilter] = useState("");
  const navigate = useNavigate();

  useEffect(() => {
    setLoading(true);
    getPlotterPositions(status)
      .then(setPoints)
      .finally(() => setLoading(false));
  }, [status]);

  const brands = useMemo(
    () => Array.from(new Set(points.map((p) => p.brand_name))).sort(),
    [points],
  );

  const visible = useMemo(
    () => (brandFilter ? points.filter((p) => p.brand_name === brandFilter) : points),
    [points, brandFilter],
  );

  return (
    <div className="page plotter-page">
      <h1>Ball motion plotter</h1>
      <p className="hint">
        Oil (light &rarr; heavy) across the bottom, motion shape (smooth &rarr; angular) up the side. Filled dots are
        positions digitized from a manufacturer's own published chart; outlined dots are our own estimate from each
        ball's core and coverstock.
      </p>

      <div className="plotter-controls">
        <div className="status-toggle" role="tablist" aria-label="Ball status">
          <button
            type="button"
            className={status === "current" ? "active" : ""}
            onClick={() => setSearchParams({ status: "current" })}
          >
            Current
          </button>
          <button
            type="button"
            className={status === "retired" ? "active" : ""}
            onClick={() => setSearchParams({ status: "retired" })}
          >
            Retired
          </button>
        </div>
        <select value={brandFilter} onChange={(e) => setBrandFilter(e.target.value)}>
          <option value="">All brands</option>
          {brands.map((b) => (
            <option key={b} value={b}>
              {b}
            </option>
          ))}
        </select>
      </div>

      {loading ? (
        <p>Loading...</p>
      ) : (
        <div className="plotter-chart-wrap">
          <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} className="plotter-svg" role="img" aria-label="Ball motion plotter">
            {/* Axes */}
            <line x1={MARGIN.left} y1={HEIGHT - MARGIN.bottom} x2={WIDTH - MARGIN.right} y2={HEIGHT - MARGIN.bottom} className="axis-line" />
            <line x1={MARGIN.left} y1={MARGIN.top} x2={MARGIN.left} y2={HEIGHT - MARGIN.bottom} className="axis-line" />
            <text x={(WIDTH) / 2} y={HEIGHT - 16} textAnchor="middle" className="axis-label">
              Oil (light &rarr; heavy)
            </text>
            <text
              x={16}
              y={HEIGHT / 2}
              textAnchor="middle"
              className="axis-label"
              transform={`rotate(-90 16 ${HEIGHT / 2})`}
            >
              Motion (smooth &rarr; angular)
            </text>

            {visible.map((p) => (
              <circle
                key={p.id}
                cx={xFor(p.oil)}
                cy={yFor(p.motion)}
                r={hovered?.id === p.id ? 8 : 6}
                className={`plotter-dot plotter-dot-${p.oil_motion_source}`}
                onMouseEnter={() => setHovered(p)}
                onMouseLeave={() => setHovered((h) => (h?.id === p.id ? null : h))}
                onClick={() => navigate(`/balls/${p.id}`)}
              >
                <title>
                  {p.brand_name} {p.name} (oil {p.oil}, motion {p.motion}
                  {p.oil_motion_source === "estimated" ? ", estimated" : ""})
                </title>
              </circle>
            ))}
          </svg>

          {hovered && (
            <div className="plotter-tooltip">
              {hovered.primary_image_url && <img src={hovered.primary_image_url} alt={hovered.name} />}
              <div>
                <div className="product-card-brand">{hovered.brand_name}</div>
                <div className="product-card-name">{hovered.name}</div>
                <div className="hint">
                  oil {hovered.oil} / motion {hovered.motion}
                  {hovered.oil_motion_source === "estimated" ? " (estimated)" : ""}
                </div>
              </div>
            </div>
          )}
        </div>
      )}

      {!loading && visible.length === 0 && <p className="empty-state">No balls to plot for this filter.</p>}
    </div>
  );
}
