-- 038_learn_videos.sql
--
-- Standalone YouTube videos filed under a Learn category -- phase 2 of
-- Bowling Tips (see DEPLOY_RUNBOOK.md 6bo/6bp). Al: "we can add videos
-- from youtube there and then using a similar workflow that we have for
-- balls it will generate an article with that video inline." An admin
-- pastes a URL on the admin SPA's Learn Videos page; admin_api looks up
-- the video's details via YouTube's videos.list and inserts a row here;
-- the Pi transcript fetcher fills in transcript/transcript_note on its
-- daily run (same residential-browser path product_videos use, since
-- YouTube blocks caption fetches from AWS). Phase 3 generates an article
-- from each row.
--
-- Why not product_videos: that table is a ball's review-video candidates
-- (product_id not null, search-then-approve workflow, match_confidence,
-- BigCommerce video sync). These are admin-picked videos about a topic,
-- not a product, with no discovery/approval step.
--
-- transcript/transcript_note follow product_videos' conventions exactly
-- (see home_transcript_fetcher.needs_transcript): both null = not tried
-- yet; transcript_note set = a real attempt happened (e.g.
-- 'no_captions_available'), so the Pi doesn't retry it daily. Clearing
-- both (admin "Retry transcript") queues it again.
--
-- category_id on delete restrict: admin_api.delete_category refuses a
-- category that still has videos, this is the backstop.

begin;

create table learn_videos (
    id uuid primary key default uuid_generate_v4(),
    youtube_video_id text not null unique,
    category_id uuid not null references categories(id) on delete restrict,

    -- From YouTube videos.list at add time (snippet + contentDetails).
    title text,
    channel_title text,
    channel_id text,
    published_at timestamptz,
    thumbnail_url text,
    duration_seconds int,
    description text,

    transcript text,
    transcript_note text,
    transcript_fetched_at timestamptz,

    added_by text,
    created_at timestamptz not null default now()
);

create index idx_learn_videos_category_id on learn_videos(category_id);

-- The Pi fetcher's one query shape: "not yet attempted".
create index idx_learn_videos_needs_transcript on learn_videos(created_at)
    where transcript is null and transcript_note is null;

comment on table learn_videos is 'Admin-added YouTube videos filed under a Learn category (migration 038) -- the source material for video-based Learn articles (Bowling Tips). Not tied to a product; see product_videos for ball review-video candidates.';
comment on column learn_videos.transcript_note is 'Same convention as product_videos.transcript_note: null + null transcript = not attempted yet (the Pi fetcher picks it up); set = a real attempt happened, so it is not retried automatically.';

commit;
