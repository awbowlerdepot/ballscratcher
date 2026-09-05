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
  // markup" -- the real admin-approval timestamp, used for Article's
  // datePublished/dateModified below (see get_product_article's own
  // docstring in the main repo on why this, not generated_at).
  reviewed_at?: string | null;
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
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
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
  const publishedAt = article.reviewed_at || undefined;
  return {
    "@context": "https://schema.org",
    "@type": "Article",
    headline: article.title,
    image: heroImage ? [heroImage] : undefined,
    datePublished: publishedAt,
    dateModified: publishedAt,
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
  const images = [...new Set([heroImage, product?.primary_image_url, card.primary_image_url].filter(Boolean))] as string[];

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

function renderArticlePage(baseHtml: string, card: ArticleCard, article: ArticleDetail): string {
  const metaDescription = escapeHtml((article.hook || card.hook || "").slice(0, 300));
  const heroImage = article.action_shot_image_url || article.product?.primary_image_url || card.primary_image_url;
  const pageTitle = `${article.title} | Learn | The Bowler Depot`;
  const canonicalUrl = `${SITE_URL}/articles/${card.product_id}/`;

  const content = `
    <div class="page">
      <a class="back-link" href="/">&larr; All reviews</a>
      <div class="article-detail-hero">
        <div class="article-detail-image">
          ${heroImage ? `<img src="${escapeHtml(heroImage)}" alt="${escapeHtml(article.product?.name ?? card.product_name)}" />` : ""}
        </div>
        <div>
          <h1>${escapeHtml(article.title)}</h1>
          <p class="article-detail-hook">${escapeHtml(article.hook)}</p>
        </div>
      </div>
      ${article.performance_summary ? `<h2>Performance</h2><p>${escapeHtml(article.performance_summary)}</p>` : ""}
      ${article.pros?.length ? `<h2>Pros</h2>${renderList(article.pros)}` : ""}
      ${article.cons?.length ? `<h2>Cons</h2>${renderList(article.cons)}` : ""}
      ${article.who_should_buy?.length ? `<h2>Who Should Buy This</h2>${renderList(article.who_should_buy)}` : ""}
      ${article.who_should_skip?.length ? `<h2>Who Should Skip This</h2>${renderList(article.who_should_skip)}` : ""}
      ${article.buying_tips ? `<h2>Buying Tips</h2><p>${escapeHtml(article.buying_tips)}</p>` : ""}
      ${article.verdict ? `<h2>Verdict</h2><p>${escapeHtml(article.verdict)}</p>` : ""}
      ${renderSpecTable(article.product)}
      ${renderFaq(article.faq)}
      ${renderRelatedReviews(article.related_reviews)}
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
