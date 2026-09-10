// Build-time static prerendering for Learn article pages -- Task #440,
// direct follow-on from the "sophisticated Learn section" pivot (Al:
// "bigcommerce is just not the place [for articles]"). WHY THIS EXISTS:
// BigCommerce's native blog gave every post real server-rendered HTML
// for free -- a search crawler (or a link-preview unfurler) got the
// actual title/body on the very first response. bowlerdepot-learn/ is a
// plain client-rendered React SPA (see src/main.tsx): the raw HTML
// response for ANY route, including /articles/<id>, is just an empty
// <div id="root"> and a <script> tag until the browser runs React. That
// would be a real regression versus what the BigCommerce sync gave up,
// not a lateral move -- most crawlers execute little or no JS, and even
// ones that do (Googlebot) pay a real, documented "second wave"
// indexing delay for JS-rendered content.
//
// This script runs as the LAST step of `npm run build` (see
// package.json), after `vite build` has already produced dist/ (the
// real index.html + hashed JS/CSS bundles). For every APPROVED article
// (GET /articles, paginated), it writes dist/articles/<product_id>/
// index.html -- a copy of the built index.html with the SPA's empty
// root div replaced by real, crawlable server-rendered-ish markup for
// that specific article (title/meta description/Open Graph tags/
// JSON-LD Review structured data, plus the actual visible content:
// hook, performance summary, pros/cons, verdict, spec table, FAQ).
//
// Deliberately NOT real React SSR (no react-dom/server, no
// ReactDOMServer.renderToString, no hydrateRoot on the client side) --
// this project has no server runtime at request time (S3 + CloudFront,
// see template.yaml's LearnSiteDistribution, task #441), and matching a
// real SSR/hydration tree exactly is a much larger undertaking for a
// content shape (mostly static text) that doesn't need it. Instead:
// this script hand-builds a plain HTML string mirroring the visible
// shape of src/pages/ArticleDetailPage.tsx closely enough for a visitor
// with JS disabled (or a crawler) to read the full review, and
// src/main.tsx's createRoot(...).render(...) (NOT hydrateRoot) then
// fully replaces that markup with the real interactive React app once
// JS loads for a normal browser visit -- no hydration-mismatch warnings
// possible since nothing is being hydrated, just plain replaced.
//
// A companion piece: writes dist/sitemap.xml listing the Learn index
// plus every prerendered article URL (public/robots.txt, copied
// verbatim by Vite, points crawlers at it) -- CHEAP, genuinely useful
// SEO infrastructure a client-rendered SPA has no way to generate any
// other way (it needs the live article list, which only exists at
// build/request time, not as a static file).
//
// KNOWN LIMITATION: this dist/articles/<id>/index.html file is only
// reachable at the exact URL /articles/<id>/ (or /articles/<id>/
// index.html) unless something rewrites a bare /articles/<id> request
// (no trailing slash) to that key -- S3 origins behind CloudFront via
// Origin Access Control (this project's setup, not the separate "S3
// static website hosting" mode) do NOT auto-append index.html to a
// directory-shaped request the way a traditional web server would.
// template.yaml's LearnSiteDistribution (task #441) MUST pair with this
// by adding a CloudFront Function on viewer-request that rewrites a
// no-extension, no-trailing-slash path to <path>/index.html before
// falling through to the existing SPA CustomErrorResponses -- see that
// resource's own comment for the exact rewrite. Without that CloudFront
// Function, this script's whole point (a crawler seeing real HTML at
// the same URL a visitor actually navigates to) doesn't hold.
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const DIST_DIR = join(__dirname, "..", "dist");

// Same env var as the app itself reads at build time (see vite.config.ts/
// .env.example) -- Vite's own `vite build` step already needed this set
// to bake VITE_PUBLIC_API_URL into the client bundle, so it's already
// present in process.env by the time this script runs right after it in
// the same `npm run build` chain (see package.json). No .env.local
// loader here on purpose -- Vite itself only auto-loads .env.local for
// `vite build`/`vite dev`, not for a plain `tsx` script invocation, so
// local prerender testing needs the var exported into the shell first,
// e.g. `export $(grep -v '^#' .env.local | xargs) && npm run prerender`.
const API_BASE = (process.env.VITE_PUBLIC_API_URL ?? "").replace(/\/+$/, "");

