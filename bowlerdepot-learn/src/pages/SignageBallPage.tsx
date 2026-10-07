import QRCode from "qrcode";
import { useEffect, useMemo, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { getArticleBySlug, getLearnPlotter, resizedImageUrl } from "../api/client";
import type { ArticleDetail, LearnPlotterPoint } from "../api/types";

// In-store signage loop for one ball (runbook 6cm) -- Al: "do you think we
// could create a 9:16 vertical format video that could be put on our
// signage screens in our stores for each ball that is an animated version
// of the action images. it could include callouts for the ball meta data
// and price on bowlerdepot.com". The stores run piSignage (Raspberry Pi
// players); Al: "we can try the webpage version first".
//
// /signage/ball/<article slug> renders a fixed 1080x1920 canvas scaled to
// fill the screen, outside the site shell (no nav/footer), looping an
// 18 s, four-scene sequence:
//   0-5 s   action shot (slow push-in) + brand / name
//   5-10 s  product shot + spec callouts (cover, core, 15 lb RG/diff/MB)
//   10-14 s ball motion plotter position + "similar to" line
//   14-18 s BowlerDepot.com price + QR code to the product page
// Every number is drawn by code from live data -- never baked into an AI
// image -- so the price is always the current BowlerDepot price.
//
// Built for a Raspberry Pi's browser: only transform/opacity animations
// (GPU-composited), images pre-sized through img.bowleriq.io, no blur or
// filter effects. The same page can later be screen-recorded to an MP4 for
// players that should run from local video instead.

const LOOP_S = 18;
const W = 1080;
const H = 1920;

function fmtPrice(price?: number | null, currency?: string | null) {
  if (price == null) return null;
  return new Intl.NumberFormat("en-US", { style: "currency", currency: currency || "USD" }).format(price);
}

function titleCase(s?: string | null) {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : null;
}

export default function SignageBallPage() {
  const { slug } = useParams<{ slug: string }>();
  const [searchParams] = useSearchParams();
  // Hero layout (6cm follow-up): an AI-animated 9:16 background clip with
  // the data overlaid on it. ?video= plus the ball's position in the frame
  // (bx/by = center as fractions, br = radius as a fraction of width) drive
  // the prototype; the production pipeline will store these per ball.
  const video = searchParams.get("video");
  const ballPos = {
    x: Number(searchParams.get("bx") ?? 0.5),
    y: Number(searchParams.get("by") ?? 0.62),
    r: Number(searchParams.get("br") ?? 0.26),
  };
  const [article, setArticle] = useState<ArticleDetail | null>(null);
  const [point, setPoint] = useState<LearnPlotterPoint | null>(null);
  const [byId, setById] = useState<Map<string, LearnPlotterPoint>>(new Map());
  const [qr, setQr] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [scale, setScale] = useState(1);

  // Fit the 1080x1920 canvas to whatever the player's window is.
  useEffect(() => {
    const fit = () => setScale(Math.min(window.innerWidth / W, window.innerHeight / H));
    fit();
    window.addEventListener("resize", fit);
    return () => window.removeEventListener("resize", fit);
  }, []);

  useEffect(() => {
    document.title = "BowlerDepot signage";
    const robots = document.createElement("meta");
    robots.name = "robots";
    robots.content = "noindex";
    document.head.appendChild(robots);
    return () => robots.remove();
  }, []);

  useEffect(() => {
    if (!slug) return;
    let cancelled = false;
    getArticleBySlug(slug)
      .then(async (res) => {
        if (cancelled) return;
        if (!res.article) throw new Error("No article for this ball");
        setArticle(res.article);
        const url = res.article.product?.ecommerce_url;
        if (url) setQr(await QRCode.toDataURL(url, { margin: 1, width: 360, color: { dark: "#0f0f2dff", light: "#ffffffff" } }));
        try {
          const items = await getLearnPlotter();
          if (cancelled) return;
          setById(new Map(items.map((p) => [p.id, p])));
          setPoint(items.find((p) => p.id === res.product_id) ?? null);
        } catch {
          /* plotter scene is optional */
        }
      })
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : "Couldn't load"));
    return () => {
      cancelled = true;
    };
  }, [slug]);

  const product = article?.product;
  const sku = useMemo(() => {
    const skus = product?.skus ?? [];
    return skus.find((s) => s.weight_lbs === 15) ?? skus[0] ?? null;
  }, [product]);
  const similar = (point?.neighbors.twins ?? []).map((n) => byId.get(n.id)).filter(Boolean).slice(0, 2) as LearnPlotterPoint[];
  const price = fmtPrice(product?.ecommerce_price, product?.ecommerce_price_currency);

  if (error) return <div style={{ background: "#0f0f2d", color: "#fff", height: "100vh", padding: 40 }}>{error}</div>;
  if (!article || !product) return <div style={{ background: "#0f0f2d", height: "100vh" }} />;

  if (video) {
    return (
      <HeroLayout
        scale={scale}
        video={video}
        ball={ballPos}
        brand={product.brand_name ?? ""}
        name={product.name}
        callouts={[
          { k: "Coverstock", v: [product.coverstock_name, titleCase(product.coverstock_type)].filter(Boolean).join(" · ") },
          { k: "Core", v: [product.core_name, titleCase(product.core_type)].filter(Boolean).join(" · ") },
          sku
            ? {
                k: `${sku.weight_lbs} lb`,
                v: [sku.rg != null && `RG ${sku.rg}`, sku.differential != null && `Diff ${sku.differential}`, sku.mass_bias != null && `MB ${sku.mass_bias}`]
                  .filter(Boolean)
                  .join("  ·  "),
              }
            : null,
        ].filter((c): c is { k: string; v: string } => Boolean(c && c.v))}
        point={point}
        similar={similar}
        price={price}
        qr={qr}
      />
    );
  }

  const action = article.action_shot_image_url || product.primary_image_url;
  const shot = article.product_shot_image_url || product.primary_image_url;
  const actionSrc = action ? resizedImageUrl(action, { w: 1080, h: 1920, fit: "cover", fmt: "webp", q: 82 }) : null;
  const shotSrc = shot ? resizedImageUrl(shot, { w: 900, h: 900, fit: "contain", fmt: "webp" }) : null;

  const callouts = [
    { k: "Coverstock", v: [product.coverstock_name, titleCase(product.coverstock_type)].filter(Boolean).join(" · ") },
    { k: "Core", v: [product.core_name, titleCase(product.core_type)].filter(Boolean).join(" · ") },
    sku ? { k: `${sku.weight_lbs} lb specs`, v: [sku.rg != null && `RG ${sku.rg}`, sku.differential != null && `Diff ${sku.differential}`, sku.mass_bias != null && `MB ${sku.mass_bias}`].filter(Boolean).join("   ") } : null,
  ].filter((c): c is { k: string; v: string } => Boolean(c && c.v));

  return (
    <div style={{ position: "fixed", inset: 0, background: "#0f0f2d", overflow: "hidden" }}>
      <style>{SIGNAGE_CSS}</style>
      <div
        className="sg-canvas"
        style={{ width: W, height: H, transform: `translate(-50%, -50%) scale(${scale})`, position: "absolute", left: "50%", top: "50%" }}
      >
        {/* Scene 1: action shot */}
        <div className="sg-scene sg-s1">
          {actionSrc ? <img className="sg-action" src={actionSrc} alt="" /> : null}
          <div className="sg-shade" />
          <div className="sg-title">
            <p className="sg-brand">{product.brand_name}</p>
            <p className="sg-name">{product.name}</p>
          </div>
        </div>

        {/* Scene 2: product shot + spec callouts */}
        <div className="sg-scene sg-s2">
          {shotSrc ? <img className="sg-shot" src={shotSrc} alt="" /> : null}
          <p className="sg-s2-name">{product.brand_name} {product.name}</p>
          <div className="sg-callouts">
            {callouts.map((c, i) => (
              <div key={c.k} className="sg-callout" style={{ animationDelay: `${i * 0.35}s` }}>
                <span className="sg-k">{c.k}</span>
                <span className="sg-v">{c.v}</span>
              </div>
            ))}
          </div>
        </div>

        {/* Scene 3: plotter position */}
        {point ? (
          <div className="sg-scene sg-s3">
            <p className="sg-h">Ball motion</p>
            <MiniPlotter oil={point.oil} motion={point.motion} others={[...byId.values()].filter((p) => p.id !== point.id)} />
            <p className="sg-pos">
              Oil {point.oil.toFixed(1)} · Motion {point.motion.toFixed(1)}
            </p>
            {similar.length ? (
              <p className="sg-similar">Similar to {similar.map((s) => `${s.brand_name} ${s.name}`).join(" and ")}</p>
            ) : null}
          </div>
        ) : null}

        {/* Scene 4: price + QR */}
        <div className={`sg-scene ${point ? "sg-s4" : "sg-s4-noplot"}`}>
          {shotSrc ? <img className="sg-shot-sm" src={shotSrc} alt="" /> : null}
          <p className="sg-s4-name">{product.name}</p>
          {price ? <p className="sg-price">{price}</p> : null}
          <p className="sg-at">at BowlerDepot.com</p>
          {qr ? (
            <div className="sg-qr">
              <img src={qr} alt="" />
              <span>Scan to shop</span>
            </div>
          ) : null}
        </div>

        <div className="sg-logo">BOWLERDEPOT.COM</div>
      </div>
    </div>
  );
}

