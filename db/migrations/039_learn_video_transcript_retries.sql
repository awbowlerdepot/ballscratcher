-- 039_learn_video_transcript_retries.sql
--
-- Real incident, 2026-09-30: the first Bowling Tips video (kdeshRiNIT0,
-- Brad and Kyle) failed twice on the Pi with
-- video_player_error_transcript_unavailable. Debug evidence pulled off
-- the Pi: the "In this video" panel opened and the Transcript tab was
-- selected, but its spinner never resolved, and the player showed
-- "Something went wrong" with no video source -- YouTube declining to
-- serve that session, not a missing transcript (the video has English
-- auto captions). It's intermittent (the same Pi got 5/5 that morning),
-- so two fixes, both without trying to get around YouTube's checks:
--
-- 1. transcript_attempts: a player-error result is retried on later
--    daily runs, up to admin_api.LEARN_VIDEO_MAX_TRANSCRIPT_ATTEMPTS, at
--    least LEARN_VIDEO_RETRY_MIN_HOURS apart (so a manual Pi run right
--    after the 7am one doesn't burn an attempt). Other notes
--    (no_captions_available, ...) are still final after one try. The
--    retry rule lives in admin_api's needs_transcript query, so the Pi
--    needs no update.
-- 2. transcript_source: 'pi' (the fetcher) or 'manual' (an admin pasted
--    it from YouTube's "Show transcript" on the Learn Videos page), so
--    phase 3 and future debugging can tell them apart.
--
-- Existing rows: attempts backfilled to 1 where a fetch already happened.

begin;

alter table learn_videos add column transcript_attempts int not null default 0;
alter table learn_videos add column transcript_source text;

update learn_videos set transcript_attempts = 1 where transcript_fetched_at is not null;
update learn_videos set transcript_source = 'pi' where transcript is not null;

-- 038's partial index matched only never-attempted rows; retry-eligible
-- rows have a note set, so widen it to "no transcript yet".
drop index idx_learn_videos_needs_transcript;
create index idx_learn_videos_needs_transcript on learn_videos(created_at) where transcript is null;

comment on column learn_videos.transcript_attempts is 'Pi fetch attempts so far (migration 039). Player-error results are retried on later daily runs until this reaches admin_api.LEARN_VIDEO_MAX_TRANSCRIPT_ATTEMPTS; admin Retry resets it to 0.';
comment on column learn_videos.transcript_source is '''pi'' (fetched by the Pi transcript fetcher) or ''manual'' (pasted by an admin). Null while there is no transcript.';

commit;
