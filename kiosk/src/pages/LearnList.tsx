import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getArticles, img, type ArticleCard } from "../lib/api";
import { useKiosk } from "../lib/kiosk";
import { BigButton, Keyboard, PageTitle, SearchField, Spinner } from "../components/ui";

// Learn (runbook 6eh): BowlerDepot Learn's reviews and guides, opened in
// the kiosk's own reader (/learn/:slug) -- never the live Learn site, which
// would let a customer wander off the kiosk.
export default function LearnList() {
  const { sessionKey } = useKiosk();
  const [items, setItems] = useState<ArticleCard[] | null>(null);
  const [q, setQ] = useState("");
  const [searching, setSearching] = useState(false);
  useEffect(() => {
    setQ("");
    setSearching(false);
  }, [sessionKey]);

  useEffect(() => {
    const term = q.trim();
    const t = window.setTimeout(
      () => {
        getArticles({ limit: 48, search: term.length >= 2 ? term : undefined })
          .then(setItems)
          .catch(() => setItems([]));
      },
      term ? 300 : 0,
    );
    return () => window.clearTimeout(t);
  }, [q]);

  return (
    <div>
      <div className="flex items-start justify-between gap-6">
        <PageTitle kicker="Reviews & guides" title="BowlerDepot Learn" sub="In-depth ball reviews and how-to guides." />
        <BigButton tone={searching ? "brand" : "ghost"} onClick={() => setSearching(!searching)}>
          🔍 {searching ? "Hide search" : "Search"}
        </BigButton>
      </div>
      {searching && (
        <div className="mb-8 grid grid-cols-[minmax(0,1fr)_auto] items-start gap-8">
          <SearchField value={q} placeholder="Search reviews and guides" />
          <Keyboard value={q} onChange={setQ} onDone={() => setSearching(false)} />
        </div>
      )}
      {!items ? (
        <Spinner />
      ) : items.length ? (
        <div className="grid grid-cols-3 gap-6">
          {items.map((a) => {
            const pic = a.product_shot_image_url || a.primary_image_url || a.video_thumbnail_url;
            return (
              <Link key={a.article_id} to={`/learn/${a.slug || a.product_id}`} className="flex flex-col overflow-hidden rounded-3xl border-2 border-edge bg-panel active:border-brand">
                <div className="aspect-[16/9] bg-black/40">
                  {pic && <img src={img(pic, 720, 405, "cover") ?? pic} alt="" className="h-full w-full object-cover" loading="lazy" />}
                </div>
                <div className="flex flex-1 flex-col p-6">
                  <div className="mb-2 text-sm font-semibold uppercase tracking-wider text-brand">
                    {a.article_kind === "video" ? a.category_name || "Guide" : `${a.brand_name ?? ""} review`}
                  </div>
                  <div className="font-display text-2xl font-bold leading-tight text-ink">{a.title}</div>
                  {a.hook && <p className="mt-3 line-clamp-3 text-lg text-mute">{a.hook}</p>}
                </div>
              </Link>
            );
          })}
        </div>
      ) : (
        <p className="text-2xl text-mute">Nothing matches “{q}”.</p>
      )}
    </div>
  );
}
