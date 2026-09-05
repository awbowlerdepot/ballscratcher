// Mirrors public_api's response shapes (src/public_api/service.py in the
// main repo) -- see list_articles/get_product_article's own docstrings
// for exactly which fields each shape guarantees vs. leaves null.

export interface ArticleCard {
  article_id: string;
  title: string;
  hook: string;
  generated_at?: string | null;
  reviewed_at?: string | null;
  product_id: string;
  product_name: string;
  product_url: string;
  brand_name: string;
  coverstock_name?: string | null;
  coverstock_type?: string | null;
  primary_image_url?: string | null;
}

export interface ProductSku {
  weight_lbs: number;
  rg?: number | null;
  differential?: number | null;
  mass_bias?: number | null;
}

export interface ArticleProductSpec {
  name: string;
  url: string;
  core_name?: string | null;
  core_type?: string | null;
  coverstock_name?: string | null;
  coverstock_type?: string | null;
  primary_image_url?: string | null;
  skus: ProductSku[];
}

export interface ComparisonRow {
  id: string;
  name: string;
  url: string;
  core_name?: string | null;
  coverstock_name?: string | null;
  primary_image_url?: string | null;
}

export interface FaqItem {
  question: string;
  answer: string;
}

export interface ArticleDetail {
  id: string;
  title: string;
  hook: string;
  performance_summary?: string | null;
  who_should_buy?: string[] | null;
  who_should_skip?: string[] | null;
  pros?: string[] | null;
  cons?: string[] | null;
  buying_tips?: string | null;
  verdict?: string | null;
  faq?: FaqItem[] | null;
  generated_at?: string | null;
  action_shot_image_url?: string | null;
  product_shot_image_url?: string | null;
  product: ArticleProductSpec | null;
  comparison_table: ComparisonRow[];
}

export interface ProductArticleResponse {
  product_id: string;
  article: ArticleDetail | null;
}
