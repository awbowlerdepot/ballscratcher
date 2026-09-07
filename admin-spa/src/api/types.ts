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
  // Al: "can we add some data over time charts to the dashboard, maybe
  // total catalog adu over time" (predates the 2026-09-06 ADU->Daily
  // Movement rename -- same metric, see admin_api/service.py's
  // get_dashboard_summary docstring).
  total_catalog_daily_movement: number;
}

export interface TopPopularityItem {
  id: string;
  name: string;
  brand_name: string;
  popularity_score: number;
}

export interface TopDailyMovementItem {
  id: string;
  name: string;
  brand_name: string;
  total_daily_movement: number;
}

export interface DailyMovementByBrandItem {
  brand_name: string;
  total_daily_movement: number;
}

export interface DailyMovementDeltaItem {
  product_id: string;
  name: string;
  brand_name: string;
  weight_lbs: number | null;
  previous_daily_movement: number;
  current_daily_movement: number;
  delta_daily_movement: number;
}

export interface DashboardSummary {
  kpis: DashboardKpis;
  top_popularity: TopPopularityItem[];
  top_daily_movement: TopDailyMovementItem[];
  daily_movement_by_brand: DailyMovementByBrandItem[];
  top_growing_daily_movement: DailyMovementDeltaItem[];
  top_shrinking_daily_movement: DailyMovementDeltaItem[];
}

// GET /admin/catalog-daily-movement-history -- backs the Dashboard's
// "Total Catalog Avg Daily Movement over time" chart, restoring a piece
// of the admin-SPA port that never made it over from admin-site/
// index.html (see service.get_catalog_daily_movement_history's own
// docstring for why this is a REAL but differently-defined number from
// DashboardKpis.total_catalog_daily_movement). One row per calendar day
// across the full observed product_sku_stock_history range -- zeros are
// real zeros, not gaps.
export interface CatalogDailyMovementHistoryPoint {
  day: string;
  total_daily_movement: number;
}

export interface CatalogDailyMovementHistoryResult {
  items: CatalogDailyMovementHistoryPoint[];
}

export type ProductStatus = "current" | "retired";

export type SourcePlatform = "netsuite" | "shopify" | "woocommerce" | "commercebuild" | "craft_cms";

// "demand_score" -- Al: "loosely avg daily movement is a demand number...
// we could take this demand number and enhance the popularity number,
// that being said im not sure what the best way to add it into that
// calculation is." Kept as a separate sort/column rather than changing
// what "popularity" means -- see admin_api/service.py's _DEMAND_SCORE_CTE
// comment for the full reasoning (scale mismatch between the two inputs,
// and popularity_score being the number public_api/consumer-site also
// shows shoppers, which this deliberately doesn't touch).
export type ProductSort = "popularity" | "newest" | "oldest" | "name_asc" | "name_desc" | "total_daily_movement" | "demand_score";

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
  total_daily_movement: number;
  // 0-1 percentile-rank blend of popularity_score and
  // total_daily_movement (see ProductSort's own comment above and
  // admin_api's _DEMAND_SCORE_CTE for the formula/reasoning). Always
  // present, never null -- every product gets ranked in the underlying
  // CTE regardless of whether it has any videos or stock history.
  demand_score: number;
  // Left-joined from product_articles (one row max per product, unique
  // on product_id -- see migration 022). article_status is null when no
  // article has ever been generated for this product yet; otherwise
  // it's that row's real status ('pending' | 'approved' | 'rejected').
  // Backs the Article status icon in ProductsPage's list (Al: "an
  // article icon with state so green if approved, yellow if pending,
  // and grey if not generated"). article_id rides along so the icon can
  // link straight into ProductDetailPage's Article sub-tab.
  article_id: string | null;
  article_status: ArticleStatus | null;
  // Aggregated from product_videos via a lateral join (a genuine
  // one-to-many table, unlike product_articles above) -- see
  // admin_api's list_products docstring for the full reasoning. Always
  // a real number, never null (coalesced to 0 server-side even when a
  // product has zero product_videos rows). Backs the Video status icon
  // next to the Article one (Al: "grey if none approved and yellow if
  // approve but no summaries and green if approved and summaries...
  // maybe a count next to the icon for number of videos") --
  // video_count is the displayed count (every candidate regardless of
  // status), approved_video_count/approved_summarized_video_count are
  // what the icon's color is derived from.
  video_count: number;
  approved_video_count: number;
  approved_summarized_video_count: number;
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

