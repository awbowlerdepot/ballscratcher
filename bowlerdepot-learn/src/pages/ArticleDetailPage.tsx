import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ApiError,
  bowlerDepotSearchUrl,
  estimateReadingTimeMinutes,
  formatArticleDate,
  getProductArticle,
  resizedImageUrl,
} from "../api/client";
import type { ProductArticleResponse } from "../api/types";
import ArticleDetailSkeleton from "../components/ArticleDetailSkeleton";

export default function ArticleDetailPage() {
  const { productId } = useParams<{ productId: string }>();
  const [data, setData] = useState<ProductArticleResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Brand lineup carousel's own scroll container (Al: "a other balls
  // from the same manufacture carousel to the bottom of each article").
  // A plain ref + scrollBy is enough here -- no need for a carousel
  // library just to nudge a native overflow-x-auto strip left/right.
  const brandLineupRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!productId) return;
    setLoading(true);
    setNotFound(false);
    setError(null);
    getProductArticle(productId)
      .then(setData)
      .catch((err) => {
        if (err instanceof ApiError && err.status === 404) {
          setNotFound(true);
        } else {
          setError("Couldn't load this review right now -- try again in a moment.");
        }
      })
      .finally(() => setLoading(false));
  }, [productId]);

  const article = data?.article;

  // The prerendered static HTML (scripts/prerender.ts) already sets the
  // right <title> for whichever article a browser lands on directly,
  // but react-router client-side navigation between articles (Related
  // Reviews / Similar Balls links) never re-runs that build-time logic,
  // so the tab title used to stick on whatever page was first loaded.
  // Same title format prerender.ts already uses, kept in sync here for
  // in-app navigation. Deliberately declared BEFORE the loading/notFound/
  // error/no-article early returns below (guarding on `article` inside
  // the effect body instead) -- a hook can never be skipped on some
  // renders and called on others (Rules of Hooks), and this component's
  // very first render always has loading=true/article=undefined, so a
  // hook placed after those early returns would only run on later
  // renders and throw "Rendered more hooks than during the previous
  // render" the first time data actually loaded.
  useEffect(() => {
    if (!article) return;
    document.title = `${article.title} | Learn | The Bowler Depot`;
  }, [article]);

  if (loading) return <ArticleDetailSkeleton />;
  if (notFound) return <p className="py-8 text-center text-muted">That ball isn't in our catalog.</p>;
  if (error) return <p className="py-8 text-center text-alert">{error}</p>;
  if (!article) return <p className="py-8 text-center text-muted">No review is published for this ball yet.</p>;

  const product = article.product;
  const rawHeroImage = article.action_shot_image_url || product?.primary_image_url;
  // 700x525 (4:3) -- 2x-retina-sized for the hero box's ~325px rendered
  // width (see the md:w-[325px] box below), served via img.bowleriq.io.
  const heroImage = rawHeroImage ? resizedImageUrl(rawHeroImage, { w: 700, h: 525, fit: "cover" }) : null;

  function scrollBrandLineup(direction: -1 | 1) {
    brandLineupRef.current?.scrollBy({ left: direction * 320, behavior: "smooth" });
  }

  // Migration 031 -- "Bowling Balls · Ball Review" eyebrow, read straight
  // off the article row rather than hardcoded (see ArticleCard's own
  // comment on the same fields). Null for a pre-migration article.
  const taxonomyLabel = [article.category_name, article.article_type_name].filter(Boolean).join(" · ");

  // Al: "can we add published dates and last updated dates to the
  // articles" -- publishedLabel from first_published_at (set once, ever
  // -- see types.ts's own comment), updatedLabel from reviewed_at
  // (re-stamped every approval). Only show "Updated" when it's actually
  // a DIFFERENT date than "Published" -- an article that's never been
  // regenerated has the same value in both fields, and showing "Published
  // Sep 5, 2026 · Updated Sep 5, 2026" reads as redundant noise rather
  // than useful information. Falls back to reviewed_at for "Published"
  // on the rare pre-migration row where first_published_at is somehow
  // still null (033_product_articles_first_published_at.sql's own
  // backfill should prevent that in practice).
  const publishedLabel = formatArticleDate(article.first_published_at ?? article.reviewed_at);
  const updatedLabel = formatArticleDate(article.reviewed_at);
  const readingTime = estimateReadingTimeMinutes(article);

  return (
    <div>
      <Link to="/" className="mb-6 inline-block text-sm font-medium text-muted hover:text-ink">
        &larr; All reviews
      </Link>

      <div className="relative left-1/2 right-1/2 mb-10 -mx-[50vw] w-screen bg-neutral-900 py-10">
        <div className="mx-auto grid max-w-5xl grid-cols-1 gap-8 px-6 md:grid-cols-[320px_1fr] md:px-8">
          <div className="flex items-center justify-center overflow-visible px-2 md:relative md:px-4">
            <div className="mt-14 mb-0 aspect-[4/3] w-full rotate-0 md:-rotate-[8deg] rounded-sm bg-white p-2 shadow-[0_12px_24px_-6px_rgba(0,0,0,0.55)] md:absolute md:left-0 md:top-1/2 md:my-0 md:w-[325px] md:-translate-y-1/2">
              {heroImage ? (
                <img
                  src={heroImage}
                  alt={product?.name || article.title}
                  className="h-full w-full rounded-[2px] object-cover"
                />
              ) : null}
            </div>
          </div>
          <div className="flex flex-col justify-center">
            <p className="mb-1 text-[11px] font-semibold uppercase tracking-widest text-white/60">
              {taxonomyLabel || product?.core_type || product?.coverstock_type}
            </p>
            <h1 className="font-display text-xl font-semibold text-white">{article.title}</h1>
            <p className="mt-2 text-base text-white/80">{article.hook}</p>
            {/* Byline -- Al: "add published dates and last updated dates
                to the articles." Author is an Organization, not a named
                person (this is AI-generated review content, no individual
                writer to credit) -- "By BowlerDepot Team" mirrors the same
                Organization author this page's JSON-LD uses (see
                prerender.ts). Text and order here intentionally match
                what prerender.ts renders into the static HTML, since
                Google's byline-date guidance calls for the visible date
                to be consistent with whatever the page's structured data
                says. */}
            <p className="mt-3 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-white/60">
              <span>By BowlerDepot Team</span>
              <span aria-hidden="true">&middot;</span>
              <span>{readingTime} min read</span>
              {publishedLabel ? (
                <>
                  <span aria-hidden="true">&middot;</span>
                  <span>Published {publishedLabel}</span>
                </>
              ) : null}
              {updatedLabel && updatedLabel !== publishedLabel ? (
                <>
                  <span aria-hidden="true">&middot;</span>
                  <span>Updated {updatedLabel}</span>
                </>
              ) : null}
            </p>
          </div>
        </div>
      </div>

      {article.performance_summary ? (
        <div className="mb-10">
          <h2 className="mb-3 font-display text-xl font-semibold text-ink">Performance</h2>
          <p className="text-ink/90">{article.performance_summary}</p>
        </div>
      ) : null}

      {article.pros?.length || article.cons?.length ? (
        <div className="mb-10">
          <h2 className="mb-3 font-display text-xl font-semibold text-ink">Pros &amp; Cons</h2>
          <div className="grid grid-cols-1 gap-6 sm:grid-cols-2">
            <div>
              <div className="mb-2 text-sm font-semibold text-green-700">Pros</div>
              <ul className="list-disc space-y-1 pl-5 text-sm text-ink/90">
                {(article.pros || []).map((p, i) => (
                  <li key={i}>{p}</li>
                ))}
              </ul>
            </div>
            <div>
              <div className="mb-2 text-sm font-semibold text-alert">Cons</div>
              <ul className="list-disc space-y-1 pl-5 text-sm text-ink/90">
                {(article.cons || []).map((c, i) => (
                  <li key={i}>{c}</li>
                ))}
              </ul>
            </div>
          </div>
        </div>
      ) : null}

      {article.who_should_buy?.length ? (
        <div className="mb-10">
          <h2 className="mb-3 font-display text-xl font-semibold text-ink">Who Should Buy This</h2>
          <ul className="list-disc space-y-1 pl-5 text-sm text-ink/90">
            {article.who_should_buy.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {article.who_should_skip?.length ? (
        <div className="mb-10">
          <h2 className="mb-3 font-display text-xl font-semibold text-ink">Who Should Skip This</h2>
          <ul className="list-disc space-y-1 pl-5 text-sm text-ink/90">
            {article.who_should_skip.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {article.buying_tips ? (
        <div className="mb-10">
          <h2 className="mb-3 font-display text-xl font-semibold text-ink">Buying Tips</h2>
          <p className="text-ink/90">{article.buying_tips}</p>
        </div>
      ) : null}

      {/* Al: "add a hero section to the article if there is a Brad and
          Kyle youtube video approved for the ball the article is
          about... i was thinking something more like the hero at the
          top, darker box with video right justified" -- then, after
          seeing that first pass: "something like this with a headline
          'Watch what Brad & Kyle have to say!' and some of the AI
          summary of the video" / "Watch this review from Brad & Kyle."
          Mirrors the page's own top hero banner's full-bleed treatment
          (bg-neutral-900 breaking out of the article's max-width
          column via the same relative/-mx-[50vw]/w-screen trick, same
          max-w-5xl inner column, font-display heading) but adds a
          distinct lighter panel (bg-white/5) behind the AI summary
          blurb on the left, echoing the reference screenshot Al shared
          -- video stays right-justified in its own black aspect-video
          box. Falls back to the video's own title when summary is
          still null (video approved but not yet summarized -- see
          public_api's own comment on this field). Only ever renders
          for the one specific channel (server-matched on channel_
          title) -- most articles will have no featured_video and this
          section simply won't appear. youtube.com/embed (not youtube-
          nocookie.com -- no precedent for privacy-enhanced mode
          anywhere in this codebase, matching the plain embed
          consumer-site's own video grid already uses). */}
      {article.featured_video ? (
        <div className="relative left-1/2 right-1/2 mb-10 -mx-[50vw] w-screen bg-neutral-900 py-10">
          <div className="mx-auto max-w-5xl px-6 md:px-8">
            <h2 className="mb-4 font-display text-xl font-semibold text-white">
              Watch this review from Brad &amp; Kyle
            </h2>
            <div className="grid grid-cols-1 overflow-hidden rounded-sm md:grid-cols-[1fr_360px]">
              <div className="flex flex-col justify-center bg-white/5 p-6">
                <p className="italic text-white/80">
                  {article.featured_video.summary || article.featured_video.title}
                </p>
              </div>
              <div className="aspect-video w-full bg-black">
                <iframe
                  className="h-full w-full"
                  src={`https://www.youtube.com/embed/${article.featured_video.youtube_video_id}`}
                  title={article.featured_video.title || "Featured video"}
                  loading="lazy"
                  allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
                  allowFullScreen
                />
              </div>
            </div>
          </div>
        </div>
      ) : null}

      {article.verdict ? (
        <div className="mb-10">
          <h2 className="mb-3 font-display text-xl font-semibold text-ink">Verdict</h2>
          <p className="text-ink/90">{article.verdict}</p>
        </div>
      ) : null}

      {/* Al: "after the verdict in each article can we include the call
          to action to shop for the ball ... This should only show while
          the ball is current." Gated on product.status (see
          ArticleProductSpec's own comment on that field) rather than
          just ecommerce_url presence -- a retired ball can still have a
          leftover BowlerDepot price-tracking row, and pushing readers to
          buy something no longer sold is worse than showing no CTA at
          all. Falls back to bowlerDepotSearchUrl the same way every
          other ecommerce link on this page already does when
          ecommerce_url itself is null. */}
      {product && product.status === "current" ? (
        <div className="mb-10 flex flex-wrap items-center justify-between gap-4 rounded-xl bg-accent px-6 py-6">
          <div>
            <p className="font-display text-lg font-semibold text-white">
              Like what you read? Shop the {product.name}.
            </p>
            <p className="mt-1 text-sm text-white/80">This ball is currently available at BowlerDepot.com.</p>
          </div>
          <a
            href={product.ecommerce_url || bowlerDepotSearchUrl(product.name)}
            target="_blank"
            rel="noreferrer"
            className="shrink-0 whitespace-nowrap rounded-lg bg-white px-5 py-3 font-display text-sm font-semibold text-accent hover:bg-white/90"
          >
            Shop this ball &rarr;
          </a>
        </div>
      ) : null}

      {/* Al: "move the related reviews section up to just below the shop
          call to action." Was originally down near the bottom, after FAQ
          (see git history / DEPLOY_RUNBOOK.md 6ao for the Related
          Reviews + Similar Balls merge that shaped this section) --
          moved up here so it surfaces right after the CTA rather than
          making readers scroll past Specs and FAQ first. Al: "how is
          related reviews curated and how is similar balls curated... i
          think they should both link to the articles and for all the
          rails use the same style and the product shot image from the
          article for its image." This section used to be two rails --
          this one and a since-removed "Similar Balls" rail that linked
          out to BowlerDepot instead of another Learn article. Once
          Similar Balls was also going to link to articles, both rails
          would have pulled the identical candidate list, so they're
          merged here. */}
      {article.related_reviews?.length ? (
        <div className="mb-10">
          <h2 className="mb-3 font-display text-xl font-semibold text-ink">Related Reviews</h2>
          <div className="grid grid-cols-1 gap-x-8 gap-y-10 sm:grid-cols-2 lg:grid-cols-3">
            {article.related_reviews.map((r) => (
              <Link key={r.product_id} className="group block" to={`/articles/${r.product_id}`}>
                <div className="aspect-[4/3] w-full overflow-hidden rounded-md bg-paper-border/40">
                  {r.image_url ? (
                    <img
                      src={resizedImageUrl(r.image_url, { w: 640, h: 480, fit: "cover" })}
                      alt={r.product_name}
                      loading="lazy"
                      className="h-full w-full object-cover"
                    />
                  ) : null}
                </div>
                <div className="mt-3">
                  <div className="font-display font-semibold text-ink group-hover:text-accent">{r.title}</div>
                  <div className="text-xs text-muted">{r.product_name}</div>
                </div>
              </Link>
            ))}
          </div>
        </div>
      ) : null}

      {product?.skus?.length ? (
        <div className="mb-10">
          <h2 className="mb-3 font-display text-xl font-semibold text-ink">Specs</h2>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr>
                  <th className="border-b border-paper-border px-3 py-2 text-left text-xs font-medium uppercase tracking-wide text-muted">
                    Weight
                  </th>
                  <th className="border-b border-paper-border px-3 py-2 text-left text-xs font-medium uppercase tracking-wide text-muted">
                    RG
                  </th>
                  <th className="border-b border-paper-border px-3 py-2 text-left text-xs font-medium uppercase tracking-wide text-muted">
                    Differential
                  </th>
                  <th className="border-b border-paper-border px-3 py-2 text-left text-xs font-medium uppercase tracking-wide text-muted">
                    Mass Bias
                  </th>
                </tr>
              </thead>
              <tbody>
                {product.skus.map((sku) => (
                  <tr key={sku.weight_lbs}>
                    <td className="border-b border-paper-border px-3 py-2">{sku.weight_lbs} lb</td>
                    <td className="border-b border-paper-border px-3 py-2">{sku.rg ?? "--"}</td>
                    <td className="border-b border-paper-border px-3 py-2">{sku.differential ?? "--"}</td>
                    <td className="border-b border-paper-border px-3 py-2">{sku.mass_bias ?? "--"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      {article.faq?.length ? (
        <div className="mb-10">
          <h2 className="mb-3 font-display text-xl font-semibold text-ink">FAQ</h2>
          {article.faq.map((item, i) => (
            <div className="border-t border-paper-border py-4" key={i}>
              <p className="mb-1 font-medium text-ink">{item.question}</p>
              <p className="text-muted">{item.answer}</p>
            </div>
          ))}
        </div>
      ) : null}

      {/* Al: "change the more from section at the bottom to be links to
          additional articles for the brand of the ball the current
          article is from and can we use the demand score to sort them."
          brand_lineup is already sorted server-side by demand_score
          descending (see public_api's own comment on that query) -- this
          just renders it, it doesn't re-sort. Reworked from the original
          shop-the-lineup rail (external ecommerce links, price shown) to
          an editorial cross-link rail: internal <Link>s to other
          articles for this brand, same card content (title/hook) as the
          Related Reviews section above, just in this section's existing
          horizontal-scroll carousel shell instead of a grid. */}
      {article.brand_lineup?.length ? (
        <div className="mb-10">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="font-display text-xl font-semibold text-ink">
              More from {product?.brand_name || "this brand"}
            </h2>
            <div className="flex shrink-0 gap-2">
              <button
                type="button"
                onClick={() => scrollBrandLineup(-1)}
                aria-label="Scroll left"
                className="rounded-full border border-paper-border px-3 py-1 text-sm text-ink hover:bg-ink hover:text-paper"
              >
                &larr;
              </button>
              <button
                type="button"
                onClick={() => scrollBrandLineup(1)}
                aria-label="Scroll right"
                className="rounded-full border border-paper-border px-3 py-1 text-sm text-ink hover:bg-ink hover:text-paper"
              >
                &rarr;
              </button>
            </div>
          </div>
          <div
            ref={brandLineupRef}
            className="flex snap-x snap-mandatory gap-6 overflow-x-auto pb-2 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
          >
            {article.brand_lineup.map((b) => (
              <Link
                key={b.product_id}
                className="group block w-48 shrink-0 snap-start"
                to={`/articles/${b.product_id}`}
              >
                <div className="aspect-[4/3] w-full overflow-hidden rounded-md bg-paper-border/40">
                  {b.image_url ? (
                    <img
                      src={resizedImageUrl(b.image_url, { w: 400, h: 300, fit: "cover" })}
                      alt={b.product_name}
                      loading="lazy"
                      className="h-full w-full object-cover"
                    />
                  ) : null}
                </div>
                <div className="mt-3">
                  <div className="font-display font-semibold text-ink group-hover:text-accent">{b.title}</div>
                  <div className="text-xs text-muted">{b.product_name}</div>
                </div>
              </Link>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