// Oil 1-16 across, motion 1-18 up: every other current ball as a faint
// dot for context, this ball's dot pulsing.
function MiniPlotter({ oil, motion, others }: { oil: number; motion: number; others: LearnPlotterPoint[] }) {
  const size = 760;
  const pad = 56;
  const px = (o: number) => pad + ((o - 1) / 15) * (size - 2 * pad);
  const py = (m: number) => size - pad - ((m - 1) / 17) * (size - 2 * pad);
  const x = px(oil);
  const y = py(motion);
  return (
    <svg className="sg-plot" width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
      <rect x={pad} y={pad} width={size - 2 * pad} height={size - 2 * pad} rx={18} fill="rgba(255,255,255,0.06)" stroke="rgba(255,255,255,0.25)" />
      {Array.from({ length: 4 }, (_, i) => pad + ((i + 1) * (size - 2 * pad)) / 5).map((v) => (
        <g key={v} stroke="rgba(255,255,255,0.12)">
          <line x1={v} y1={pad} x2={v} y2={size - pad} />
          <line x1={pad} y1={v} x2={size - pad} y2={v} />
        </g>
      ))}
      {others.map((p) => (
        <circle key={p.id} cx={px(p.oil)} cy={py(p.motion)} r={7} fill="rgba(255,255,255,0.22)" />
      ))}
      <text x={pad} y={size - 12} fill="#cbd5e1" fontSize={30} fontWeight={700}>Light oil</text>
      <text x={size - pad} y={size - 12} fill="#cbd5e1" fontSize={30} fontWeight={700} textAnchor="end">Heavy oil</text>
      <text x={pad} y={pad - 14} fill="#cbd5e1" fontSize={30} fontWeight={700}>Angular ↑</text>
      <text x={size - pad} y={pad - 14} fill="#cbd5e1" fontSize={30} fontWeight={700} textAnchor="end">↓ Smooth at bottom</text>
      <circle className="sg-dot-pulse" cx={x} cy={y} r={40} fill="#f59e0b" opacity={0.35} />
      <circle cx={x} cy={y} r={22} fill="#f59e0b" stroke="#fff" strokeWidth={6} />
    </svg>
  );
}

