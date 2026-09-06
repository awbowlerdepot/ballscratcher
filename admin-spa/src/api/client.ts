import { getValidIdToken } from "../auth/cognito";
import type {
  AdminUser,
  Article,
  ArticleListItem,
  ArticleImageCandidate,
  ArticleRegenerateMode,
  ApproveReviewResult,
  BlockedChannel,
  Brand,
  CatalogDailyMovementHistoryResult,
  Core,
  CoreDetail,
  Coverstock,
  CoverstockDetail,
  CheckPriceResult,
  CreateUserInput,
  CreateUserResult,
  DashboardSummary,
  DeleteProductPriceSourceResult,
  DiscoverPriceSourcesResult,
  DiscoverVideosResult,
  ImageReorderResult,
  ImageUpdateInput,
  ImageUpdateResult,
  ListArticlesParams,
  ListCoresParams,
  ListCoverstocksParams,
  ListPriceSourcesParams,
  ListProductsParams,
  ListReviewQueueParams,
  ListVideoCandidatesParams,
  ManualSeedUrl,
  ManualSeedUrlCreateInput,
  PriceHistoryResult,
  PriceSite,
  PriceSiteCreateInput,
  PriceSiteUpdateInput,
  PriceSourceListResult,
  Product,
  ProductDetail,
  ProductPriceSource,
  ProductPriceSourceCreateInput,
  ProductPriceSourceCreateResult,
  QueueArticleGenerationResult,
  QueueArticleSyncResult,
  ReassignVideoResult,
  RefreshRollupResult,
  RejectReviewResult,
  RescrapeResult,
  ReviewQueueListResult,
  SelectImageCandidateResult,
  SetPublishedResult,
  SkuStockHistoryResult,
  VideoCandidateListResult,
} from "./types";

// Unlike consumer-site's PublicApiFunction client, every request here
// needs an Authorization header -- AdminHttpApi is gated by
// admin_api_authorizer's dual-mode check (Cognito ID token here; the 17
// existing automation scripts still use the shared-secret path, see
// src/admin_api_authorizer/app.py). Stripped of a trailing slash for
// the same double-slash reason as consumer-site/src/api/client.ts.
const API_BASE = (import.meta.env.VITE_ADMIN_API_URL ?? "").replace(/\/+$/, "");

