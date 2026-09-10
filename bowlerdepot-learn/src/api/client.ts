import type { ArticleCard, ArticleDetail, Category, ProductArticleResponse } from "./types";

// Same unauthenticated-PublicApiFunction posture as consumer-site/src/
// api/client.ts (see its own comments for the full "why no auth"
// reasoning, unchanged here) -- trailing slash stripped for the same
// double-slash reason documented there.
const API_BASE = (import.meta.env.VITE_PUBLIC_API_URL ?? "").replace(/\/+$/, "");

if (!API_BASE) {
  // eslint-disable-next-line no-console
  console.error(
    "VITE_PUBLIC_API_URL is not set -- copy .env.example to .env.local and fill in the deployed PublicApiUrl.",
  );
}

class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export { ApiError };

async function apiGet<T>(path: string, params: Record<string, string | number | undefined> = {}): Promise<T> {
  const url = new URL(`${API_BASE}${path}`);
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }
  const resp = await fetch(url.toString());
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = body.detail ?? detail;
    } catch {
      // body wasn't JSON -- keep statusText
    }
    throw new ApiError(resp.status, detail);
  }
  return resp.json() as Promise<T>;
}

export interface ListArticlesParams {
  brand_id?: string;
  coverstock_id?: string;
  // Migration 031 -- id of a row from getCategories() below.
  category_id?: string;
  search?: string;
  // 'newest' | 'oldest' | 'title_asc' | 'title_desc' -- see public_api/
  // service.py's _ARTICLE_SORT_ORDER_BY docstring in the main repo.
  sort?: string;
  limit?: number;
  offset?: number;
}

export function listArticles(params: ListArticlesParams = {}): Promise<ArticleCard[]> {
  return apiGet<{ items: ArticleCard[] }>("/articles", {
    brand_id: params.brand_id,
    coverstock_id: params.coverstock_id,
    category_id: params.category_id,
    search: params.search,
    sort: params.sort,
    limit: params.limit,
    offset: params.offset,
  }).then((r) => r.items);
}

export function getBrands(): Promise<{ id: string; name: string }[]> {
  return apiGet<{ items: { id: string; name: string }[] }>("/brands").then((r) => r.items);
}

// Learn-site content taxonomy (migration 031) -- see types.ts's Category
// comment. Small/static, fetched once and cached in module scope: every
// page on the site wants the same list (nav label, eyebrow text), and it
// changes only when an admin adds a new category/article_type, not per
// request.
let _categoriesCache: Promise<Category[]> | null = null;

export function getCategories(): Promise<Category[]> {
  if (!_categoriesCache) {
    _categoriesCache = apiGet<{ items: Category[] }>("/categories").then((r) => r.items);
  }
  return _categoriesCache;
}

export function getProductArticle(productId: string): Promise<ProductArticleResponse> {
  return apiGet<ProductArticleResponse>(`/products/${encodeURIComponent(productId)}/article`);
}

// BowlerDepot's storefront doesn't expose a stable per-product URL from
// this project's data (bowlerdepot_products only stores the BigCommerce
// numeric product id/SKU -- see 001_init_schema.sql -- not a resolvable
// slug/permalink, and BigCommerce's Stencil storefronts don't offer a
// generic "view by id" route). Confirmed live (2026-09-05):
// bowlerdepot.com's own Stencil search (search.php?search_query=...)
// reliably lands a visitor on the matching product's real search result,
// one click from the actual product page -- the same UX a manual search
// box would give, just pre-filled. Good enough for a "Shop this ball"
// link without inventing a URL scheme this project can't actually
// verify resolves.
export function bowlerDepotSearchUrl(productName: string): string {
  return `https://bowlerdepot.com/search.php?search_query=${encodeURIComponent(productName)}`;
}

// On-demand image resizer/optimizer (src/image_resizer in the main repo,
// fronted by CloudFront at img.bowleriq.io) -- Al: "with the learn site
// nearing a release i think it is time to optimize the images." Every
// image field this API returns (primary_image_url, product_shot_image_url,
// action_shot_image_url, etc.) is a raw, full-resolution ImageBucket S3
// URL of the form https://<bucket>.s3.amazonaws.com/product-images/...
// or .../article-images/... -- exactly the two prefixes the resizer
// accepts. This rewrites one of those raw URLs into a resized/optimized
// one; returns the input UNCHANGED (not null) for anything that isn't a
// recognizable ImageBucket URL, so a caller can always fall back to
// rendering the original rather than losing the image entirely.
const IMAGE_RESIZER_ORIGIN = "https://img.bowleriq.io";

export interface ResizeOptions {
  w?: number;
  h?: number;
  fit?: "cover" | "contain" | "inside";
  fmt?: "webp" | "avif" | "jpeg" | "png";
  q?: number;
}

export function resizedImageUrl(rawUrl: string, options: ResizeOptions): string {
  let key: string;
  try {
    key = new URL(rawUrl).pathname.replace(/^\/+/, "");
  } catch {
    return rawUrl;
  }
  if (!key.startsWith("product-images/") && !key.startsWith("article-images/")) {
    return rawUrl;
  }
  const url = new URL(`${IMAGE_RESIZER_ORIGIN}/${key}`);
  if (options.w) url.searchParams.set("w", String(options.w));
  if (options.h) url.searchParams.set("h", String(options.h));
  if (options.fit) url.searchParams.set("fit", options.fit);
  if (options.fmt) url.searchParams.set("fmt", options.fmt);
  if (options.q) url.searchParams.set("q", String(options.q));
  return url.toString();
}

// Al: "can we add published dates and last updated dates to the
// articles." Google's own byline-date guidance (Search Central,
// "Influence your byline dates") is specific: show a labeled,
// human-readable date near the byline ("Published Feb 4, 2019" /
// "Updated Feb 14, 2019") and keep it consistent with whatever's in
// the page's structured data (see prerender.ts's Article JSON-LD,
// which reads the SAME first_published_at/reviewed_at fields this
// formats). No time-of-day shown -- Google's guidance treats the date
// itself as the useful part; a bare date also reads more naturally as
// visible page copy than a full timestamp would.
export function formatArticleDate(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return new Intl.DateTimeFormat("en-US", { year: "numeric", month: "long", day: "numeric" }).format(d);
}

// Plain-text word count across every prose field an article actually
// renders (see ArticleDetailPage.tsx/prerender.ts's own section-by-
// section rendering) -- deliberately NOT product.description or any
// other non-article copy, since "reading time" should describe the
// review itself. 200 wpm is the commonly-cited average adult silent
// reading speed most sites (Medium, WordPress reading-time plugins,
// etc.) already build their own estimates on; rounded up and floored
// at 1 minute so a very short article never reads as "0 min read."
export function estimateReadingTimeMinutes(article: ArticleDetail): number {
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
