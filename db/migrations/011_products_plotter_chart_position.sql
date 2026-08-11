-- 011_products_plotter_chart_position.sql
--
-- Al shared an existing interactive "Ball Motion Comparison" plotter he'd
-- built in another Cowork project (a single-page scatter chart: X axis =
-- oil the ball reads best on, 1 light -> 16 heavy; Y axis = ball motion
-- shape, 1 smooth -> 18 angular). Its positions were hand-digitized from
-- Brunswick's own published "Ball Motion Comparison Chart" PDF (Form
-- #0526-19, Jul/Aug 2026) -- a real, authoritative, but narrow dataset:
-- only 56 balls across the six brands that chart covers (Brunswick,
-- Hammer, Track, Ebonite, Radical, DV8), nowhere near this project's full
-- catalog. Al's own decision on the gap (asked directly): estimate the
-- rest of the catalog algorithmically from RG/DIFF/core/coverstock (see
-- src/public_api/service.py's estimate_oil_motion) rather than only ever
-- showing the curated 56 or building a full admin-editable-field workflow.
--
-- These two columns hold ONLY the authoritative, chart-sourced values --
-- null everywhere until scripts/backfill_plotter_chart_positions.py (a
-- one-time matching pass against the digitized dataset in
-- scripts/data/brunswick_chart_positions.json) runs. The algorithmic
-- estimate is never written back here; it's computed on read, on demand,
-- in public_api -- keeping "this position came from Brunswick's own
-- published chart" and "this position is our own heuristic guess"
-- structurally distinct rather than indistinguishable integers in the
-- same column. public_api's plotter endpoint reports which kind each
-- product got via an oil_motion_source field ('chart' | 'estimated') so
-- the frontend can render them differently (e.g. filled vs. outlined
-- marker) rather than implying false precision on the estimated ones.
--
-- No scraper touches these fields -- same "admin/curation-layer concern,
-- not raw scraped data" split as product_images' display_order/
-- is_thumbnail/is_visible (migration 010) and coverstocks.name -- set
-- only via the new admin_api endpoint (PATCH /products/{id}/plotter-
-- position) the backfill script calls.

begin;

alter table products add column oil_rating smallint;
alter table products add column motion_rating smallint;

alter table products add constraint products_oil_rating_range
    check (oil_rating is null or (oil_rating between 1 and 16));
alter table products add constraint products_motion_rating_range
    check (motion_rating is null or (motion_rating between 1 and 18));

comment on column products.oil_rating is 'Authoritative chart position (1-16, light->heavy oil) digitized from Brunswick''s own published Ball Motion Comparison Chart -- null unless scripts/backfill_plotter_chart_positions.py matched this product. Never written by a scraper or by public_api''s algorithmic estimate; see this migration''s header comment.';
comment on column products.motion_rating is 'Authoritative chart position (1-18, smooth->angular motion) digitized from Brunswick''s own published Ball Motion Comparison Chart -- null unless scripts/backfill_plotter_chart_positions.py matched this product. Never written by a scraper or by public_api''s algorithmic estimate; see this migration''s header comment.';

commit;