// One 18 s loop; every scene's keyframes are percentages of it, so the
// whole sequence repeats seamlessly with no JS timers.
const SIGNAGE_CSS = `
.sg-canvas { font-family: "Space Grotesk", Arial, sans-serif; color: #fff; overflow: hidden; background: #0f0f2d; }
.sg-scene { position: absolute; inset: 0; opacity: 0; animation-duration: ${LOOP_S}s; animation-iteration-count: infinite; animation-timing-function: ease-in-out; will-change: opacity, transform; }
.sg-s1 { animation-name: sg-show-1; }
.sg-s2 { animation-name: sg-show-2; display: flex; flex-direction: column; align-items: center; padding-top: 170px; background: radial-gradient(circle at 50% 30%, #1f439e 0%, #0f0f2d 70%); }
.sg-s3 { animation-name: sg-show-3; display: flex; flex-direction: column; align-items: center; padding-top: 260px; background: #0f0f2d; }
.sg-s4 { animation-name: sg-show-4; display: flex; flex-direction: column; align-items: center; padding-top: 200px; background: radial-gradient(circle at 50% 35%, #1f439e 0%, #0f0f2d 75%); }
.sg-s4-noplot { animation-name: sg-show-4-long; display: flex; flex-direction: column; align-items: center; padding-top: 200px; background: radial-gradient(circle at 50% 35%, #1f439e 0%, #0f0f2d 75%); }
@keyframes sg-show-1 { 0% {opacity:0} 2% {opacity:1} 26% {opacity:1} 29% {opacity:0} 100% {opacity:0} }
@keyframes sg-show-2 { 0%,27% {opacity:0} 30% {opacity:1} 54% {opacity:1} 57% {opacity:0} 100% {opacity:0} }
@keyframes sg-show-3 { 0%,55% {opacity:0} 58% {opacity:1} 76% {opacity:1} 79% {opacity:0} 100% {opacity:0} }
@keyframes sg-show-4 { 0%,77% {opacity:0} 80% {opacity:1} 98% {opacity:1} 100% {opacity:0} }
@keyframes sg-show-4-long { 0%,55% {opacity:0} 58% {opacity:1} 98% {opacity:1} 100% {opacity:0} }
.sg-action { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; animation: sg-push ${LOOP_S}s linear infinite; transform-origin: 50% 40%; }
@keyframes sg-push { 0% {transform: scale(1.0)} 28% {transform: scale(1.14)} 100% {transform: scale(1.14)} }
.sg-shade { position: absolute; inset: 0; background: linear-gradient(to top, rgba(15,15,45,0.95) 0%, rgba(15,15,45,0.2) 40%, rgba(15,15,45,0) 60%); }
.sg-title { position: absolute; left: 80px; right: 80px; bottom: 200px; animation: sg-rise-1 ${LOOP_S}s ease-out infinite; }
@keyframes sg-rise-1 { 0% {transform: translateY(80px); opacity:0} 5% {transform: translateY(0); opacity:1} 100% {transform: translateY(0); opacity:1} }
.sg-brand { font-size: 52px; font-weight: 700; letter-spacing: 6px; text-transform: uppercase; color: #fbbf24; margin: 0; }
.sg-name { font-size: 132px; font-weight: 700; line-height: 1.0; margin: 10px 0 0; text-transform: uppercase; }
.sg-shot { width: 820px; height: 820px; object-fit: cover; border-radius: 40px; box-shadow: 0 30px 80px rgba(0,0,0,0.5); animation: sg-slide ${LOOP_S}s ease-out infinite; }
@keyframes sg-slide { 0%,27% {transform: translateX(260px) scale(0.96); opacity:0} 32% {transform: translateX(0) scale(1); opacity:1} 55% {transform: translateX(0) scale(1.04); opacity:1} 100% {transform: translateX(0) scale(1.04); opacity:1} }
.sg-s2-name { font-size: 64px; font-weight: 700; margin: 30px 60px 40px; text-align: center; text-transform: uppercase; }
.sg-callouts { display: flex; flex-direction: column; gap: 26px; width: 900px; }
.sg-callout { display: flex; flex-direction: column; background: rgba(255,255,255,0.08); border-left: 10px solid #fbbf24; border-radius: 14px; padding: 22px 30px; animation: sg-callout ${LOOP_S}s ease-out infinite; }
@keyframes sg-callout { 0%,30% {transform: translateX(-120px); opacity:0} 34% {transform: translateX(0); opacity:1} 100% {transform: translateX(0); opacity:1} }
.sg-k { font-size: 34px; letter-spacing: 3px; text-transform: uppercase; color: #fbbf24; font-weight: 700; }
.sg-v { font-size: 52px; font-weight: 600; margin-top: 6px; }
.sg-h { font-size: 64px; font-weight: 700; letter-spacing: 4px; text-transform: uppercase; color: #fbbf24; margin: 0 0 40px; }
.sg-plot { animation: sg-zoom-in ${LOOP_S}s ease-out infinite; }
@keyframes sg-zoom-in { 0%,56% {transform: scale(0.85); opacity:0} 61% {transform: scale(1); opacity:1} 100% {transform: scale(1); opacity:1} }
.sg-dot-pulse { transform-box: fill-box; transform-origin: center; animation: sg-pulse 1.6s ease-in-out infinite; }
@keyframes sg-pulse { 0%,100% {transform: scale(0.8); opacity:0.5} 50% {transform: scale(1.4); opacity:0.15} }
.sg-pos { font-size: 72px; font-weight: 700; margin: 40px 0 0; }
.sg-similar { font-size: 44px; color: #cbd5e1; margin: 30px 80px 0; text-align: center; line-height: 1.3; }
.sg-shot-sm { width: 560px; height: 560px; object-fit: cover; border-radius: 32px; box-shadow: 0 24px 60px rgba(0,0,0,0.45); }
.sg-s4-name { font-size: 72px; font-weight: 700; margin: 20px 60px 0; text-align: center; text-transform: uppercase; }
.sg-price { font-size: 190px; font-weight: 700; margin: 10px 0 0; color: #fbbf24; line-height: 1; animation: sg-pop ${LOOP_S}s ease-out infinite; }
@keyframes sg-pop { 0%,79% {transform: scale(0.7); opacity:0} 83% {transform: scale(1.06); opacity:1} 86% {transform: scale(1)} 100% {transform: scale(1); opacity:1} }
.sg-at { font-size: 54px; font-weight: 600; margin: 10px 0 50px; color: #e2e8f0; }
.sg-qr { display: flex; flex-direction: column; align-items: center; gap: 14px; background: #fff; border-radius: 24px; padding: 26px 26px 18px; }
.sg-qr img { width: 300px; height: 300px; }
.sg-qr span { color: #0f0f2d; font-size: 38px; font-weight: 700; }
.sg-logo { position: absolute; top: 60px; left: 0; right: 0; text-align: center; font-size: 40px; font-weight: 700; letter-spacing: 8px; color: rgba(255,255,255,0.9); }
`;


