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
