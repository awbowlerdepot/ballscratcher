// Field names/shapes here mirror src/admin_api/service.py's actual
// query columns/dict keys as of the dashboard (GET /admin/dashboard)
// and products (GET /products) endpoints -- see that file in the main
// repo as the source of truth if these two drift apart.

export interface DashboardKpis {
  total_products: number;
  current_products: number;
  retired_products: number;
  missing_core: number;
  missing_coverstock: number;
  missing_skus: number;
  products_with_video: number;
  products_with_price_tracking: number;
  total_catalog_adu: number;
}

export interface TopPopularityItem {
  id: string;
  name: string;
  brand_name: string;
  popularity_score: number;
}

export interface TopAduItem {
  id: string;
  name: string;
  brand_name: string;
  total_adu: number;
}

export interface AduByBrandItem {
  brand_name: string;
  total_adu: number;
}

export interface AduDeltaItem {
  product_id: string;
  name: string;
  brand_name: string;
  weight_lbs: number | null;
  previous_adu: number;
  current_adu: number;
  delta_adu: number;
}

export interface DashboardSummary {
  kpis: DashboardKpis;
  top_popularity: TopPopularityItem[];
  top_adu: TopAduItem[];
  adu_by_brand: AduByBrandItem[];
  top_growing_adu: AduDeltaItem[];
  top_shrinking_adu: AduDeltaItem[];
}

export type ProductStatus = "current" | "retired";

export type SourcePlatform = "netsuite" | "shopify" | "woocommerce" | "commercebuild" | "craft_cms";

export type ProductSort = "popularity" | "newest" | "oldest" | "name_asc" | "name_desc" | "total_adu";

export interface Product {
  id: string;
  brand_id: string;
  brand_name: string;
  name: string;
  url: string;
  status: ProductStatus;
  published: boolean;
  updated_at: string;
  core_id: string | null;
  core_name: string | null;
  release_date: string | null;
  coverstock_id: string | null;
  coverstock_name: string | null;
  popularity_score: number;
  total_adu: number;
}

export interface ListProductsParams {
  published?: boolean;
  brand_id?: string;
  search?: string;
  needs_video_summary_refresh?: boolean;
  has_approved_video_summaries?: boolean;
  missing_core?: boolean;
  missing_coverstock?: boolean;
  missing_skus?: boolean;
  html_fallback_skus?: boolean;
  missing_video_candidates?: boolean;
  source_platform?: SourcePlatform;
  status?: ProductStatus;
  sort?: ProductSort;
  limit?: number;
  offset?: number;
}

export interface RescrapeResult {
  queued: boolean;
  reason?: string;
  product_id?: string;
  url?: string;
  queue_env_var?: string;
}

export type ReviewQueueStatus = "pending" | "approved" | "rejected";

// field_name doubles as the "what kind of review is this" signal --
// there's no separate table/type column. It's either a whitelisted
// product column (name, coverstock_material, published, etc.) or a
// per-SKU field matching `(rg|differential|mass_bias)_(\d{1,2})lb`
// (e.g. "rg_15lb") -- see parse_review_field_name in
// src/admin_api/service.py. current_value/proposed_value are always
// strings here regardless of the underlying column's real type
// (numeric/bool casting happens server-side on approve).
export interface ReviewQueueItem {
  id: string;
  product_id: string;
  product_name: string;
  product_url: string;
  field_name: string;
  current_value: string | null;
  proposed_value: string | null;
  source: string | null;
  reason: string | null;
  status: ReviewQueueStatus;
  created_at: string;
  resolved_at: string | null;
  resolved_by: string | null;
}

export interface ListReviewQueueParams {
  status?: ReviewQueueStatus;
  product_id?: string;
  limit?: number;
  offset?: number;
}

export interface ReviewQueueListResult {
  items: ReviewQueueItem[];
  // Only populated when status === "pending" -- null otherwise (see
  // GET /review-queue in admin_api/app.py).
  pending_count: number | null;
}

export interface ApproveReviewResult {
  review_id: string;
  status: "approved";
  applied: { table: string; column: string; value: unknown; where: unknown };
}

export interface RejectReviewResult {
  review_id: string;
  status: "rejected";
}

export type VideoCandidateStatus = "pending" | "approved" | "rejected";

