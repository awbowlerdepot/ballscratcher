-- Runbook 6dd: announcement social posts + images per BALL, built from the
-- manufacturer's description and specs -- for new releases that have no
-- videos (so no article) yet. Al: "can we add the ability to generate
-- social posts and images from the manufacture description if it exists
-- so we can get social out as soon as they are announced before there are
-- videos to generate an article". Article-based posts (migration 042) are
-- unchanged; this is a separate, product-level set.
create table if not exists product_announcements (
    product_id                 uuid primary key references products(id) on delete cascade,
    social_posts               jsonb,          -- facebook, instagram, x, tiktok_hook, tiktok_caption
    social_posts_generated_at  timestamptz,
    link_url                   text,           -- the bowlerdepot.com listing used, or null ("coming soon")
    visual_theme               text,           -- derived from the name/description for the images
    image_status               text not null default 'idle'
                               check (image_status in ('idle', 'generating', 'ready', 'failed')),
    image_job_started_at       timestamptz,
    image_error                text,
    feed_image_key             text,           -- square 1:1 themed scene
    feed_image_url             text,
    product_image_key          text,           -- square 1:1 clean product shot
    product_image_url          text,
    images_generated_at        timestamptz,
    updated_at                 timestamptz not null default now()
);
