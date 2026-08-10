-- 010_product_images_ordering_thumbnail_visibility.sql
--
-- Al, looking ahead to an eventual customer-facing site: "once we
-- actually have a customer facing site we will want to order the
-- images, set a thumbnail image and control visibility." Nothing on
-- product_images today captures any of that -- rows only ever get
-- ordered by whatever order Postgres happens to return them in
-- (effectively insertion order), there's no notion of "the" thumbnail
-- vs. just whichever image a consumer picks first, and every image a
-- scraper pulls down is implicitly visible forever with no way to hide
-- a bad/duplicate/irrelevant one without deleting the row outright (which
-- would just have it re-inserted on the next rescrape anyway).
--
-- Three additive columns, admin-editable via admin_api/admin-site (this
-- migration only adds the columns + sensible backfilled defaults for
-- every image already in the DB -- no scraper touches any of these three
-- fields, this is purely an admin/display-layer concern, same "raw
-- scraped data vs. admin-curated data" split as coverstock_name vs.
-- coverstocks.name):
--
-- display_order: integer, defaults to each product's images in their
-- existing (insertion) order, 0-based, so nothing visibly reshuffles the
-- moment this migration runs -- reordering only happens when an admin
-- explicitly acts on it via the new reorder endpoint.
--
-- is_thumbnail: boolean, defaults true on one row per product (the first
-- 'main'-typed image if one exists, falling back to the first image by
-- display_order otherwise -- 'main' is already the highest-signal
-- image_type every scraper produces for the primary product shot) so
-- every existing product has a sensible thumbnail out of the box rather
-- than requiring a manual pass across the whole catalog before a
-- customer-facing site could rely on it being set. Enforced to at most
-- one true per product_id via a partial unique index (`create unique
-- index ... where is_thumbnail`) -- Postgres allows any number of rows
-- where a unique-indexed column is null/false and only enforces
-- uniqueness among the true rows, so this is the standard "at most one
-- flagged row per group" pattern, not a same product_id + is_thumbnail
-- literal unique constraint) -- an admin setting a new thumbnail should
-- unset the old one atomically, handled in admin_api's
-- update_product_image rather than left to a race between two separate
-- UPDATEs.
--
-- is_visible: boolean, not null default true -- every existing image
-- stays visible (this migration changes nothing about current behavior,
-- since no customer-facing site consumes this yet); an admin can flip it
-- off per row later without deleting anything, so a bad pull is
-- correctable without losing provenance (source_url/stored_url) or
-- triggering a re-download on the next scrape (upsert_product's on-
-- conflict path only ever updates image_type, never touches is_visible).

begin;

alter table product_images add column display_order integer;
alter table product_images add column is_thumbnail boolean not null default false;
alter table product_images add column is_visible boolean not null default true;

-- Backfill display_order: existing (insertion) order per product, via
-- created_at then id as the tiebreaker for rows created in the same
-- transaction/instant.
with ordered as (
    select id, row_number() over (partition by product_id order by created_at, id) - 1 as rn
    from product_images
)
update product_images pi
set display_order = o.rn
from ordered o
where pi.id = o.id;

alter table product_images alter column display_order set not null;

-- Backfill is_thumbnail: one 'main'-typed image per product if one
-- exists, else the first image by the display_order just computed.
with candidate as (
    select distinct on (product_id) id
    from product_images
    order by product_id,
             (image_type = 'main') desc,  -- true sorts before false, so a 'main' row wins when one exists
             display_order
)
update product_images pi
set is_thumbnail = true
from candidate c
where pi.id = c.id;

create unique index idx_product_images_one_thumbnail_per_product
    on product_images (product_id) where is_thumbnail;

create index idx_product_images_display_order on product_images (product_id, display_order);

commit;
