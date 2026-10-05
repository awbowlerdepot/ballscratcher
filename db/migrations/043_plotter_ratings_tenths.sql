-- Migration 043: products.oil_rating / motion_rating to one decimal
-- (DEPLOY_RUNBOOK.md 6by).
--
-- Al: "can we make these accurate down to on tenth so that we can support
-- zooming in on the motion plotter? default is to round to the whole
-- number". Since 011 these were smallint, which threw away everything the
-- v3 estimator (6bx) computes below the integer -- so two balls the model
-- separates by 0.4 oil landed on the same dot. numeric(3,1) holds 1.0-18.0
-- at 0.1 resolution; the plotter page still rounds to the whole-number
-- grid by default, and finer positions only matter once you zoom.
--
-- smallint -> numeric is a lossless cast (6 stays 6.0), so every chart,
-- manual and estimated row keeps its exact value; the button-only re-
-- estimate (Admin -> Batch Jobs) is what fills in the tenths for
-- estimated rows. The 011 range CHECKs and 012's consistency CHECK are
-- unchanged and keep working on numeric. Scrapers still write v2's whole
-- numbers into a null position, which numeric accepts as-is.

begin;

alter table products
    alter column oil_rating type numeric(3,1),
    alter column motion_rating type numeric(3,1);

comment on column products.oil_rating is 'Plotter oil position, 1.0-16.0 light->heavy, to 0.1 (migration 043). oil_motion_source says where it came from: chart (digitized from a published manufacturer chart), manual (admin correction), or estimated (v3 estimator, recalculated only by the admin Re-estimate button).';
comment on column products.motion_rating is 'Plotter motion position, 1.0-18.0 smooth->angular back-end motion, to 0.1 (migration 043). See oil_rating for oil_motion_source.';

commit;
