-- 062_product_skus_usbc_spec_check.sql
--
-- Runbook 6gm. Al: "it looks like the 900 Global Portal bowling ball has
-- a bad spec for its mass bias number. 19.000 is likely supposed to be
-- .019. Here is the USBC spec for equipment that could be used as a check
-- to make sure they are at least within that."
--
-- Portal wasn't alone: 40 of 5,959 ball SKU rows were outside what a ball
-- can physically be, from every scraper platform:
--   - 900 Global's image-only Tech Data PDFs go through Textract, which
--     drops the ".0" -- mass bias 19.000, differential 54.000 (Portal,
--     Harsh Reality, Viking, Eternity PI, Origin EX ...). Motiv's Covert
--     VIP EXJ has the same thing in its HTML (56.000).
--   - Columns swapped or repeated: TNBA Chalk rg 0.039 / diff 2.600,
--     Cyborg diff 2.530 (its RG again), DV8 Diva Style's RG column holding
--     differentials (0.040-0.052).
--   - Manufacturer typos we can't guess the fix for: Raw Hammer Burn RG
--     0.480, Recon RX1 Silver RG 2.290, Motiv TR2 differential 0.31-0.52,
--     Track 300T differential 0.090.
--
-- The check lives here, as a BEFORE trigger, because six writers insert
-- into product_skus (one per scraper platform plus pdf_parser) and the
-- review queue's approve path writes it too. One rule in the database
-- covers every one of them, and every future scraper, without six copies
-- kept in sync.
--
-- The limits come from the USBC Equipment Specifications and
-- Certifications Manual (10/25), Physical Specifications table. For a
-- ball of 13.00 lb or more:
--   radius of gyration  2.447" - 2.813" (any single ball)
--   differential RG     0.060" max
-- Mass bias is the manual's "intermediate differential" (high RG minus
-- intermediate RG axis), so it can't be more than the total differential.
-- The manual has no rule for balls under 13 lb, so those get a plausibility
-- range taken from the catalog instead (99.5% of light-ball rows are
-- RG 2.577-2.870, differential at most 0.059): RG 2.40-2.95, differential
-- at most 0.080.
--
-- Manufacturers print two decimals of RG ("2.44"), and a ball that
-- measures 2.4475 is printed 2.45 but one measuring 2.447 can be printed
-- 2.44 by a truncating spec sheet, so the RG window is widened by that
-- rounding: 2.44-2.82. Ebonite's 14 lb Gamebreaker/Pin Slasher/Playmaker
-- (2.44) are real published numbers and stay.
--
-- What the trigger does, in order:
--   1. Repairs a lost decimal. A whole number from 1 to 60 in
--      differential or mass_bias is read as thousandths (19 -> 0.019),
--      which is how every one of those rows reads on the manufacturer's
--      own sheet. Whole numbers only: 0.310 or 2.530 are not this mistake.
--   2. Repairs swapped RG/differential: an RG under 0.1 next to a
--      differential that is itself an RG (2.40-2.95).
--   3. Blanks anything still out of range and sets needs_review. A blank
--      spec reads as "not published" everywhere downstream (consumer
--      site, partner API, plotter estimator, BigCommerce sync), which is
--      the truth; a wrong one gets shown and drawn on the plotter. The
--      raw value goes to review_queue (source 'usbc_spec_check') so it
--      isn't lost, with the reason, for a person to fill in from the
--      manufacturer's sheet on the product page.
--
-- Repairs don't go to the review queue: they're certain, and the row
-- shows up in /v1/changes like any other spec change (the AFTER triggers
-- on this table bump the product's content_changed_at).
--
-- A Sunday re-scrape writes the same bad value again, and the trigger
-- blanks it again; the review row isn't repeated while one exists for
-- the same SKU, field and raw value, whatever its status, so rejecting it
-- is final.

create or replace function product_skus_usbc_spec_check() returns trigger
language plpgsql as $$
declare
    heavy boolean := coalesce(new.weight_lbs >= 13, true);
    rg_min numeric := case when heavy then 2.44 else 2.40 end;
    rg_max numeric := case when heavy then 2.82 else 2.95 end;
    diff_max numeric := case when heavy then 0.060 else 0.080 end;
    swapped numeric;
    bad text[] := '{}';
    raw jsonb := jsonb_build_object('rg', new.rg, 'differential', new.differential, 'mass_bias', new.mass_bias);
    f text;
begin
    -- 1. Lost decimal.
    if new.differential >= 1 and new.differential <= 60 and new.differential = trunc(new.differential) then
        new.differential := new.differential / 1000;
    end if;
    if new.mass_bias >= 1 and new.mass_bias <= 60 and new.mass_bias = trunc(new.mass_bias) then
        new.mass_bias := new.mass_bias / 1000;
    end if;

    -- 2. Swapped RG and differential.
    if new.rg < 0.1 and new.differential between 2.40 and 2.95 then
        swapped := new.rg;
        new.rg := new.differential;
        new.differential := swapped;
    end if;

    -- 3. Out of range: blank and flag.
    if new.rg is not null and (new.rg < rg_min or new.rg > rg_max) then
        bad := bad || 'rg'::text;
        new.rg := null;
    end if;
    if new.differential is not null and (new.differential < 0 or new.differential > diff_max) then
        bad := bad || 'differential'::text;
        new.differential := null;
    end if;
    if new.mass_bias is not null
       and (new.mass_bias < 0 or new.mass_bias > diff_max
            or (new.differential is not null and new.mass_bias > new.differential)) then
        bad := bad || 'mass_bias'::text;
        new.mass_bias := null;
    end if;

    if cardinality(bad) > 0 then
        new.needs_review := true;
        foreach f in array bad loop
            insert into review_queue (product_id, product_sku_id, field_name, current_value, proposed_value, source, reason)
            -- product_sku_id stays null: on an INSERT the row doesn't
            -- exist yet, so its id would fail review_queue's foreign key.
            -- field_name carries the weight, as the other writers' do.
            select new.product_id, null, f || '_' || new.weight_lbs || 'lb', raw ->> f, null, 'usbc_spec_check',
                   format('%s %s is outside the spec limits for a %s lb ball (RG %s-%s, differential up to %s, mass bias up to the differential); blanked until someone enters it from the manufacturer''s sheet',
                          f, raw ->> f, new.weight_lbs, rg_min, rg_max, diff_max)
            where not exists (
                select 1 from review_queue q
                where q.source = 'usbc_spec_check' and q.product_id = new.product_id
                  and q.field_name = f || '_' || new.weight_lbs || 'lb'
                  and q.current_value is not distinct from raw ->> f);
        end loop;
    end if;
    return new;
end;
$$;

drop trigger if exists product_skus_usbc_spec_check on product_skus;
create trigger product_skus_usbc_spec_check
    before insert or update of rg, differential, mass_bias, weight_lbs on product_skus
    for each row execute function product_skus_usbc_spec_check();

-- Run the existing rows through it. Only rows the trigger would change are
-- touched, so the rest keep their updated_at and content_changed_at.
update product_skus set rg = rg
where (differential >= 1 or mass_bias >= 1 or rg < 2.40 or rg > 2.95
       or differential < 0 or mass_bias < 0 or mass_bias > differential
       or (weight_lbs >= 13 and (rg < 2.44 or rg > 2.82 or differential > 0.060 or mass_bias > 0.060))
       or (weight_lbs < 13 and (differential > 0.080 or mass_bias > 0.080)));