// Matches list_video_candidates' SELECT in admin_api/service.py.
// Ordering is match_confidence asc, created_at asc, id asc (id is a
// deliberate pagination tiebreaker, see that function's own history).
// No rejection-reason column is persisted -- a reject's `reason` is
// transient (used only in the request), not stored/returned here.
export interface VideoCandidate {
  id: string;
  product_id: string;
  product_name: string;
  brand_name: string;
  youtube_video_id: string;
  title: string;
  channel_title: string;
  published_at: string | null;
  thumbnail_url: string | null;
  match_query: string | null;
  // NOT a numeric score -- a text enum, 'high' | 'low' (see
  // video_discovery.score_match / db/migrations/004_product_videos.sql).
  // Real incident: this was originally typed as `number | null` and
  // rendered with `.toFixed(2)`, which threw at runtime the first time
  // this page actually ran against live data ("r.match_confidence.
  // toFixed is not a function") -- fixed here, kept as a comment so the
  // mistake doesn't get repeated.
  match_confidence: "high" | "low" | null;
  transcript_note: string | null;
  status: VideoCandidateStatus;
  source: string | null;
  created_at: string;
  resolved_at: string | null;
  resolved_by: string | null;
  view_count: number | null;
  like_count: number | null;
  comment_count: number | null;
  duration_seconds: number | null;
  stats_fetched_at: string | null;
  has_summary: boolean;
}

export interface ListVideoCandidatesParams {
  // "all" omits the status filter server-side (mapped to NULL) -- see
  // GET /video-candidates. The admin-site tab's own dropdown never
  // actually exposes "all" as a choice (only product-detail's fetch
  // does), so admin-spa mirrors that: pending/approved/rejected only.
  status?: VideoCandidateStatus | "all";
  product_id?: string;
  limit?: number;
  offset?: number;
}

export interface VideoCandidateListResult {
  items: VideoCandidate[];
  pending_count: number | null;
}

export interface ReassignVideoResult {
  video_id: string;
  product_id: string;
  origin_video_id: string;
  merged_with_existing: boolean;
}

// Ball-review articles (022_product_articles.sql onward). Unlike Review
// Queue/Video Candidates, GET /articles does NOT return a pending_count
// -- admin-site's own Articles tab has no pending badge either (checked
// its renderArticles before assuming one existed here).
export type ArticleStatus = "pending" | "approved" | "rejected";

// Matches list_articles' SELECT in admin_api/service.py -- a lighter
// projection than get_article's full row (no hook/performance_summary/
// faq/etc.), just enough for the list view and its inline thumbnail/
// sync-toggle.
export interface ArticleListItem {
  id: string;
  product_id: string;
  product_name: string;
  brand_name: string;
  status: ArticleStatus;
  title: string | null;
  generated_at: string | null;
  reviewed_at: string | null;
  resolved_by: string | null;
  created_at: string;
  action_shot_image_url: string | null;
  product_shot_image_url: string | null;
  images_generated_at: string | null;
  sync_to_bigcommerce: boolean;
  bigcommerce_post_id: string | null;
  bowlerdepot_synced_at: string | null;
}

export interface ArticleFaqItem {
  question: string;
  answer: string;
}

// comparison_table rows are heuristically assembled per-product (see
// 022_product_articles.sql's header comment) and their exact field set
// has grown ad hoc (pricing fields added later, see public_api's own
// history) -- typed loosely here since admin-spa only needs to display
// the row count and raw JSON, not read specific fields off it (same
// "dump it as JSON" approach admin-site/index.html's own
// renderArticlePreviewHtml takes).
export type ArticleComparisonRow = Record<string, unknown>;

// Full detail for one article (GET /articles/{id}) -- select pa.* plus
// product_name/brand_name, see get_article in admin_api/service.py.
export interface Article {
  id: string;
  product_id: string;
  product_name: string;
  brand_name: string;
  status: ArticleStatus;
  title: string | null;
  hook: string | null;
  performance_summary: string | null;
  who_should_buy: string[];
  who_should_skip: string[];
  pros: string[];
  cons: string[];
  buying_tips: string | null;
  verdict: string | null;
  faq: ArticleFaqItem[];
  comparison_table: ArticleComparisonRow[];
  sibling_product_ids: string[];
  source_video_ids: string[];
  generated_at: string | null;
  reviewed_at: string | null;
  resolved_by: string | null;
  created_at: string;
  action_shot_image_key: string | null;
  action_shot_image_url: string | null;
  product_shot_image_key: string | null;
  product_shot_image_url: string | null;
  images_generated_at: string | null;
  sync_to_bigcommerce: boolean;
  bigcommerce_post_id: string | null;
  bowlerdepot_synced_at: string | null;
}

export type ArticleImageVariant = "action_shot" | "product_shot";

// 026_product_article_image_candidates.sql -- every image ever
// generated for an article's action_shot/product_shot, from either
// Gemini or Stability (see model_id). seed is Stability-only (null for
// Gemini, which has no reproducible seed param -- see that migration's
// own column comment).
export interface ArticleImageCandidate {
  id: string;
  article_id: string;
  variant: ArticleImageVariant;
  model_id: string;
  image_key: string;
  image_url: string;
  seed: number | null;
  is_selected: boolean;
  created_at: string;
}

