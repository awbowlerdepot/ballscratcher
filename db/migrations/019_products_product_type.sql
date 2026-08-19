-- 019_products_product_type.sql
--
-- Al's ask: "currently all the products are bowling ball and im thinking of
-- pulling in bags shoes and other items, what do you think is the best way
-- to pull in additional categories and product types." First category
-- onboarded is Bags, prototyped on Brunswick (same Craft-CMS platform/brand
-- already scraped for balls -- see src/product_scraper/app.py's new
-- detect_product_type/parse_bag_product_page for the confirmed-live page
-- structure this scrapes).
--
-- Scope note (Al confirmed, catalog-first): this migration only adds enough
-- to store bag catalog data (name/color/part_number/description/images) --
-- it does NOT touch product_skus or the price/stock pipeline. Real bag
-- product pages have no weight-style variant axis at all (confirmed live:
-- each color is its own page/URL, e.g. .../blitz-double-roller-black vs.
-- .../blitz-double-roller-purple), and product_skus.weight_lbs is NOT NULL
-- and price_checker's whole stock-matching pipeline is hard-keyed off it
-- (see match_sku_weights_to_variants) -- extending that to a non-weight
-- category is real follow-up work, deliberately deferred until there's
-- actual bag catalog data in hand to test a matching key against.
--
-- product_type is the single dispatch column every layer (scrapers,
-- admin_api, consumer/admin sites) branches on. Defaults to 'ball' so
-- every existing row (100% ball today) needs no backfill -- Postgres fills
-- the default for existing rows as part of the ADD COLUMN itself.

begin;

alter table products add column product_type text not null default 'ball';

alter table products add constraint products_product_type_check
    check (product_type in ('ball', 'bag', 'shoe'));

comment on column products.product_type is 'Catalog category dispatch key: ball, bag, or shoe (migration 019). Ball-specific fields/pipelines (cores/coverstocks, RG/DIFF SKUs, oil/motion plotter estimate, price_checker''s weight-keyed stock matching) are only ever populated/run for product_type=''ball'' -- see product_scraper/app.py''s upsert_product for the estimate-on-scrape guard, and this migration''s header comment for what''s deliberately NOT yet supported for bag rows.';

commit;
