-- 018_bowlerdepot_products_dedupe_by_product.sql
--
-- Real incident, Al: "it still finds the ai version" -- reported AFTER
-- fuzzy_match_product's own suffix-collision bug was already fixed
-- (matching "Storm iQ Tour" to a separate "Storm iQ Tour AI" listing).
-- The matching algorithm fix alone didn't self-heal an already-matched
-- product, because of a SEPARATE storage bug found while investigating:
-- upsert_bowlerdepot_match's ON CONFLICT target was
-- (bigcommerce_product_id, bigcommerce_sku) -- keyed on the BigCommerce
-- side, not on our own product_id. A corrected re-match for a product
-- that already had a stored (wrong) match therefore didn't overwrite
-- that old row, it INSERTED A SECOND ROW for the same product_id (the
-- new, correct bigcommerce_product_id/sku pair had never been seen
-- before, so nothing collided). price_checker.list_bowlerdepot_matches
-- has no DISTINCT/ORDER BY, so which of the two rows for a given
-- product "won" was effectively arbitrary -- explaining why the AI
-- match kept surfacing even after the algorithm itself was fixed and
-- redeployed.
--
-- This migration:
--   1. Collapses any existing duplicate rows per product_id down to one
--      -- keeps the most-recently-synced row (last_synced_at desc,
--      nulls last, then created_at desc, then id desc as a final
--      tiebreaker for exact ties), deletes the rest. Uses a window-
--      function delete rather than the old (bigcommerce_product_id,
--      bigcommerce_sku)-keyed self-join pattern, since the whole point
--      is to dedupe by product_id specifically.
--   2. Drops the old (bigcommerce_product_id, bigcommerce_sku) unique
--      constraint (looked up dynamically via pg_constraint/pg_attribute
--      rather than hardcoding Postgres's auto-generated constraint name,
--      since that name is long enough to risk NAMEDATALEN truncation and
--      isn't worth guessing at).
--   3. Adds a new unique constraint on product_id -- each of our
--      products should have exactly one current bowlerdepot_products
--      row going forward; upsert_bowlerdepot_match (src/bowlerdepot_
--      reconciliation/app.py) now conflicts on this column instead.
--
-- NOTE: this migration does NOT touch product_price_sources. An
-- existing already-approved price source that was created from the old,
-- wrong match keeps pointing at the wrong BigCommerce product until an
-- admin re-triggers "Find price sources" (POST /products/{id}/discover-
-- price-sources) for that product after this migration + the algorithm
-- fix are both deployed -- discover_bigcommerce_candidates corrects an
-- existing approved row in place when the underlying match has changed
-- (see upsert_bigcommerce_price_source_candidate), it just isn't
-- triggered automatically by this migration.

begin;

delete from bowlerdepot_products
where id in (
    select id from (
        select id,
               row_number() over (
                   partition by product_id
                   order by last_synced_at desc nulls last, created_at desc, id desc
               ) as rn
        from bowlerdepot_products
        where product_id is not null
    ) ranked
    where rn > 1
);

do $$
declare
    old_constraint_name text;
begin
    select conname into old_constraint_name
    from pg_constraint
    where conrelid = 'bowlerdepot_products'::regclass
      and contype = 'u'
      and conkey = (
          select array_agg(attnum order by attnum)
          from pg_attribute
          where attrelid = 'bowlerdepot_products'::regclass
            and attname in ('bigcommerce_product_id', 'bigcommerce_sku')
      );
    if old_constraint_name is not null then
        execute format('alter table bowlerdepot_products drop constraint %I', old_constraint_name);
    end if;
end $$;

alter table bowlerdepot_products add constraint bowlerdepot_products_product_id_key unique (product_id);

commit;
