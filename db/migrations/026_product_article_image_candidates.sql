-- 026_product_article_image_candidates.sql
--
-- v4 of the article-images feature (see 023/024/025's own header comments
-- for the full v1/v2/v3 history). v3 (a single plain Stable Diffusion
-- image-to-image call per variant) shipped and Al sent back real
-- screenshots: the ball's own surface graphics/logo text came out garbled
-- ("Voiid"/"Bowing" instead of the real brand text), and the generated
-- backgrounds were poor quality -- verbatim, "looks like dropping the
-- cutout was a bad idea... The ball needs to be mostly untouched... The
-- background couldn't be worse." His concrete ask: "The ball needs to be
-- masked to get rid of anything that is in the image now, white mostly.
-- Then that needs to be places in a scene based on its name. The fallout
-- is a perfect example of this" -- pointing at the same "Fallout"
-- reference image he sent during the v3 pivot (see 025's own header
-- comment) as the quality bar to hit. He then sent a THIRD example, from
-- a tool called yeri.ai, built from the prompt "generate a scene depicting
-- a Fallout environment and then place the ball in the reference image
-- into that scene, match the color scheme of the reference image" -- a
-- visibly better result than anything Stability/Bedrock had produced so
-- far (real ambient lighting/shadow on the ball, no hard-pasted edge).
--
-- Researched (not assumed) what's actually behind that kind of result:
-- Google's Gemini 2.5 Flash Image model ("Nano Banana") is documented by
-- Google itself as built for exactly this -- "put an object into a scene,
-- restyle a room with a color scheme, fuse images with a single prompt" --
-- a genuinely different capability from Stability's own models on Bedrock,
-- which only do single-image text-to-image/image-to-image, no native
-- "insert this reference subject into a newly generated scene" operation.
-- Confirmed Gemini image models are NOT available on Bedrock as of this
-- writing (checked AWS's own current model catalog) -- reaching this model
-- means calling Google's own Gemini API directly (a new outbound HTTPS
-- call + a new API key secret, not an AWS-internal one), priced around
-- $0.039/image. Asked Al directly whether to add a non-Bedrock provider
-- for this: yes, alongside the existing Bedrock/Stability path, not
-- instead of it -- so both are real candidate SOURCES now, not just
-- multiple background variations from one model.
--
-- So v4 does two things: (1) revives v2's actual Bedrock/Stability
-- mechanism (Remove Background cutout + generated scene + Pillow
-- composite -- the ball's own pixels carried through untouched) as ONE
-- candidate per shot, and (2) adds Gemini 2.5 Flash Image -- called
-- directly with the product's real (unmodified) reference photo plus a
-- single integrated prompt asking it to place that exact ball into a
-- newly generated, name/theme-grounded scene -- as the other candidates
-- per shot (see src/product_article_generator/app.py's build_gemini_
-- scene_prompt/call_gemini_for_image). Asked directly how selection
-- should work: Al chose a per-article candidate picker, not a system-wide
-- default -- every run generates 3 candidates per shot (2 Gemini + 1
-- Stability composite, see app.py's NUM_GEMINI_CANDIDATES/NUM_STABILITY_
-- CANDIDATES), all stored, and an admin picks the best one in the
-- Articles review tab rather than the pipeline silently committing to
-- whichever came out first.
--
-- This is why a NEW TABLE, not just more columns on product_articles:
-- product_articles.action_shot_image_key/url/product_shot_image_key/url
-- (023) stay exactly as they are and keep meaning "the currently LIVE
-- image for this variant" -- public_api and everything else that already
-- reads those four columns needs zero changes. This table holds every
-- candidate that was ever generated for an article (including the one
-- currently live, which is the row with is_selected=true for that
-- variant), from EITHER provider, so an admin can revisit and switch
-- their pick later without re-running generation, and so a future
-- "generate more options" action has somewhere to append new candidates
-- without disturbing old ones.
--
-- No status/review workflow of its own on this table -- picking a
-- candidate is a lightweight admin action (like reordering product
-- images, see 013's own precedent), not a separate approve/reject queue.
-- The article's own product_articles.status (pending/approved/rejected)
-- still gates public exposure of the whole row, images included, same as
-- before.

begin;

create table product_article_image_candidates (
    id uuid primary key default uuid_generate_v4(),
    article_id uuid not null references product_articles(id) on delete cascade,
    variant text not null check (variant in ('action_shot', 'product_shot')),
    model_id text not null,       -- which model actually produced this candidate, e.g. 'gemini-2.5-flash-image' or 'stability.sd3-5-large-v1:0' -- now a real, meaningful distinction since v4 mixes two different providers, not just a config detail
    image_key text not null,      -- S3 key, e.g. article-images/{product_id}/action_shot_2.png (candidate index suffixed, unlike 023's un-suffixed single-image key convention)
    image_url text not null,      -- https://{bucket}.s3.amazonaws.com/{key}
    seed integer,                 -- the Stable Diffusion seed that produced this candidate's background -- Stability-only; null for Gemini candidates, since Gemini's image model does not support a reliable/reproducible seed parameter (confirmed via research before building, not assumed)
    is_selected boolean not null default false,  -- exactly one true per (article_id, variant) at a time -- see the partial unique index below
    created_at timestamptz not null default now()
);

create index product_article_image_candidates_article_id_idx on product_article_image_candidates (article_id);

-- Enforces "exactly one selected candidate per article+variant" at the DB
-- level, not just in application code -- admin_api.select_article_image_
-- candidate flips the old selected row to false and the new one to true in
-- the same transaction (see that function's own docstring), but a partial
-- unique index catches any future bug that tries to leave two selected (or
-- silently relies on ordering) rather than corrupting product_articles'
-- own image columns with an ambiguous "which one is live" state.
create unique index product_article_image_candidates_one_selected_idx
    on product_article_image_candidates (article_id, variant)
    where is_selected;

comment on table product_article_image_candidates is 'Every AI-generated image candidate ever produced for an article''s action_shot/product_shot, from either Gemini 2.5 Flash Image or Stability/Bedrock (see model_id), including the one currently live (product_articles.action_shot_image_key/product_shot_image_key mirrors whichever candidate row has is_selected=true for that variant). Added 026_product_article_image_candidates.sql for the v4 image pipeline (see this migration''s own header comment) so an admin can pick among several generated options -- across providers, not just across background variations of one model -- instead of the pipeline silently committing to whichever one generated first.';
comment on column product_article_image_candidates.model_id is 'Which model produced this candidate -- ''gemini-2.5-flash-image'' (Google''s own API, called directly since Gemini image models are not on Bedrock) or a Stability modelId like ''stability.sd3-5-large-v1:0'' (Bedrock, this stack''s existing provider). Kept as a real column, not inferred from image_key naming, since which provider is actually best for this task is an open empirical question this whole candidate-picker feature exists to let Al answer by comparing.';
comment on column product_article_image_candidates.seed is 'The Stable Diffusion seed used for this candidate''s background generation -- Stability-only. Null for Gemini candidates: Gemini''s image model does not expose a reliable/reproducible seed parameter (confirmed via research, not assumed), so candidate diversity there comes from prompt-phrasing variation and the model''s own inherent stochasticity instead.';
comment on column product_article_image_candidates.is_selected is 'Exactly one true per (article_id, variant) -- enforced by product_article_image_candidates_one_selected_idx, not just application logic. The selected candidate''s image_key/image_url are mirrored onto product_articles.action_shot_image_key/url (or product_shot_*) so every other part of this codebase (public_api, admin-site''s non-candidate views) keeps reading those four columns unchanged.';

commit;
