import { useEffect, useMemo, useRef } from "react";
import { resizedImageUrl } from "../../api/client";
import type { LearnPlotterPoint } from "../../api/types";
import {
  CHART_MARGIN,
  DOMAIN,
  MOTION_MAX,
  MOTION_MIN,
  OIL_MAX,
  OIL_MIN,
  ROLE_BY_KEY,
  TENTHS_ZOOM,
  ZOOM_MAX,
  ZOOM_MIN,
  clampView,
  displayPosition,
  formatPosition,
  rolesFor,
  spans,
  zoomAbout,
  type PlotterView,
} from "./plotterModel";

// Zoomable SVG ball motion plotter (runbook 6cc). Al wanted zoom so the
// 0.1-precision positions (migration 043) mean something: at default zoom
// balls snap to the whole-number grid like Brunswick's printed chart and
// balls on the same spot stack (click a stack to zoom into it); from
// TENTHS_ZOOM up they sit at their real tenths with half/tenth gridlines.
//
// Interaction: drag to pan, wheel or pinch to zoom (wheel only when
// `wheelZoom`, so an embedded panel never hijacks page scrolling), +/-/
// reset buttons always. Selecting a ball dims everything except it and
// its recommendations, which are linked to it with role-colored lines.

interface Props {
  points: LearnPlotterPoint[];
  byId: Map<string, LearnPlotterPoint>;
  selectedId: string | null;
  onSelect: (id: string) => void;
  view: PlotterView;
  onViewChange: (view: PlotterView) => void;
  width: number; // viewBox units; the SVG scales to its container width
  height: number;
  wheelZoom?: boolean;
  ariaLabel?: string;
}

const M = CHART_MARGIN;
const BALL_RADIUS_MAX = 64;
// Gap kept between neighboring balls after the layout pass.
const BALL_GAP = 3;

const roleTagWidth = (role: keyof typeof ROLE_BY_KEY) => ROLE_BY_KEY[role].short.length * 6 + 14;

interface LayoutItem {
  group: LearnPlotterPoint[];
  ball: LearnPlotterPoint; // the one drawn (a stack shows one)
  stacked: boolean;
  anchor: { oil: number; motion: number }; // true (snapped) position
  dx: number; // nudge from the anchor, viewBox px
  dy: number;
  r: number;
}

// Nudges overlapping balls apart (a few rounds of pairwise separation,
// like a collision force). The selected ball never moves and its
// recommendations barely do, so the thing being looked at stays exactly
// where the data says. Works in pan-independent px offsets, so dragging
// never reshuffles the layout -- it only reruns when the zoom changes.
function separate(items: LayoutItem[], scaleX: number, scaleY: number, fixedId: string | null, heavy: Set<string>) {
  const pos = items.map((it, i) => {
    // Identical spots (colorways) need a deterministic nudge to separate.
    const a = (i * 2.399963) % (2 * Math.PI);
    return { x: it.anchor.oil * scaleX + Math.cos(a) * 0.01, y: -it.anchor.motion * scaleY + Math.sin(a) * 0.01 };
  });
  const weight = items.map((it) => (it.ball.id === fixedId ? 0 : heavy.has(it.ball.id) ? 0.25 : 1));
  for (let iter = 0; iter < 60; iter++) {
    let moved = false;
    for (let i = 0; i < items.length; i++) {
      for (let j = i + 1; j < items.length; j++) {
        const ddx = pos[j].x - pos[i].x;
        const ddy = pos[j].y - pos[i].y;
        const min = items[i].r + items[j].r + BALL_GAP;
        if (Math.abs(ddx) >= min || Math.abs(ddy) >= min) continue;
        const d = Math.hypot(ddx, ddy) || 0.01;
        if (d >= min) continue;
        const wSum = weight[i] + weight[j];
        if (wSum === 0) continue;
        const push = min - d;
        const ux = ddx / d;
        const uy = ddy / d;
        pos[i].x -= ux * push * (weight[i] / wSum);
        pos[i].y -= uy * push * (weight[i] / wSum);
        pos[j].x += ux * push * (weight[j] / wSum);
        pos[j].y += uy * push * (weight[j] / wSum);
        moved = true;
      }
    }
    if (!moved) break;
  }
  items.forEach((it, i) => {
    it.dx = pos[i].x - it.anchor.oil * scaleX;
    it.dy = pos[i].y + it.anchor.motion * scaleY;
  });
}

