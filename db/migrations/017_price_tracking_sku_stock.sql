-- 017_price_tracking_sku_stock.sql
--
-- Al, clarifying 016's product-level in_stock boolean: "for the instock
-- i was refering to actual number of each sku instock." Confirmed via a
-- follow-up design conversation: (1) track quantity per SKU (weight),
-- efficiently, in whatever shape supports later answering "how many are
-- being sold and when are they restocked"; (2) the existing product-
-- level in_stock boolean (016) stays, but should now be DERIVED from
-- these per-SKU quantities rather than BigCommerce's product-level
-- inventory_tracking/availability fields -- "should follow the
-- quantities and once 0 it should be false"; (3) BigCommerce's variant
-- weights should be matched against our own product_skus weights, and
-- any mismatch either direction ("we track a weight BigCommerce doesn't
-- sell" or "BigCommerce sells a weight we don't track") should be
-- surfaced "so we can fix whatever is causing the discrepency."
--
-- KEY DESIGN DECISION: a new table, not a JSON column bolted onto
-- product_price_history. product_skus (001_init_schema.sql) already
-- models weight as its own row per product -- product_sku_stock_history
-- reuses that as its join key, one row per (product_sku_id, checked_at),
-- same append-only "store the raw reading, compute anything derived at
-- read time" shape product_price_history already uses (see that table's
-- own header comment in 014). "How many are being sold and when are
-- they restocked" (Al's own framing) is answerable by comparing
-- consecutive quantity readings for one product_sku_id at read time --
-- a day-over-day DROP is (at most) units sold since the last check, a
-- RISE is a restock -- without this table needing to store a derived
-- delta/event column itself, same live-computed-not-stored posture this
-- project already takes for popularity_score/latest_price elsewhere.
-- Honesty note baked into that: a periodic snapshot can't fully
-- distinguish "12 sold, 0 restocked" from "2 sold, 10 restocked" on a
-- day where both happened -- it only ever sees the net change since the
-- last check. Good enough for the stated goal (spot depletion/restock
-- patterns over time), not a substitute for a real order-events feed.
--
-- price_source_id (FK to product_price_sources, same table price/cost/
-- in_stock's own history already hangs off) ties every quantity reading
-- back to which site's check produced it, and gives this table the same
-- on-delete-cascade cleanup product_price_history already has if a
-- source is ever deleted. quantity is nullable -- BigCommerce not
-- tracking inventory for a given variant (inventory_level absent/null)
-- is recorded as "unknown," never silently coerced to 0, so the product-
-- level in_stock derivation below can tell "confirmed empty" apart from
-- "we don't actually know."
--
-- Weight-mismatch tracking reuses review_queue (001_init_schema.sql),
-- not a new table -- it's already exactly "a data-quality item a human
-- needs to look at and resolve," the same role it plays for bowlerdepot_
-- reconciliation's accuracy mismatches and bowwwl_cross_check's own
-- findings. source='price_checker' is new; see price_checker.write_sku_
-- weight_mismatch_reviews for the one deliberate difference from those
-- two modules' own review_queue writers: this checks for an existing
-- pending row with the same (product_id, field_name, source) before
-- inserting, since this runs DAILY (not weekly like bowwwl) and an
-- unresolved mismatch is expected to often persist across many checks --
-- without that guard, a single persisting discrepancy would insert a
-- fresh review_queue row every day forever.

begin;

create table product_sku_stock_history (
    id uuid primary key default uuid_generate_v4(),
    product_sku_id uuid not null references product_skus(id) on delete cascade,
    price_source_id uuid not null references product_price_sources(id) on delete cascade,
    quantity int,
    checked_at timestamptz not null default now()
);

-- Mirrors product_price_history's own idx_product_price_history_source_
-- checked_at -- "latest N readings for one SKU, newest first" is the
-- access pattern both a chart and the in_stock derivation need.
create index idx_product_sku_stock_history_sku_checked_at
    on product_sku_stock_history(product_sku_id, checked_at desc);
create index idx_product_sku_stock_history_price_source
    on product_sku_stock_history(price_source_id);

comment on column product_sku_stock_history.quantity is 'BigCommerce variant.inventory_level for this SKU''s matched weight, as of checked_at. Null means BigCommerce isn''t tracking inventory for that variant (unknown), NOT zero -- see price_checker.determine_in_stock_from_sku_quantities for why that distinction matters for the product-level in_stock derivation.';

-- price_checker.determine_in_stock (product-level BigCommerce inventory_
-- tracking/availability heuristic, 016) is superseded by price_checker.
-- determine_in_stock_from_sku_quantities (derived from THIS table's own
-- matched-weight quantities for the same check) -- updating this
-- migration's own comment left over from 016 so it doesn't point at a
-- function that no longer produces this column's value.
comment on column product_price_history.in_stock is 'BowlerDepot-only for now (via the BigCommerce API) -- derived from this same check''s own per-SKU quantities (see product_sku_stock_history / price_checker.determine_in_stock_from_sku_quantities): true if any matched SKU has a confirmed quantity above zero, false only once every matched SKU with a real reading is confirmed at zero, null if nothing usable was measured. Null for scrape-sourced checks.';

commit;
