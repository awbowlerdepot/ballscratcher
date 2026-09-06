-- 029_product_articles_visual_theme.sql
--
-- Al: "the first set of images were amazing and then it started to
-- drift away from building the image placing the ball in an
-- environment that matched the name of the ball" -- follow-up to the
-- 2026-09-06 "people/venue elements drifting in" investigation and fix.
--
-- Root cause of THIS drift, traced through fetch_existing_article's own
-- pre-existing docstring caveat: visual_theme -- the 1-2 sentence,
-- name/branding-derived scene concept the article-generation model
-- invents (025_product_article_images_theme_driven_pipeline.sql, Al's
-- "background driven by the ball's name" ask) -- has NEVER been
-- persisted anywhere. It only ever existed as a field on the ONE
-- Bedrock article-text response that produced it. That was fine at v3
-- (images always generated in the same call as the text, so the theme
-- was always fresh) but stopped being fine once v7 (2026-09-05)
-- decoupled "Regenerate images" from "Regenerate text": an images-only
-- regenerate skips the Bedrock text call entirely and reuses the
-- EXISTING row's performance_summary/hook as image-prompt context
-- (generate_article_for_product's own v7 docstring) -- and _resolve_
-- visual_context's fallback chain (app.py) means every one of those
-- images-only runs quietly downgrades from the sharp, name-derived
-- theme ("a Fallout ball should evoke a wasteland") to generic review-
-- narrative prose ("reads early and hooks hard off the friction"),
-- which produces a much less thematic, more genre-generic scene prompt.
-- That's exactly the "first set was amazing, then it drifted" pattern
-- Al described -- every "Regenerate images" click after the first
-- generation lost the theme a little more.
--
-- Fix: persist visual_theme so it survives independently of whether the
-- text gets regenerated. A full text regenerate (regenerate_text=True)
-- writes a FRESH theme (the model re-derives it from the current
-- article draft each time, same as it always has). An images-only
-- regenerate (regenerate_text=False) now reads the PERSISTED theme back
-- via fetch_existing_article and reuses it verbatim, instead of falling
-- back to performance_summary/hook -- that fallback chain in _resolve_
-- visual_context stays in place as the safety net for the genuinely
-- theme-less case (an old article generated before this migration, or a
-- response that omitted the optional field), not as the everyday path
-- for a images-only regenerate.
--
-- Nullable, no default -- an existing article row from before this
-- migration simply has no persisted theme yet (falls back exactly as it
-- always has, same _resolve_visual_context chain, no backfill needed:
-- the next TEXT regenerate for that product will populate it going
-- forward).

begin;

alter table product_articles add column visual_theme text;

comment on column product_articles.visual_theme is 'The 1-2 sentence, name/branding-derived scene concept the article-generation model invents for this product (build_article_prompt''s optional visual_theme ask, migration 025) -- e.g. a ball named "Fallout" evoking a post-apocalyptic wasteland. Persisted as of migration 029 so an images-only regenerate (generate_article_for_product, regenerate_text=False) can reuse the SAME theme instead of falling back to the more generic performance_summary/hook text -- see this migration''s own header comment for the real-incident drift that caused (Al, 2026-09-06). Written fresh every time regenerate_text=True runs; left untouched by an images-only regenerate. Null for articles generated before this migration, or if the model omitted the optional field -- _resolve_visual_context''s existing fallback chain (performance_summary, then hook) still applies in that case.';

commit;
