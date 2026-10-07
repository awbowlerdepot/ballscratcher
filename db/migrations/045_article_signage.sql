-- Migration 045: in-store signage per ball article (DEPLOY_RUNBOOK.md 6cn).
--
-- Al: "can there just be a generate button on the ball on the admin site so
-- we can pick and choose" -- signage for the stores' piSignage screens is
-- made per ball, on demand, from the article preview in admin:
--   1. Generate -> 3 native 9:16 "signage shot" stills (Gemini, the
--      article's visual_theme) + tagline options (Bedrock Haiku)
--   2. pick a still and a tagline (editable)
--   3. Animate -> Veo 3.1 Fast clip from the still, made into a seamless
--      loop, plus where the ball sits in the frame (for the callout lines)
--   4. Approve -> the public signage page serves it
--   5. Render MP4 -> overlay composited onto the loop, for piSignage
--
-- One row per article (it's a live working set, not a history): a new
-- Generate replaces the stills/tagline options; picks and the clip stay
-- until replaced. status is the job marker the admin polls -- same
-- "status on the owning row" pattern as learn_videos' article generation.
-- The product's own article must be a BALL article (product_id not null);
-- that's enforced in admin_api, not here.

begin;

create table article_signage (
  article_id uuid primary key references product_articles(id) on delete cascade,
  status text not null default 'idle'
    check (status in ('idle', 'generating', 'stills_ready', 'animating', 'rendering', 'ready', 'failed')),
  job_started_at timestamptz,
  error text,

  -- [{"key": s3 key, "url": public url}, ...] from the latest Generate
  still_candidates jsonb not null default '[]'::jsonb,
  selected_still_key text,
  selected_still_url text,

  -- ["Talons find the pocket.", ...] from the latest Generate
  tagline_options jsonb not null default '[]'::jsonb,
  tagline text,

  -- Veo output (raw) and the seamless loop the page/renderer use
  raw_clip_key text,
  clip_key text,
  clip_url text,
  clip_generated_at timestamptz,
  -- ball position in the 9:16 frame: center x/y as fractions of width/
  -- height, radius as a fraction of width
  ball_x real,
  ball_y real,
  ball_r real,

  approved boolean not null default false,
  approved_at timestamptz,
  approved_by text,

  mp4_key text,
  mp4_url text,
  mp4_rendered_at timestamptz,
  -- the drilled price drawn into that MP4, so admin can flag a stale one
  mp4_price numeric(10, 2),

  updated_at timestamptz not null default now()
);

comment on table article_signage is
  'In-store 9:16 signage per ball article (runbook 6cn): still candidates, tagline, looping Veo clip, ball position, approval, rendered MP4, and the admin job status.';

commit;