export interface SetPublishedResult {
  product_id: string;
  published: boolean;
}

// POST /products/{id}/discover-videos -- same soft-fail queued/reason
// shape as RescrapeResult above (see queue_video_discovery's own
// docstring), just without url/queue_env_var since there's no scrape
// queue involved.
export interface DiscoverVideosResult {
  queued: boolean;
  reason?: string;
  product_id?: string;
}

// One row of product_skus (GET /products/{id} -- select * so every
// column rides through; typed loosely for the columns admin-spa
// actually renders, see ProductDetail's own comment for why the rest
// stays untyped rather than chasing product_skus' full DDL here).
export interface ProductSku {
  id: string;
  product_id: string;
  weight_lbs: number;
  rg: number | null;
  differential: number | null;
  mass_bias: number | null;
  source: string | null;
  needs_review: boolean;
  [key: string]: unknown;
}

// One row of product_images (GET /products/{id} -- select * ordered by
// display_order, id). stored_url is the processed "detail" size variant
// (null until image_processor catches up); source_url is always the
// original manufacturer image, used as a fallback for display -- see
// image_processor/app.py's storage-convention comment and admin-site's
// own imageCards builder (loadProductDetailInto) for the exact same
// full/thumb fallback logic this admin-spa page ports.
export interface ProductImage {
  id: string;
  product_id: string;
  image_type: string | null;
  source_url: string;
  stored_url: string | null;
  weight_lbs_context: number | null;
  display_order: number;
  is_thumbnail: boolean;
  is_visible: boolean;
  [key: string]: unknown;
}

export interface ImageUpdateInput {
  is_visible?: boolean;
  is_thumbnail?: boolean;
}

export interface ImageUpdateResult {
  image_id: string;
  product_id: string;
  is_visible?: boolean;
  is_thumbnail?: boolean;
}

export interface ImageReorderResult {
  product_id: string;
  image_ids: string[];
}

// Full detail for one product (GET /products/{id}) -- service.get_product
// does `select p.*` plus core/brand/manufacturer names joined on, so this
// carries every products column PLUS the handful admin-spa actually
// renders typed explicitly. Deliberately loose (see the `[key: string]:
// unknown` escape hatch) rather than hand-mirroring products' full DDL
// here -- same reasoning admin-site's own renderProductRawFields takes
// (dump whatever's there), and it means a new products column never
// requires a matching types.ts edit just to keep compiling.
export interface ProductDetail {
  id: string;
  brand_id: string;
  brand_name: string;
  manufacturer_name: string | null;
  name: string;
  url: string;
  status: ProductStatus;
  published: boolean;
  source_platform: SourcePlatform | null;
  description: string | null;
  core_id: string | null;
  core_name: string | null;
  core_type: string | null;
  coverstock_id: string | null;
  coverstock_name: string | null;
  release_date: string | null;
  created_at: string;
  updated_at: string;
  video_reviews_summary: string | null;
  video_reviews_summary_generated_at: string | null;
  video_reviews_summary_video_count: number | null;
  skus: ProductSku[];
  images: ProductImage[];
  discovered_url: Record<string, unknown> | null;
  bowlerdepot_matches: Record<string, unknown>[];
  bowwwl_matches: Record<string, unknown>[];
  [key: string]: unknown;
}

// Per-product price-source row shape (GET /products/{id}/price-sources)
// -- distinct from the catalog-wide PriceSource type below: no product_
// name/brand_name (redundant on a page already scoped to one product),
// but carries fetch_method/latest_price/latest_cost_price/latest_in_stock/
// base_url that list_product_price_sources' own correlated subqueries add
// -- see that function's docstring in admin_api/service.py.
export interface ProductPriceSource {
  id: string;
  price_site_id: string;
  site_name: string;
  fetch_method: PriceSiteFetchMethod;
  product_url: string;
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
  latest_price: number | null;
  latest_checked_at: string | null;
  latest_error: string | null;
  latest_cost_price: number | null;
  latest_in_stock: boolean | null;
  base_url: string | null;
}

