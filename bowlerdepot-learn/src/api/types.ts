// Mirrors public_api's response shapes (src/public_api/service.py in the
// main repo) -- see list_articles/get_product_article's own docstrings
// for exactly which fields each shape guarantees vs. leaves null.

// Learn-site content taxonomy (migration 031) -- Al: "having Categories
// with one being Bowling balls and Ball review being a type of article.
// Just to ensure future expansion." article_types is nested inline
// (mirrors admin_api's own list_categories shape) so a category's
// available write-up kinds are always available without a second
// round-trip. Today there's exactly one of each ("Bowling Balls" /
// "Ball Review"), but nav/label components should read these off
// GET /categories rather than hardcode the strings, so a future category
// or article_type just shows up.
export interface ArticleType {
  id: string;
  category_id: string;
  slug: string;
  name: string;
  description?: string | null;
  display_order: number;
}

export interface Category {
  id: string;
  slug: string;
  name: string;
  description?: string | null;
  display_order: number;
  article_types: ArticleType[];
}

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
  // The article's own AI-generated stylized product hero shot (023
  // migration), not the ball's raw scraped photo -- preferred for the
  // Learn index card's image when present (Al: "can we use the product
  // shot for the card in the list of review articles"). Null until
  // image generation has succeeded for this article; fall back to
  // primary_image_url in that case, same pattern the detail page's hero
  // image already uses.
  product_shot_image_url?: string | null;
  // Migration 031 -- null for a pre-migration article or a not-yet-
  // onboarded product_type; a card should simply omit the label then.
  category_name?: string | null;
  category_slug?: string | null;
  article_type_name?: string | null;
  article_type_slug?: string | null;
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
  // 'current' | 'retired', same products.status column the admin/
  // consumer sites already filter on. Gates the post-verdict "shop this
  // ball" CTA (see ArticleDetailPage.tsx) -- a retired ball keeps its
  // article (specs/verdict stay accurate per this file's other fields'
  // "never regenerate" posture) but shouldn't push readers to buy
  // something no longer sold.
  status?: "current" | "retired" | null;
  core_name?: string | null;
  core_type?: string | null;
  coverstock_name?: string | null;
  coverstock_type?: string | null;
  brand_name?: string | null;
  primary_image_url?: string | null;
  // Real BowlerDepot storefront product-page URL, resolved from
  // price_checker's own BigCommerce price-tracking data (014/016
  // migrations) -- null until that product has an approved+active
  // BowlerDepot price source. See client.ts's bowlerDepotSearchUrl()
  // for the fallback a frontend should use when this is null.
  ecommerce_url?: string | null;
  // Real, last-checked price/currency/availability from that same
  // BowlerDepot price source (product_price_history) -- all null
  // together until price_checker has actually checked this product at
  // least once. Never fabricated/estimated; used for Product JSON-LD's
  // Offer block (Task #448/#450), which is omitted entirely when these
  // are null rather than guessed.
  ecommerce_price?: number | null;
  ecommerce_price_currency?: string | null;
  ecommerce_in_stock?: boolean | null;
  skus: ProductSku[];
}

export interface ComparisonRow {
  id: string;
  name: string;
  url: string;
  core_name?: string | null;
  coverstock_name?: string | null;
  primary_image_url?: string | null;
  ecommerce_url?: string | null;
  // Real, last-checked BowlerDepot price/currency/availability for this
  // sibling (same product_price_sources row ecommerce_url resolves from)
  // -- Al: "include links and pricing for it using the bowlerdepot.com
  // pricing data" on the Similar Balls list. All three null together
  // until price_checker has actually priced this sibling at least once;
  // never fabricated/estimated.
  ecommerce_price?: number | null;
  ecommerce_price_currency?: string | null;
  ecommerce_in_stock?: boolean | null;
}

export interface RelatedReview {
  product_id: string;
  product_name: string;
  article_id: string;
  title: string;
  hook?: string | null;
  reviewed_at?: string | null;
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
  // Date the review was actually written/finalized (distinct from
  // generated_at, the draft-generation timestamp) -- used as Article
  // JSON-LD's datePublished/dateModified (Task #448/#450) since it's
  // the closer real-world analogue of "when this review went live."
  // Null for articles never yet reviewed.
  reviewed_at?: string | null;
  action_shot_image_url?: string | null;
  product_shot_image_url?: string | null;
  // Migration 031 -- see ArticleCard's own comment on these four fields.
  category_name?: string | null;
  category_slug?: string | null;
  article_type_name?: string | null;
  article_type_slug?: string | null;
  product: ArticleProductSpec | null;
  comparison_table: ComparisonRow[];
  related_reviews: RelatedReview[];
}

export interface ProductArticleResponse {
  product_id: string;
  article: ArticleDetail | null;
}
