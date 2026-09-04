-- 027_manual_seed_urls.sql
--
-- REAL INCIDENT (2026-09-04): Al reported "the original equinox bowling
-- ball is missing from the storm site" (storm-equinox-bowling-ball).
-- Root-caused as a genuine orphan page: live, in-stock, indexable --
-- but present in NEITHER of commercebuild_url_discovery's two discovery
-- sources (the "Bowling Balls" category listing, and sitemap_products.
-- xml), because stormbowling.com itself stopped linking to it
-- internally (superseded on-site by "Equinox Hybrid"/"Equinox Solid"
-- variant pages). A one-off manual Lambda invocation
-- ({"url": "...", "brand_id": "..."} against commercebuild_product_
-- scraper directly) fixed that one product, but left no permanent way
-- to catch the NEXT page like it, or to remember that this one needs
-- re-checking if the manufacturer's site structure changes again.
--
-- This table is that permanent catch: a small admin-curated list of
-- "always include this URL in discovery, no matter what the site's own
-- crawlable structure says" seeds, one row per known/suspected orphan.
-- Deliberately generic (keyed by brand_id, not commercebuild-specific)
-- since discovered_urls itself is already shared across every platform's
-- url_discovery Lambda -- see this migration's own comment on
-- discovered_urls' scope in 001_init_schema.sql. Only wired into
-- commercebuild_url_discovery/app.py's handler() so far (see that
-- module's own docstring), since that's the platform this incident
-- actually happened on -- extending to the other four url_discovery
-- Lambdas (Brunswick/Radical/DV8's craft-CMS sitemap, WooCommerce,
-- NetSuite, Shopify) is a small, mechanical follow-up if an orphan page
-- ever turns up on one of those instead (just union in the same
-- discover_manual_seed_urls(conn, brand_id) query, no schema change
-- needed).
--
-- url has a plain UNIQUE constraint (not case-insensitive like
-- blocked_video_channels.channel_title) since these are real URLs, not
-- free-text display names -- two different-cased URLs are typically two
-- different real resources on the web, not the same thing typed two
-- ways, so normalizing case here would be actively wrong.

begin;

create table manual_seed_urls (
    id uuid primary key default uuid_generate_v4(),
    brand_id uuid not null references brands(id),
    url text not null unique,
    note text,  -- optional free-text reason, e.g. "orphaned on-site, superseded by Equinox Hybrid/Solid variants -- 2026-09-04"
    created_at timestamptz not null default now()
);

create index idx_manual_seed_urls_brand on manual_seed_urls(brand_id);

comment on table manual_seed_urls is 'Admin-curated list of product URLs to always union into a url_discovery Lambda''s own discovered set, regardless of what that platform''s normal crawlable discovery sources (category listing, sitemap, etc.) find on their own. Exists for genuine orphan pages -- live, in-stock, real products the manufacturer''s own site has stopped linking to internally (see this migration''s own header comment for the real Storm Equinox incident that motivated it, 2026-09-04). A seeded URL flows through the exact same discovered_urls diff + SQS publish path as any normally-discovered URL, so it only gets (re-)scraped once, not on every single discovery run, once it lands in discovered_urls.';

commit;
