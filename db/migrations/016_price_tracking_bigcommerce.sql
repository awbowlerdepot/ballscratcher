-- 016_price_tracking_bigcommerce.sql
--
-- Al: "this... is a project for the same company that owns bowlerdepot.com
-- which is why we have the API access for that. Going with the API for
-- that one would be great and there are some additional data points that
-- would be nice to pull in for the admin side. In stock over time and
-- cost price over time so once per day for those too." Confirmed via a
-- follow-up design conversation: in-stock + cost price stay BowlerDepot-
-- only (via the API, alongside price) rather than extending to the
-- scraped retailer sites too, and this plugs into the existing price
-- tracker (014/015) as a new source TYPE rather than a separate pipeline
-- -- one review workflow, one chart, one admin UI for every source,
-- BowlerDepot included.
--
-- No real BowlerDepot store_hash/API token exists in this project yet
-- (bowlerdepot_reconciliation's own module docstring: "still not
-- obtained", its daily schedule left disabled) -- same posture as that
-- module, this is built against the confirmed BigCommerce v3 Catalog API
-- contract, ready to smoke-test the moment real credentials land.
--
-- KEY DESIGN DECISION: reuse bowlerdepot_products (001_init_schema.sql),
-- not a fresh fuzzy-match pass. bowlerdepot_reconciliation already runs
-- daily and maintains that table as the durable product_id <->
-- bigcommerce_product_id mapping (match_status 'matched'/'ambiguous'/
-- 'unmatched'). price_checker's job is tracking price/cost/stock over
-- time for products ALREADY matched, not re-deriving the match -- so its
-- BigCommerce discovery pass reads that table directly instead of
-- duplicating fuzzy_match_product's logic a second time.
--
-- price_sites gains fetch_method ('scrape' | 'api') -- the existing
-- generic search+CSS-selector "site setup" (014) only ever made sense
-- for a scraped retailer; an API-backed site has no search page to crawl
-- at all (one bulk/batched API call finds every matched product at
-- once), so search_url_template/result_link_selector/default_css_selector
-- are relaxed to nullable, with a CHECK enforcing they're still required
-- for a 'scrape' row and that api_provider is required for an 'api' row
-- instead. base_url (also new, nullable) resolves the relative storefront
-- path BigCommerce's custom_url.url field returns into an absolute,
-- clickable admin-UI link -- see price_checker.extract_bigcommerce_
-- price_fields.
--
-- product_price_sources gains external_product_id -- the matched
-- platform's own native product id (BigCommerce's numeric id, stored as
-- text, same "store the platform's id as text" convention bowlerdepot_
-- products.bigcommerce_product_id already uses), so checking can fetch
-- the exact right product every day without re-matching. Null for every
-- scrape-sourced row.
--
-- product_price_history gains cost_price and in_stock -- both null for a
-- scrape-sourced check (a competitor's storefront never publishes their
-- own cost, and "in stock" was deliberately scoped out of the generic
-- scrape path this round, see this migration's header). Al's own framing
-- -- "obviously the over time will gain value as we get more days in the
-- past but starting now will start building that value... we can use
-- them for forcasting and other things in the future" -- is exactly why
-- these are just two more nullable columns on the SAME append-only log
-- table price already lives in, not a separate table: one row per check,
-- one time series per (price_source_id, checked_at), price/cost_price/
-- in_stock all riding along together for free.

begin;

alter table price_sites add column fetch_method text not null default 'scrape';
alter table price_sites add column api_provider text;
alter table price_sites add column base_url text;

alter table price_sites alter column search_url_template drop not null;
alter table price_sites alter column result_link_selector drop not null;
alter table price_sites alter column default_css_selector drop not null;

alter table price_sites add constraint price_sites_fetch_method_check
    check (fetch_method in ('scrape', 'api'));

-- A 'scrape' site still needs its full generic search+selector config
-- (unchanged from 014); an 'api' site needs api_provider set (the only
-- value this project supports so far is 'bigcommerce', dispatched on in
-- price_checker.discover_price_sources) instead of any of those three.
alter table price_sites add constraint price_sites_fetch_method_fields_check
    check (
        (fetch_method = 'scrape' and search_url_template is not null
            and result_link_selector is not null and default_css_selector is not null)
        or
        (fetch_method = 'api' and api_provider is not null)
    );

alter table product_price_sources add column external_product_id text;

alter table product_price_history add column cost_price numeric(10, 2);
alter table product_price_history add column in_stock boolean;

comment on column price_sites.fetch_method is '''scrape'' (generic site-search + CSS selector, 014''s original design) or ''api'' (a direct platform API call, currently only BowlerDepot/BigCommerce) -- see price_checker.discover_price_sources for how each is handled.';
comment on column price_sites.api_provider is 'Which API integration to use for an ''api''-fetch_method site -- only ''bigcommerce'' exists so far. Null for ''scrape'' sites.';
comment on column price_sites.base_url is 'Storefront base URL, used to resolve a relative product path (e.g. BigCommerce''s custom_url.url) into an absolute link. Only meaningfully used by ''api'' sites today.';
comment on column product_price_sources.external_product_id is 'The matched platform''s own native product id (e.g. BigCommerce''s numeric product id, as text) -- lets checking fetch the exact right product every day without re-matching. Null for scrape-sourced rows.';
comment on column product_price_history.cost_price is 'BowlerDepot-only for now (via the BigCommerce API) -- a competitor''s storefront never publishes their own cost. Null for scrape-sourced checks.';
comment on column product_price_history.in_stock is 'BowlerDepot-only for now (via the BigCommerce API) -- see price_checker.determine_in_stock for how it''s derived and its one known caveat (variant-level inventory tracking). Null for scrape-sourced checks.';

commit;
