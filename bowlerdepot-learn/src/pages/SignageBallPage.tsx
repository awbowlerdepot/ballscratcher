import QRCode from "qrcode";
import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
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
