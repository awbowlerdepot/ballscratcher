-- Runbook 6ed: retired balls are published automatically.
--
-- 6dl published every retired ball once (Al: "can we go ahead and publish
-- all the retired balls please. they are used in the api and need to be
-- returned"), but nothing kept it that way: the 4 retired Hammer balls the
-- 6ec sweep added on 2026-10-20 arrived with products.published's default
-- (false) and had to be published by hand. Al: "yes publish retired balls
-- automatically".
--
-- A trigger rather than a change in each scraper: five scrapers insert
-- products (Craft CMS, CommerceBuild, NetSuite, WooCommerce, Shopify), plus
-- anything added later; this covers them all in one place.
--
-- It sets published = true for a ball when:
--   * it is INSERTED with status 'retired', or
--   * an UPDATE changes its status to 'retired' (a current ball retiring).
-- It deliberately does NOT fire on other updates, so an admin who later
-- unpublishes a retired ball on purpose isn't overridden by the next scrape.
--
-- Named products_aa_... so it sorts (and so runs) before 044's
-- products_content_changed_at BEFORE UPDATE trigger -- Postgres runs
-- same-timing row triggers alphabetically -- so a current -> retired
-- transition's publish is seen by 044 and reaches the partner changes feed.

begin;

create or replace function products_publish_retired_balls() returns trigger
language plpgsql as $$
begin
  if new.product_type = 'ball' and new.status = 'retired'
     and (tg_op = 'INSERT' or old.status is distinct from 'retired') then
    new.published := true;
  end if;
  return new;
end;
$$;

drop trigger if exists products_aa_publish_retired on products;
create trigger products_aa_publish_retired
  before insert or update of status on products
  for each row execute function products_publish_retired_balls();

comment on function products_publish_retired_balls() is
  'Runbook 6ed: publishes a ball when it is inserted as retired or its status changes to retired. Other updates are left alone, so a deliberate unpublish sticks.';

commit;
