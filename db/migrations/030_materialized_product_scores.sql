-- 030_materialized_product_scores.sql
--
-- Al: "there are some performance bottlenecks at this point on the
-- public/admin facing sites. what are some options to optimize this?"
-- REAL PERFORMANCE INCIDENT, found by reading the queries themselves
-- (not a live EXPLAIN ANALYZE -- no DB access from the environment doing
-- this investigation): popularity_score and total_daily_movement are
-- NOT stored anywhere. They're computed as correlated subqueries, per
-- product row, on every single request that touches them -- and this is
-- the "live-computed-not-stored posture this project already takes for
-- popularity_score/latest_price elsewhere" that 017_price_tracking_sku_
-- stock.sql's own header comment names as a deliberate prior choice, not
-- an oversight. That choice was reasonable when it was made (small
-- catalog, popularity_score's own original comment in admin_api/
-- service.py explicitly says so: "run unconditionally... rather than a
-- separate async round-trip... if this ever turns out to be the slow
-- part of the page at a larger catalog size, splitting it into its own
-- endpoint... is the fallback"). This migration is that reconsideration,
-- confirmed by actually reading how many places now pay this same cost:
--
--   - admin_api.list_products recomputes BOTH popularity_score and
--     total_daily_movement for every returned row, AND (via the
--     `demand` CTE) recomputes them AGAIN, unconditionally, over the
--     ENTIRE unfiltered catalog, on every single call regardless of
--     filters/pagination -- just to rank demand_score's percent_rank().
--     That's the whole catalog's cost paid twice per request.
--   - admin_api.get_dashboard_summary recomputes total_daily_movement a
--     THIRD time (summed across the whole catalog, for the KPI card)
--     and popularity_score/total_daily_movement a FOURTH and FIFTH time
--     (top_popularity/top_daily_movement's own top-10 queries) -- one
--     Dashboard page load is five full-catalog passes of these
--     correlated subqueries.
--   - public_api.list_products (the actual public-facing consumer-site
--     Browse page) recomputes popularity_score per card on every page
--     load too -- this is the literal public-site query cost Al asked
--     about, not just an admin-tool problem.
--
-- No index fixes this -- the underlying FK columns (product_videos.
-- product_id, product_sku_stock_history(product_sku_id, checked_at),
-- product_skus.product_id) already have one each (004_product_videos.
-- sql, 017 above, 001_init_schema.sql), so each individual correlated
-- subquery execution is already about as fast as it can be. The cost is
-- structural: real per-row aggregation work, repeated on every request,
-- against a table that's already grown well past "small catalog."
--
-- FIX: stop computing these live. Store them as real columns, recomputed
-- once a day by a new scheduled Lambda (refresh_product_scores) instead
-- of once per API request. Daily matches the actual freshness ceiling
-- already in place -- price_checker (SKU stock, feeds total_daily_
-- movement) and video_discovery's stats refresh (view counts, feeds
-- popularity_score) both already run `rate(1 day)`, so recomputing more
-- often than that would just be re-deriving the same numbers from data
-- that hasn't changed yet.
--
-- demand_score is included here too even though it was never its own
-- separate cost before this migration in the same "recomputed 5 times"
-- sense -- once popularity_score/total_daily_movement are real columns,
-- computing demand_score's percent_rank() over them becomes a single
-- cheap pass over two already-materialized numeric columns (see
-- refresh_product_scores/app.py), rather than the CTE's own percent_
-- rank()-over-a-correlated-subquery shape it had before. Storing it
-- keeps list_products' demand_score sort just as simple as the other two
-- (read a column, sort on it) instead of leaving one of the three sorts
-- still doing real work per request.
--
-- Defaults to 0 (not null) rather than nullable -- every existing row
-- gets a real, correct value the moment refresh_product_scores first
-- runs (its UPDATE touches every row unconditionally, no WHERE), so
-- there's no meaningful "not yet computed" state worth distinguishing
-- from "computed as zero" (a product with no approved videos and no SKU
-- stock history legitimately IS 0 on both, same as today's coalesce(...,
-- 0) in the live formulas). Until that first scheduled/manual run
-- happens post-deploy, every row reads 0 -- see DEPLOY_RUNBOOK.md's
-- writeup for the one-time manual-invoke step to avoid a 24h window of
-- flat zeros after deploy.

begin;

alter table products
    add column popularity_score numeric not null default 0,
    add column total_daily_movement numeric not null default 0,
    add column demand_score numeric not null default 0;

-- One index per sort column -- list_products' existing sort options
-- (popularity/total_daily_movement/demand_score, each "column desc, id
-- asc") now read and sort on these directly instead of an inline
-- computed expression, so a plain btree index on each is actually
-- usable by the planner for the first time.
create index idx_products_popularity_score on products(popularity_score desc);
create index idx_products_total_daily_movement on products(total_daily_movement desc);
create index idx_products_demand_score on products(demand_score desc);

comment on column products.popularity_score is 'Materialized as of migration 030 -- see this migration''s own header comment for the real performance incident that prompted it. Recomputed daily by refresh_product_scores (same formula admin_api/public_api''s old inline _POPULARITY_SCORE_SQL used: half-life-weighted avg approved-video view count * ln(1 + video count)). 0 for a product with no approved, view-counted videos, or before the first refresh_product_scores run after this migration deploys.';
comment on column products.total_daily_movement is 'Materialized as of migration 030 -- see this migration''s own header comment. Recomputed daily by refresh_product_scores (same formula admin_api''s old inline _TOTAL_DAILY_MOVEMENT_SQL used: trailing 30-day drops-only per-SKU depletion rate, summed across the product''s SKUs). 0 for a product with no qualifying SKU stock history, or before the first refresh_product_scores run after this migration deploys.';
comment on column products.demand_score is 'Materialized as of migration 030 -- see this migration''s own header comment. Recomputed daily by refresh_product_scores as a 50/50 percent_rank() blend of this same row''s popularity_score/total_daily_movement columns (unchanged formula/weights from admin_api''s old inline _DEMAND_SCORE_CTE, just computed once over real columns instead of per-request over correlated subqueries). 0 for every row until the first refresh_product_scores run after this migration deploys (percent_rank() needs the table populated to mean anything -- there is no meaningful "default" demand rank for a lone new row before that first run).';

commit;
