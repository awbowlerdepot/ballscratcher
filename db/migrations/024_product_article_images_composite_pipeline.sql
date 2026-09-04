-- 024_product_article_images_composite_pipeline.sql
--
-- Al, after 023_product_article_images.sql shipped: "those images are not
-- very good at all, they need to be specific aspect ratio and add
-- backgrounds to them and just leave the ball as is. We can not alter the
-- ball in any way just add some contextual background and maybe other
-- balls with same name in background or similar balls etc."
--
-- 023's original mechanism (Stable Diffusion 3.5 Large image-to-image,
-- strength=0.6) regenerates the ENTIRE image through diffusion, including
-- the ball itself -- it can bias toward the reference photo but can never
-- guarantee the ball comes out pixel-identical, which is exactly what Al
-- ruled out. This migration makes NO schema change (023's key/url/
-- images_generated_at columns already carry a final composited PNG per
-- variant just fine) -- it only corrects those columns' comments, which
-- still described the old img2img mechanism, so the schema's own
-- documentation doesn't contradict how product_article_generator/app.py
-- actually generates these images as of this migration.
--
-- New mechanism (see app.py's own module docstring for the full research
-- trail): the product's real reference photo goes through Bedrock's
-- Stability AI "Remove Background" model first -- a segmentation cutout,
-- not a diffusion regeneration, so the ball's own pixels are carried
-- through untouched, only the background is stripped to transparent. A
-- new background scene is generated separately via plain text-to-image
-- (no ball in the prompt at all) at an explicit aspect_ratio -- 16:9 for
-- action_shot, 1:1 for product_shot, per Al's "specific aspect ratio"
-- ask. The untouched ball cutout is then pasted onto that generated
-- background with Pillow (composite_ball_on_background) -- alpha
-- compositing, not a second diffusion pass -- and THAT flattened PNG is
-- what gets stored under action_shot_image_key/product_shot_image_key,
-- same as before.
--
-- "maybe other balls with same name in background or similar balls" --
-- Al's follow-up answer when asked directly: skip for v1, but the
-- intended future shape is narrower than "maybe" reads here -- only when
-- 2+ products in the system have very similar names (the same
-- infer_sibling_products heuristic this module already uses for the
-- article text's comparison_table), OR when reviewers actually mention
-- another specific ball alongside this one in their video content. Not
-- implemented yet; no column here carries it. When it is built, it's
-- expected to reuse the same cutout+composite mechanism (real sibling
-- photos, background-removed and composited in smaller/behind), not an
-- AI-imagined ball -- consistent with "we can not alter the ball in any
-- way" applying to every ball placed in these images, not just the
-- primary one.

begin;

comment on column product_articles.action_shot_image_key is 'S3 key (IMAGE_BUCKET, article-images/ prefix) for the action/lifestyle hero shot -- a 16:9 image assembled by pasting the product''s real photo, cut out via Bedrock''s Stability AI Remove Background model (ball pixels untouched, only the background stripped), onto a separately text-to-image-generated background scene (composite_ball_on_background, plain Pillow alpha compositing, not a second diffusion pass over the ball). Null if image generation has not yet succeeded for this row.';
comment on column product_articles.product_shot_image_key is 'S3 key (IMAGE_BUCKET, article-images/ prefix) for the stylized product hero shot -- same cutout+generated-background+composite pipeline as action_shot_image_key, but at a 1:1 aspect ratio and an elevated studio-backdrop prompt instead of a bowling-lane one. Null if image generation has not yet succeeded for this row.';
comment on column product_articles.images_generated_at is 'Set once the image-generation step runs and produces AT LEAST ONE of the two images -- not gated on both succeeding. As of the remove-background+composite pipeline (see this migration), a failed Remove Background call on the reference photo skips BOTH images for the run (there is no ball cutout to composite into either variant without it) -- check action_shot_image_key/product_shot_image_key individually (either may still be null) to see which one actually succeeded.';

commit;