export interface ListArticlesParams {
  // "all" omits the status filter server-side, same convention as
  // Video Candidates -- see GET /articles in admin_api/app.py.
  status?: ArticleStatus | "all";
  product_id?: string;
  limit?: number;
  offset?: number;
}

export type ArticleRegenerateMode = "both" | "text" | "images";

export interface QueueArticleGenerationResult {
  queued: boolean;
  reason?: string;
  product_id?: string;
  mode?: ArticleRegenerateMode;
}

// sync-to-bigcommerce/resync-to-bigcommerce share this shape -- both are
// fire-and-forget lambda:InvokeFunction triggers, see queue_article_sync/
// queue_article_resync in admin_api/service.py.
export interface QueueArticleSyncResult {
  queued: boolean;
  reason?: string;
  article_id?: string;
  resync?: boolean;
}

export interface SelectImageCandidateResult {
  candidate_id: string;
  article_id: string;
  variant: ArticleImageVariant;
  image_key: string;
  image_url: string;
}

// Price tracking (014_price_tracking.sql onward). Al asked whether it
// makes sense to combine admin-site's two separate top-level tabs here
// -- "Price Sources" (the discovered-match review queue, product_price_
// sources) and "Price Sites" (the retailer registry, price_sites) --
// since one configures the other (every price source points at a price
// site via price_site_id). Checked admin-site/index.html first: nothing
// there ever combines them (the registry's own tab is read-mostly,
// separate from the review queue), but nothing about the two backend
// resources conflicts either, so admin-spa combines them into one
// PriceSitesPage with two sections -- the registry rows feed a live
// site-id/name lookup for source rows without a second page to visit.
export type PriceSourceStatus = "pending" | "approved" | "rejected";

// Matches list_price_sources' SELECT in admin_api/service.py.
// match_confidence is a text enum ('high'|'low'|null), NOT a numeric
// score -- checked db/migrations/014_price_tracking.sql directly before
// typing this, given the identical wrong-assumption incident on Video
// Candidates' match_confidence (see api/types.ts's VideoCandidate
// comment) was exactly this class of mistake.
export interface PriceSource {
  id: string;
  product_id: string;
  product_name: string;
  brand_name: string;
  price_site_id: string;
  site_name: string;
  product_url: string;
  base_url: string | null;
  css_selector: string | null;
  match_query: string | null;
  match_confidence: "high" | "low" | null;
  status: PriceSourceStatus;
  source: "site_search" | "manual";
  is_active: boolean;
  last_checked_at: string | null;
  created_at: string;
  resolved_at: string | null;
  resolved_by: string | null;
}

export interface ListPriceSourcesParams {
  // "all" omits the status filter server-side, same convention as
  // Video Candidates/Articles.
  status?: PriceSourceStatus | "all";
  product_id?: string;
  limit?: number;
  offset?: number;
}

export interface PriceSourceListResult {
  items: PriceSource[];
  // Only populated when status === "pending" (GET /price-sources).
  pending_count: number | null;
}

export type PriceSiteFetchMethod = "scrape" | "api";

// Matches list_price_sites' SELECT. search_url_template/
// result_link_selector/default_css_selector are null for an 'api' site;
// api_provider/base_url are null for a 'scrape' site (016_price_
// tracking_bigcommerce.sql made the scrape-only fields nullable
// specifically for this split -- see that migration's own column
// comments).
export interface PriceSite {
  id: string;
  name: string;
  search_url_template: string | null;
  result_link_selector: string | null;
  default_css_selector: string | null;
  notes: string | null;
  is_active: boolean;
  created_at: string;
  fetch_method: PriceSiteFetchMethod;
  api_provider: string | null;
  base_url: string | null;
}

export interface PriceSiteCreateInput {
  name: string;
  fetch_method: PriceSiteFetchMethod;
  search_url_template?: string;
  result_link_selector?: string;
  default_css_selector?: string;
  api_provider?: string;
  base_url?: string;
  notes?: string;
}

// All optional/independent -- a caller sets whichever field it's
// actually changing (see update_price_site's own docstring).
// fetch_method itself is deliberately NOT included here, mirroring
// admin-site's editPriceSite: switching a site between 'scrape' and
// 'api' means swapping which fields even apply (the DB's own
// price_sites_fetch_method_fields_check), which is a delete-and-recreate,
// not a quick edit.
export interface PriceSiteUpdateInput {
  name?: string;
  search_url_template?: string;
  result_link_selector?: string;
  default_css_selector?: string;
  api_provider?: string;
  base_url?: string;
  notes?: string;
  is_active?: boolean;
}
