-- 035_products_finish_category.sql
--
-- Al, after the 2026-09-11 fix telling Gemini not to alter a ball's
-- surface finish in generated article images (see build_gemini_scene_
-- prompt's own "REAL INCIDENT (2026-09-11, Al)" docstring): "are we
-- capturing the finish for these balls?" Investigation found products.
-- factory_finish IS captured by every scraper already, but it's a raw
-- manufacturer-specific string ("500/1000/2000 Siaair Micro Pad",
-- "5000 Grit LSS", "800 Abranet(R), 1000, 2000 Abralon(R) Power House
-- Factory Finish Polish") -- never normalized into anything resembling
-- a matte/satin/glossy sheen category, and never fed into the image
-- generator's prompt at all (that prompt currently relies entirely on
-- Gemini visually inferring sheen from the reference photo, with zero
-- textual hint). Al, on hearing that different manufacturers' pads/
-- grits aren't directly comparable: "if you think we can get that as
-- structured that would be great but no all manufactures use the same
-- surfacing pads or polishes... they could be mapped as similar but
-- they are not 100% the same" -- explicitly asking for an approximate,
-- honestly-caveated bucketing, not false precision.
--
-- Domain research (Abralon/Siaair Micro Pad/Abranet are foam-backed
-- mechanical sanding discs, not liquids) grounds the three buckets this
-- column holds:
--   'dull'     -- sanded only, grit below ~3000 (the common "box finish"
--                 range, e.g. 500-2000) -- reads as a dull/hazy surface.
--   'satin'    -- sanded only, but at very fine grit (>=3000, e.g. 3000/
--                 5000 grit) -- fine-enough abrasion starts producing a
--                 visible sheen on its own, without any polish step, but
--                 short of true gloss.
--   'polished' -- the raw text mentions a polish/compound step (Royal
--                 Compound, Factory Finish Polish, etc.) -- a genuinely
--                 distinct liquid-finishing step that produces real
--                 gloss/shine beyond what sanding alone achieves, per
--                 bowling-industry sources (Storm, Brunswick, ball-
--                 maintenance retailers).
-- NULL means "no factory_finish captured to classify from" (unknown),
-- same NULL-means-unknown convention as core_id/coverstock_id.
--
-- Known, deliberately-unaddressed caveat (documented rather than
-- silently ignored, since Al explicitly cares about honesty over false
-- precision here): a pearl coverstock's own mica-like additive can make
-- a ball look shinier than its finish_category alone would suggest,
-- independent of the sanding/polish step -- this column classifies the
-- STATED FACTORY FINISH PROCESS, not the ball's actual final appearance.
-- product_article_generator's prompt (see its own docstring) surfaces
-- this caveat directly to Gemini alongside the classification, and
-- treats the reference photo itself as the final authority when the two
-- disagree.
--
-- No backfill in this migration -- see the new POST /admin/backfill-
-- finish-categories admin_api route and scripts/backfill_finish_
-- categories.py, same "migration adds the column, a separate one-shot
-- admin-triggered backfill fills it" split this project already used
-- for 005/last_video_discovery_at.

begin;

alter table products add column finish_category text
    check (finish_category in ('dull', 'satin', 'polished'));

comment on column products.finish_category is 'Coarse sheen bucket (dull/satin/polished) derived from products.factory_finish''s raw manufacturer text via admin_api.classify_factory_finish (035_products_finish_category.sql) -- NULL means no factory_finish to classify from. An approximate mapping, not a claim that all manufacturers'' pads/grits are equivalent (Al: "they could be mapped as similar but they are not 100% the same"). Overridable via the review-queue correction mechanism (PRODUCT_UPDATABLE_FIELDS) same as any scraped field, since this is a derived value with no "rescrape" to fall back on if the classifier gets a specific ball wrong.';

commit;
