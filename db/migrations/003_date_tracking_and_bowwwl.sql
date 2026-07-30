-- 003_date_tracking_and_bowwwl.sql
--
-- Two additions, both needed for the bowwwl.com / BowlerDepot reconciliation
-- work: explicit lifecycle date tracking on `products`, and a bowwwl_products
-- match-cache table mirroring the existing bowlerdepot_products one.
--
-- ---------------------------------------------------------------------
-- Lifecycle dates on products
-- ---------------------------------------------------------------------
--
-- Four concepts, each with a real (or honestly absent) data source --
-- deliberately not treated as interchangeable:
--
--   first_seen_at    -- SYSTEM-OBSERVED. The first time this pipeline's own
--                        discovery/scraping found this product. Backfilled
--                        from `created_at` below, because `created_at`
--                        already served exactly this purpose by accident:
--                        no upsert_product() in any scraper (craft_cms,
--                        woocommerce, netsuite) has ever included created_at
--                        in its "on conflict do update set" clause, so it's
--                        been an accurate, untouched first-insert timestamp
--                        all along. Given its own column now so that fact
--                        doesn't depend on every future scraper author
--                        remembering not to touch created_at, and so the
--                        column name is self-documenting.
--
--   release_date      -- MANUFACTURER-PUBLISHED. Already existed (see
--                        001_init_schema.sql) but nothing has ever written
--                        to it -- every scraper captures release_date_raw
--                        as free text and stops there. This migration adds
--                        no new column for this; the code-side fix (parsing
--                        each platform's real raw format into an actual
--                        date and persisting it) ships alongside this
--                        migration in the same pass.
--
--   discontinued_detected_at -- SYSTEM-OBSERVED. The first time this
--                        pipeline saw a product's status flip from
--                        'current' to 'retired' on re-scrape. This is a
--                        detection timestamp, not the manufacturer's actual
--                        discontinuation date -- there's necessarily some
--                        lag against reality bounded by how often each
--                        platform's discovery function runs. Set once (via
--                        a CASE expression in each upsert_product's ON
--                        CONFLICT clause, comparing the existing row's
--                        status against the incoming one) and left alone on
--                        subsequent re-scrapes; cleared if status reverts to
--                        'current' (a real ball, e.g. a "bring-back"
--                        release, though not observed in any real data this
--                        session).
--
--   announced_date, discontinued_date -- MANUFACTURER-PUBLISHED, RESERVED.
--                        Added as columns but deliberately left unpopulated
--                        by any scraper in this pass: none of the three
--                        manufacturer platforms built so far (craft_cms,
--                        woocommerce, netsuite) expose a distinct
--                        "announcement date" separate from release/
--                        availability date, or an explicit "discontinued on"
--                        date anywhere in their HTML. Rather than fabricate
--                        a mapping from something that isn't actually that
--                        field, these stay null until a real source is
--                        found (bowwwl.com's own "Discontinued" field is a
--                        boolean, not a date, per bowwwl_cross_check's
--                        module docstring -- doesn't populate this either).
--                        USBC/PBA approval date is a distinct, real,
--                        already-existing column (`usbc_approval_date`,
--                        see 001_init_schema.sql) -- bowwwl_cross_check is
--                        the first thing in this project that can actually
--                        populate it, from bowwwl's own real
--                        "PBA Approval Date" field (confirmed live this
--                        session, ISO-precision datetime attribute even
--                        though the display text is often just "Mon YYYY").

begin;

alter table products add column first_seen_at timestamptz;
update products set first_seen_at = created_at;
alter table products alter column first_seen_at set not null;
alter table products alter column first_seen_at set default now();

alter table products add column announced_date date;
alter table products add column discontinued_date date;
alter table products add column discontinued_detected_at timestamptz;

comment on column products.first_seen_at is 'System-observed: first time this pipeline discovered/scraped this product. Backfilled from created_at; never updated after insert.';
comment on column products.release_date is 'Manufacturer-published release/availability date, parsed from each platform''s own release-date field where the format allows.';
comment on column products.announced_date is 'Reserved: no current scraper populates this. No platform built so far exposes a distinct announcement date separate from release date.';
comment on column products.discontinued_date is 'Reserved: manufacturer-published discontinuation date, if one is ever found. Not populated by any scraper as of this migration.';
comment on column products.discontinued_detected_at is 'System-observed: first time this pipeline saw status flip current -> retired on re-scrape. A detection timestamp, not the manufacturer''s actual discontinuation date -- bounded by discovery run frequency.';

-- ---------------------------------------------------------------------
-- bowwwl.com match cache
-- ---------------------------------------------------------------------
--
-- Mirrors bowlerdepot_products' shape (match_status enum, a unique
-- external identifier, last-checked timestamp) for the same reason: avoid
-- re-deriving the brand+ball -> bowwwl-URL match from scratch on every
-- cross-check run. Separate table rather than reusing
-- bowlerdepot_match_status/bowlerdepot_products directly -- these are two
-- independent external systems with independent match state, and keeping
-- them apart avoids a confusing shared enum whose name says "bowlerdepot"
-- but whose rows sometimes mean bowwwl.

create type bowwwl_match_status as enum ('matched', 'unmatched', 'ambiguous');

create table bowwwl_products (
    id uuid primary key default uuid_generate_v4(),
    product_id uuid references products(id),
    bowwwl_url text not null,
    match_status bowwwl_match_status not null default 'unmatched',
    last_checked_at timestamptz,
    created_at timestamptz not null default now(),
    unique (bowwwl_url)
);

create index idx_bowwwl_products_product on bowwwl_products(product_id);

commit;
