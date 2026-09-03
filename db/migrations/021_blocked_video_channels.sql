-- 021_blocked_video_channels.sql
--
-- Al's ask, right after 020's BowlerDepot video sync shipped: "i feel
-- like a filter is probably necessary because some of these videos are
-- from our competitors and we should avoid putting those on there. I
-- would like to include the summary of summaries even if it is built
-- off of one of theirs."
--
-- Two-part ask, two very different fixes:
--   1. Don't push a COMPETITOR'S actual YouTube video onto BowlerDepot's
--      own product page via bowlerdepot_video_sync -- that's what THIS
--      migration + the new blocked_video_channels table exist for.
--   2. The aggregate video_reviews_summary rollup paragraph (built by
--      video_summarizer from every approved video's per-video summary,
--      competitor-sourced or not) should keep drawing on ALL of them --
--      Al explicitly wants that left alone. No code change needed there;
--      video_summarizer's rollup query is untouched by this migration.
--
-- No existing "competitor channel" concept exists anywhere in this
-- codebase (checked: no channel_id column, no denylist table, no
-- channel-based logic in video_discovery/admin_api/auto-approval --
-- approval today is purely title/brand-token matching, see
-- video_discovery.score_match). product_videos.channel_title (migration
-- 004) is the only channel signal captured -- a YouTube display name, not
-- a stable channel id (YouTube's search.list response does include
-- snippet.channelId, but video_discovery/app.py never captured it) --
-- so this table matches on that display name, case-insensitively, which
-- is good enough for a human-curated blocklist an admin adds to as they
-- spot a competitor's video in the review queue, not a fully automated
-- competitor-detection system.
--
-- channel_title has a case-insensitive unique constraint (a functional
-- unique index on lower(channel_title), not a plain UNIQUE on the raw
-- column) so "Bowling.com" and "bowling.com" can't both be added as
-- separate rows and silently only half-match in the sync query below.

begin;

create table blocked_video_channels (
    id uuid primary key default uuid_generate_v4(),
    channel_title text not null,  -- matched case-insensitively against product_videos.channel_title; see the unique index below for how duplicates are prevented
    note text,                    -- optional free-text reason, e.g. "competitor retailer's own review channel"
    created_at timestamptz not null default now()
);

create unique index blocked_video_channels_channel_title_ci_idx
    on blocked_video_channels (lower(channel_title));

comment on table blocked_video_channels is 'Admin-curated list of YouTube channel names (e.g. a competitor retailer''s own review channel) whose videos must never be pushed into BigCommerce''s native Product Videos feature via src/bowlerdepot_video_sync (migration 020/021). Deliberately does NOT affect video_discovery, approval, or the video_reviews_summary rollup -- see this migration''s own header comment for why those stay untouched.';

commit;
