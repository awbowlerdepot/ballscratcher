-- 013_product_videos_stats.sql
--
-- Adds YouTube engagement/metadata columns to product_videos beyond what
-- video_discovery's search.list call ever returned (title, channel,
-- published_at, thumbnail_url) -- view/like/comment counts, duration, and
-- the full description all come from a separate videos.list call (see
-- src/video_discovery/app.py's fetch_video_statistics/parse_video_details_
-- response), search.list's snippet part never includes them. Al: "for the
-- videos can we get pull down more data points from the videos, date it
-- was added current view counts and any other data that make sense."
--
-- "date it was added" is already covered by two existing columns this
-- migration doesn't touch: published_at (when the video went up on
-- YouTube) and created_at (when this row was first discovered) -- see
-- 004_product_videos.sql.
--
-- stats_fetched_at is deliberately separate from created_at/published_at
-- -- view/like/comment counts are a snapshot, not a fixed fact like a
-- publish date, and go stale the moment they're written. Tracking when
-- they were last pulled is what makes video_discovery.refresh_video_stats'
-- ordering (stats_fetched_at asc nulls first) meaningful: it's how repeated
-- {"refresh_stats": true} invocations know which rows are the most overdue,
-- not just which ones happen to be missing data outright.

begin;

alter table product_videos
    add column view_count bigint,
    add column like_count bigint,
    add column comment_count bigint,
    add column duration_seconds integer,
    add column description text,
    add column stats_fetched_at timestamptz;

commit;