if (!API_BASE) {
  // eslint-disable-next-line no-console
  console.error(
    "VITE_ADMIN_API_URL is not set -- copy .env.example to .env.local and fill in the deployed admin API URL.",
  );
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function authHeaders(): Promise<HeadersInit> {
  const token = await getValidIdToken();
  if (!token) {
    // 401 here (rather than letting fetch run without a header) means
    // callers can rely on ApiError.status === 401 to mean "not signed
    // in" uniformly, whether the failure was local (no session) or
    // server-side (session rejected).
    throw new ApiError(401, "Not signed in");
  }
  return { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
}

async function handleResponse<T>(resp: Response): Promise<T> {
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

async function apiGet<T>(path: string, params: Record<string, string | number | boolean | undefined> = {}): Promise<T> {
  const url = new URL(`${API_BASE}${path}`);
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }
  const headers = await authHeaders();
  const resp = await fetch(url.toString(), { headers });
  return handleResponse<T>(resp);
}

async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  const headers = await authHeaders();
  const resp = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  return handleResponse<T>(resp);
}

// Only PATCH /articles/{id}/bigcommerce-sync uses this method so far
// (see set_article_bigcommerce_sync in admin_api/service.py) -- a
// single-boolean toggle, not a review-workflow action, hence PATCH
// rather than a POST .../approve-shaped route.
async function apiPatch<T>(path: string, body: unknown): Promise<T> {
  const headers = await authHeaders();
  const resp = await fetch(`${API_BASE}${path}`, {
    method: "PATCH",
    headers,
    body: JSON.stringify(body),
  });
  return handleResponse<T>(resp);
}

// Only DELETE /price-sites/{id} uses this method so far -- a real hard
// delete (cascades to every product_price_sources/product_price_history
// row pointed at that site, see delete_price_site's own docstring), not
// used anywhere else in this client.
async function apiDelete<T>(path: string): Promise<T> {
  const headers = await authHeaders();
  const resp = await fetch(`${API_BASE}${path}`, { method: "DELETE", headers });
  return handleResponse<T>(resp);
}

export function getDashboardSummary(): Promise<DashboardSummary> {
  return apiGet<DashboardSummary>("/admin/dashboard");
}

// Full history, no query params -- same "fetch once, filter client-side
// per range button" shape as getSkuStockHistory/getPriceHistory below.
export function getCatalogDailyMovementHistory(): Promise<CatalogDailyMovementHistoryResult> {
  return apiGet<CatalogDailyMovementHistoryResult>("/admin/catalog-daily-movement-history");
}

export function listProducts(params: ListProductsParams = {}): Promise<Product[]> {
  return apiGet<{ items: Product[] }>("/products", { ...params }).then((r) => r.items);
}

export function rescrapeProduct(id: string): Promise<RescrapeResult> {
  return apiPost<RescrapeResult>(`/products/${encodeURIComponent(id)}/rescrape`);
}

// Product detail page (the sub-tabs view) -- get_product's `select p.*`
// plus skus/images/discovered_url/bowlerdepot_matches/bowwwl_matches, see
// ProductDetail's own comment in types.ts.
export function getProduct(id: string): Promise<ProductDetail> {
  return apiGet<ProductDetail>(`/products/${encodeURIComponent(id)}`);
}

export function setProductPublished(id: string, published: boolean): Promise<SetPublishedResult> {
  return apiPatch(`/products/${encodeURIComponent(id)}/published`, { published });
}

// "Search for videos again" on the product detail Videos sub-tab -- same
// soft-fail queued/reason convention as rescrapeProduct, see
// service.queue_video_discovery's docstring.
export function discoverVideosForProduct(id: string): Promise<DiscoverVideosResult> {
  return apiPost<DiscoverVideosResult>(`/products/${encodeURIComponent(id)}/discover-videos`);
}

// Per-image visibility/thumbnail toggles (migration 010) -- product
// detail Overview sub-tab's image cards.
export function updateProductImage(
  productId: string,
  imageId: string,
  input: ImageUpdateInput,
): Promise<ImageUpdateResult> {
  return apiPatch(`/products/${encodeURIComponent(productId)}/images/${encodeURIComponent(imageId)}`, input);
}

// Whole-list reorder (move up/down resubmits the full resulting id list)
// -- see service.reorder_product_images' docstring for why.
export function reorderProductImages(productId: string, imageIds: string[]): Promise<ImageReorderResult> {
  return apiPost<ImageReorderResult>(`/products/${encodeURIComponent(productId)}/images/reorder`, {
    image_ids: imageIds,
  });
}

// This product's own "site setup" for price tracking -- status="all"
// default (pending/approved/rejected together) mirrors the product
// detail view's own default, see list_product_price_sources' docstring.
export function listProductPriceSources(productId: string, status = "all"): Promise<ProductPriceSource[]> {
  return apiGet<{ items: ProductPriceSource[] }>(`/products/${encodeURIComponent(productId)}/price-sources`, {
    status,
  }).then((r) => r.items);
}

// days widened to 3650 by admin-site's own product detail panel (see that
// file's loadProductDetailInto comment) so a chart's range picker can
// filter client-side without a re-fetch per click -- callers here default
// the same way.
export function getPriceHistory(productId: string, days = 3650): Promise<PriceHistoryResult> {
  return apiGet<PriceHistoryResult>(`/products/${encodeURIComponent(productId)}/price-history`, { days });
}

export function getSkuStockHistory(productId: string, days = 3650): Promise<SkuStockHistoryResult> {
  return apiGet<SkuStockHistoryResult>(`/products/${encodeURIComponent(productId)}/sku-stock-history`, { days });
}

// Product detail Pricing sub-tab's own action buttons -- "Find price
// sources"/"Check price now" (mirroring the Videos sub-tab's "search
// again" trigger), the manual-add form, and per-row delete. All three
// hit already-deployed admin_api routes (admin-site's own product-detail
// price tracking section has used them for a while, see
// buildPriceTrackingSection) -- these were simply missing from
// admin-spa's client until ProductDetailPage needed them too.
export function discoverPriceSourcesForProduct(productId: string): Promise<DiscoverPriceSourcesResult> {
  return apiPost<DiscoverPriceSourcesResult>(`/products/${encodeURIComponent(productId)}/discover-price-sources`);
}

export function checkPriceForProduct(productId: string): Promise<CheckPriceResult> {
  return apiPost<CheckPriceResult>(`/products/${encodeURIComponent(productId)}/check-price`);
}

// Manual-override path -- see create_product_price_source's own
// docstring. Immediately approved/source='manual', not a candidate to
// review.
export function createProductPriceSource(
  productId: string,
  input: ProductPriceSourceCreateInput,
): Promise<ProductPriceSourceCreateResult> {
  return apiPost<ProductPriceSourceCreateResult>(`/products/${encodeURIComponent(productId)}/price-sources`, input);
}

// Hard delete -- cascades to that source's own product_price_history
// rows (migration 014's on-delete-cascade), see
// delete_product_price_source's own docstring. Named *Product*PriceSource
// to distinguish from deletePriceSite (the price_sites registry, a
// different resource entirely).
export function deleteProductPriceSource(sourceId: string): Promise<DeleteProductPriceSourceResult> {
  return apiDelete(`/price-sources/${encodeURIComponent(sourceId)}`);
}

export function refreshVideoSummary(id: string): Promise<RefreshRollupResult> {
  return apiPost<RefreshRollupResult>(`/products/${encodeURIComponent(id)}/refresh-video-summary`);
}

// Backs the Manual Seed URLs brand picker on the Batch Jobs page --
// same GET /brands admin-site's own brand-picker dropdowns use.
export function listBrands(): Promise<Brand[]> {
  return apiGet<{ items: Brand[] }>("/brands").then((r) => r.items);
}

export function listReviewQueue(params: ListReviewQueueParams = {}): Promise<ReviewQueueListResult> {
  return apiGet<ReviewQueueListResult>("/review-queue", { ...params });
}

// resolved_by is intentionally omitted -- admin_api's ApproveRequest/
// RejectRequest both make it Optional and fall back to the
// authenticated caller's identity (see get_caller in admin_api/app.py)
// when the client doesn't supply one. There's no field for editing
// proposed_value before approving -- the backend always applies the
// row's stored value as-is (see service.py's build_update_plan).
export function approveReviewItem(id: string): Promise<ApproveReviewResult> {
  return apiPost<ApproveReviewResult>(`/review-queue/${encodeURIComponent(id)}/approve`, {});
}

export function rejectReviewItem(id: string, reason?: string): Promise<RejectReviewResult> {
  return apiPost<RejectReviewResult>(`/review-queue/${encodeURIComponent(id)}/reject`, { reason });
}

export function listVideoCandidates(params: ListVideoCandidatesParams = {}): Promise<VideoCandidateListResult> {
  return apiGet<VideoCandidateListResult>("/video-candidates", { ...params });
}

export function approveVideoCandidate(id: string): Promise<{ video_id: string; status: "approved" }> {
  return apiPost(`/video-candidates/${encodeURIComponent(id)}/approve`, {});
}

export function rejectVideoCandidate(id: string, reason?: string): Promise<{ video_id: string; status: "rejected" }> {
  return apiPost(`/video-candidates/${encodeURIComponent(id)}/reject`, { reason });
}

// No resolved_by/reason param at all -- restore just clears back to
// pending (see POST /video-candidates/{id}/restore in admin_api/app.py).
export function restoreVideoCandidate(id: string): Promise<{ video_id: string; status: "pending" }> {
  return apiPost(`/video-candidates/${encodeURIComponent(id)}/restore`);
}

// Works from any status -- tombstones the origin row as rejected and
// copies/merges onto the target product (see reassign_video_candidate
// in admin_api/service.py).
export function reassignVideoCandidate(id: string, targetProductId: string): Promise<ReassignVideoResult> {
  return apiPost<ReassignVideoResult>(`/video-candidates/${encodeURIComponent(id)}/reassign`, {
    product_id: targetProductId,
  });
}

// Ball-review articles (022_product_articles.sql onward). No
// pending_count on GET /articles -- see the comment on ArticleListItem
// in types.ts.
export function listArticles(params: ListArticlesParams = {}): Promise<ArticleListItem[]> {
  return apiGet<{ items: ArticleListItem[] }>("/articles", { ...params }).then((r) => r.items);
}

export function getArticle(id: string): Promise<Article> {
  return apiGet<Article>(`/articles/${encodeURIComponent(id)}`);
}

// resolved_by omitted, same reasoning as approveReviewItem/
// approveVideoCandidate above. Only a pending article can be
// approved/rejected -- product_articles has no restore endpoint (an
// already-resolved article can only move by being regenerated, which
// resets it to pending -- see approve_article/reject_article's own
// docstrings).
export function approveArticle(id: string): Promise<{ article_id: string; status: "approved" }> {
  return apiPost(`/articles/${encodeURIComponent(id)}/approve`, {});
}

export function rejectArticle(id: string, reason?: string): Promise<{ article_id: string; status: "rejected" }> {
  return apiPost(`/articles/${encodeURIComponent(id)}/reject`, { reason });
}

// Freely reversible admin preference, not a review resolution -- can be
// flipped on/off on an article of any status (see
// set_article_bigcommerce_sync's own docstring).
export function setArticleBigcommerceSync(
  id: string,
  syncToBigcommerce: boolean,
): Promise<{ article_id: string; sync_to_bigcommerce: boolean }> {
  return apiPatch(`/articles/${encodeURIComponent(id)}/bigcommerce-sync`, { sync_to_bigcommerce: syncToBigcommerce });
}

// "Sync now" -- pushes immediately instead of waiting for
// BowlerdepotArticleSyncFunction's hourly schedule. queued:false with a
// reason means ARTICLE_SYNC_FUNCTION_NAME isn't configured on this
// deployment, not an error.
export function syncArticleNow(id: string): Promise<QueueArticleSyncResult> {
  return apiPost<QueueArticleSyncResult>(`/articles/${encodeURIComponent(id)}/sync-to-bigcommerce`);
}

// "Resync" -- overwrites an ALREADY-synced article's live BigCommerce
// post (update, not create). See queue_article_resync's docstring for
// why this exists separately from syncArticleNow.
export function resyncArticleNow(id: string): Promise<QueueArticleSyncResult> {
  return apiPost<QueueArticleSyncResult>(`/articles/${encodeURIComponent(id)}/resync-to-bigcommerce`);
}

// Empty list is a normal state (images never configured, or every
// candidate failed) -- see list_article_image_candidates' own
// docstring.
export function listArticleImageCandidates(articleId: string): Promise<ArticleImageCandidate[]> {
  return apiGet<{ items: ArticleImageCandidate[] }>(`/articles/${encodeURIComponent(articleId)}/image-candidates`).then(
    (r) => r.items,
  );
}

// resolved_by omitted -- accepted by the backend but not persisted
// anywhere (see select_article_image_candidate's own docstring).
export function selectArticleImageCandidate(candidateId: string): Promise<SelectImageCandidateResult> {
  return apiPost<SelectImageCandidateResult>(`/article-image-candidates/${encodeURIComponent(candidateId)}/select`, {});
}

// The original combined "Generate article" trigger -- only path for a
// brand-new article (no existing row yet). mode defaults to "both" on
// the backend.
export function generateArticle(productId: string): Promise<QueueArticleGenerationResult> {
  return apiPost<QueueArticleGenerationResult>(`/products/${encodeURIComponent(productId)}/generate-article`);
}

// Decoupled regenerate -- text-only re-runs the article-text model call
// and leaves existing images untouched; images-only skips the text call
// and generates new image candidates using the EXISTING article text as
// context. Both require an article to already exist (see
// queue_article_generation's own v7 docstring) -- mode is accepted here
// purely for call-site clarity, the actual routing lives server-side on
// which of the two endpoints gets hit.
export function regenerateArticleText(productId: string): Promise<QueueArticleGenerationResult> {
  return apiPost<QueueArticleGenerationResult>(`/products/${encodeURIComponent(productId)}/regenerate-article-text`);
}

export function regenerateArticleImages(productId: string): Promise<QueueArticleGenerationResult> {
  return apiPost<QueueArticleGenerationResult>(`/products/${encodeURIComponent(productId)}/regenerate-article-images`);
}

// Kept as a re-export purely so callers importing from client.ts don't
// also need a separate import from types.ts just for this one type.
export type { ArticleRegenerateMode };

// Price Sources -- the discovered-match review queue (product_price_
// sources). Same approve/reject/restore shape as Review Queue/Video
// Candidates/Price Sites' own registry -- see PriceSource's own comment
// in types.ts for why match_confidence is typed as a string enum.
export function listPriceSources(params: ListPriceSourcesParams = {}): Promise<PriceSourceListResult> {
  return apiGet<PriceSourceListResult>("/price-sources", { ...params });
}

// resolved_by omitted, same reasoning as approveReviewItem/
// approveVideoCandidate/approveArticle above.
export function approvePriceSource(id: string): Promise<{ source_id: string; status: "approved" }> {
  return apiPost(`/price-sources/${encodeURIComponent(id)}/approve`, {});
}

export function rejectPriceSource(id: string, reason?: string): Promise<{ source_id: string; status: "rejected" }> {
  return apiPost(`/price-sources/${encodeURIComponent(id)}/reject`, { reason });
}

// No resolved_by/reason param, same as restoreVideoCandidate -- there's
// no decision to attribute when undoing one.
export function restorePriceSource(id: string): Promise<{ source_id: string; status: "pending" }> {
  return apiPost(`/price-sources/${encodeURIComponent(id)}/restore`);
}

// Price Sites -- the retailer registry price_checker's discovery job
// searches. Read-mostly, expected to stay small and change rarely (see
// list_price_sites' own docstring) -- unlike the review-queue functions
// above, this has no bulk/pagination shape at all, matching GET
// /price-sites returning every configured site (active or not) in one
// call.
export function listPriceSites(): Promise<PriceSite[]> {
  return apiGet<{ items: PriceSite[] }>("/price-sites").then((r) => r.items);
}

export function createPriceSite(input: PriceSiteCreateInput): Promise<PriceSite> {
  return apiPost<PriceSite>("/price-sites", input);
}

export function updatePriceSite(id: string, input: PriceSiteUpdateInput): Promise<{ id: string }> {
  return apiPatch(`/price-sites/${encodeURIComponent(id)}`, input);
}

// Hard delete -- cascades to every product_price_sources row (and their
// product_price_history rows) pointed at this site. See
// delete_price_site's own docstring for why this is safe to be final
// unlike a video-candidate-style delete (nothing re-creates a
// price_sites row on its own).
export function deletePriceSite(id: string): Promise<{ id: string }> {
  return apiDelete(`/price-sites/${encodeURIComponent(id)}`);
}

// Cores -- read-only in admin_api (see Core's own comment in types.ts
// for why there's no create/update/delete here).
export function listCores(params: ListCoresParams = {}): Promise<Core[]> {
  return apiGet<{ items: Core[] }>("/cores", { ...params }).then((r) => r.items);
}

export function getCore(id: string): Promise<CoreDetail> {
  return apiGet<CoreDetail>(`/cores/${encodeURIComponent(id)}`);
}

// Coverstocks -- read-only in admin_api, same reasoning as Cores.
export function listCoverstocks(params: ListCoverstocksParams = {}): Promise<Coverstock[]> {
  return apiGet<{ items: Coverstock[] }>("/coverstocks", { ...params }).then((r) => r.items);
}

export function getCoverstock(id: string): Promise<CoverstockDetail> {
  return apiGet<CoverstockDetail>(`/coverstocks/${encodeURIComponent(id)}`);
}

// Blocked video channels -- admin-curated denylist, see BlockedChannel's
// own comment in types.ts. No update endpoint (a row here IS the
// block) and no pagination (expected to stay a short, hand-curated
// list, same shape as Price Sites).
export function listBlockedChannels(): Promise<BlockedChannel[]> {
  return apiGet<{ items: BlockedChannel[] }>("/blocked-channels").then((r) => r.items);
}

export function createBlockedChannel(channelTitle: string, note?: string): Promise<BlockedChannel> {
  return apiPost<BlockedChannel>("/blocked-channels", { channel_title: channelTitle, note });
}

// Hard delete -- "unblocking" a channel. Its videos become eligible for
// the BigCommerce push again on the next bowlerdepot_video_sync run;
// nothing about their approval status or the video_reviews_summary
// rollup changes.
export function deleteBlockedChannel(id: string): Promise<{ deleted: boolean; id: string }> {
  return apiDelete(`/blocked-channels/${encodeURIComponent(id)}`);
}

// Manual seed URLs -- see ManualSeedUrl's own comment in types.ts.
// Same list/create/delete-only shape as Blocked Channels.
export function listManualSeedUrls(): Promise<ManualSeedUrl[]> {
  return apiGet<{ items: ManualSeedUrl[] }>("/manual-seed-urls").then((r) => r.items);
}

export function createManualSeedUrl(input: ManualSeedUrlCreateInput): Promise<ManualSeedUrl> {
  return apiPost<ManualSeedUrl>("/manual-seed-urls", input);
}

export function deleteManualSeedUrl(id: string): Promise<{ deleted: boolean; id: string }> {
  return apiDelete(`/manual-seed-urls/${encodeURIComponent(id)}`);
}

// User management (Cognito) -- Admins-only on the backend (see
// require_admin_role in admin_api/service.py); UsersPage.tsx also hides
// itself from the nav for anyone whose AuthUser.role isn't "admin", but
// that's a UX nicety, not the security boundary -- an Editor calling
// these directly still gets a 403 from admin_api_authorizer/app.py.
// username below is always an email address (see create_user's own
// docstring on why) -- encodeURIComponent handles the "@".
export function listUsers(): Promise<AdminUser[]> {
  return apiGet<AdminUser[]>("/users");
}

export function createUser(input: CreateUserInput): Promise<CreateUserResult> {
  return apiPost<CreateUserResult>("/users", input);
}

export function setUserGroup(username: string, group: "Admins" | "Editors"): Promise<{ username: string; group: string }> {
  return apiPatch(`/users/${encodeURIComponent(username)}/group`, { group });
}

export function setUserEnabled(username: string, enabled: boolean): Promise<{ username: string; enabled: boolean }> {
  return apiPatch(`/users/${encodeURIComponent(username)}/enabled`, { enabled });
}

export function deleteUser(username: string): Promise<{ username: string; deleted: boolean }> {
  return apiDelete(`/users/${encodeURIComponent(username)}`);
}