// GET /products/{id}/price-history -- sources for a legend, raw history
// rows for the chart, see service.get_price_history's docstring.
export interface PriceHistoryPoint {
  price_source_id: string;
  price: number | null;
  error: string | null;
  checked_at: string;
  cost_price: number | null;
  in_stock: boolean | null;
}

export interface PriceHistoryResult {
  sources: { id: string; site_name: string }[];
  history: PriceHistoryPoint[];
}

// GET /products/{id}/sku-stock-history -- same two-part shape as price
// history above, see service.get_sku_stock_history's docstring.
export interface SkuStockHistoryPoint {
  product_sku_id: string;
  price_source_id: string;
  quantity: number | null;
  checked_at: string;
}

export interface SkuStockHistoryResult {
  skus: { id: string; weight_lbs: number }[];
  history: SkuStockHistoryPoint[];
}

// POST /products/{id}/discover-price-sources and .../check-price -- same
// soft-fail queued/reason shape as DiscoverVideosResult (see
// queue_price_discovery/queue_price_check's own docstrings, both mirror
// queue_video_discovery).
export interface DiscoverPriceSourcesResult {
  queued: boolean;
  reason?: string;
  product_id?: string;
}

export interface CheckPriceResult {
  queued: boolean;
  reason?: string;
  product_id?: string;
}

// POST /products/{id}/price-sources -- the manual-override path (see
// create_product_price_source's own docstring): an admin attaching an
// exact URL directly rather than waiting on/correcting discovery.
export interface ProductPriceSourceCreateInput {
  price_site_id: string;
  product_url: string;
  css_selector?: string;
  resolved_by?: string;
  external_product_id?: string;
}

export interface ProductPriceSourceCreateResult {
  id: string;
  product_id: string;
  price_site_id: string;
  product_url: string;
  external_product_id: string | null;
  status: "approved";
  source: "manual";
}

// DELETE /price-sources/{id} -- hard delete, cascades to that source's
// own price-history rows (see delete_product_price_source's docstring).
export interface DeleteProductPriceSourceResult {
  deleted: boolean;
  id: string;
}

// POST /products/{id}/refresh-video-summary's response -- "no
// approved+summarized videos yet" is a normal, expected outcome
// (rollup_regenerated: false + reason), not an HTTP error, same
// convention as RescrapeResult's queued/reason shape.
export interface RefreshRollupResult {
  product_id: string;
  rollup_regenerated: boolean;
  reason?: string;
  video_count?: number;
}

