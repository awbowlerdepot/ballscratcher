-- 014_price_tracking.sql
--
-- New feature, Al: "id like to start a price tracker. this should be
-- configurable to have site setup so that it will pull the current price
-- from a number of sites on a frequency of likely daily? then store this
-- in a way that would allow for charting that price over time in the
-- admin ui and eventually the consumer UI."
--
-- DESIGN CORRECTION, mid-build: the first draft of this migration had
-- "site setup" meaning a generic URL + CSS selector an admin pastes in
-- per product per site -- built after Al's "Generic URL + CSS selector"
-- answer to an early design question, but that question was actually
-- about selector-config *flexibility*, not about who supplies the URL.
-- Al's actual clarification: "when i said site config i meant that we
-- would configure which sites to pull prices from. bowling.com,
-- bowlingball.com, bowlersmart.com, etc. there would be a daily entry
-- per product per site" -- i.e. price_sites is a registry of real named
-- retailers, and product_price_sources.product_url is DISCOVERED, not
-- typed in by hand, one per product per site. Followed by a second
-- explicit choice, after weighing auto-track-immediately against a
-- pending-review gate and briefly going back and forth on it: "the
-- reccomended path is best" -- i.e. mirror product_videos (004)'s
-- pending/approved/rejected review workflow exactly, including the
-- undo/restore capability product_videos only grew after Al hit that gap
-- live ("it appears if i accidentally reject a video i can not undo that
-- action") -- built in from the start here instead.
--
-- "Sites" means third-party retailers selling a given ball (price
-- comparison across sellers), not the manufacturer's own listed price --
-- none of the five existing scrapers persist price today (MOTIV's
-- netsuite_product_scraper parses a price_raw field off the page it
-- already visits, but only "for completeness," never written to the DB --
-- see that module's own docstring). One price per product (not per
-- SKU/weight) -- most retail listings show a single price regardless of
-- weight.
--
-- Three tables, a deliberate departure from this project's usual "one
-- row, update in place" idiom (products.oil_rating, product_videos.
-- stats_fetched_at) -- see product_price_history's own comment below for
-- why: charting requires every past value, not just the latest.
--
-- price_sites: the registry of retailer sites price_checker knows how to
-- search AND check -- name, a site-search URL template (with a {query}
-- placeholder), a CSS selector for picking product links out of that
-- site's search-results page, and a default CSS selector for where price
-- text sits on that site's own product pages. Adding a new retailer is a
-- new row here, not a new Lambda -- same "generic, no bespoke scraper per
-- site" posture the original design had, just aimed at site SEARCH now
-- too, not only price extraction.
--
-- product_price_sources: candidate (and, once approved, active) "site
-- setup" for a product -- mirrors product_videos' shape closely on
-- purpose (match_query/match_confidence/status/source/resolved_at/
-- resolved_by), since this is the exact same "automated search produces
-- candidates, an admin approves or rejects them" workflow, just searching
-- retailer sites instead of YouTube. product_url is the discovered
-- product-page URL on that site; css_selector is an optional override of
-- the site's default_css_selector for the rare page that doesn't match
-- the site-wide pattern. Only 'approved' + is_active rows are ever
-- actually checked for price (see price_checker.list_price_sources_due) --
-- pending/rejected candidates just sit here for an admin to resolve, same
-- as a pending/rejected product_videos row never gets summarized.
-- last_checked_at is a denormalized "checked most recently" pointer (same
-- idiom as products.last_video_discovery_at, migration 005) so the daily
-- batch job can order by "most overdue first" without a correlated
-- subquery against the history table on every run. Unique on (product_id,
-- price_site_id, product_url) rather than just (product_id, price_site_id)
-- -- unlike the old manual-entry design, a single site search can turn up
-- more than one plausible candidate URL for the same product (mirrors
-- product_videos allowing multiple pending candidates per product), so
-- the site alone can't be the uniqueness key; the exact URL can.
--
-- product_price_history: the actual point of this feature -- append-
-- only, one row per check, never updated in place. price is nullable: a
-- failed check (page down, selector stopped matching after a site
-- redesign) still writes a row, with the failure recorded in `error`,
-- so "we tried and it broke" is visible in the admin UI instead of
-- silently vanishing -- same "secondary data must not block/change the
-- primary outcome" posture this project already takes with video-stats
-- enrichment failures (see 013_product_videos_stats.sql's sibling
-- reasoning, though that one is a single mutable snapshot and this is a
-- log).

