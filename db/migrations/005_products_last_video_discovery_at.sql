-- 005_products_last_video_discovery_at.sql
--
-- Fixes a real rotation gap found in production: video_discovery's default
-- ({} / brand_id) scope picks products via
--     order by p.updated_at desc limit :max_products
-- Nothing in this pipeline ever touches products.updated_at, so repeated
-- {} invocations (the documented "run it once a day to cover the whole
-- catalog under the ~90/day search quota" pattern -- see
-- DEPLOY_RUNBOOK.md 6i) were re-selecting the exact same top-N products
-- every single time, never progressing to the rest of the catalog.
--
-- last_video_discovery_at records when video_discovery actually completed
-- a search.list call for a product (see video_discovery.mark_product_
-- searched -- set only on the success path, not when a search errors, so
-- a transient failure like quota exhaustion doesn't falsely count as "this
-- product was covered" and push it to the back of the line). Ordering by
-- this column ascending, nulls first, means never-searched products always
-- come before any previously-searched one, and once the whole catalog has
-- been searched at least once, it naturally cycles back to the
-- least-recently-searched products -- which is also the right behavior
-- long term, since new review videos get posted after a ball's initial
-- release.

begin;

alter table products add column last_video_discovery_at timestamptz;

commit;
