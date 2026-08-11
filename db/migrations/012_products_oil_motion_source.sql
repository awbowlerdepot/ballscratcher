-- 012_products_oil_motion_source.sql
--
-- Migration 011 shipped oil_rating/motion_rating with the plan "the
-- algorithmic estimate is never written back here; it's computed on read,
-- on demand, in public_api". Al's own direct follow-up after seeing that in
-- practice: "while i think that is a good approach it will cause for
-- potential inconsistencies, i would prefer for it to just back fill the
-- values once in the DB and then estimate on scrape if not set and then
-- they can be adjusted in the admin api to a value that is more accurate
-- if necessary". Recomputing estimate_oil_motion live on every GET
-- /products/plotter call meant the SAME product could report a different
-- estimated position on two different calls if any input (core_type from
-- a since-completed cores backfill, coverstock fields from a rescrape,
-- etc.) changed in between -- not a bug exactly, but not the stable,
-- pin-it-down behavior a real plotter page should have either.
--
-- So as of this migration, oil_rating/motion_rating are written ONCE and
-- read as-is from then on, from one of three places:
--   'chart'     -- scripts/backfill_plotter_chart_positions.py, digitized
--                  from Brunswick's own published chart (unchanged from
--                  migration 011).
--   'estimated' -- written automatically the first time a product is
--                  scraped with no plotter position yet (see every
--                  upsert_product across all five scraper Lambdas), or by
--                  admin_api.backfill_estimated_plotter_positions for
--                  whatever predates that hook. Same estimate_oil_motion
--                  heuristic as before, just persisted instead of
--                  recomputed on every read.
--   'manual'    -- an admin corrected an estimate to something more
--                  accurate via PATCH /products/{id}/plotter-position
--                  (Al's own "adjusted... to a value that is more
--                  accurate if necessary") -- the default source that
--                  endpoint writes when none is given.
--
-- This column is what makes "an admin's real correction" distinguishable
-- from "still just our own guess" after the fact -- oil_rating/
-- motion_rating alone can't tell those apart once both are just integers
-- in the same two columns. The tri-state CHECK plus the "both set or both
-- null together" constraint keeps oil_motion_source from ever drifting out
-- of sync with the two rating columns it describes.
--
-- Every write path (scrapers' new hook, backfill_estimated_plotter_
-- positions, set_plotter_position) guards on "where oil_rating is null" --
-- a rescrape or re-run backfill never clobbers a chart match or a manual
-- correction with a fresh estimate.

begin;

alter table products add column oil_motion_source text
    check (oil_motion_source in ('chart', 'estimated', 'manual'));

-- Data fix for whatever's already been through
-- scripts/backfill_plotter_chart_positions.py before this migration ran --
-- until this moment that script (via the old set_plotter_position, no
-- source concept yet) was the ONLY writer of oil_rating/motion_rating
-- (migration 011's own header comment: "No scraper touches these fields
-- ... set only via the new admin_api endpoint the backfill script
-- calls"), so any row that already has a rating is safely known to be a
-- chart match. Must run before the consistency CHECK below, or this
-- migration would fail validating existing rows against it.
update products set oil_motion_source = 'chart'
    where oil_rating is not null and oil_motion_source is null;

alter table products add constraint products_oil_motion_source_consistency
    check (
        (oil_rating is null and motion_rating is null and oil_motion_source is null)
        or
        (oil_rating is not null and motion_rating is not null and oil_motion_source is not null)
    );

comment on column products.oil_motion_source is 'How oil_rating/motion_rating (migration 011) got their value: chart (Brunswick''s own published chart), estimated (estimate_oil_motion heuristic, written once at scrape time or by admin_api.backfill_estimated_plotter_positions), or manual (an admin''s correction via PATCH /products/{id}/plotter-position). Null iff both rating columns are null -- see this migration''s header comment.';

commit;
