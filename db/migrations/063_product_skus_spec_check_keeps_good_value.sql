-- 063_product_skus_spec_check_keeps_good_value.sql
--
-- Runbook 6gn. 062's check blanks an impossible spec. Al then had the
-- real values looked up and written ("yes write them all"): Motiv TR2's
-- differentials (its page prints 0.31-0.52 for .031-.052), Tribal Fire
-- 12 lb (.31), Raw Hammer Burn 15 lb RG (Hammer's page says ".48" for
-- 2.48), DV8 Diva Style from its info sheet.
--
-- But those balls are re-scraped every Sunday, and the Shopify, NetSuite
-- and Craft scrapers overwrite rg and differential outright
-- (`rg = excluded.rg`). The page's typo would come back, 062 would blank
-- it, and the corrected value would be gone. So on an UPDATE, an
-- out-of-range value now keeps the value already stored when that one is
-- in range, instead of blanking it. A bad scrape never wipes a good value,
-- whether a person entered it or an earlier scrape got it right. Blanking
-- and the review row still happen when there's nothing good to keep (a
-- new row, or a stored value that's blank).
--
-- Only the function changes; 062's trigger keeps calling it.

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

    -- 3. Out of range: keep the stored value if it's good (063),
    --    otherwise blank and flag.
    if new.rg is not null and (new.rg < rg_min or new.rg > rg_max) then
        if tg_op = 'UPDATE' and old.rg between rg_min and rg_max then
            new.rg := old.rg;
        else
            bad := bad || 'rg'::text;
            new.rg := null;
        end if;
    end if;
    if new.differential is not null and (new.differential < 0 or new.differential > diff_max) then
        if tg_op = 'UPDATE' and old.differential between 0 and diff_max then
            new.differential := old.differential;
        else
            bad := bad || 'differential'::text;
            new.differential := null;
        end if;
    end if;
    if new.mass_bias is not null
       and (new.mass_bias < 0 or new.mass_bias > diff_max
            or (new.differential is not null and new.mass_bias > new.differential)) then
        if tg_op = 'UPDATE' and old.mass_bias between 0 and diff_max
           and (new.differential is null or old.mass_bias <= new.differential) then
            new.mass_bias := old.mass_bias;
        else
            bad := bad || 'mass_bias'::text;
            new.mass_bias := null;
        end if;
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
