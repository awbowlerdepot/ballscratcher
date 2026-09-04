-- 023_product_article_images.sql
--
-- Al: "can we have it generate some images for the article from the
-- bowling ball images and using the article to give it some context" --
-- follow-up to 022_product_articles.sql's ball-review article feature.
-- Scoped via follow-up questions: generate BOTH an action/lifestyle hero
-- shot (the ball in motion on the lane, bowling.com-article-editorial
-- style) and a stylized product hero shot (an elevated, premium
-- product-photo-style rendering, not in motion) per article, and generate
-- both automatically in the same product_article_generator Lambda run
-- that produces the article text -- not a separate on-demand-only step --
-- for both the daily batch and the on-demand regenerate path.
--
-- Images are AI-generated content proper to the article itself (grounded
-- in the article's own generated text plus the product's real photo as
-- visual reference), not live product data -- unlike specs/comparison_table
-- (022's header comment), there's nothing to keep in sync by joining live,
-- so the generated image URLs are stored directly on product_articles
-- rather than derived at read time.
--
-- Same "whole row is one reviewed unit" convention product_articles
-- already uses for its text fields: there is no per-field/per-image
-- review UI, and a regenerate (product_article_generator.store_article)
-- resets the whole row -- including these image columns -- back through
-- status='pending' alongside the text, rather than only resetting text
-- and leaving a stale approved image live.
--
-- Storage follows image_processor/app.py's existing convention: PNG
-- objects in the same public-read IMAGE_BUCKET, raw
-- https://{bucket}.s3.amazonaws.com/{key} URLs (no CloudFront), but under
-- an "article-images/" prefix rather than image_processor's own
-- "product-images/" prefix, since these aren't a product's actual photos
-- and shouldn't be swept up in product_scraper's product-images/*
-- orphan-cleanup listing.
--
-- images_generated_at is tracked separately from the existing
-- generated_at (text) column because image generation is a second,
-- independently-fallible Bedrock call in the same run -- article text can
-- succeed and store even if the image step errors, and images_generated_at
-- staying null is how a partial failure is told apart from "not attempted
-- this run" without inspecting logs.

begin;

alter table product_articles
    add column action_shot_image_key text,     -- S3 key under article-images/{product_id}/, e.g. article-images/{product_id}/action-shot.png
    add column action_shot_image_url text,      -- https://{bucket}.s3.amazonaws.com/{key}
    add column product_shot_image_key text,     -- S3 key, e.g. article-images/{product_id}/product-shot.png
    add column product_shot_image_url text,
    add column images_generated_at timestamptz;

comment on column product_articles.action_shot_image_key is 'S3 key (IMAGE_BUCKET, article-images/ prefix) for the AI-generated action/lifestyle hero shot -- the ball in motion on the lane, generated using the product''s real photo as visual reference and the article''s own generated text as thematic context. Null if image generation has not yet succeeded for this row.';
comment on column product_articles.product_shot_image_key is 'S3 key (IMAGE_BUCKET, article-images/ prefix) for the AI-generated stylized product hero shot -- an elevated, premium rendering of the ball itself, not in motion. Null if image generation has not yet succeeded for this row.';
comment on column product_articles.images_generated_at is 'Set once the image-generation step runs and produces AT LEAST ONE of the two images -- not gated on both succeeding, since the two Bedrock image calls fail independently. Check action_shot_image_key/product_shot_image_key individually (either may be null) to see which one actually succeeded; a non-null generated_at with a null images_generated_at means article text generated successfully but neither image did.';

commit;
