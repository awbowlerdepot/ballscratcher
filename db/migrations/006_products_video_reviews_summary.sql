-- 006_products_video_reviews_summary.sql
--
-- "Summary of summaries": a single, product-level rollup synthesized from
-- every approved video's own Bedrock summary (product_videos.summary),
-- regenerated automatically by video_summarizer every time a video gets
-- (re)summarized for a product -- see video_summarizer.refresh_video_
-- reviews_rollup for the actual generation logic.
--
-- video_reviews_summary_video_count is stored alongside the text rather
-- than recomputed on read so callers (admin API, eventually the storefront)
-- can show "based on N reviews" without a second query, and so it's
-- trivially visible whether the rollup is stale relative to how many
-- approved+summarized videos currently exist for the product.

begin;

alter table products add column video_reviews_summary text;
alter table products add column video_reviews_summary_video_count integer not null default 0;
alter table products add column video_reviews_summary_updated_at timestamptz;

commit;
