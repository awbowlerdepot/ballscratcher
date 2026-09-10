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
  // Set once, on this article's first-ever approval, never touched by a
  // later regenerate+re-approve (033_product_articles_first_published_at.sql)
  // -- the honest "Published" date; reviewed_at is "Updated." See
  // ArticleDetail's own comment for the full reasoning. Falls back to
  // reviewed_at in display code for the rare pre-migration row where
  // this somehow ended up null.
  first_published_at?: string | null;
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

// Al: "change the more from section at the bottom to be links to
// additional articles for the brand of the ball the current article is
// from and can we use the demand score to sort them." Same shape as
// RelatedReview below (this is now an editorial cross-link rail, not a
// shop-the-lineup rail) -- server orders by products.demand_score
// descending, so the array's own order already reflects that; no
// demand_score field is exposed here since the frontend never needs to
// re-sort or display it.
export interface BrandLineupItem {
  product_id: string;
  product_name: string;
  article_id: string;
  title: string;
  hook?: string | null;
  // The sibling's OWN article's AI-generated product shot, preferred
  // over its raw product photo -- see RelatedReview's own comment on
  // this field for the full fallback chain and reasoning (Al: "for all
  // the rails use the same style and the product shot image from the
  // article for its image").
  image_url?: string | null;
}

// Al: "how is related reviews curated and how is similar balls
// curated... i think they should both link to the articles and for all
// the rails use the same style and the product shot image from the
// article for its image." This used to be two separate rails -- this
// one (siblings with their own approved article, linking to another
// Learn article) and a "Similar Balls" rail (every published sibling
// regardless of article, linking out to BowlerDepot with core/
// coverstock/price shown). Once Similar Balls also needed to link to
// articles, the two would have pulled the identical candidate list, so
// they were merged into this single rail.
export interface RelatedReview {
  product_id: string;
  product_name: string;
  article_id: string;
  title: string;
  hook?: string | null;
  reviewed_at?: string | null;
  // Prefers the sibling's own article's AI-generated product shot
  // (product_shot_image_url, 023_product_article_images.sql) over its
  // raw scraped/generated product photo, falling back to that photo
  // only when the sibling's article predates image generation or image
  // generation never succeeded for it -- same preference ArticleCard's
  // own product_shot_image_url field already documents, now applied
  // consistently across every article-linking rail on the detail page.
  image_url?: string | null;
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
  // Re-stamped on EVERY admin approval, including a re-approval after a
  // regenerate run -- the honest "Updated" date (Article JSON-LD's
  // dateModified). Null for articles never yet reviewed.
  reviewed_at?: string | null;
  // Set once, on this article's first-ever approval, and never touched
  // again by a later regenerate+re-approve
  // (033_product_articles_first_published_at.sql) -- the honest
  // "Published" date (Article JSON-LD's datePublished). Al: "add
  // published dates and last updated dates to the articles" -- reviewed_at
  // alone couldn't answer this once an article had ever been regenerated,
  // since it gets overwritten every approval. Falls back to reviewed_at
  // in display code for the rare pre-migration row where this somehow
  // ended up null (the migration's own backfill should prevent that in
  // practice).
  first_published_at?: string | null;
  action_shot_image_url?: string | null;
  product_shot_image_url?: string | null;
  // Migration 031 -- see ArticleCard's own comment on these four fields.
  category_name?: string | null;
  category_slug?: string | null;
  article_type_name?: string | null;
  article_type_slug?: string | null;
  product: ArticleProductSpec | null;
  related_reviews: RelatedReview[];
  // "More from [Brand]" rail -- every OTHER published product sharing
  // this article's brand that ALSO has its own approved article, ordered
  // by demand_score descending (see service.py's own comment for the
  // full history: originally a shop-the-lineup rail sorted by price,
  // reworked into this editorial cross-link rail per Al's ask). Empty
  // array (never null/undefined) when there are none.
  brand_lineup: BrandLineupItem[];
}

export interface ProductArticleResponse {
  product_id: string;
  article: ArticleDetail | null;
}
