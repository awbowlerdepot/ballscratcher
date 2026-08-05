-- 007_cores_table.sql
--
-- Al noticed core name was never being captured, even though every one of
-- the four product scrapers already parses it (product_scraper's "Core"
-- spec field, commercebuild's "Weight Block"/symmetry fields, woocommerce's
-- "Bowling Ball Core Name" attribute, netsuite's "Weight Block" field) --
-- parsed["core_name"] was computed on every scrape and then silently
-- dropped, since upsert_product never referenced it. Multiple named
-- products can share one physical core (his example: DV8's Collision core
-- is used by six differently-named balls -- Intense Collision, Severe
-- Collision, Wicked Collision, Violent Collision, Brutal Collision, and
-- Collision itself), so this needs to be a real many-products-to-one-core
-- relationship, not a free-text column repeated on every product row.
--
-- migration 001 already defined exactly this shape as `ball_families`
-- (brand-scoped name + core_name + core_type, with products.family_id
-- referencing it) but it was never wired into any scraper's upsert
-- logic -- family_id has been null on every products row since the schema
-- was created. Rather than leave that unused table in place and add a
-- second, overlapping one, this repurposes it: renamed to `cores`, and the
-- redundant `name`/`core_name` pair (nothing ever distinguished them,
-- since nothing ever populated either) collapsed into a single `name`
-- column -- the core's own name, e.g. "Collision", "Portal X", "Fury".
-- core_type and release_era carry over unchanged; the latter is still
-- unused by any scraper today but harmless to keep.

begin;

alter table ball_families rename to cores;
alter table cores drop column core_name;
alter table cores rename constraint ball_families_brand_id_name_key to cores_brand_id_name_key;

alter table products rename column family_id to core_id;
alter index idx_products_family rename to idx_products_core;

commit;
