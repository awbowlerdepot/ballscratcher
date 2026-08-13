-- 015_products_last_price_discovery_at.sql
--
-- Same rotation idiom as 005_products_last_video_discovery_at.sql, added
-- proactively this time rather than after finding the same "always
-- re-selects the same top-N" bug in production a second time: price_
-- checker's {"discover": true} default/brand_id scope needs a way to pick
-- "which products haven't had a price-source search run against them
-- recently" without an ever-repeating top-N.
--
-- last_price_discovery_at records when price_checker actually completed a
-- discovery pass (one site-search attempt per configured, active
-- price_sites row) for a product -- set only on a pass that actually ran,
-- not one that errored out before completing, same "don't credit a
-- product as covered when it wasn't" reasoning as mark_product_searched.
-- Ordering by this column ascending, nulls first, means never-searched
-- products always come before any previously-searched one, and once the
-- whole catalog has been searched at least once, it naturally cycles back
-- to the least-recently-searched products.

begin;

alter table products add column last_price_discovery_at timestamptz;

commit;