// ---------------------------------------------------------------------------
// Hero layout (runbook 6cm follow-up). Al: "i was thinking a bit more custom.
// not just cards using the existing assets. something built on those with
// the background being animated and the meta data overlaid on the image".
// One continuous scene: the Veo-animated 9:16 signage shot loops underneath
// (8 s clip), and a 16 s overlay timeline plays over it --
//   0.5 s  name + brand settle in at the top (stay)
//   1.5 s  price lower third + QR (stay -- shoppers always see the price)
//   2-9 s  spec callouts: a thin line draws from the ball's edge outward,
//          then its label appears at the end of the line
//   9.5-15 s motion badge: mini chart + "Similar to ..."
// Only transform/opacity/stroke animations, no backdrop blur (Pi-friendly).
const HERO_LOOP_S = 16;

type Callout = { k: string; v: string };

function HeroLayout(props: {
  scale: number;
  video: string;
  ball: { x: number; y: number; r: number };
  brand: string;
  name: string;
  callouts: Callout[];
  point: LearnPlotterPoint | null;
  similar: LearnPlotterPoint[];
  price: string | null;
  qr: string | null;
}) {
  const { scale, video, ball, brand, name, callouts, point, similar, price, qr } = props;
  const cx = ball.x * W;
  const cy = ball.y * H;
  const r = ball.r * W;
  // Callout anchors: start on the ball's edge at these angles, run outward
  // to a label slot. Left, right, left-low -- alternating sides keeps the
  // labels clear of each other and of the ball.
  // Stacked down the gap between the title (~y 560) and the ball, each
  // ~190px apart so two-line labels never collide; the third sits beside
  // the ball's lower right.
  const slots = [
    { angle: -150, lx: 60, ly: Math.max(620, cy - r - 300), align: "left" as const },
    { angle: -30, lx: W - 60, ly: Math.max(810, cy - r - 110), align: "right" as const },
    { angle: 20, lx: W - 60, ly: cy + r * 0.55, align: "right" as const },
  ];
  return (
    <div style={{ position: "fixed", inset: 0, background: "#000", overflow: "hidden" }}>
      <style>{HERO_CSS}</style>
      <div
        className="hx-canvas"
        style={{ width: W, height: H, transform: `translate(-50%, -50%) scale(${scale})`, position: "absolute", left: "50%", top: "50%" }}
      >
        <video className="hx-video" src={video} autoPlay muted loop playsInline />
        <div className="hx-top-shade" />
        <div className="hx-bottom-shade" />

        <div className="hx-title">
          <p className="hx-brand">{brand}</p>
          <p className="hx-name">{name}</p>
        </div>

        <svg className="hx-lines" width={W} height={H} viewBox={`0 0 ${W} ${H}`}>
          {callouts.map((c, i) => {
            const s = slots[i % slots.length];
            const a = (s.angle * Math.PI) / 180;
            const x1 = cx + Math.cos(a) * (r + 8);
            const y1 = cy + Math.sin(a) * (r + 8);
            const elbowX = s.align === "left" ? s.lx + 420 : s.lx - 420;
            const d = `M ${x1} ${y1} L ${elbowX} ${s.ly + 36} L ${s.align === "left" ? s.lx : s.lx} ${s.ly + 36}`;
            return (
              <g key={c.k} className="hx-callout-g" style={{ animationDelay: `${i * 1.1}s` }}>
                <circle cx={x1} cy={y1} r={10} fill="#fbbf24" className="hx-dot" style={{ animationDelay: `${i * 1.1}s` }} />
                <path d={d} className="hx-line" style={{ animationDelay: `${i * 1.1}s` }} pathLength={1} />
              </g>
            );
          })}
        </svg>
        {callouts.map((c, i) => {
          const s = slots[i % slots.length];
          return (
            <div
              key={c.k}
              className={`hx-label hx-label-${s.align}`}
              style={{
                top: s.ly - 46,
                ...(s.align === "left" ? { left: s.lx } : { right: W - s.lx }),
                animationDelay: `${i * 1.1}s`,
              }}
            >
              <span className="hx-k">{c.k}</span>
              <span className="hx-v">{c.v}</span>
            </div>
          );
        })}

        {point ? (
          <div className="hx-badge">
            <MiniBadge oil={point.oil} motion={point.motion} />
            <div>
              <p className="hx-badge-h">Ball motion</p>
              <p className="hx-badge-pos">
                Oil {point.oil.toFixed(1)} · Motion {point.motion.toFixed(1)}
              </p>
              {similar.length ? <p className="hx-badge-sim">Similar to {similar.map((x) => `${x.brand_name} ${x.name}`).join(", ")}</p> : null}
            </div>
          </div>
        ) : null}

        <div className="hx-lower">
          <div>
            {price ? <p className="hx-price">{price}</p> : null}
            <p className="hx-at">at BowlerDepot.com</p>
          </div>
          {qr ? (
            <div className="hx-qr">
              <img src={qr} alt="" />
              <span>Scan to shop</span>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function MiniBadge({ oil, motion }: { oil: number; motion: number }) {
  const s = 200;
  const p = 14;
  const x = p + ((oil - 1) / 15) * (s - 2 * p);
  const y = s - p - ((motion - 1) / 17) * (s - 2 * p);
  return (
    <svg width={s} height={s} viewBox={`0 0 ${s} ${s}`} style={{ flexShrink: 0 }}>
      <rect x={p} y={p} width={s - 2 * p} height={s - 2 * p} rx={12} fill="rgba(255,255,255,0.08)" stroke="rgba(255,255,255,0.35)" />
      <line x1={s / 2} y1={p} x2={s / 2} y2={s - p} stroke="rgba(255,255,255,0.15)" />
      <line x1={p} y1={s / 2} x2={s - p} y2={s / 2} stroke="rgba(255,255,255,0.15)" />
      <circle className="sg-dot-pulse" cx={x} cy={y} r={16} fill="#fbbf24" opacity={0.4} />
      <circle cx={x} cy={y} r={9} fill="#fbbf24" stroke="#fff" strokeWidth={3} />
    </svg>
  );
}

const HERO_CSS = `
.hx-canvas { font-family: "Space Grotesk", Arial, sans-serif; color: #fff; overflow: hidden; background: #000; }
.hx-video { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.hx-top-shade { position: absolute; left: 0; right: 0; top: 0; height: 520px; background: linear-gradient(to bottom, rgba(5,8,25,0.85), rgba(5,8,25,0)); }
.hx-bottom-shade { position: absolute; left: 0; right: 0; bottom: 0; height: 560px; background: linear-gradient(to top, rgba(5,8,25,0.95) 30%, rgba(5,8,25,0)); }
.hx-title { position: absolute; top: 110px; left: 70px; right: 70px; animation: hx-title ${HERO_LOOP_S}s ease-out infinite; }
@keyframes hx-title { 0% {opacity:0; transform: translateY(-40px)} 4% {opacity:1; transform: translateY(0)} 97% {opacity:1} 100% {opacity:0} }
.hx-brand { margin: 0; font-size: 48px; font-weight: 700; letter-spacing: 10px; text-transform: uppercase; color: #fbbf24; text-shadow: 0 4px 18px rgba(0,0,0,0.6); }
.hx-name { margin: 6px 0 0; font-size: 150px; line-height: 0.95; font-weight: 700; text-transform: uppercase; text-shadow: 0 8px 30px rgba(0,0,0,0.7); }
.hx-lines { position: absolute; inset: 0; pointer-events: none; }
.hx-line { fill: none; stroke: #fbbf24; stroke-width: 4; stroke-dasharray: 1; stroke-dashoffset: 1; animation: hx-draw ${HERO_LOOP_S}s ease-out infinite; filter: drop-shadow(0 2px 6px rgba(0,0,0,0.6)); }
@keyframes hx-draw { 0%,12% {stroke-dashoffset: 1; opacity: 1} 18% {stroke-dashoffset: 0; opacity:1} 56% {stroke-dashoffset: 0; opacity:1} 60% {stroke-dashoffset: 0; opacity: 0} 100% {stroke-dashoffset: 1; opacity: 0} }
.hx-dot { opacity: 0; animation: hx-dot ${HERO_LOOP_S}s ease-out infinite; }
@keyframes hx-dot { 0%,11% {opacity:0} 13% {opacity:1} 56% {opacity:1} 60% {opacity:0} 100% {opacity:0} }
.hx-label { position: absolute; display: flex; flex-direction: column; max-width: 560px; background: rgba(8,10,30,0.72); border: 2px solid rgba(251,191,36,0.85); border-radius: 18px; padding: 16px 26px; opacity: 0; animation: hx-label ${HERO_LOOP_S}s ease-out infinite; box-shadow: 0 10px 30px rgba(0,0,0,0.45); }
.hx-label-right { text-align: right; align-items: flex-end; }
@keyframes hx-label { 0%,17% {opacity:0; transform: translateY(14px)} 21% {opacity:1; transform: translateY(0)} 56% {opacity:1} 60% {opacity:0} 100% {opacity:0} }
.hx-k { font-size: 28px; letter-spacing: 4px; text-transform: uppercase; color: #fbbf24; font-weight: 700; }
.hx-v { font-size: 44px; font-weight: 600; margin-top: 4px; line-height: 1.15; }
.hx-badge { position: absolute; left: 60px; right: 60px; top: 520px; display: flex; gap: 28px; align-items: center; background: rgba(8,10,30,0.72); border: 2px solid rgba(255,255,255,0.25); border-radius: 24px; padding: 22px 28px; opacity: 0; animation: hx-badge ${HERO_LOOP_S}s ease-out infinite; }
@keyframes hx-badge { 0%,60% {opacity:0; transform: translateX(-60px)} 64% {opacity:1; transform: translateX(0)} 94% {opacity:1} 97% {opacity:0} 100% {opacity:0} }
.hx-badge-h { margin: 0; font-size: 30px; letter-spacing: 4px; text-transform: uppercase; color: #fbbf24; font-weight: 700; }
.hx-badge-pos { margin: 6px 0 0; font-size: 48px; font-weight: 700; }
.hx-badge-sim { margin: 8px 0 0; font-size: 32px; color: #e2e8f0; line-height: 1.25; }
.hx-lower { position: absolute; left: 70px; right: 70px; bottom: 90px; display: flex; align-items: flex-end; justify-content: space-between; gap: 30px; animation: hx-lower ${HERO_LOOP_S}s ease-out infinite; }
@keyframes hx-lower { 0%,7% {opacity:0; transform: translateY(40px)} 12% {opacity:1; transform: translateY(0)} 97% {opacity:1} 100% {opacity:0} }
.hx-price { margin: 0; font-size: 170px; line-height: 1; font-weight: 700; color: #fbbf24; text-shadow: 0 8px 30px rgba(0,0,0,0.7); }
.hx-at { margin: 10px 0 0; font-size: 46px; font-weight: 600; color: #f1f5f9; }
.hx-qr { display: flex; flex-direction: column; align-items: center; gap: 8px; background: #fff; border-radius: 22px; padding: 18px 18px 12px; }
.hx-qr img { width: 230px; height: 230px; }
.hx-qr span { color: #0f0f2d; font-size: 30px; font-weight: 700; }
.sg-dot-pulse { transform-box: fill-box; transform-origin: center; animation: sg-pulse 1.6s ease-in-out infinite; }
@keyframes sg-pulse { 0%,100% {transform: scale(0.8); opacity:0.5} 50% {transform: scale(1.4); opacity:0.15} }
`;
