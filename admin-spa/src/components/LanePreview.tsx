import { useEffect, useRef } from "react";

// Top-down lane, the whole bed (runbook 6el). Al: "for the lane preview can
// we make it the actual length of the lane with the pin deck and pin spots.
// 60' from the foul line to the headpin". Real geometry in inches -- 39
// boards across 41.5", 60' foul line to the headpin's center, pin spots 12"
// apart in the triangle (rows 10.39" apart), the deck ending just behind
// the 7-10 -- plus the markings a bowler aims with: the dots at 7', the
// arrows (a V from 12.5' on boards 5/35 to 15.5' on board 20) and the
// range finders on boards 10/30 at 34'-40'. Width and length use different
// scales (a true-scale lane is 18x taller than wide, which would turn the
// oil into a hairline); the triangle is squashed the same way. Oil is the
// partner API's profile (µL per board per foot), one row per foot.
const LANE_IN = 41.5;
const BOARD_IN = LANE_IN / 39;
const GUTTER_IN = 9.25;
const HEADPIN_FT = 60;
const PIN_ROW_IN = 10.392;
const DECK_END_FT = HEADPIN_FT + (3 * PIN_ROW_IN + 8) / 12; // pit edge, a little behind the back row
const PINS: [number, number][] = [
  [0, 0],
  [1, -6], [1, 6],
  [2, -12], [2, 0], [2, 12],
  [3, -18], [3, -6], [3, 6], [3, 18],
]; // [row, inches from the headpin's center line (board 20), + toward board 39]
const ARROWS: [number, number][] = [[5, 12.5], [10, 13.5], [15, 14.5], [20, 15.5], [25, 14.5], [30, 13.5], [35, 12.5]];
const DOTS = [3, 5, 8, 11, 14, 26, 29, 32, 35, 37];

