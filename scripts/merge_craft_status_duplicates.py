#!/usr/bin/env python3
"""
Merge Craft CMS (Brunswick / Radical / DV8) current/retired duplicate
products (runbook 6fc).

Those sites put the status in the URL (/products/balls/current/<slug> ->
/products/balls/retired/<slug>), and products is keyed on url, so a
retirement used to create a second, thin products row (images + SKUs only)
while the original -- videos, article, prices, BowlerDepot / bowwwl links
-- stayed 'current'. product_scraper.move_status_sibling stops new ones;
this cleans up the pairs that already exist. Al: "yes do 1 and 2".

For each pair (same brand, same slug, one under /current/ one under
/retired/):
  * KEEP the row with the most attached data (videos + articles + price
    sources + BowlerDepot/bowwwl links; ties -> the older row). It takes
    the URL the brand site uses now (the retired one, unless the current
    row is the newer page) and that status; discontinued_detected_at comes
    from the duplicate when the keeper had none.
  * RETIRE the other copy without deleting it: published = false (so the
    partner /v1/changes feed sends partners a 'remove' for it) and its url
    gets '#duplicate-of-<keeper id>' so it can't collide or be scraped
    again. Its own images/SKUs stay with it; the keeper already has its own.

Dry run by default; --apply writes, one transaction per pair.
Usage (PG* from the bowling-scraper-db secret):
    .venv/bin/python scripts/merge_craft_status_duplicates.py
    .venv/bin/python scripts/merge_craft_status_duplicates.py --apply
"""
import sys

import psycopg2

PAIRS_SQL = """
with c as (
  select p.id, p.brand_id, p.url, p.status, p.created_at, p.discontinued_detected_at,
         regexp_replace(p.url, '/products/balls/(current|retired)/', '/products/balls/*/') as slug_key,
         (select count(*) from product_videos v where v.product_id = p.id)
       + (select count(*) from product_articles a where a.product_id = p.id)
       + (select count(*) from product_price_sources s where s.product_id = p.id)
       + (select count(*) from bowlerdepot_products s where s.product_id = p.id)
       + (select count(*) from bowwwl_products s where s.product_id = p.id) as attached,
         p.name
  from products p
  where p.source_platform = 'craft_cms' and p.url ~ '/products/balls/(current|retired)/' and p.url not like '%%#duplicate-of-%%'
)
select a.slug_key, a.brand_id,
       json_agg(json_build_object('id', a.id, 'url', a.url, 'status', a.status, 'created_at', a.created_at,
                                  'discontinued_detected_at', a.discontinued_detected_at, 'attached', a.attached,
                                  'name', a.name) order by a.status)
from c a group by a.slug_key, a.brand_id having count(distinct a.status) = 2 and count(*) = 2
"""


def main(apply: bool) -> None:
    conn = psycopg2.connect()
    with conn.cursor() as cur:
        cur.execute(PAIRS_SQL)
        pairs = cur.fetchall()
    print(f"{len(pairs)} duplicate pair(s)")
    for _, _, rows in pairs:
        keep, dup = sorted(rows, key=lambda r: (-r["attached"], r["created_at"]))
        # The brand site's live URL is whichever row was created last.
        live = max(rows, key=lambda r: r["created_at"])
        print(f"  {keep['name']}: keep {keep['id']} ({keep['attached']} attached, was {keep['status']}) -> "
              f"{live['status']} at {live['url']}; hide {dup['id']} ({dup['attached']} attached)")
        if not apply:
            continue
        with conn.cursor() as cur:
            cur.execute("update products set url = url || '#duplicate-of-' || %s, published = false where id = %s",
                        (keep["id"], dup["id"]))
            cur.execute(
                "update products set url = %s, status = %s, "
                "discontinued_detected_at = case when %s = 'retired' "
                "  then coalesce(discontinued_detected_at, %s::timestamptz, now()) else null end, "
                "updated_at = now() where id = %s",
                (live["url"], live["status"], live["status"], dup["discontinued_detected_at"], keep["id"]),
            )
        conn.commit()
    if not apply:
        print("(dry run; --apply to write)")
    conn.close()


if __name__ == "__main__":
    main("--apply" in sys.argv[1:])
