import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  MATERIAL_LABEL,
  getPlotterBall,
  getProduct,
  getProductArticle,
  img,
  titleCase,
  type ArticleDetail,
  type LearnPlotterPoint,
  type ProductDetail,
  type ProductVideo,
} from "../lib/api";
import { useKiosk } from "../lib/kiosk";
import { BigButton, Markdown, Spinner, TakeItWithYou, VideoModal } from "../components/ui";
import { FocusPlotter, Suggestions, usePlotterIndex } from "../components/plotterBits";

// Ball page (runbook 6eh): everything about one ball on one screen, top to
// bottom -- photo and key facts with the QR hand-off, specs per weight,
// where it sits on the ball motion plotter and the balls around it, the
// review summary and the article's highlights, then playable review
// videos. No prices (Al's call); buying goes through staff or the phone.

export default function Ball() {
  const { id = "" } = useParams();
  const { compare, toggleCompare } = useKiosk();
  const [product, setProduct] = useState<ProductDetail | null>(null);
  const [article, setArticle] = useState<ArticleDetail | null>(null);
  const [ball, setBall] = useState<LearnPlotterPoint | null>(null);
  const [error, setError] = useState(false);
  const [playing, setPlaying] = useState<ProductVideo | null>(null);
  const { points, byId } = usePlotterIndex();

  useEffect(() => {
    setProduct(null);
    setArticle(null);
    setBall(null);
    setError(false);
    window.scrollTo(0, 0);
    getProduct(id).then(setProduct).catch(() => setError(true));
    getProductArticle(id).then(setArticle).catch(() => undefined);
    getPlotterBall(id).then(setBall).catch(() => undefined);
  }, [id]);

  if (error) return <p className="text-2xl text-mute">We couldn't load this ball. Please ask our staff.</p>;
  if (!product) return <Spinner />;

  const hero = article?.product_shot_image_url || product.primary_image_url;
  const inCompare = compare.includes(product.id);
  const skus = [...(product.skus ?? [])].sort((a, b) => b.weight_lbs - a.weight_lbs);
  const videos = (product.videos ?? []).filter((v) => v.youtube_video_id).slice(0, 8);
  const facts: [string, string | null][] = [
    ["Cover", product.coverstock_full_name || product.coverstock_name],
    ["Cover type", product.coverstock_type ? titleCase(product.coverstock_type) : null],
    ["Material", product.coverstock_material ? MATERIAL_LABEL[product.coverstock_material] ?? product.coverstock_material : null],
    ["Finish", product.factory_finish],
    ["Core", [product.core_name, product.core_type && titleCase(product.core_type)].filter(Boolean).join(" · ") || null],
    ["Color", product.color],
    ["Released", product.release_date ? new Date(product.release_date + "T00:00:00").toLocaleDateString(undefined, { month: "long", year: "numeric" }) : null],
  ];

  return (
    <div className="flex flex-col gap-12">
      {/* Hero */}
      <div className="grid grid-cols-[minmax(0,5fr)_minmax(0,6fr)] gap-10">
        <div className="flex aspect-square items-center justify-center overflow-hidden rounded-[2rem] bg-white">
          {hero && <img src={img(hero, 1000, 1000) ?? ""} alt={product.name} className="h-full w-full object-contain p-6" />}
        </div>
        <div className="flex flex-col gap-6">
          <div>
            <div className="text-xl font-semibold uppercase tracking-[0.2em] text-brand">{product.brand_name}</div>
            <h1 className="font-display text-7xl font-bold leading-[1.02] text-ink">{product.name}</h1>
            {product.status === "retired" && (
              <span className="mt-3 inline-block rounded-full bg-hot/20 px-4 py-1 text-lg font-semibold text-hot">Retired</span>
            )}
            {article?.hook && <p className="mt-5 line-clamp-4 text-2xl leading-snug text-mute">{article.hook}</p>}
          </div>
          <div className="flex flex-wrap gap-4">
            <BigButton tone={inCompare ? "brand" : "ghost"} onClick={() => toggleCompare(product.id)}>
              {inCompare ? "✓ In compare" : "+ Add to compare"}
            </BigButton>
            {article?.slug && <BigButton to={`/learn/${article.slug}`}>📖 Read the full review</BigButton>}
            {videos.length > 0 && (
              <BigButton tone="ghost" onClick={() => document.getElementById("videos")?.scrollIntoView({ behavior: "smooth" })}>
                ▶ {videos.length} review videos
              </BigButton>
            )}
          </div>
          <TakeItWithYou ballId={product.id} name={product.name} />
        </div>
      </div>

      {/* Specs */}
      <div className="grid grid-cols-2 gap-8">
        <div className="rounded-3xl border-2 border-edge bg-panel p-8">
          <h2 className="mb-5 font-display text-3xl font-bold text-ink">Specs</h2>
          <dl className="grid grid-cols-[auto_1fr] gap-x-8 gap-y-3 text-xl">
            {facts
              .filter(([, v]) => v)
              .map(([k, v]) => (
                <div key={k} className="contents">
                  <dt className="text-mute">{k}</dt>
                  <dd className="font-semibold text-ink">{v}</dd>
                </div>
              ))}
          </dl>
        </div>
        <div className="rounded-3xl border-2 border-edge bg-panel p-8">
          <h2 className="mb-5 font-display text-3xl font-bold text-ink">By weight</h2>
          {skus.length ? (
            <table className="w-full text-left text-xl">
              <thead>
                <tr className="text-base uppercase tracking-wider text-mute">
                  <th className="pb-3">Weight</th>
                  <th className="pb-3">RG</th>
                  <th className="pb-3">Differential</th>
                  {skus.some((s) => s.mass_bias !== null) && <th className="pb-3">Mass bias</th>}
                </tr>
              </thead>
              <tbody>
                {skus.map((s) => (
                  <tr key={s.weight_lbs} className="border-t border-edge">
                    <td className="py-3 font-semibold text-ink">{s.weight_lbs} lb</td>
                    <td className="py-3 text-ink">{s.rg?.toFixed(2) ?? "--"}</td>
                    <td className="py-3 text-ink">{s.differential?.toFixed(3) ?? "--"}</td>
                    {skus.some((x) => x.mass_bias !== null) && <td className="py-3 text-ink">{s.mass_bias?.toFixed(3) ?? "--"}</td>}
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="text-xl text-mute">The manufacturer hasn't published per-weight specs yet.</p>
          )}
        </div>
      </div>

      {/* Ball motion */}
      {ball && points && (
        <div className="flex flex-col gap-6">
          <div>
            <h2 className="font-display text-4xl font-bold text-ink">Where it sits on the ball motion chart</h2>
            <p className="mt-2 text-xl text-mute">
              Left to right is oil (light to heavy). Bottom to top is how it moves (smooth to angular). The colored balls are our suggestions.
            </p>
          </div>
          <FocusPlotter ball={ball} points={points} byId={byId} />
          <Suggestions ball={ball} byId={byId} />
        </div>
      )}

      {/* Reviews */}
      {(product.video_reviews_summary || article) && (
        <div className="grid grid-cols-[minmax(0,3fr)_minmax(0,2fr)] gap-8">
          {product.video_reviews_summary && (
            <div className="rounded-3xl border-2 border-edge bg-panel p-8">
              <div className="mb-1 text-base font-semibold uppercase tracking-widest text-brand">
                What reviewers say{product.video_reviews_summary_video_count ? ` · ${product.video_reviews_summary_video_count} videos` : ""}
              </div>
              <Markdown text={product.video_reviews_summary} />
            </div>
          )}
          {article && (
            <div className="flex flex-col gap-6">
              {article.pros?.length ? <ListCard title="Pros" items={article.pros} tone="text-[#7ee2b8]" /> : null}
              {article.cons?.length ? <ListCard title="Cons" items={article.cons} tone="text-hot" /> : null}
              {article.who_should_buy?.length ? <ListCard title="Who it's for" items={article.who_should_buy} tone="text-gold" /> : null}
              {article.slug && (
                <Link to={`/learn/${article.slug}`} className="text-2xl font-semibold text-brand">
                  Read the full review ›
                </Link>
              )}
            </div>
          )}
        </div>
      )}

      {/* Videos */}
      {videos.length > 0 && (
        <div id="videos">
          <h2 className="mb-5 font-display text-4xl font-bold text-ink">Review videos</h2>
          <div className="grid grid-cols-4 gap-6">
            {videos.map((v) => (
              <button key={v.youtube_video_id} type="button" onClick={() => setPlaying(v)} className="text-left">
                <div className="relative aspect-video overflow-hidden rounded-2xl bg-black">
                  {v.thumbnail_url && <img src={v.thumbnail_url} alt="" className="h-full w-full object-cover" loading="lazy" />}
                  <span className="absolute inset-0 flex items-center justify-center">
                    <span className="flex h-16 w-16 items-center justify-center rounded-full bg-black/70 text-3xl text-white">▶</span>
                  </span>
                </div>
                <div className="mt-3 line-clamp-2 text-lg font-semibold leading-snug text-ink">{v.title}</div>
                {v.channel_title && <div className="text-base text-mute">{v.channel_title}</div>}
              </button>
            ))}
          </div>
        </div>
      )}

      {playing && <VideoModal videoId={playing.youtube_video_id} title={playing.title} onClose={() => setPlaying(null)} />}
    </div>
  );
}

function ListCard({ title, items, tone }: { title: string; items: string[]; tone: string }) {
  return (
    <div className="rounded-3xl border-2 border-edge bg-panel p-6">
      <div className={`mb-3 font-display text-2xl font-bold ${tone}`}>{title}</div>
      <ul className="list-disc space-y-2 pl-6 text-lg text-mute">
        {items.slice(0, 4).map((it, i) => (
          <li key={i}>{it}</li>
        ))}
      </ul>
    </div>
  );
}
