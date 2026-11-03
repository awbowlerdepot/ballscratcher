-- 059_products_status_override.sql -- a ball's current/retired status set
-- by a person sticks through re-scrapes (DEPLOY_RUNBOOK.md 6fs).
--
-- Al (2026-11-02), on Storm's Identity and Identity B/C/P: "identity are
-- discontinued limited releases", then "yes do option 1". stormbowling.com
-- still files both pages under "Bowling Balls", not "Bowling Balls
-- Archive", so commercebuild_product_scraper's classify_product_status
-- reads them as current, and Sunday's full re-scrape (6dx) would undo a
-- hand-set 'retired'. The same is true of every scraper family, and of a
-- bowwwl_cross_check 'retired' proposal approved in admin (6fd).
--
-- status_override: when set, status always equals it, whatever writes the
-- row. A trigger rather than a change in each scraper, the same reasoning
-- as 050: five scraper families write products, plus anything added later.
-- null (the default) means "the scrapers decide", as before.
--
-- Named products_a0_... so it runs FIRST of the BEFORE triggers on
-- products (they run alphabetically): before 050's products_aa_publish_
-- retired, so a ball retired by override is published like any other
-- retired ball, and before 044's products_content_changed_at, so the
-- changes feed sees the final status and a scrape that tries to flip it
-- back doesn't count as a change.

begin;

alter table products add column if not exists status_override product_status;
alter table products add column if not exists status_override_note text;
alter table products add column if not exists status_override_at timestamptz;

create or replace function products_apply_status_override() returns trigger
language plpgsql as $$
begin
  if new.status_override is not null then
    new.status := new.status_override;
  end if;
  return new;
end;
$$;

drop trigger if exists products_a0_status_override on products;
create trigger products_a0_status_override
  before insert or update on products
  for each row execute function products_apply_status_override();

comment on column products.status_override is
  'Runbook 6fs: set by a person (admin, or approving a status review item); when not null, status always equals it, so a re-scrape cannot flip it. null = the scrapers decide.';

commit;