begin;

create table price_sites (
    id uuid primary key default uuid_generate_v4(),
    name text not null unique,                  -- e.g. "Bowling.com", "BowlingBall.com", "BowlersMart"
    search_url_template text not null,          -- e.g. "https://www.bowlingball.com/catalogsearch/result/?q={query}" -- {query} is url-encoded by price_checker before substitution
    result_link_selector text not null,         -- CSS selector matching <a> tags on the search-results page whose href is a candidate product URL
    default_css_selector text not null,         -- e.g. ".price-item--sale", "[itemprop=price]" -- where price text sits on this site's product pages
    notes text,
    is_active boolean not null default true,
    created_at timestamptz not null default now()
);

create table product_price_sources (
    id uuid primary key default uuid_generate_v4(),
    product_id uuid not null references products(id) on delete cascade,
    price_site_id uuid not null references price_sites(id) on delete cascade,

    product_url text not null,
    css_selector text,                          -- null = use price_sites.default_css_selector

    -- What search produced this candidate, and how confident the match
    -- heuristic was (see price_checker.score_match) -- both kept for
    -- audit/debugging when an admin is deciding whether to approve, same
    -- reasoning as product_videos.match_query/match_confidence.
    match_query text,                           -- null for a manually-added override row (source='manual')
    match_confidence text,                      -- 'high' | 'low' | null (manual), see price_checker.score_match

    status review_status not null default 'pending',
    source text not null default 'site_search', -- 'site_search' | 'manual'

    is_active boolean not null default true,
    last_checked_at timestamptz,

    created_at timestamptz not null default now(),
    resolved_at timestamptz,
    resolved_by text,

    unique (product_id, price_site_id, product_url)
);

create index idx_product_price_sources_status on product_price_sources(status) where status = 'pending';
create index idx_product_price_sources_product_id on product_price_sources(product_id);
create index idx_product_price_sources_last_checked_at on product_price_sources(last_checked_at nulls first) where status = 'approved' and is_active = true;

create table product_price_history (
    id uuid primary key default uuid_generate_v4(),
    price_source_id uuid not null references product_price_sources(id) on delete cascade,
    price numeric(10, 2),                       -- null when a check fails, see `error`
    currency text not null default 'USD',
    raw_price_text text,                        -- the matched element's raw text, for debugging a bad parse
    error text,                                 -- null on success; fetch/parse failure reason otherwise
    checked_at timestamptz not null default now()
);

-- (price_source_id, checked_at desc) is this table's one real query shape:
-- "give me this source's price history, most recent first" for charting.
create index idx_product_price_history_source_checked_at on product_price_history(price_source_id, checked_at desc);

comment on table price_sites is 'Registry of retailer sites price_checker knows how to search and check -- name + site-search URL template + result-link selector + default price selector. Adding a new site is a new row here, not a new Lambda.';
comment on table product_price_sources is 'Per-product candidate/active "site setup": mirrors product_videos'' search-then-review shape (match_query/match_confidence/status/source/resolved_at/resolved_by) -- price_checker''s site search produces pending candidates, an admin approves/rejects/restores them via the admin API, and only approved+active rows are ever checked for price.';
comment on table product_price_history is 'Append-only price-check log, one row per attempt against an approved source -- the actual time series this feature exists to chart. price/raw_price_text/error are all nullable/optional in combination: a successful check has price+raw_price_text set and error null; a failed one has error set and price null, but always still gets a row.';

commit;
