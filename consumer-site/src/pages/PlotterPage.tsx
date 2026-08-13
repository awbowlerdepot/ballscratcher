import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { getPlotterPositions } from "../api/client";
import type { PlotterPoint, ProductStatus } from "../api/types";
import { useCompare } from "../context/CompareContext";

// Standalone page, per Al's own decision when asked how the plotter
// should fit into the site (not the main Browse view, not the
// comparison-page picker). Data comes straight off GET /products/
// plotter -- oil (1 light -> 16 heavy) on X, motion (1 smooth -> 18
// angular) on Y.
//
// Gridlines + ball-image markers ported from Al's original
// (reference/plotter_reference.html's grid()/build() functions) --
// one vertical line per integer oil unit, one horizontal line per
// integer motion unit, and each point rendered as the ball's own
// product photo (clipped to a circle) instead of a plain dot, with a
// colored ring around the image standing in for the original's
// solid-vs-dashed source distinction (chart-digitized vs our own
// algorithmic estimate vs an admin-set manual value) since a photo
// can't be "filled vs outlined" the way a bare circle could.
//
// Brand toggle chips and the size slider are now ported too (multi-
// select chips replace the single <select>; the slider drives ball
// marker diameter same as the original's #size range input, default
// 42, range 20-72). Still not ported: the search box and label toggle.
const WIDTH = 900;
const HEIGHT = 640;
// top/bottom padded generously beyond the axis area itself -- at the
// max size-slider setting (72px diameter, radius 36, +5 more on hover)
// a ball plotted right at the motion=18 or motion=1 edge needs real
// clearance or it gets clipped by the <svg> root's own viewBox bounds
// (SVG's default overflow: hidden crops anything drawn above y=0 or
// below y=HEIGHT). 48/72 comfortably fit a hovered max-size ball at
// either edge with room to spare. Known, accepted gap: a large exploded
// cluster (see `positions` below) sitting right in a corner can still
// push a member or two past this margin -- rare in practice (needs both
// a same-position pile-up AND a corner grid position) and not worth the
// extra complexity of clamping ring positions back into bounds for.
const MARGIN = { top: 48, right: 24, bottom: 72, left: 56 };
const OIL_MIN = 1;
const OIL_MAX = 16;
const MOTION_MIN = 1;
const MOTION_MAX = 18;
const SIZE_MIN = 20;
const SIZE_MAX = 72;
const SIZE_DEFAULT = 42;