// Production origin this sitemap's <loc> entries and robots.txt's
// Sitemap: line both assume -- see public/robots.txt's own hardcoded
// copy of this same value. Overridable via SITE_URL for a staging build
// pointed at a different domain (template.yaml's LearnSiteDomainName
// isn't necessarily this exact value in every deploy).
const SITE_URL = (process.env.SITE_URL ?? "https://learn.bowlerdepot.com").replace(/\/+$/, "");

const ARTICLE_PAGE_SIZE = 100;

// Same real BowlerDepot logo asset src/components/Nav.tsx already pulls
// live off bowlerdepot.com -- reused here (not re-fetched) for
// Article's publisher.logo (Task #448, Al: "add all the proper google
// structured data to the markup").
const PUBLISHER_LOGO_URL =
  "https://cdn11.bigcommerce.com/s-83dch55a9c/images/stencil/201x75/bowlerdepot_logo_website_logo_website_logo_1566417769__86141.original.png";

interface ArticleCard {
  article_id: string;
  title: string;
  hook: string;
  reviewed_at?: string | null;
  // Set once, on this article's first-ever approval, never touched again
  // (033_product_articles_first_published_at.sql) -- see ArticleDetail's
  // own comment for the full reasoning.
  first_published_at?: string | null;
  product_id: string;
  product_name: string;
  brand_name: string;
  coverstock_name?: string | null;
  coverstock_type?: string | null;
  primary_image_url?: string | null;
  // AI-generated stylized product hero shot (023 migration) -- Task
  // #452, Al: "can we use the product shot for the card in the list of
  // review articles." This interface just mirrors the real GET /articles
  // shape; the client-rendered Learn index page (ArticleCard.tsx) is
  // what actually picks it over primary_image_url.
  product_shot_image_url?: string | null;
}