export default function PlotterChart({
  points,
  byId,
  selectedId,
  onSelect,
  view,
  onViewChange,
  width,
  height,
  wheelZoom = false,
  ariaLabel = "Ball motion plotter",
}: Props) {
  const svgRef = useRef<SVGSVGElement>(null);
  const plotW = width - M.left - M.right;
  const plotH = height - M.top - M.bottom;
  const { sx, sy } = spans(view.k);
  const x0 = view.cx - sx / 2;
  const y1 = view.cy + sy / 2;
  const px = (oil: number) => M.left + ((oil - x0) / sx) * plotW;
  const py = (motion: number) => M.top + ((y1 - motion) / sy) * plotH;
  const toData = (vx: number, vy: number) => ({ oil: x0 + ((vx - M.left) / plotW) * sx, motion: y1 - ((vy - M.top) / plotH) * sy });

  // Ball size scales with zoom (Al: "can you make the size of the ball
  // images increase when zooming in closer and the ball won't overlap") --
  // 0.42 of a grid unit at default zoom, so neighbors one whole number
  // apart never touch, up to a cap where a ball photo is still a ball on
  // a chart rather than a wall of photos. Overlaps from balls a few
  // tenths apart are resolved by the layout pass below.
  const scaleX = plotW / sx; // viewBox px per oil unit at this zoom
  const scaleY = plotH / sy; // ... per motion unit
  const unitPx = Math.min(scaleX, scaleY);
  // Grows as zoom^0.6 rather than 1:1 with the grid -- 1:1 made dense
  // clusters (lots of balls a few tenths apart) push each other far from
  // their real spots. This keeps balls clearly bigger when zoomed while
  // the layout pass only has to nudge them a little.
  const radius = Math.max(9, Math.min(BALL_RADIUS_MAX, (unitPx / view.k) * 0.42 * view.k ** 0.6));
  const tenths = view.k >= TENTHS_ZOOM;

  const selected = selectedId ? (byId.get(selectedId) ?? null) : null;
  const roles = useMemo(() => rolesFor(selected), [selected]);

  // Everything drawn: the visible points, plus the selected ball and its
  // recommendations even if a brand filter hid them.
  const drawn = useMemo(() => {
    const ids = new Set(points.map((p) => p.id));
    const extra: LearnPlotterPoint[] = [];
    if (selected && !ids.has(selected.id)) extra.push(selected);
    for (const id of roles.keys()) {
      const p = byId.get(id);
      if (p && !ids.has(id)) extra.push(p);
    }
    return [...points, ...extra];
  }, [points, selected, roles, byId]);

  // Group balls that land on the same drawn spot. Below TENTHS_ZOOM a
  // group is a stack (top ball + count; click zooms in). At tenths zoom
  // each ball is drawn on its own and the layout pass keeps them apart.
  const groups = useMemo(() => {
    const map = new Map<string, LearnPlotterPoint[]>();
    for (const p of drawn) {
      const d = displayPosition(p, view.k);
      const key = `${d.oil}:${d.motion}`;
      const g = map.get(key);
      if (g) g.push(p);
      else map.set(key, [p]);
    }
    // Paint order: plain balls, then recommendations, then the selection.
    const rank = (g: LearnPlotterPoint[]) =>
      g.some((p) => p.id === selectedId) ? 2 : g.some((p) => roles.has(p.id)) ? 1 : 0;
    return [...map.values()].sort((a, b) => rank(a) - rank(b));
  }, [drawn, view.k, selectedId, roles]);

  // One layout item per drawn ball (a stack at default zoom is one item),
  // nudged apart so nothing overlaps.
  const layout = useMemo(() => {
    const items: LayoutItem[] = [];
    for (const group of groups) {
      const stacked = group.length > 1 && !tenths;
      const shown = stacked
        ? [group.find((p) => p.id === selectedId) ?? group.find((p) => roles.has(p.id)) ?? group[0]]
        : group;
      for (const ball of shown) {
        items.push({
          group,
          ball,
          stacked,
          anchor: displayPosition(ball, view.k),
          dx: 0,
          dy: 0,
          r: ball.id === selectedId ? radius * 1.15 : radius,
        });
      }
    }
    separate(items, scaleX, scaleY, selectedId, new Set(roles.keys()));
    return items;
  }, [groups, tenths, selectedId, roles, view.k, radius, scaleX, scaleY]);
  const drawnAt = new Map(layout.map((it) => [it.ball.id, it]));
  const centerOf = (it: LayoutItem) => ({ x: px(it.anchor.oil) + it.dx, y: py(it.anchor.motion) + it.dy });

  // --- pointer: drag to pan, two-finger pinch to zoom -------------------
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const gesture = useRef<{ moved: boolean; startDist?: number; startView?: PlotterView } | null>(null);
  const viewRef = useRef(view);
  viewRef.current = view;

  function toViewBox(clientX: number, clientY: number) {
    const rect = svgRef.current!.getBoundingClientRect();
    return { x: ((clientX - rect.left) / rect.width) * width, y: ((clientY - rect.top) / rect.height) * height };
  }

  function onPointerDown(e: React.PointerEvent) {
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.current.size === 1) gesture.current = { moved: false };
    if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      gesture.current = { moved: true, startDist: Math.hypot(a.x - b.x, a.y - b.y), startView: viewRef.current };
    }
  }

  function onPointerMove(e: React.PointerEvent) {
    const prev = pointers.current.get(e.pointerId);
    if (!prev || !gesture.current) return;
    const rect = svgRef.current!.getBoundingClientRect();
    if (pointers.current.size === 1) {
      const dx = e.clientX - prev.x;
      const dy = e.clientY - prev.y;
      if (!gesture.current.moved && Math.hypot(dx, dy) < 4) return;
      if (!gesture.current.moved) svgRef.current!.setPointerCapture(e.pointerId);
      gesture.current.moved = true;
      pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
      const v = viewRef.current;
      const s = spans(v.k);
      onViewChange(
        clampView({
          ...v,
          cx: v.cx - (dx / rect.width) * width * (s.sx / plotW),
          cy: v.cy + (dy / rect.height) * height * (s.sy / plotH),
        }),
      );
    } else if (pointers.current.size === 2 && gesture.current.startDist && gesture.current.startView) {
      pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
      const [a, b] = [...pointers.current.values()];
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      const mid = toViewBox((a.x + b.x) / 2, (a.y + b.y) / 2);
      const start = gesture.current.startView;
      const s = spans(start.k);
      const anchor = {
        oil: start.cx - s.sx / 2 + ((mid.x - M.left) / plotW) * s.sx,
        motion: start.cy + s.sy / 2 - ((mid.y - M.top) / plotH) * s.sy,
      };
      onViewChange(zoomAbout(start, dist / gesture.current.startDist, anchor.oil, anchor.motion));
    }
  }

  function onPointerUp(e: React.PointerEvent) {
    pointers.current.delete(e.pointerId);
    if (pointers.current.size === 0) {
      // Keep `moved` readable by the click handler that fires next.
      setTimeout(() => {
        if (pointers.current.size === 0) gesture.current = null;
      }, 0);
    }
  }

  // A drag ends with a click on whatever was under the pointer -- ignore it.
  const wasDrag = () => Boolean(gesture.current?.moved);

  // Wheel zoom needs a non-passive listener to stop the page scrolling.
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg || !wheelZoom) return;
    function onWheel(e: WheelEvent) {
      e.preventDefault();
      const { x, y } = toViewBox(e.clientX, e.clientY);
      const anchor = toData(x, y);
      onViewChange(zoomAbout(viewRef.current, Math.exp(-e.deltaY * 0.0015), anchor.oil, anchor.motion));
    }
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
  });

  function zoomButton(factor: number) {
    onViewChange(zoomAbout(view, factor, view.cx, view.cy));
  }

  function onGroupClick(group: LearnPlotterPoint[], clicked: LearnPlotterPoint) {
    if (wasDrag()) return;
    if (group.length > 1 && !tenths) {
      // A stack at default zoom: zoom in on it so its balls separate.
      const d = displayPosition(clicked, view.k);
      onViewChange(clampView({ cx: d.oil, cy: d.motion, k: Math.max(TENTHS_ZOOM * 1.4, view.k * 2.5) }));
      return;
    }
    onSelect(clicked.id);
  }

  // --- gridlines ------------------------------------------------------
  const gridLines = (lo: number, hi: number, vMin: number, vMax: number) => {
    const out: { v: number; level: 0 | 1 | 2 }[] = [];
    const step = view.k >= 6 ? 0.1 : view.k >= TENTHS_ZOOM ? 0.5 : 1;
    const start = Math.ceil(Math.max(lo, vMin) / step) * step;
    for (let v = start; v <= Math.min(hi, vMax) + 1e-9; v += step) {
      const r = Math.round(v * 10) / 10;
      out.push({ v: r, level: Number.isInteger(r) ? 0 : Math.abs(r * 2 - Math.round(r * 2)) < 1e-9 ? 1 : 2 });
    }
    return out;
  };
  const xLines = gridLines(x0, x0 + sx, OIL_MIN, OIL_MAX);
  const yLines = gridLines(y1 - sy, y1, MOTION_MIN, MOTION_MAX);
  const labelEvery = (level: number) => level === 0 || (level === 1 && view.k >= 4);

  const clipId = useMemo(() => `plotter-ball-clip-${Math.random().toString(36).slice(2, 8)}`, []);

  return (
    <div className="relative select-none">
      <svg
        ref={svgRef}
        viewBox={`0 0 ${width} ${height}`}
        className="block h-auto w-full touch-none rounded-lg border border-paper-border bg-white"
        role="img"
        aria-label={ariaLabel}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        style={{ cursor: gesture.current?.moved ? "grabbing" : "grab" }}
      >
        <defs>
          <clipPath id={clipId} clipPathUnits="objectBoundingBox">
            <circle cx="0.5" cy="0.5" r="0.5" />
          </clipPath>
          <clipPath id={`${clipId}-plot`}>
            <rect x={M.left} y={M.top} width={plotW} height={plotH} />
          </clipPath>
        </defs>

        {/* Axis bands: light -> heavy oil, smooth -> angular */}
        <g clipPath={`url(#${clipId}-plot)`}>
          {xLines.map((l) => (
            <line
              key={`x${l.v}`}
              x1={px(l.v)}
              x2={px(l.v)}
              y1={M.top}
              y2={M.top + plotH}
              stroke={l.level === 0 ? "#d9d3c7" : l.level === 1 ? "#ebe6dc" : "#f3f0ea"}
              strokeWidth={l.level === 0 ? 1 : 0.75}
            />
          ))}
          {yLines.map((l) => (
            <line
              key={`y${l.v}`}
              x1={M.left}
              x2={M.left + plotW}
              y1={py(l.v)}
              y2={py(l.v)}
              stroke={l.level === 0 ? "#d9d3c7" : l.level === 1 ? "#ebe6dc" : "#f3f0ea"}
              strokeWidth={l.level === 0 ? 1 : 0.75}
            />
          ))}

          {/* Where a nudged ball really sits: a dot at its true spot and a
              thin leader line to the ball. */}
          {layout.map((it) => {
            if (Math.hypot(it.dx, it.dy) < it.r * 0.35) return null;
            const ax = px(it.anchor.oil);
            const ay = py(it.anchor.motion);
            const c = centerOf(it);
            const dim = selected && it.ball.id !== selectedId && !roles.has(it.ball.id);
            return (
              <g key={`lead-${it.ball.id}`} opacity={dim ? 0.25 : 0.7} pointerEvents="none">
                <line x1={ax} y1={ay} x2={c.x} y2={c.y} stroke="#9ca3af" strokeWidth={1} />
                <circle cx={ax} cy={ay} r={2.5} fill="#6b6b63" />
              </g>
            );
          })}

          {/* Recommendation links, drawn to where each ball is drawn */}
          {selected && drawnAt.get(selected.id)
            ? [...roles.entries()].map(([id, role]) => {
                const target = drawnAt.get(id);
                if (!target) return null;
                const a = centerOf(drawnAt.get(selected.id)!);
                const b = centerOf(target);
                return (
                  <line
                    key={`link-${id}`}
                    x1={a.x}
                    y1={a.y}
                    x2={b.x}
                    y2={b.y}
                    stroke={ROLE_BY_KEY[role].color}
                    strokeWidth={2}
                    strokeDasharray="5 4"
                    opacity={0.8}
                  />
                );
              })
            : null}

          {layout.map((it) => {
            const p = it.ball;
            const { x: cx, y: cy } = centerOf(it);
            const r = it.r;
            const role = roles.get(p.id);
            const isSel = p.id === selectedId;
            const dim = selected && !isSel && !role;
            const ring = isSel ? "#0f0f2d" : role ? ROLE_BY_KEY[role].color : "#9ca3af";
            // Request a sharper image as balls get bigger (2x for retina).
            const imgPx = r > 40 ? 192 : r > 22 ? 128 : 96;
            const img = p.primary_image_url
              ? resizedImageUrl(p.primary_image_url, { w: imgPx, h: imgPx, fit: "contain", fmt: "webp" })
              : null;
            return (
              <g
                key={p.id}
                role="button"
                tabIndex={0}
                aria-label={`${p.brand_name} ${p.name}, ${formatPosition(p, TENTHS_ZOOM)}`}
                opacity={dim ? 0.28 : 1}
                style={{ cursor: "pointer" }}
                onClick={() => onGroupClick(it.group, p)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onSelect(p.id);
                  }
                }}
              >
                <title>
                  {p.brand_name} {p.name} ({formatPosition(p, TENTHS_ZOOM)}
                  {p.oil_motion_source === "estimated" ? ", estimated" : ""})
                  {it.stacked ? ` + ${it.group.length - 1} more here -- click to zoom in` : ""}
                </title>
                <circle cx={cx} cy={cy} r={r} fill="#eef0f6" />
                {img ? (
                  <image
                    href={img}
                    x={cx - r}
                    y={cy - r}
                    width={r * 2}
                    height={r * 2}
                    preserveAspectRatio="xMidYMid meet"
                    clipPath={`url(#${clipId})`}
                  />
                ) : null}
                {/* Solid ring = from a published chart; dashed = estimated */}
                <circle
                  cx={cx}
                  cy={cy}
                  r={r}
                  fill="none"
                  stroke={ring}
                  strokeWidth={isSel || role ? 3 : 1.5}
                  strokeDasharray={p.oil_motion_source === "estimated" ? "4 3" : undefined}
                />
                {/* Role tag under each suggestion, so the chart reads on
                    its own without the details card (runbook 6ce). */}
                {role && !it.stacked ? (
                  <g pointerEvents="none">
                    <rect
                      x={cx - roleTagWidth(role) / 2}
                      y={cy + r + 3}
                      width={roleTagWidth(role)}
                      height={15}
                      rx={7.5}
                      fill={ROLE_BY_KEY[role].color}
                    />
                    <text x={cx} y={cy + r + 10.5} textAnchor="middle" dominantBaseline="central" fontSize={10} fontWeight={700} fill="#fff">
                      {ROLE_BY_KEY[role].short}
                    </text>
                  </g>
                ) : null}
                {it.stacked ? (
                  <g>
                    <circle cx={cx + r * 0.8} cy={cy - r * 0.8} r={Math.max(8, r * 0.45)} fill="#0f0f2d" />
                    <text
                      x={cx + r * 0.8}
                      y={cy - r * 0.8}
                      textAnchor="middle"
                      dominantBaseline="central"
                      fontSize={Math.max(9, r * 0.5)}
                      fontWeight={700}
                      fill="#fff"
                    >
                      {it.group.length}
                    </text>
                  </g>
                ) : null}
              </g>
            );
          })}
        </g>

        {/* Axes */}
        <rect x={M.left} y={M.top} width={plotW} height={plotH} fill="none" stroke="#d9d3c7" />
        {xLines
          .filter((l) => labelEvery(l.level))
          .map((l) => (
            <text key={`xl${l.v}`} x={px(l.v)} y={M.top + plotH + 14} textAnchor="middle" fontSize={11} fill="#6b6b63">
              {l.level === 0 ? l.v : l.v.toFixed(1)}
            </text>
          ))}
        {yLines
          .filter((l) => labelEvery(l.level))
          .map((l) => (
            <text key={`yl${l.v}`} x={M.left - 6} y={py(l.v)} textAnchor="end" dominantBaseline="central" fontSize={11} fill="#6b6b63">
              {l.level === 0 ? l.v : l.v.toFixed(1)}
            </text>
          ))}
        <text x={M.left} y={height - 6} fontSize={11} fontWeight={700} fill="#0f0f2d">
          ← Light oil
        </text>
        <text x={M.left + plotW} y={height - 6} textAnchor="end" fontSize={11} fontWeight={700} fill="#0f0f2d">
          Heavy oil →
        </text>
        <text
          x={12}
          y={M.top + plotH / 2}
          transform={`rotate(-90 12 ${M.top + plotH / 2})`}
          textAnchor="middle"
          fontSize={11}
          fontWeight={700}
          fill="#0f0f2d"
        >
          Smooth ← Motion → Angular
        </text>
      </svg>

      {/* Same 32px, labeled-on-hover sizing as the details card's toolbar
          (runbook 6cg) -- these were small bare glyphs. */}
      <div className="absolute right-2 top-2 flex flex-col overflow-hidden rounded-lg border border-paper-border bg-white/95 shadow">
        <button
          type="button"
          aria-label="Zoom in"
          title="Zoom in"
          className="flex h-9 w-9 items-center justify-center text-xl leading-none text-ink hover:bg-paper disabled:opacity-40"
          onClick={() => zoomButton(1.6)}
          disabled={view.k >= ZOOM_MAX}
        >
          +
        </button>
        <button
          type="button"
          aria-label="Zoom out"
          title="Zoom out"
          className="flex h-9 w-9 items-center justify-center border-t border-paper-border text-xl leading-none text-ink hover:bg-paper disabled:opacity-40"
          onClick={() => zoomButton(1 / 1.6)}
          disabled={view.k <= ZOOM_MIN}
        >
          −
        </button>
        <button
          type="button"
          aria-label="Show the whole chart"
          title="Show the whole chart"
          className="flex h-9 w-9 items-center justify-center border-t border-paper-border text-[11px] font-bold uppercase text-ink hover:bg-paper disabled:opacity-40"
          onClick={() => onViewChange(clampView({ cx: (DOMAIN.x0 + DOMAIN.x1) / 2, cy: (DOMAIN.y0 + DOMAIN.y1) / 2, k: 1 }))}
          disabled={view.k <= ZOOM_MIN}
        >
          All
        </button>
      </div>
      {tenths ? (
        <p className="pointer-events-none absolute right-14 top-3 rounded bg-white/90 px-1.5 py-0.5 text-[11px] text-muted">
          Zoomed in · positions to 0.1
        </p>
      ) : null}
    </div>
  );
}
