-- 042_article_social_posts.sql
--
-- Ready-to-paste social media copy per Learn article, edited in the
-- admin SPA. Al: "for the bowling ball articles, in the admin ui can we
-- add a social media post section that has copy to put for text on
-- social media posts." Facebook, Instagram, X, and TikTok/Reels;
-- generated on demand (a button in the article preview -- only for
-- articles actually being promoted), editable, with copy buttons.
--
-- social_posts is a flat object of strings so every field is one
-- editable text box: {"facebook", "instagram", "x", "tiktok_hook",
-- "tiktok_caption"}. Null until first generated. Edits overwrite it;
-- regenerating replaces it (the admin UI confirms before discarding
-- edits). social_posts_generated_at is the last GENERATION, not edit.

begin;

alter table product_articles add column social_posts jsonb;
alter table product_articles add column social_posts_generated_at timestamptz;

comment on column product_articles.social_posts is 'Social media copy for this article (migration 042): {"facebook", "instagram", "x", "tiktok_hook", "tiktok_caption"} strings. Generated on demand by admin_api.generate_article_social_posts, editable in the admin SPA. Null until generated.';

commit;