interface ArticleDetail {
  title: string;
  hook: string;
  performance_summary?: string | null;
  who_should_buy?: string[] | null;
  who_should_skip?: string[] | null;
  pros?: string[] | null;
  cons?: string[] | null;
  buying_tips?: string | null;
  verdict?: string | null;
  faq?: { question: string; answer: string }[] | null;
  // Task #448, Al: "add all the proper google structured data to the
  // markup" -- re-stamped on EVERY admin approval (including a
  // re-approval after a regenerate run), so this is the honest "Updated"
  // signal -- Article's dateModified below.
  reviewed_at?: string | null;
  // Task #627/033_product_articles_first_published_at.sql, Al's later,
  // more literal follow-up: "can we add published dates and last updated
  // dates to the articles." Set once, on this article's first-ever
  // approval, and never touched by a later regenerate+re-approve --
  // Article's datePublished below. Before this field existed, datePublished
  // and dateModified both read the same reviewed_at value, which was
  // silently wrong for datePublished the moment an article was ever
  // regenerated and re-approved (see get_product_article's docstring in
  // the main repo for the full incident writeup).
  first_published_at?: string | null;
  action_shot_image_url?: string | null;
  product_shot_image_url?: string | null;
  product: {
    name: string;
    core_name?: string | null;
    core_type?: string | null;
    coverstock_name?: string | null;
    coverstock_type?: string | null;
    brand_name?: string | null;
    primary_image_url?: string | null;
    // Real BowlerDepot storefront data (014/016 price-tracking, reused
    // -- see get_product_article's own docstring), all three null
    // together whenever price_checker hasn't matched/approved/checked a
    // source for this product yet. NEVER fabricated -- renderProductLd
    // below omits the whole `offers` block rather than guess a price.
    ecommerce_url?: string | null;
    ecommerce_price?: number | null;
    ecommerce_price_currency?: string | null;
    ecommerce_in_stock?: boolean | null;
    skus: { weight_lbs: number; rg?: number | null; differential?: number | null; mass_bias?: number | null }[];
  } | null;
  // Task #444, Al: "add cross linking at the bottom to 'related' ball
  // reviews" -- rendered as real <a href> links below (renderRelatedReviews),
  // not just left for the client bundle, specifically so a crawler
  // discovers/follows the internal link graph between review pages from
  // this build-time HTML, same reasoning this whole script exists for.
  related_reviews?: { product_id: string; product_name: string; title: string }[] | null;
  // Al: "change the more from section at the bottom to be links to
  // additional articles for the brand of the ball the current article
  // is from and can we use the demand score to sort them." Now rendered
  // in this build-time HTML too (renderBrandLineup), same as
  // related_reviews above -- the old exclusion (see renderArticlePage's
  // own comment, below the CTA-skip note) no longer applies now that
  // this is internal article-to-article links instead of external,
  // price-bearing ecommerce links.
  brand_lineup?: { product_id: string; product_name: string; title: string }[] | null;
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// On-demand image resizer/optimizer (src/image_resizer in the main repo,
// fronted by CloudFront at img.bowleriq.io) -- see src/api/client.ts's
// resizedImageUrl for the browser-side twin of this; duplicated here
// (not imported) rather than shared, same reason this file's
// ArticleCard/ArticleDetail interfaces above are their own copies of
// src/api/types.ts rather than imports -- this script runs standalone
// via tsx outside the Vite app bundle. Every raw image field this
// script touches (action_shot_image_url, product_shot_image_url,
// primary_image_url) is a full-resolution ImageBucket S3 URL; this
// rewrites one into a resized/optimized img.bowleriq.io URL, or returns
// it unchanged if it isn't a recognizable ImageBucket URL.
function resizedImageUrl(
  rawUrl: string,
  options: { w?: number; h?: number; fit?: "cover" | "contain" | "inside"; fmt?: "webp" | "avif" | "jpeg" | "png"; q?: number },
): string {
  let key: string;
  try {
    key = new URL(rawUrl).pathname.replace(/^\/+/, "");
  } catch {
    return rawUrl;
  }
  if (!key.startsWith("product-images/") && !key.startsWith("article-images/")) {
    return rawUrl;
  }
  const url = new URL(`https://img.bowleriq.io/${key}`);
  if (options.w) url.searchParams.set("w", String(options.w));
  if (options.h) url.searchParams.set("h", String(options.h));
  if (options.fit) url.searchParams.set("fit", options.fit);
  if (options.fmt) url.searchParams.set("fmt", options.fmt);
  if (options.q) url.searchParams.set("q", String(options.q));
  return url.toString();
}

// Duplicated (not imported) from src/api/client.ts's formatArticleDate/
// estimateReadingTimeMinutes -- same "standalone script outside the Vite
// bundle" reason this file already duplicates resizedImageUrl and the
// ArticleCard/ArticleDetail interfaces above rather than importing them.
// Kept byte-for-byte in sync by hand with client.ts's copies so the
// static prerendered byline (renderArticlePage below) and the client-
// rendered one (ArticleDetailPage.tsx) always show the identical text --
// Google's byline-date guidance explicitly calls for the visible date to
// match what's in the page's own structured data, and this page has
// both a static and a client-rendered version of that visible text.
function formatArticleDate(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return new Intl.DateTimeFormat("en-US", { year: "numeric", month: "long", day: "numeric" }).format(d);
}

function estimateReadingTimeMinutes(article: ArticleDetail): number {
  const parts: string[] = [
    article.hook,
    article.performance_summary,
    article.buying_tips,
    article.verdict,
    ...(article.who_should_buy ?? []),
    ...(article.who_should_skip ?? []),
    ...(article.pros ?? []),
    ...(article.cons ?? []),
    ...(article.faq ?? []).flatMap((f) => [f.question, f.answer]),
  ].filter((s): s is string => Boolean(s));
  const wordCount = parts.join(" ").trim().split(/\s+/).filter(Boolean).length;
  return Math.max(1, Math.ceil(wordCount / 200));
}

async function fetchAllArticles(): Promise<ArticleCard[]> {
  const all: ArticleCard[] = [];
  let offset = 0;
  for (;;) {
    const url = `${API_BASE}/articles?limit=${ARTICLE_PAGE_SIZE}&offset=${offset}`;
    const resp = await fetch(url);
    if (!resp.ok) {
      throw new Error(`GET /articles failed: ${resp.status} ${resp.statusText}`);
    }
    const body = (await resp.json()) as { items: ArticleCard[] };
    all.push(...body.items);
    if (body.items.length < ARTICLE_PAGE_SIZE) break;
    offset += ARTICLE_PAGE_SIZE;
  }
  return all;
}

async function fetchArticleDetail(productId: string): Promise<ArticleDetail | null> {
  const resp = await fetch(`${API_BASE}/products/${encodeURIComponent(productId)}/article`);
  if (!resp.ok) return null;
  const body = (await resp.json()) as { article: ArticleDetail | null };
  return body.article;
}

function renderList(items: string[] | null | undefined): string {
  if (!items || items.length === 0) return "";
  return `<ul>${items.map((i) => `<li>${escapeHtml(i)}</li>`).join("")}</ul>`;
}

function renderSpecTable(product: ArticleDetail["product"]): string {
  if (!product?.skus?.length) return "";
  const rows = product.skus
    .map(
      (s) =>
        `<tr><td>${s.weight_lbs} lb</td><td>${s.rg ?? "--"}</td><td>${s.differential ?? "--"}</td><td>${s.mass_bias ?? "--"}</td></tr>`,
    )
    .join("");
  return `
    <h2>Specs</h2>
    <table class="comparison-table">
      <thead><tr><th>Weight</th><th>RG</th><th>Differential</th><th>Mass Bias</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// Real <a href="/articles/<id>/"> links to sibling review pages -- see
// this field's own interface comment on why this matters for crawlability
// specifically (not just a nice-to-have for JS-enabled visitors, who
// already get this from ArticleDetailPage.tsx's own Related Reviews
// section).
function renderRelatedReviews(related: ArticleDetail["related_reviews"]): string {
  if (!related?.length) return "";
  const cards = related
    .map(
      (r) =>
        `<a class="article-card" href="/articles/${escapeHtml(r.product_id)}/"><div class="article-card-body"><div class="article-card-title">${escapeHtml(r.title)}</div><div class="article-card-meta">${escapeHtml(r.product_name)}</div></div></a>`,
    )
    .join("");
  return `<h2>Related Reviews</h2><div class="article-grid">${cards}</div>`;
}

// Same real <a href="/articles/<id>/"> treatment as renderRelatedReviews
// above, now that brand_lineup is an internal article-to-article rail
// too (see this type's own field comment on the rework). Reuses the
// exact same article-card markup/classes -- no separate CSS needed.
function renderBrandLineup(lineup: ArticleDetail["brand_lineup"]): string {
  if (!lineup?.length) return "";
  const cards = lineup
    .map(
      (b) =>
        `<a class="article-card" href="/articles/${escapeHtml(b.product_id)}/"><div class="article-card-body"><div class="article-card-title">${escapeHtml(b.title)}</div><div class="article-card-meta">${escapeHtml(b.product_name)}</div></div></a>`,
    )
    .join("");
  return `<h2>More from This Brand</h2><div class="article-grid">${cards}</div>`;
}

function renderFaq(faq: ArticleDetail["faq"]): string {
  if (!faq?.length) return "";
  return `
    <h2>FAQ</h2>
    ${faq
      .map(
        (f) =>
          `<div class="faq-item"><p class="faq-question">${escapeHtml(f.question)}</p><p class="faq-answer">${escapeHtml(f.answer)}</p></div>`,
      )
      .join("")}`;
}

// Task #448, Al: "add all the proper google structured data to the
// markup." Four separate JSON-LD blocks, one per schema.org type --
// Google explicitly supports multiple structured-data blocks on one
// page, and keeping each type in its own <script> tag (rather than one
// @graph) keeps each builder function below independently readable/
// testable and lets any one type be dropped without touching the others.
//
// REPLACES the earlier single ad-hoc "Review" block this function used
// to emit: that block's top-level @type was Review, and Google's Review
// snippet documentation requires `reviewRating`/`reviewRating.ratingValue`
// on every Review (see developers.google.com/search/docs/appearance/
// structured-data/review-snippet's own "Structured data type
// definitions" table) -- a genuine numeric star rating this project's
// data model has never captured (product_articles has no rating column;
// these are AI-generated prose reviews, not user star ratings). Emitting
// reviewRating with a made-up number would violate Google's own
// guidelines ("Don't include fake or undisclosed incentivized reviews");
// leaving it out made the old block non-compliant/ineligible for the
// Review rich result it was nominally claiming. The fix isn't a bigger
// Review block -- it's using the schema.org types this content actually
// qualifies for honestly, below.

// Article/BlogPosting -- headline/image/dates/author/publisher. No
// required properties per Google's own Article guide (unlike Review),
// so this is straightforwardly compliant with real data alone.
function buildArticleLd(card: ArticleCard, article: ArticleDetail, heroImage: string | null | undefined, canonicalUrl: string) {
  // Task #627/033_product_articles_first_published_at.sql -- these two
  // USED to both read the same reviewed_at value (see ArticleDetail's
  // own comment on that field), which was wrong for datePublished on any
  // article that had ever been regenerated and re-approved. first_
  // published_at is immutable after an article's first approval;
  // reviewed_at is re-stamped every approval. Falls back to reviewed_at
  // for datePublished only on the rare pre-migration row where first_
  // published_at is somehow still null.
  const datePublished = article.first_published_at || article.reviewed_at || undefined;
  const dateModified = article.reviewed_at || undefined;
  return {
    "@context": "https://schema.org",
    "@type": "Article",
    headline: article.title,
    image: heroImage ? [heroImage] : undefined,
    datePublished,
    dateModified,
    mainEntityOfPage: { "@type": "WebPage", "@id": canonicalUrl },
    author: { "@type": "Organization", name: "The Bowler Depot", url: SITE_URL },
    publisher: {
      "@type": "Organization",
      name: "The Bowler Depot",
      logo: { "@type": "ImageObject", url: PUBLISHER_LOGO_URL },
    },
  };
}

// Product -- name/image/brand plus whichever of `review`/`offers` this
// article actually has real data for (Google's Product snippet
// documentation requires at least one of review/aggregateRating/offers;
// aggregateRating is skipped entirely -- no genuine aggregate exists).
// `review` here uses ONLY positiveNotes/negativeNotes (the pros/cons
// summary sub-feature), which Google's own docs describe as needing "at
// least two statements in any combination," NOT a reviewRating -- this
// is a real, honest way to carry this project's actual pros/cons content
// into Product structured data without a star rating. `offers` is
// included only when price_checker has a real, checked price for this
// product (article.product.ecommerce_price non-null) -- never
// fabricated; most of the catalog will have no `offers` block yet, same
// "for some balls" reality ArticleDetailPage.tsx's own ecommerce-link
// fallback already accounts for.
function buildProductLd(card: ArticleCard, article: ArticleDetail, heroImage: string | null | undefined) {
  const product = article.product;
  const name = product?.name ?? card.product_name;
  // heroImage arrives here already resized (see renderArticlePage) --
  // the two fallback sources don't, so resize them the same way rather
  // than mixing a full-resolution S3 URL into the same `image` array.
  const images = [
    ...new Set(
      [
        heroImage,
        product?.primary_image_url ? resizedImageUrl(product.primary_image_url, { w: 1200, h: 630, fit: "cover", fmt: "jpeg", q: 85 }) : null,
        card.primary_image_url ? resizedImageUrl(card.primary_image_url, { w: 1200, h: 630, fit: "cover", fmt: "jpeg", q: 85 }) : null,
      ].filter(Boolean),
    ),
  ] as string[];

  const positiveNotes = article.pros?.length
    ? { "@type": "ItemList", itemListElement: article.pros.map((p, i) => ({ "@type": "ListItem", position: i + 1, name: p })) }
    : undefined;
  const negativeNotes = article.cons?.length
    ? { "@type": "ItemList", itemListElement: article.cons.map((c, i) => ({ "@type": "ListItem", position: i + 1, name: c })) }
    : undefined;
  const noteCount = (article.pros?.length ?? 0) + (article.cons?.length ?? 0);
  const review =
    noteCount >= 2
      ? { "@type": "Review", author: { "@type": "Organization", name: "The Bowler Depot" }, positiveNotes, negativeNotes }
      : undefined;

  const offers =
    product?.ecommerce_price != null
      ? {
          "@type": "Offer",
          url: product.ecommerce_url || undefined,
          price: product.ecommerce_price,
          priceCurrency: product.ecommerce_price_currency || "USD",
          availability:
            product.ecommerce_in_stock == null
              ? undefined
              : product.ecommerce_in_stock
                ? "https://schema.org/InStock"
                : "https://schema.org/OutOfStock",
        }
      : undefined;

  if (!review && !offers) return undefined; // nothing genuine to satisfy Product's own eligibility requirement

  return {
    "@context": "https://schema.org",
    "@type": "Product",
    name,
    image: images.length ? images : undefined,
    brand: product?.brand_name ? { "@type": "Brand", name: product.brand_name } : undefined,
    review,
    offers,
  };
}

// BreadcrumbList -- Home > this article. Two ListItems is the minimum
// Google's breadcrumb documentation requires; real content either way
// (the Learn index really is this page's parent in the site's own nav).
function buildBreadcrumbLd(article: ArticleDetail, canonicalUrl: string) {
  return {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    itemListElement: [
      { "@type": "ListItem", position: 1, name: "Learn", item: `${SITE_URL}/` },
      { "@type": "ListItem", position: 2, name: article.title, item: canonicalUrl },
    ],
  };
}

// FAQPage -- straight from article.faq, real content already rendered
// visibly on the page by renderFaq above (Google's own general
// guideline: marked-up content must be visible to users, which this is).
// NOTE: Google deprecated the FAQ rich-result feature in 2026 (FAQPage
// no longer produces a visible Search result, and Rich Results Test/
// Search Console dropped FAQ-specific support) -- this markup is kept as
// still-valid, harmless schema.org data (other consumers, e.g. AI
// answer engines, may still read it), not as a live Google rich-result
// bet. Cheap to leave in; nothing here regresses if Google never
// reinstates it.
function buildFaqLd(article: ArticleDetail) {
  if (!article.faq?.length) return undefined;
  return {
    "@context": "https://schema.org",
    "@type": "FAQPage",
    mainEntity: article.faq.map((f) => ({
      "@type": "Question",
      name: f.question,
      acceptedAnswer: { "@type": "Answer", text: f.answer },
    })),
  };
}

function renderStructuredData(card: ArticleCard, article: ArticleDetail, heroImage: string | null | undefined, canonicalUrl: string): string {
  const blocks = [
    buildArticleLd(card, article, heroImage, canonicalUrl),
    buildProductLd(card, article, heroImage),
    buildBreadcrumbLd(article, canonicalUrl),
    buildFaqLd(article),
  ].filter(Boolean);
  return blocks.map((b) => `<script type="application/ld+json">${JSON.stringify(b)}</script>`).join("\n    ");
}

// Note: the post-verdict "shop this ball" CTA (Al: "after the verdict in
// each article can we include the call to action to shop for the ball
// ... This should only show while the ball is current") is deliberately
// NOT rendered into this function's static HTML: it's an external
// BowlerDepot storefront link, not crawl-relevant content, and
// product.status can flip current->retired between builds, so a stale
// prerendered CTA could outlive the point it should've stopped showing.
// ArticleDetailPage.tsx renders it client-side instead, gated live on
// product.status. (The Learn site's OTHER cross-link rails -- Related
// Reviews and brand_lineup, both below -- link internally to other
// Learn articles instead of the storefront, so neither has this
// go-stale-between-builds problem; both ARE prerendered.)
//
// brand_lineup used to get the same treatment (it was an external
// ecommerce-links-and-prices rail, subject to a sibling ball's status/
// price changing between builds). Al reworked it into an internal
// article-to-article rail sorted by demand_score ("change the more from
// section ... to be links to additional articles for the brand"), which
// is the same nature as related_reviews above -- so it's now rendered
// here too, via renderBrandLineup, for the same crawlability reasoning.
function renderArticlePage(baseHtml: string, card: ArticleCard, article: ArticleDetail): string {
  const metaDescription = escapeHtml((article.hook || card.hook || "").slice(0, 300));
  const rawHeroImage = article.action_shot_image_url || article.product?.primary_image_url || card.primary_image_url;
  // 1200x630 -- the standard Open Graph/Twitter Card social-preview
  // dimensions, reused as-is for the no-JS body <img>, og:image, and the
  // Article/Product JSON-LD image fields below (one resized URL, shared
  // everywhere this page needs an image). fmt=jpeg rather than the
  // resizer's webp default -- some link-unfurlers (Slack, older
  // LinkedIn) still don't reliably render webp og:image previews, and
  // jpeg is the safest universally-supported choice for this specific
  // use.
  const heroImage = rawHeroImage ? resizedImageUrl(rawHeroImage, { w: 1200, h: 630, fit: "cover", fmt: "jpeg", q: 85 }) : null;
  const pageTitle = `${article.title} | Learn | The Bowler Depot`;
  const canonicalUrl = `${SITE_URL}/articles/${card.product_id}/`;

  // Al: "add published dates and last updated dates to the articles" --
  // text/order here intentionally matches ArticleDetailPage.tsx's own
  // byline (see that component's comment) so the visible copy a crawler
  // or no-JS visitor sees here is identical to what a JS-enabled visitor
  // sees once the client app takes over, and both match the datePublished/
  // dateModified values in buildArticleLd above (Google's byline-date
  // guidance: keep visible and structured dates consistent).
  const publishedLabel = formatArticleDate(article.first_published_at || article.reviewed_at);
  const updatedLabel = formatArticleDate(article.reviewed_at);
  const readingTime = estimateReadingTimeMinutes(article);
  const bylineParts = ["By BowlerDepot Team", `${readingTime} min read`];
  if (publishedLabel) bylineParts.push(`Published ${publishedLabel}`);
  if (updatedLabel && updatedLabel !== publishedLabel) bylineParts.push(`Updated ${updatedLabel}`);
  const byline = bylineParts.map(escapeHtml).join(" &middot; ");

  const content = `
    <div class="page">
      <a class="back-link" href="/">&larr; All reviews</a>
      <div class="article-detail-hero">
        <div class="article-detail-hero-inner">
          <div class="article-detail-image-frame">
            <div class="article-detail-image">
              ${heroImage ? `<img src="${escapeHtml(heroImage)}" alt="${escapeHtml(article.product?.name ?? card.product_name)}" />` : ""}
            </div>
          </div>
          <div>
            <h1>${escapeHtml(article.title)}</h1>
            <p class="article-detail-hook">${escapeHtml(article.hook)}</p>
            <p class="article-detail-byline">${byline}</p>
          </div>
        </div>
      </div>
      ${article.performance_summary ? `<h2>Performance</h2><p>${escapeHtml(article.performance_summary)}</p>` : ""}
      ${article.pros?.length ? `<h2>Pros</h2>${renderList(article.pros)}` : ""}
      ${article.cons?.length ? `<h2>Cons</h2>${renderList(article.cons)}` : ""}
      ${article.who_should_buy?.length ? `<h2>Who Should Buy This</h2>${renderList(article.who_should_buy)}` : ""}
      ${article.who_should_skip?.length ? `<h2>Who Should Skip This</h2>${renderList(article.who_should_skip)}` : ""}
      ${article.buying_tips ? `<h2>Buying Tips</h2><p>${escapeHtml(article.buying_tips)}</p>` : ""}
      ${article.verdict ? `<h2>Verdict</h2><p>${escapeHtml(article.verdict)}</p>` : ""}
      ${/* Al: "move the related reviews section up to just below the shop
           call to action." The live page now renders Related Reviews
           right after the Shop CTA, ahead of Specs/FAQ -- this static
           HTML has no CTA to anchor to (see the exclusion note above),
           but moved Related Reviews ahead of Specs/FAQ here too so the
           prerendered content order keeps mirroring the live page's
           order as closely as this file's own stated goal calls for. */ ""}
      ${renderRelatedReviews(article.related_reviews)}
      ${renderSpecTable(article.product)}
      ${renderFaq(article.faq)}
      ${renderBrandLineup(article.brand_lineup)}
    </div>`;

  let html = baseHtml;
  html = html.replace(/<title>.*?<\/title>/, `<title>${escapeHtml(pageTitle)}</title>`);
  const headExtras = `
    <meta name="description" content="${metaDescription}" />
    <link rel="canonical" href="${canonicalUrl}" />
    <meta property="og:type" content="article" />
    <meta property="og:title" content="${escapeHtml(article.title)}" />
    <meta property="og:description" content="${metaDescription}" />
    <meta property="og:url" content="${canonicalUrl}" />
    ${heroImage ? `<meta property="og:image" content="${escapeHtml(heroImage)}" />` : ""}
    ${renderStructuredData(card, article, heroImage, canonicalUrl)}
  </head>`;
  html = html.replace("</head>", headExtras);
  html = html.replace('<div id="root"></div>', `<div id="root">${content}</div>`);
  return html;
}

async function main() {
  if (!API_BASE) {
    throw new Error("VITE_PUBLIC_API_URL is not set -- prerender needs it to fetch articles at build time.");
  }

  const baseHtml = await readFile(join(DIST_DIR, "index.html"), "utf-8");
  const cards = await fetchAllArticles();

  const sitemapUrls: { loc: string; lastmod?: string }[] = [{ loc: `${SITE_URL}/` }];
  let written = 0;

  for (const card of cards) {
    const article = await fetchArticleDetail(card.product_id);
    if (!article) {
      // Shouldn't happen (list_articles already filters to approved
      // articles on published products), but a public_api hiccup or a
      // product unpublished between the two calls shouldn't fail the
      // whole build -- skip just this one page.
      // eslint-disable-next-line no-console
      console.warn(`Skipping prerender for ${card.product_id}: no article returned`);
      continue;
    }
    const outDir = join(DIST_DIR, "articles", card.product_id);
    await mkdir(outDir, { recursive: true });
    await writeFile(join(outDir, "index.html"), renderArticlePage(baseHtml, card, article), "utf-8");
    sitemapUrls.push({ loc: `${SITE_URL}/articles/${card.product_id}/`, lastmod: card.reviewed_at ?? undefined });
    written += 1;
  }

  const sitemapXml = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${sitemapUrls
  .map(
    (u) =>
      `  <url><loc>${escapeHtml(u.loc)}</loc>${u.lastmod ? `<lastmod>${u.lastmod.slice(0, 10)}</lastmod>` : ""}</url>`,
  )
  .join("\n")}
</urlset>
`;
  await writeFile(join(DIST_DIR, "sitemap.xml"), sitemapXml, "utf-8");

  // eslint-disable-next-line no-console
  console.log(`Prerendered ${written}/${cards.length} article page(s) + sitemap.xml.`);
}

main().catch((err) => {
  // eslint-disable-next-line no-console
  console.error(err);
  process.exit(1);
});
