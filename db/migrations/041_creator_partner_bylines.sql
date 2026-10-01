-- 041_creator_partner_bylines.sql
--
-- First-person video articles under the creators' own byline, for
-- partner channels only. Al, on the first Bowling Tips article: "the
-- perspective of the article is off for the brad and kyle video. i think
-- it should be written by brad and kyle as the authors and not as a third
-- person reviewing the video" -- confirmed Brad and Kyle are BowlerDepot
-- partners and fine with it, byline "By Brad and Kyle".
--
-- An AI-written article presented in someone's own voice and name is
-- only OK when they've agreed to it, so this is an explicit allow-list,
-- not a default: a video from any channel NOT in creator_partners keeps
-- the neutral third-person "BowlerDepot Team" article.
--
-- creator_partners: matched case-insensitively on the YouTube channel
-- title (same convention as blocked_video_channels). author_name is what
-- the byline and the article's structured data say.
--
-- product_articles.author_name: set by product_article_generator when a
-- video article is written in a partner's voice; null = "BowlerDepot
-- Team" (every ball article, and non-partner video articles).

begin;

create table creator_partners (
    id uuid primary key default uuid_generate_v4(),
    channel_title text not null,
    author_name text not null,
    note text,
    created_at timestamptz not null default now()
);

create unique index idx_creator_partners_channel_title on creator_partners (lower(channel_title));

insert into creator_partners (channel_title, author_name, note)
values ('Brad and Kyle', 'Brad and Kyle', 'BowlerDepot partners -- agreed to first-person articles under their byline (2026-09-30).');

alter table product_articles add column author_name text;

comment on table creator_partners is 'YouTube channels whose videos get first-person Learn articles under the creators'' own byline (migration 041). Allow-list on purpose: only creators who have agreed to it.';
comment on column product_articles.author_name is 'Byline for a video article written in a partner creator''s voice (migration 041); null = "BowlerDepot Team".';

commit;
