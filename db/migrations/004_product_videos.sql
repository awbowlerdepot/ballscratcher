-- 004_product_videos.sql
--
-- Supports the new YouTube content-enrichment feature: video_discovery
-- searches YouTube for candidate videos per product and stores them here as
-- 'pending'; an admin approves/rejects each candidate via the admin API
-- (mirrors the exact approve/reject workflow review_queue already has --
-- see 001_init_schema.sql's review_status enum, reused as-is below rather
-- than inventing a parallel status type). Approving a row publishes it to
-- VideoSummarizeQueue, which video_summarizer consumes to fetch the
-- transcript and produce a Bedrock summary.
--
-- Deliberately NOT reusing review_queue itself: that table's shape
-- (field_name/current_value/proposed_value) is built around "here's a
-- proposed replacement for one column's value," which doesn't fit "here's
-- a candidate video, is this really about this product." A dedicated table
-- with its own domain-specific columns (title, channel, transcript,
-- summary, ...) is a cleaner fit than overloading review_queue's shape.

begin;

create table product_videos (
    id uuid primary key default uuid_generate_v4(),
    product_id uuid not null references products(id) on delete cascade,

    youtube_video_id text not null,
    title text,
    channel_title text,
    published_at timestamptz,
    thumbnail_url text,

    -- What search produced this candidate, and how confident the match
    -- heuristic was (see video_discovery.score_match) -- both kept for
    -- audit/debugging when an admin is deciding whether to approve.
    match_query text not null,
    match_confidence text not null,          -- 'high' | 'low', see video_discovery.score_match

    -- Populated by video_summarizer once a row is approved. transcript_note
    -- explains why transcript/summary are still null when captions
    -- couldn't be found (not every YouTube video has them) -- this is a
    -- disclosed, expected gap, not a retryable error, so a failure here
    -- must not land the SQS message in a DLQ forever.
    transcript text,
    transcript_note text,
    summary text,

    status review_status not null default 'pending',
    source text not null default 'youtube_search',

    created_at timestamptz not null default now(),
    resolved_at timestamptz,
    resolved_by text,

    unique (product_id, youtube_video_id)
);

create index idx_product_videos_status on product_videos(status) where status = 'pending';
create index idx_product_videos_product on product_videos(product_id);

commit;