export default function LanePreview({ rows }: { rows: number[][] }) {
  const ref = useRef<HTMLCanvasElement>(null);
  const PX_IN = 6.5; // horizontal px per inch
  const PX_FT = 11; // vertical px per foot (Al: "can we make it a bit longer please")
  const LEFT = 30; // foot labels
  const TOP = 14; // pit
  const BOTTOM = 14; // approach strip under the foul line
  const W = LEFT + (2 * GUTTER_IN + LANE_IN) * PX_IN + 6;
  const H = TOP + DECK_END_FT * PX_FT + BOTTOM;
  useEffect(() => {
    const c = ref.current;
    if (!c) return;
    const ctx = c.getContext("2d");
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    c.width = Math.round(W * dpr);
    c.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const laneX = LEFT + GUTTER_IN * PX_IN;
    const laneW = LANE_IN * PX_IN;
    const y = (ft: number) => TOP + (DECK_END_FT - ft) * PX_FT; // feet from the foul line -> px
    const bx = (board: number) => laneX + (board - 0.5) * BOARD_IN * PX_IN; // board center

    ctx.clearRect(0, 0, W, H);
    // Pit, gutters, approach.
    ctx.fillStyle = "#1f2024";
    ctx.fillRect(LEFT, 0, W - LEFT - 6, TOP);
    ctx.fillStyle = "#55575e";
    ctx.fillRect(LEFT, TOP, GUTTER_IN * PX_IN, DECK_END_FT * PX_FT);
    ctx.fillRect(laneX + laneW, TOP, GUTTER_IN * PX_IN, DECK_END_FT * PX_FT);
    ctx.fillStyle = "#d9c49a";
    ctx.fillRect(LEFT, y(0), W - LEFT - 6, BOTTOM);
    // Lane bed: wood to the headpin, the pin deck a shade darker.
    ctx.fillStyle = "#e9d8b4";
    ctx.fillRect(laneX, y(HEADPIN_FT - 0.4), laneW, (HEADPIN_FT - 0.4) * PX_FT);
    ctx.fillStyle = "#dcc79c";
    ctx.fillRect(laneX, TOP, laneW, (DECK_END_FT - HEADPIN_FT + 0.4) * PX_FT);

    // Oil.
    const max = Math.max(1, ...rows.flat());
    const bw = BOARD_IN * PX_IN;
    rows.forEach((row, r) =>
      row.forEach((v, b) => {
        if (v <= 0) return;
        ctx.fillStyle = `rgba(31,67,158,${Math.min(1, 0.06 + 0.94 * (v / max))})`;
        ctx.fillRect(laneX + b * bw, y(r + 1), Math.ceil(bw), Math.ceil(PX_FT));
      }),
    );

    // Board lines, faint.
    ctx.strokeStyle = "rgba(120,90,40,0.12)";
    ctx.lineWidth = 1;
    for (let b = 1; b < 39; b++) {
      const x = Math.round(laneX + b * bw) + 0.5;
      ctx.beginPath();
      ctx.moveTo(x, y(HEADPIN_FT - 0.4));
      ctx.lineTo(x, y(0));
      ctx.stroke();
    }

    // Markings.
    ctx.fillStyle = "#3b2f1d";
    DOTS.forEach((b) => {
      ctx.beginPath();
      ctx.arc(bx(b), y(7), 1.6, 0, Math.PI * 2);
      ctx.fill();
    });
    ARROWS.forEach(([b, ft]) => {
      ctx.beginPath();
      ctx.moveTo(bx(b), y(ft + 0.6));
      ctx.lineTo(bx(b) - 3, y(ft - 0.6));
      ctx.lineTo(bx(b) + 3, y(ft - 0.6));
      ctx.closePath();
      ctx.fill();
    });
    ctx.strokeStyle = "#3b2f1d";
    ctx.lineWidth = 1.5;
    [10, 30].forEach((b) => {
      ctx.beginPath();
      ctx.moveTo(bx(b), y(34));
      ctx.lineTo(bx(b), y(40));
      ctx.stroke();
    });
    // Foul line.
    ctx.fillStyle = "#b91c1c";
    ctx.fillRect(laneX - GUTTER_IN * PX_IN, y(0) - 1, laneW + 2 * GUTTER_IN * PX_IN, 2);

    // Pattern end.
    if (rows.length) {
      ctx.strokeStyle = "rgba(31,67,158,0.9)";
      ctx.setLineDash([4, 3]);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(laneX, Math.round(y(rows.length)) + 0.5);
      ctx.lineTo(laneX + laneW, Math.round(y(rows.length)) + 0.5);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // Pin spots.
    PINS.forEach(([row, dx]) => {
      const px = bx(20) + dx * PX_IN;
      const py = y(HEADPIN_FT + (row * PIN_ROW_IN) / 12);
      ctx.beginPath();
      ctx.arc(px, py, 3.2, 0, Math.PI * 2);
      ctx.fillStyle = "#ffffff";
      ctx.fill();
      ctx.strokeStyle = "#b91c1c";
      ctx.lineWidth = 1;
      ctx.stroke();
    });

    // Foot labels.
    ctx.fillStyle = "#8b8d94";
    ctx.font = "10px -apple-system, BlinkMacSystemFont, sans-serif";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let ft = 0; ft <= HEADPIN_FT; ft += 10) ctx.fillText(`${ft}'`, LEFT - 4, y(ft));
    if (rows.length && rows.length % 10) {
      ctx.fillStyle = "#6366f1";
      ctx.fillText(`${rows.length}'`, LEFT - 4, y(rows.length));
    }
  }, [rows, W, H]);
  const total = rows.flat().reduce((a, b) => a + b, 0);
  return (
    <div className="rounded-md border border-ink-200 p-2">
      <div className="mb-1 flex justify-between text-xs text-ink-500">
        <span>Lane: foul line to the pin deck, board 1 at left (width not to scale)</span>
        <span className="tabular-nums">{(total / 1000).toFixed(2)} mL</span>
      </div>
      <canvas ref={ref} style={{ aspectRatio: `${W} / ${H}` }} className="mx-auto block h-auto w-full max-w-[460px]" />
      <div className="mt-1 text-right text-xs text-ink-400">Pattern {rows.length} ft · headpin 60 ft</div>
    </div>
  );
}
