-- 056_products_score_evidence.sql -- what each materialized score rests on,
-- and when it was computed (DEPLOY_RUNBOOK.md 6fg).
--
-- Spurline (the sourceware-sync repo) publishes the Google Merchant Center
-- feed for bowlerdepot.com and wants custom_label_4 to be a demand band:
-- BowlerIQ's demand score for bowling balls. It reads that score from the
-- Partner API (`demand` on the v1 Ball). The problem is 030's own design
-- choice: popularity_score and total_daily_movement default to, and are
-- reset to, 0 for "no data" -- a ball with no approved, view-counted
-- videos and a ball with genuinely unpopular videos both read 0, and so do
-- a ball with no SKU stock history and a ball that truly didn't move. That
-- was fine for an admin sort; it is not fine for a feed that puts real ad
-- money behind the number. demand_score inherits the same ambiguity (a
-- percent_rank over a column that's mostly "no data" zeros).
--
-- So refresh_product_scores now also stores the evidence counts behind
-- each score, and the time of the run that wrote them:
--   * popularity_video_count: approved videos with a view_count -- exactly
--     the rows _POPULARITY_SCORE_SQL averages over. 0 = no data.
--   * movement_sku_count: SKUs with qualifying stock history in the
--     trailing DAILY_MOVEMENT_LOOKBACK_DAYS window -- exactly the SKUs
--     that contribute a nonzero-elapsed depletion rate to
--     _TOTAL_DAILY_MOVEMENT_SQL (>= 2 non-null readings spanning > 0 days).
--     0 = no data, NOT "nothing moved".
--   * scores_computed_at: the refresh transaction's time. NULL = this row
--     has never been through refresh_product_scores (every existing row
--     until the first run after this migration, and any product created
--     since the last daily run). The Partner API returns `demand: null`
--     for those rather than a fabricated 0.
--
-- Counts default to 0 (not null), same as 030's columns; scores_computed_at
-- is what distinguishes "never computed" from "computed as 0".
--
-- None of these are in 044's products_bump_content_changed_at column list,
-- deliberately: they change every day for every ball, and bumping
-- content_changed_at would make the partner /v1/changes feed resend the
-- whole catalog daily (exactly what 044 was written to avoid). Partners
-- that want demand read it from /v1/balls; its own `computed_at` says how
-- fresh it is.
--
-- Also: an index on bowlerdepot_products.bigcommerce_product_id. 018
-- dropped the old (bigcommerce_product_id, bigcommerce_sku) unique
-- constraint (and with it the only index on that column); the Partner
-- API's new bigcommerce_product_id field checks "does any OTHER product
-- claim this BigCommerce product" per ball, which looks rows up by it.

begin;

alter table products
    add column popularity_video_count integer not null default 0,
    add column movement_sku_count integer not null default 0,
    add column scores_computed_at timestamptz;

create index if not exists idx_bowlerdepot_products_bigcommerce_product_id
    on bowlerdepot_products(bigcommerce_product_id);

comment on column products.popularity_video_count is 'Migration 056: number of approved, view-counted product_videos that products.popularity_score was computed from. 0 = no data (not the same as low popularity). Written by refresh_product_scores.';
comment on column products.movement_sku_count is 'Migration 056: number of SKUs with qualifying stock history (>= 2 readings spanning > 0 days in the trailing lookback window) that products.total_daily_movement was computed from. 0 = no data (not "nothing moved"). Written by refresh_product_scores.';
comment on column products.scores_computed_at is 'Migration 056: when refresh_product_scores last wrote popularity_score/total_daily_movement/demand_score and the two evidence counts for this row. NULL = never refreshed. Not part of content_changed_at (migration 044) on purpose.';

commit;
