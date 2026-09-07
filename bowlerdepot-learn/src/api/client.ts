import type { ArticleCard, Category, ProductArticleResponse } from "./types";

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
