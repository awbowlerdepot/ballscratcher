-- 040_video_articles.sql
--
-- Phase 3 of Bowling Tips: Learn articles written from a learn_videos
-- transcript instead of about a ball. Al: "using a similar workflow that
-- we have for balls it will generate an article with that video inline"
-- -- a Generate Article button on the Learn Videos page, AI images same
-- as ball articles, and an intro / sections / key takeaways / FAQ /
-- bottom-line shape.
--
-- One table for both kinds rather than a parallel video_articles table,
-- so the admin review queue, Learn index, category counts, slugs, and
-- image-candidate tooling cover both. Exactly one of product_id /
-- learn_video_id is set (product_articles_one_subject). Ball-only
-- columns (performance_summary, pros/cons, who_should_*, comparison_
-- table, sibling_product_ids, buying_tips) stay at their defaults on a
-- video article; video articles use the new sections / key_takeaways,
-- and hook / verdict / faq / title / images like ball articles.
--
-- product_id's unique constraint stays (nulls don't collide), and
-- learn_video_id gets its own, so each video has at most one article --
-- the generator upserts on it.
--
-- Generation status for a video article lives on learn_videos itself
-- (article_generation_started_at / _mode), same "live marker, cleared in
-- a finally" design as 034's product_article_generation_status table.

begin;

alter table product_articles alter column product_id drop not null;

alter table product_articles
    add column learn_video_id uuid unique references learn_videos(id) on delete cascade;

alter table product_articles
    add constraint product_articles_one_subject
    check ((product_id is null) <> (learn_video_id is null));

alter table product_articles add column sections jsonb not null default '[]'::jsonb;
alter table product_articles add column key_takeaways jsonb not null default '[]'::jsonb;

alter table learn_videos add column article_generation_started_at timestamptz;
alter table learn_videos add column article_generation_mode text;

comment on column product_articles.learn_video_id is 'Set for an article written from a learn_videos transcript (migration 040), instead of product_id. Exactly one of the two is set (product_articles_one_subject).';
comment on column product_articles.sections is 'Video articles (migration 040): [{"heading": ..., "body": ...}] -- the article body. [] for ball articles.';
comment on column product_articles.key_takeaways is 'Video articles (migration 040): short bullet strings. [] for ball articles.';
comment on column learn_videos.article_generation_started_at is 'Set while product_article_generator is generating this video''s article (migration 040), cleared when it finishes or fails. Same live-marker design as product_article_generation_status.';

commit;
