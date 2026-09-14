-- 036_product_articles_slug.sql
--
-- Al: "can we make the slugs for the pages more human readable, does
-- google still prefer that?" -- confirmed via Google's own developer
-- documentation (still recommends "simple, descriptive words... in a
-- manner most intelligible to humans" over opaque ids/params). Today
-- every Learn article URL is /articles/<raw product_articles.product_id
-- uuid>/ (see bowlerdepot-learn's ArticleCard.tsx/ArticleDetailPage.tsx
-- and public_api's GET /products/{product_id}/article) -- this migration
-- adds the column a human-readable replacement will be persisted into.
--
-- Scoped via two follow-up questions: slug FORMAT is "brand + full
-- product name" (e.g. "storm-phaze-ii-pearl"), deliberately keeping
-- finish/color qualifier words (Solid/Pearl/Hybrid/etc.) IN the slug --
-- this catalog has a well-known naming-collision problem across those
-- variants of the same base ball name (see 022_product_articles.sql's
-- own sibling_product_ids heuristic, which strips exactly those words
-- to detect family groupings), so a bare product-name slug would
-- collide constantly; brand + the FULL name (qualifiers included)
-- avoids most of that without needing a visible id suffix in the
-- normal case. OLD bare-uuid URLs get a 301 redirect to the new slug
-- URL (see template.yaml's LearnArticleSlugRedirectsStore/
-- LearnSitePrettyUrlFunction and bowlerdepot-learn/scripts/
-- sync-slug-redirects.mjs) rather than just switching cold -- Al's
-- own choice, and the right one for a page that may already be
-- indexed by Google or linked externally.
--
-- Nullable + unique (not NOT NULL): mirrors first_published_at's own
-- posture (033_product_articles_first_published_at.sql) -- computed
-- and persisted exactly once, on an article's FIRST-EVER approval (see
-- admin_api/service.py's approve_article), and never touched again by
-- a later regenerate+re-approve cycle, so a slug -- and therefore any
-- 301 redirect target pointing at it, and any external link/bookmark/
-- search-index entry -- stays stable for the life of the article. A
-- 'pending' article that has never been approved has no slug yet (there
-- is nothing public to link to), same reasoning first_published_at
-- stays null until the first approval. Existing already-approved rows
-- are backfilled by scripts/backfill_article_slugs.py (a Python script,
-- not inline SQL here) -- unlike first_published_at's trivial "copy
-- reviewed_at" backfill, computing a real slug needs the same
-- normalization + collision-suffix logic approve_article uses
-- (admin_api/service.py's slugify_product_name/generate_unique_article_
-- slug), which only exists in Python, not as a SQL expression.

begin;

alter table product_articles add column slug text unique;

comment on column product_articles.slug is 'Human-readable URL slug ("brand-product-name", finish/color qualifier words included on purpose -- see this migration''s header comment on the Solid/Pearl/Hybrid collision problem), e.g. "storm-phaze-ii-pearl". Set exactly once, on this article''s first-ever approval (mirrors first_published_at''s own coalesce-based "never touched again" posture in admin_api.approve_article) -- stays stable across any later regenerate+re-approve cycle so external links/search-index entries never go stale. Null for an article that has never been approved yet, or for an already-approved pre-migration row until scripts/backfill_article_slugs.py runs.';

commit;
