#!/usr/bin/env python3
"""
Load one brand's lineups and create its brand_art task (runbook 6fq,
migration 058).

Al (2026-11-02): "This is one per brand, not a one-off for Storm...
adding the next brand should only mean loading its lineups and creating
its task." So a brand is a data file, scripts/brand_lineups/<brand>.json:

  {"brand": "Storm",                     -- BowlerIQ brands.name
   "owner": "bowlerdepot",               -- the partner the task is for
   "theme_key": "storm",                 -- optional: seed balls from the theme
   "task": {"slug", "bigcommerce_brand_id", "palette": {"background", "accent"},
            "line_groupings": [...], "heroes"?: [...]},
   "groupings": [{"key", "label", "source", "theme_group"?,
                  "lines": [{"key", "label", "text"?, "assign_rule"?}]}]}

assign_rule is how brand_art files a new release into the line (see
058's comment on brand_lines); without one the line is manual.

SEEDING FROM THE THEME (--theme-nav). The Bowler Depot theme keeps the
lineups the brand-stores session read from each brand's own site in
templates/components/brand-store/navigation.html, as BigCommerce product
ids with a per-ball "read" note. With --theme-nav those become
brand_line_balls rows (added_by 'load', note = the read), mapped to our
balls through the matched bowlerdepot_products link. Lines that have a
rule are refilled by the rule on the first dispatch anyway; the seed is
what the lineup looks like before that, and keeps the notes.

Dry run by default (prints what it would write). --apply writes.
Re-running is safe: groupings, lines and the task are upserted by key,
seed rows insert-if-missing. The task's params are replaced by the file's
(that's a params change, so the next dispatch redraws the slots).

  .venv/bin/python scripts/load_brand_lineups.py scripts/brand_lineups/storm.json \\
      --theme-nav ../bicommerce-site/bowlerdepot-theme-brands/templates/components/brand-store/navigation.html
  .venv/bin/python scripts/load_brand_lineups.py scripts/brand_lineups/storm.json --theme-nav ... --apply
"""

import argparse
import json
import re
import sys


def read_theme_nav(path: str) -> dict:
    """The brand navigation JSON inside the theme template's JSONparseSafe
    string."""
    text = open(path).read()
    m = re.search(r"\{\{#JSONparseSafe '\n(.*?)\n'\}\}", text, re.S)
    if not m:
        raise SystemExit(f"no navigation JSON found in {path}")
    return json.loads(m.group(1))


def theme_lines(nav: dict, theme_key: str) -> dict:
    """{(group label, line key): [{"id": bc id, "read": note}]} for a brand."""
    out = {}
    for cat in (nav.get(theme_key) or {}).get("categories") or []:
        for line in cat.get("lines") or []:
            if line.get("products"):
                out[(line.get("group"), line["key"])] = line["products"]
    return out


def get_db_connection():
    import psycopg2
    return psycopg2.connect()  # PG* from the bowling-scraper-db secret, as the other scripts


def load(conn, cfg: dict, nav: dict | None, apply: bool) -> dict:
    report = {"groupings": [], "lines": [], "seeded": [], "unmapped": [], "task": None}
    with conn.cursor() as cur:
        cur.execute("select id from brands where name = %s", (cfg["brand"],))
        row = cur.fetchone()
        if not row:
            raise SystemExit(f"no BowlerIQ brand named {cfg['brand']!r}")
        brand_id = row[0]
        seeds = theme_lines(nav, cfg.get("theme_key")) if nav and cfg.get("theme_key") else {}
        cur.execute("""
            select bdp.bigcommerce_product_id, p.id, p.name from bowlerdepot_products bdp
            join products p on p.id = bdp.product_id
            where bdp.match_status = 'matched' and p.brand_id = %s
        """, (brand_id,))
        by_bc = {str(r[0]): (r[1], r[2]) for r in cur.fetchall()}

        for gi, g in enumerate(cfg["groupings"]):
            cur.execute("""
                insert into brand_groupings (brand_id, key, label, source, display_order)
                values (%s, %s, %s, %s, %s)
                on conflict (brand_id, key) do update set label = excluded.label, source = excluded.source,
                    display_order = excluded.display_order, updated_at = now()
                returning id
            """, (brand_id, g["key"], g["label"], g.get("source"), gi))
            gid = cur.fetchone()[0]
            report["groupings"].append(g["key"])
            for li, line in enumerate(g["lines"]):
                cur.execute("""
                    insert into brand_lines (grouping_id, key, label, text, display_order, assign_rule)
                    values (%s, %s, %s, %s, %s, %s)
                    on conflict (grouping_id, key) do update set label = excluded.label, text = excluded.text,
                        display_order = excluded.display_order, assign_rule = excluded.assign_rule, updated_at = now()
                    returning id
                """, (gid, line["key"], line["label"], line.get("text"), li,
                      json.dumps(line.get("assign_rule") or {"type": "manual"})))
                lid = cur.fetchone()[0]
                report["lines"].append(f"{g['key']}/{line['key']} ({(line.get('assign_rule') or {}).get('type', 'manual')})")
                for pos, prod in enumerate(seeds.get((g.get("theme_group"), line["key"])) or []):
                    hit = by_bc.get(str(prod["id"]))
                    if not hit:
                        report["unmapped"].append(f"{line['key']}: BigCommerce {prod['id']}")
                        continue
                    cur.execute("""
                        insert into brand_line_balls (line_id, product_id, position, note, added_by)
                        values (%s, %s, %s, %s, 'load') on conflict do nothing
                    """, (lid, hit[0], pos, prod.get("read")))
                    if cur.rowcount:
                        report["seeded"].append(f"{line['key']}: {hit[1]}")

        t = cfg["task"]
        cur.execute("""
            insert into tasks (owner, brand_id, kind, params) values (%s, %s, 'brand_art', %s)
            on conflict (owner, brand_id, kind) do update set params = excluded.params, updated_at = now()
            returning id
        """, (cfg["owner"], brand_id, json.dumps(t)))
        report["task"] = str(cur.fetchone()[0])
    if apply:
        conn.commit()
    else:
        conn.rollback()
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("config", help="scripts/brand_lineups/<brand>.json")
    ap.add_argument("--theme-nav", help="the theme's templates/components/brand-store/navigation.html, to seed balls")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run, rolled back)")
    args = ap.parse_args(argv)
    cfg = json.load(open(args.config))
    nav = read_theme_nav(args.theme_nav) if args.theme_nav else None
    conn = get_db_connection()
    try:
        report = load(conn, cfg, nav, args.apply)
    finally:
        conn.close()
    print(f"{cfg['brand']}: groupings {report['groupings']}")
    for line in report["lines"]:
        print(f"  line {line}")
    print(f"  seeded {len(report['seeded'])} ball(s) from the theme; {len(report['unmapped'])} unmapped")
    for u in report["unmapped"]:
        print(f"    unmapped {u}")
    print(f"  task {report['task']} ({cfg['owner']}, brand_art)")
    print("applied" if args.apply else "(dry run; --apply to write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