// Third tablist option alongside the two real ProductStatus values -- Al:
// "add a tablist toggle to the ball motion plotter that is 'compare' and
// plots the currently selected balls in the compare feature." Kept in the
// same "status" URL param as Current/Retired rather than a separate query
// param -- it's still exactly one active tab at a time, same as those two,
// and reusing the param means an existing bookmarked/shared link shape
// (?status=...) just gained a third valid value instead of needing a
// second param plumbed through everywhere status already is.
type PlotterView = ProductStatus | "compare";

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
  const view = (searchParams.get("status") as PlotterView) || "current";
  const { ids: compareIds } = useCompare();
  const [points, setPoints] = useState<PlotterPoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [hovered, setHovered] = useState<PlotterPoint | null>(null);
  const [size, setSize] = useState(SIZE_DEFAULT);
  // Chip-based brand toggle, like the original's state.brands Set --
  // a brand is shown unless explicitly toggled off. Reset to "every
  // brand on" whenever the underlying point set changes (a status/view
  // toggle swaps in a whole different catalog) rather than trying to
  // carry toggles across an unrelated dataset.
  const [enabledBrands, setEnabledBrands] = useState<Set<string>>(new Set());
  const navigate = useNavigate();

  useEffect(() => {
    // Compare tab with nothing in the compare list yet -- skip the fetch
    // entirely rather than falling through to getPlotterPositions, which
    // would otherwise ignore an empty ids array and silently plot the
    // whole "current" catalog instead of an honest empty state (see
    // getPlotterPositions' own ids.length > 0 check).
    if (view === "compare" && compareIds.length === 0) {
      setPoints([]);
      setEnabledBrands(new Set());
      setLoading(false);
      return;
    }
    setLoading(true);
    const request = view === "compare" ? getPlotterPositions("current", compareIds) : getPlotterPositions(view);
    request
      .then((data) => {
        setPoints(data);
        setEnabledBrands(new Set(data.map((p) => p.brand_name)));
      })
      .finally(() => setLoading(false));
  }, [view, compareIds]);

  const brands = useMemo(
    () => Array.from(new Set(points.map((p) => p.brand_name))).sort(),
    [points],
  );

  const visible = useMemo(
    () => points.filter((p) => enabledBrands.has(p.brand_name)),
    [points, enabledBrands],
  );

  // Multiple balls can land on the exact same (oil, motion) position --
  // round-number estimates collide constantly, and chart/manual values
  // aren't guaranteed unique either. Stacked exactly on top of each
  // other, only the last-drawn one would ever be visible or clickable.
  // Al: "explode out the balls that are on top of each other if there
  // are multiple with the same values." Groups by the literal oil/motion
  // pair and, for any group bigger than one, fans its members out evenly
  // around the true grid position on a small ring. Depends on `size`
  // (not `hovered`) -- the ring has to grow with the marker diameter the
  // slider controls, but a hover shouldn't reflow anyone's position, only
  // which one paints on top (see renderOrder below) and its own radius.
  // p.oil/p.motion themselves (used everywhere else: tooltip, title,
  // click-through) are never touched -- this is purely a render position.
  const positions = useMemo(() => {
    const groups = new Map<string, PlotterPoint[]>();
    for (const p of visible) {
      const key = `${p.oil}:${p.motion}`;
      const group = groups.get(key);
      if (group) group.push(p);
      else groups.set(key, [p]);
    }

    const radius = size / 2;
    const result = new Map<string, { cx: number; cy: number }>();
    for (const group of groups.values()) {
      const baseCx = xFor(group[0].oil);
      const baseCy = yFor(group[0].motion);
      if (group.length === 1) {
        result.set(group[0].id, { cx: baseCx, cy: baseCy });
        continue;
      }
      // Stable order (by id) so a re-render never reshuffles which ball
      // lands at which angle in the ring.
      const ordered = [...group].sort((a, b) => a.id.localeCompare(b.id));
      const n = ordered.length;
      // radius / sin(pi/n) is the ring size at which adjacent markers'
      // circles are exactly tangent (their centers end up a full
      // diameter apart) -- the 1.15 factor adds a small visible gap
      // instead of leaving them just touching edge-to-edge.
      const explodeRadius = (radius / Math.sin(Math.PI / n)) * 1.15;
      ordered.forEach((p, i) => {
        const angle = (2 * Math.PI * i) / n - Math.PI / 2; // start straight up, then clockwise
        result.set(p.id, {
          cx: baseCx + explodeRadius * Math.cos(angle),
          cy: baseCy + explodeRadius * Math.sin(angle),
        });
      });
    }
    return result;
  }, [visible, size]);

  // Bring the hovered ball to front -- SVG paint order is purely
  // document order (no independent z-index the way CSS box layout has),
  // so "on top" means "rendered last". Al: "change the z index of the
  // hovered ball on the plotter to be on top." Only reorders the array
  // that gets mapped over below; never changes what's plotted or its
  // computed position.
  const renderOrder = useMemo(() => {
    if (!hovered) return visible;
    const rest = visible.filter((p) => p.id !== hovered.id);
    const found = visible.find((p) => p.id === hovered.id);
    return found ? [...rest, found] : visible;
  }, [visible, hovered]);

  function toggleBrand(name: string) {
    setEnabledBrands((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }

  return (
    <div className="page plotter-page">
      <h1>Ball motion plotter</h1>
      <p className="hint">
        Oil (light &rarr; heavy) across the bottom, motion shape (smooth &rarr; angular) up the side. A solid ring
        around a ball is a position digitized from a manufacturer's own published chart; a dashed ring is our own
        estimate from that ball's core and coverstock; an amber ring has been set manually.
      </p>

      <div className="plotter-controls">
        <div className="status-toggle" role="tablist" aria-label="Plotter view">
          <button
            type="button"
            role="tab"
            aria-selected={view === "current"}
            className={view === "current" ? "active" : ""}
            onClick={() => setSearchParams({ status: "current" })}
          >
            Current
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={view === "retired"}
            className={view === "retired" ? "active" : ""}
            onClick={() => setSearchParams({ status: "retired" })}
          >
            Retired
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={view === "compare"}
            className={view === "compare" ? "active" : ""}
            onClick={() => setSearchParams({ status: "compare" })}
          >
            Compare{compareIds.length > 0 ? ` (${compareIds.length})` : ""}
          </button>
        </div>

        {points.length > 0 && (
          <>
            <label className="plotter-size-control">
              Size
              <input
                type="range"
                min={SIZE_MIN}
                max={SIZE_MAX}
                value={size}
                onChange={(e) => setSize(Number(e.target.value))}
                aria-label="Ball image size"
              />
            </label>

            <span className="plotter-count">
              {visible.length} of {points.length} balls shown
            </span>
          </>
        )}
      </div>

      {points.length > 0 && (
        <div className="plotter-brand-chips">
          <span className="plotter-brand-chips-label">Brand:</span>
          {brands.map((b) => (
            <button
              key={b}
              type="button"
              className={enabledBrands.has(b) ? "chip" : "chip chip-off"}
              onClick={() => toggleBrand(b)}
              aria-pressed={enabledBrands.has(b)}
            >
              {b}
            </button>
          ))}
        </div>
      )}

      {loading ? (
        <p>Loading...</p>
      ) : view === "compare" && compareIds.length === 0 ? null : (
        <div className="plotter-chart-wrap">
          <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} className="plotter-svg" role="img" aria-label="Ball motion plotter">
            {/* Gridlines -- one per integer oil/motion unit, same
                density as the original's grid() function. */}
            {Array.from({ length: OIL_MAX - OIL_MIN + 1 }, (_, i) => OIL_MIN + i).map((x) => (
              <g key={`vx-${x}`}>
                <line
                  x1={xFor(x)}
                  y1={MARGIN.top}
                  x2={xFor(x)}
                  y2={HEIGHT - MARGIN.bottom}
                  className="plotter-gridline"
                />
                <text x={xFor(x)} y={HEIGHT - MARGIN.bottom + 16} textAnchor="middle" className="plotter-tick">
                  {x}
                </text>
              </g>
            ))}
            {Array.from({ length: MOTION_MAX - MOTION_MIN + 1 }, (_, i) => MOTION_MIN + i).map((y) => (
              <g key={`hy-${y}`}>
                <line
                  x1={MARGIN.left}
                  y1={yFor(y)}
                  x2={WIDTH - MARGIN.right}
                  y2={yFor(y)}
                  className="plotter-gridline"
                />
                <text x={MARGIN.left - 10} y={yFor(y)} dominantBaseline="middle" textAnchor="end" className="plotter-tick">
                  {y}
                </text>
              </g>
            ))}

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

            {/* Ball markers -- the ball's own product photo, clipped to
                a circle, standing in for the original's rounded <img>
                markers. A plain filled circle is the fallback for a
                product with no image yet (see BrowsePage's identical
                placeholder reasoning). Mapped over renderOrder (not
                visible directly) so the hovered ball paints last/on top
                -- see renderOrder's own comment. Position comes from the
                precomputed `positions` map (see its own comment) rather
                than xFor/yFor directly, so balls sharing an exact oil/
                motion value fan out instead of stacking exactly on top
                of each other. */}
            {renderOrder.map((p) => {
              const pos = positions.get(p.id)!;
              const cx = pos.cx;
              const cy = pos.cy;
              const radius = size / 2;
              const r = hovered?.id === p.id ? radius + 5 : radius;
              const clipId = `plotter-clip-${p.id}`;
              return (
                <g
                  key={p.id}
                  className="plotter-ball"
                  onMouseEnter={() => setHovered(p)}
                  onMouseLeave={() => setHovered((h) => (h?.id === p.id ? null : h))}
                  onClick={() => navigate(`/balls/${p.id}`)}
                >
                  <title>
                    {p.brand_name} {p.name} (oil {p.oil}, motion {p.motion}
                    {p.oil_motion_source === "estimated" ? ", estimated" : ""})
                  </title>
                  {p.primary_image_url ? (
                    <>
                      <clipPath id={clipId}>
                        <circle cx={cx} cy={cy} r={r} />
                      </clipPath>
                      <image
                        href={p.primary_image_url}
                        x={cx - r}
                        y={cy - r}
                        width={r * 2}
                        height={r * 2}
                        clipPath={`url(#${clipId})`}
                        preserveAspectRatio="xMidYMid slice"
                      />
                    </>
                  ) : (
                    <circle cx={cx} cy={cy} r={r} className="plotter-ball-placeholder" />
                  )}
                  <circle
                    cx={cx}
                    cy={cy}
                    r={r}
                    fill="none"
                    className={`plotter-ball-ring plotter-ball-ring-${p.oil_motion_source}`}
                  />
                  {/* Dedicated hit target, drawn last (on top) so it
                      owns pointer events across the WHOLE circle. The
                      ring above is fill="none", which by default only
                      registers hover/click on its thin stroked edge --
                      that's the "only works on the edge" bug. A
                      transparent (not "none") fill still counts as
                      painted for hit-testing, so this circle catches
                      hover/click everywhere inside the marker, not
                      just a couple of pixels of outline. */}
                  <circle cx={cx} cy={cy} r={r} fill="transparent" className="plotter-ball-hit" />
                </g>
              );
            })}
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

      {!loading && visible.length === 0 && view === "compare" && (
        <p className="empty-state">
          Nothing to plot yet. <Link to="/">Browse balls</Link> and add a few to compare, or add some from{" "}
          <Link to="/compare">the compare page</Link>.
        </p>
      )}
      {!loading && visible.length === 0 && view !== "compare" && (
        <p className="empty-state">No balls to plot for this filter.</p>
      )}
    </div>
  );
}
