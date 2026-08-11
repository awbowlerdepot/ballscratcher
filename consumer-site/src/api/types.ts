// Mirrors public_api's response shapes as closely as possible (see
// src/public_api/service.py in the main repo) -- deliberately loose
// (most fields optional/nullable) since a card-shaped list_products row
// and a full get_product row share overlapping but not identical keys,
// and the backend itself never guarantees every field is populated
// (e.g. a product with no cores.core_type match, no videos yet, etc.).

export interface Brand {
  id: string;
  name: string;
}

export interface ProductCard {
  id: string;
  name: string;
  url: string;
  color?: string | null;
  status: "current" | "retired";
  brand_name: string;
  core_name?: string | null;
  core_type?: string | null;
  coverstock_name?: string | null;
  coverstock_type?: string | null;
  coverstock_material?: string | null;
  release_date?: string | null;
  primary_image_url?: string | null;
  video_reviews_summary_video_count?: number | null;
}

export interface ProductSku {
  weight_lbs: number;
  rg?: number | null;
  differential?: number | null;
  mass_bias?: number | null;
}

export interface ProductImage {
  id: string;
  image_type: string;
  stored_url: string;
  is_thumbnail: boolean;
  display_order: number;
}

export interface ProductVideo {
  youtube_video_id: string;
  title: string;
  channel_title?: string | null;
  published_at?: string | null;
  thumbnail_url?: string | null;
  summary: string;
}

export interface ProductDetail {
  id: string;
  name: string;
  url: string;
  color?: string | null;
  coverstock_material?: string | null;
  coverstock_type?: string | null;
  coverstock_name?: string | null;
  has_particle?: boolean | null;
  has_custom_graphic?: boolean | null;
  factory_finish?: string | null;
  part_number?: string | null;
  weights_available?: string | null;
  usbc_approval_date?: string | null;
  release_date?: string | null;
  description?: string | null;
  status: "current" | "retired";
  primary_image_url?: string | null;
  video_reviews_summary?: string | null;
  video_reviews_summary_video_count?: number | null;
  video_reviews_summary_updated_at?: string | null;
  brand_id: string;
  brand_name: string;
  manufacturer_name?: string | null;
  core_id?: string | null;
  core_name?: string | null;
  core_type?: string | null;
  coverstock_id?: string | null;
  coverstock_full_name?: string | null;
  skus: ProductSku[];
  images: ProductImage[];
  videos: ProductVideo[];
}

export interface SimilarProduct {
  id: string;
  name: string;
  url: string;
  color?: string | null;
  brand_name: string;
  core_type?: string | null;
  coverstock_type?: string | null;
  coverstock_material?: string | null;
  coverstock_name?: string | null;
  primary_image_url?: string | null;
  similarity_score: number;
}

export interface PlotterPoint {
  id: string;
  name: string;
  url: string;
  brand_name: string;
  primary_image_url?: string | null;
  oil: number;
  motion: number;
  oil_motion_source: "chart" | "estimated" | "manual";
}

export type ProductStatus = "current" | "retired";