// GET /brands -- backs a real name-based dropdown instead of a raw-UUID
// text field. Short, unpaginated (a dozen or so brands total).
export interface Brand {
  id: string;
  name: string;
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

// Cores (007_cores_table.sql) -- the "other direction" view of
// products.core_id: one row per physical core (e.g. DV8's Collision
// core), with a product_count rolling up every differently-named
// product that shares it. Read-only in admin_api -- no create/update/
// delete endpoints exist for cores at all, only GET /cores and GET
// /cores/{id} (rows are created/attached by the scrapers themselves via
// get_or_create_core_id, never by hand through this API).
export interface Core {
  id: string;
  brand_id: string;
  brand_name: string;
  name: string;
  core_type: string | null;
  release_era: string | null;
  created_at: string;
  // Only present on list rows (GET /cores), not on CoreDetail (GET
  // /cores/{id} returns `products` instead -- see that type below).
  product_count: number;
}

// The Products tab's own list-row fields, reused here since get_core's
// per-product rows are "enough for the admin UI to link straight back
// into the Products tab" (see that function's own docstring) -- not
// the full Product type, a smaller projection.
export interface CoreProductSummary {
  id: string;
  name: string;
  url: string;
  status: ProductStatus;
  published: boolean;
  updated_at: string;
}

export interface CoreDetail {
  id: string;
  brand_id: string;
  brand_name: string;
  name: string;
  core_type: string | null;
  release_era: string | null;
  created_at: string;
  products: CoreProductSummary[];
}

export interface ListCoresParams {
  brand_id?: string;
  search?: string;
  limit?: number;
  offset?: number;
}

// Coverstocks (008_coverstocks_table.sql): the exact same "other
// direction" view as Cores above, one migration later -- a
// coverstock_name is a shared, brand-scoped marketing name multiple
// differently-named products can reuse. Also read-only in admin_api
// (GET /coverstocks, GET /coverstocks/{id} only) -- rows are created/
// attached by get_or_create_coverstock_id in each scraper, never by
// hand. material/type come off Postgres enum columns
// (coverstock_material/coverstock_type) but admin_api returns them as
// plain strings, so typed loosely as string | null here, same as
// Core's core_type/release_era.
export interface Coverstock {
  id: string;
  brand_id: string;
  brand_name: string;
  name: string;
  material: string | null;
  type: string | null;
  created_at: string;
  // Only present on list rows (GET /coverstocks) -- CoverstockDetail
  // returns `products` instead, same split as Core/CoreDetail.
  product_count: number;
}

export interface CoverstockDetail {
  id: string;
  brand_id: string;
  brand_name: string;
  name: string;
  material: string | null;
  type: string | null;
  created_at: string;
  products: CoreProductSummary[];
}

export interface ListCoverstocksParams {
  brand_id?: string;
  search?: string;
  limit?: number;
  offset?: number;
}

// Blocked video channels (021_blocked_video_channels.sql): an admin-
// curated denylist of YouTube channel display names (not stable
// channel ids -- see the migration's header comment for why) whose
// videos must never be pushed to BigCommerce's Product Videos feature
// via src/bowlerdepot_video_sync. Deliberately does NOT affect
// video_discovery, approval, or the video_reviews_summary rollup --
// a blocked channel's videos stay approved and keep feeding the
// aggregate summary. No update endpoint exists -- a row here IS the
// block, so admin_api only exposes list/create/delete.
export interface BlockedChannel {
  id: string;
  channel_title: string;
  note: string | null;
  created_at: string;
}

// Manual seed URLs (027_manual_seed_urls.sql): the permanent catch for
// a real, live, in-stock product page a manufacturer's own site has
// stopped linking to internally, so it never surfaces via a category-
// listing crawl or sitemap fetch no matter how often discovery runs
// (real incident: storm-equinox-bowling-ball). Seeding a URL here gets
// it force-included on that platform's next scheduled discovery run --
// currently only wired into commercebuild's own discovery
// (Storm/Roto Grip/900 Global). Same list/create/delete-only shape as
// BlockedChannel, but deduped on the URL itself (plain unique
// constraint, not case-insensitive -- URLs are case-sensitive in a way
// a channel display name isn't).
export interface ManualSeedUrl {
  id: string;
  brand_id: string;
  brand_name: string;
  url: string;
  note: string | null;
  created_at: string;
}

export interface ManualSeedUrlCreateInput {
  brand_id: string;
  url: string;
  note?: string;
}

// User management (Cognito) -- Al: "can we add user management and a
// user group that has no access to user managment." See admin_api/
// service.py's own "User management (Cognito)" section header comment
// for the full design: two Cognito groups (Admins/Editors, not a third),
// Admins-only enforcement, and the last-Admin lockout guard. `group` is
// nullable for the same reason it's nullable on AuthUser -- an account
// that exists but was never added to either group is a real, valid "no
// access" state, not an oversight to paper over with a default.
export interface AdminUser {
  username: string;
  email: string;
  status: string; // Cognito's UserStatus, e.g. "CONFIRMED", "FORCE_CHANGE_PASSWORD"
  enabled: boolean;
  created_at: string;
  group: "Admins" | "Editors" | null;
}

export interface CreateUserInput {
  email: string;
  group: "Admins" | "Editors";
}

// Only returned once, directly from create_user's own response -- never
// persisted or re-fetchable, so the UI must show it to the creating
// admin immediately (see CreateUserResult's own usage in UsersPage.tsx)
// or it's gone for good (a fresh admin-set-user-password / delete+
// recreate would be the only recovery).
export interface CreateUserResult {
  email: string;
  group: "Admins" | "Editors";
  password: string;
}
