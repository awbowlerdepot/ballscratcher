#!/usr/bin/env python3
"""
Add the retired balls bowlerdepot.com hid on 2026-11-02 to BowlerIQ, from the
store's own copy, when BowlerIQ doesn't have them yet (runbook 6ga).

Al, via the Spurline session: add the retired bowling balls Spurline hid on
bowlerdepot.com today "but only the ones BowlerIQ doesn't already have". Spurline
hid 258 balls with every weight at zero and none ever in Classic's feed; they
predate Spurline and BowlerIQ, and some are gone from the manufacturers' sites,
so the store copy may be the only source left. Its rules: copy the images (Al
may delete the BigCommerce products later and the CDN links would break), add
them as retired, take only what the store has ("don't fill in specs it doesn't
have"), and say what matched and how, so a near name isn't counted as missing
without a check.

INPUT. The JSON Spurline read from BigCommerce on 2026-11-02 (no cost fields):
one entry per product with bigcommerce_product_id, name ("Storm Phaze III",
brand first), brand, custom_url, mpn/upc, description_html, custom_fields (the
spec labels), images (zoom URLs) and variants (sku, upc, weight option).

WHO IS MISSING. Each entry lands in one tier, checked in this order, and only
`missing` is ever written:
  * linked       -- a bowlerdepot_products row already has its BigCommerce id;
  * part_number  -- its mpn or a variant sku equals a products/product_skus
                    part_number (BowlerIQ stores no UPCs);
  * exact_name   -- reconciliation's _normalize_name equal, same brand;
  * near_name    -- reconciliation's fuzzy_match_product, the link script's
                    medium tier, or the same letters with spaces/punctuation
                    gone ("Pay Back" / "Payback", "Hell Cat" / "Hellcat");
  * hold         -- not a near name, but every word of the store's name is in
                    one of ours ("Rhino Black/Blue/Silver" vs our
                    "... Silver Pearl", "Messenger PowerCOR" vs "... Solid"),
                    or the two differ only by Viz-a-Ball/Spare filler. Could be
                    the same ball; never imported unless named in --include;
  * missing      -- none of the above.
The other way round (OUR name inside THEIRS, "Harsh Reality" vs "Harsh Reality
Pearl") is a different ball, so it stays `missing`.

WHAT IS WRITTEN for each missing ball, in one transaction per ball:
  * products: status 'retired', source_platform 'other', url = its
    bowlerdepot.com listing (a unique key -- the page itself 404s while
    hidden), name without the brand prefix, part_number = mpn. From the custom
    fields only: coverstock_name ("Coverstock"), material/type and particle
    ("Cover Type", falling back to the coverstock name), core + core_type
    ("Core", "Core Type"), factory_finish ("Finish"), color, fragrance;
    description = description_html as text; weights_available from the
    variants. release_date, announced_date, discontinued_date stay null: the
    listing's date_created is when the store listed it, not a release date.
    The products_aa_publish_retired trigger publishes it, like every retired
    ball (6dl).
  * product_skus: one 'html' row per weight the variants list. RG /
    differential / mass bias only on the 15 lb row, because the store's labels
    are 15 lb values ("RG (15 lb)", "Radius of Gyration(15lb)"); every other
    weight gets null specs, not a guess.
  * oil/motion: estimated the same way every scraper does on insert
    (commercebuild's estimate_oil_motion, oil_motion_source 'estimated').
  * product_images: every image, thumbnail first ('main', then 'other'), with
    source_url = the CDN URL; after commit each one is sent to
    ImageProcessQueue, so image_processor copies it into our bucket
    (stored_url) like any scraped image.
  * bowlerdepot_products: (product_id, bigcommerce id, 'matched'), so the ball
    stays linked to its listing if the store ever shows it again. No price
    source: the listing isn't live (6fz would switch it off anyway).
Re-running is safe: products is `on conflict (url) do nothing`, and a ball
that already exists after a run lands in `linked` the next time.

Usage (PG* from the bowling-scraper-db secret; AWS creds for SQS):
    .venv/bin/python scripts/import_bowlerdepot_hidden_balls.py hidden-balls.json            # dry run + CSV
    .venv/bin/python scripts/import_bowlerdepot_hidden_balls.py hidden-balls.json --apply
    .venv/bin/python scripts/import_bowlerdepot_hidden_balls.py hidden-balls.json --apply --include 3291,1234
"""
import argparse
import csv
import html
import importlib.util
import json
import os
import re
import sys
from collections import Counter, defaultdict


def _load(name: str, *parts):
    """Load a module by path under its own name: scripts and tests that do
    `import app` would otherwise shadow each other (CLAUDE.md)."""
    path = os.path.join(os.path.dirname(__file__), "..", *parts)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rec = _load("bowlerdepot_reconciliation_app", "src", "bowlerdepot_reconciliation", "app.py")
cb = _load("commercebuild_product_scraper_app", "src", "commercebuild_product_scraper", "app.py")
link = _load("link_bowlerdepot_products", "scripts", "link_bowlerdepot_products.py")

STACK_NAME = "bowling-scraper"
TIERS = ["linked", "part_number", "exact_name", "near_name", "hold", "missing"]
# Words that don't tell two listings apart for the `hold` check: retail filler
# plus the Viz-a-Ball / Spare naming the store and the manufacturers swap.
HOLD_FILLER = rec._GENERIC_NAME_SUFFIX_TOKENS | {"viz", "a", "spare"}
WEIGHT_RANGE = range(6, 17)  # a "16lbs" option is a ball weight; anything else isn't


# --------------------------------------------------------------------
# Matching (pure)
# --------------------------------------------------------------------

def _brand_key(name: str) -> str:
    return rec._normalize_name(name or "").replace(" ", "")


def _letters(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def classify(entry: dict, ours_by_brand: dict, linked_ids: set, part_numbers: dict) -> dict:
    """One report row for a store entry: its tier, the BowlerIQ ball it
    matched (if any) and a note. ours_by_brand: {_brand_key: [{id, brand,
    name}]}; part_numbers: {PART: {product_id}}."""
    bid = str(entry["bigcommerce_product_id"])
    name = entry["name"]
    row = {"tier": "", "bigcommerce_product_id": bid, "bc_name": name, "brand": entry.get("brand", ""),
           "product_id": "", "our_name": "", "note": ""}

    def hit(tier, ours, note=""):
        row.update(tier=tier, product_id=ours["id"] if ours else "",
                   our_name=f"{ours['brand']} {ours['name']}" if ours else "", note=note)
        return row

    if bid in linked_ids:
        return hit("linked", None, "bowlerdepot_products already has this BigCommerce id")

    codes = {(entry.get("mpn") or "").strip().upper()}
    codes |= {(v.get("sku") or "").strip().upper() for v in entry.get("variants") or []}
    codes.discard("")
    cands = ours_by_brand.get(_brand_key(entry.get("brand")), [])
    by_id = {o["id"]: o for o in cands}
    for code in sorted(codes):
        for pid in sorted(part_numbers.get(code, ())):
            if pid in by_id:
                return hit("part_number", by_id[pid], f"part number {code}")

    full = lambda o: f"{o['brand']} {o['name']}"
    target = rec._normalize_name(name)
    exact = [o for o in cands if rec._normalize_name(full(o)) == target]
    if exact:
        return hit("exact_name", exact[0], "" if len(exact) == 1 else f"{len(exact)} exact names")

    found, ratio = rec.fuzzy_match_product(name, [{"id": o["id"], "name": full(o)} for o in cands])
    if found is not None:
        return hit("near_name", by_id[found["id"]], f"fuzzy {ratio:.2f}")
    for o in cands:
        if link.match_tier(name, full(o), o["brand"]) == "medium":
            return hit("near_name", o, "word order / filler")
        if _letters(name) == _letters(full(o)):
            return hit("near_name", o, "same letters, different spacing")

    theirs = rec._loose_tokens(name)
    for o in cands:
        mine = rec._loose_tokens(full(o))
        if theirs < mine:
            return hit("hold", o, "every word of the store name is in ours: " + " ".join(sorted(mine - theirs)))
        if theirs - HOLD_FILLER == mine - HOLD_FILLER:
            return hit("hold", o, "differs only by Viz-a-Ball / Spare filler")
    return hit("missing", None)


# --------------------------------------------------------------------
# The store copy -> our columns (pure)
# --------------------------------------------------------------------

def _field(fields: dict, *names):
    for n in names:
        if fields.get(n):
            return fields[n].strip() or None
    return None


def _strip_brand(name: str, brand: str) -> str:
    if brand and name.lower().startswith(brand.lower() + " "):
        return name[len(brand) + 1:].strip()
    return name.strip()


def _description(description_html: str):
    if not description_html:
        return None
    text = re.sub(r"<[^>]+>", " ", description_html)
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text or None


def weights_from_variants(variants: list) -> list:
    weights = set()
    for v in variants or []:
        for opt in v.get("options") or []:
            m = re.search(r"(\d+)\s*(?:lb|lbs)?\b", str(opt).lower())
            if m and int(m.group(1)) in WEIGHT_RANGE:
                weights.add(int(m.group(1)))
    return sorted(weights)


def build_ball(entry: dict, base_url: str) -> dict:
    """Everything one ball writes, from the store copy alone (see the module
    docstring). Unknown stays None."""
    fields = {f["name"].strip().lower(): f.get("value") for f in entry.get("custom_fields") or []}
    cover_type = _field(fields, "cover type")
    coverstock_name = _field(fields, "coverstock", "cover stock")
    parsed_type = cb.parse_coverstock(cover_type) if cover_type else {"coverstock_material": None, "coverstock_type": None}
    parsed_name = cb.parse_coverstock(coverstock_name) if coverstock_name else {"coverstock_material": None, "coverstock_type": None}
    material = parsed_type["coverstock_material"] if cover_type else parsed_name["coverstock_material"]
    cs_type = parsed_type["coverstock_type"] or parsed_name["coverstock_type"]

    specs = rec.extract_specs_from_custom_fields({"custom_fields": entry.get("custom_fields") or []})
    weights = weights_from_variants(entry.get("variants"))
    notes = []
    skus = []
    for w in weights:
        on_15 = w == rec.BOWLERDEPOT_REFERENCE_WEIGHT_LBS
        skus.append({"weight_lbs": w, "rg": specs["rg"] if on_15 else None,
                     "differential": specs["differential"] if on_15 else None,
                     "mass_bias": specs["mass_bias"] if on_15 else None, "source": "html"})
    if any(v is not None for v in specs.values()) and rec.BOWLERDEPOT_REFERENCE_WEIGHT_LBS not in weights:
        notes.append("15 lb specs but no 15 lb variant: specs not stored")
    if specs["rg"] is not None and not 2.3 <= specs["rg"] <= 2.9:
        notes.append(f"RG {specs['rg']} looks off")
    if specs["differential"] is not None and not 0 <= specs["differential"] <= 0.09:
        notes.append(f"differential {specs['differential']} looks off")

    images = sorted(entry.get("images") or [], key=lambda i: not i.get("is_thumbnail"))
    seen, image_urls = set(), []
    for img in images:
        if img.get("url") and img["url"] not in seen:
            seen.add(img["url"])
            image_urls.append(img["url"])

    reference_diff = specs["differential"] if rec.BOWLERDEPOT_REFERENCE_WEIGHT_LBS in weights else None
    core_type = cb.parse_core_type(_field(fields, "core type"))
    has_particle = "particle" in (cover_type or "").lower()
    estimate = cb.estimate_oil_motion(core_type=core_type, coverstock_type=cs_type, coverstock_material=material,
                                      has_particle=has_particle, differential=reference_diff)
    return {
        "bigcommerce_product_id": str(entry["bigcommerce_product_id"]),
        "name": _strip_brand(entry["name"], entry.get("brand", "")),
        "url": base_url.rstrip("/") + entry["custom_url"],
        "color": _field(fields, "color"),
        "coverstock_name": coverstock_name,
        "coverstock_material": material,
        "coverstock_type": cs_type,
        "has_particle": has_particle,
        "core_name": _field(fields, "core"),
        "core_type": core_type,
        "factory_finish": _field(fields, "finish"),
        "fragrance": _field(fields, "fragrance"),
        "part_number": (entry.get("mpn") or "").strip() or None,
        "description": _description(entry.get("description_html")),
        "weights": weights,
        "skus": skus,
        "image_urls": image_urls,
        "oil_rating": estimate["oil"],
        "motion_rating": estimate["motion"],
        "notes": notes,
    }


# --------------------------------------------------------------------
# Database
# --------------------------------------------------------------------

def load_db(conn):
    with conn.cursor() as cur:
        cur.execute("select id::text, name from brands")
        brands = {_brand_key(name): (bid, name) for bid, name in cur.fetchall()}
        cur.execute("""select p.id::text, b.name, p.name, p.part_number
                       from products p join brands b on b.id = p.brand_id where p.product_type = 'ball'""")
        ours_by_brand = defaultdict(list)
        part_numbers = defaultdict(set)
        for pid, brand, name, part in cur.fetchall():
            ours_by_brand[_brand_key(brand)].append({"id": pid, "brand": brand, "name": name})
            if part:
                part_numbers[part.strip().upper()].add(pid)
        cur.execute("select product_id::text, part_number from product_skus where coalesce(part_number, '') <> ''")
        for pid, part in cur.fetchall():
            part_numbers[part.strip().upper()].add(pid)
        cur.execute("select bigcommerce_product_id from bowlerdepot_products where product_id is not null")
        linked_ids = {str(r[0]) for r in cur.fetchall()}
        cur.execute("select base_url from price_sites where api_provider = 'bigcommerce' order by is_active desc limit 1")
        row = cur.fetchone()
    return brands, ours_by_brand, part_numbers, linked_ids, (row[0] if row else "https://bowlerdepot.com")


def insert_ball(conn, brand_id: str, ball: dict) -> dict:
    """One transaction per ball. Returns {"product_id", "image_jobs"}, or
    None when a product with this url already exists (nothing written)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into products (
                brand_id, name, url, color, coverstock_material, coverstock_type, coverstock_name,
                has_particle, factory_finish, part_number, weights_available, description,
                status, source_platform, product_type, fragrance)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    case when %s::int is null then null else int4range(%s::int, %s::int, '[]') end,
                    %s, 'retired', 'other', 'ball', %s)
            on conflict (url) do nothing
            returning id
            """,
            (brand_id, ball["name"], ball["url"], ball["color"], ball["coverstock_material"],
             ball["coverstock_type"], ball["coverstock_name"], ball["has_particle"], ball["factory_finish"],
             ball["part_number"], min(ball["weights"], default=None), min(ball["weights"], default=None),
             max(ball["weights"], default=None), ball["description"], ball["fragrance"]),
        )
        row = cur.fetchone()
        if row is None:
            conn.rollback()
            return None
        product_id = row[0]
    # The scraper's own helpers, so the cores/coverstocks rows are shared with
    # the brand's scraped balls (same (brand_id, name) keys).
    core_id = cb.get_or_create_core_id(conn, brand_id, ball["core_name"], ball["core_type"])
    coverstock_id = cb.get_or_create_coverstock_id(conn, brand_id, ball["coverstock_name"],
                                                   ball["coverstock_material"], ball["coverstock_type"])
    image_jobs = []
    with conn.cursor() as cur:
        cur.execute(
            """
            update products set core_id = %s, coverstock_id = %s,
                oil_rating = %s, motion_rating = %s, oil_motion_source = 'estimated'
            where id = %s
            """,
            (core_id, coverstock_id, ball["oil_rating"], ball["motion_rating"], product_id),
        )
        for sku in ball["skus"]:
            cur.execute(
                """
                insert into product_skus (product_id, weight_lbs, rg, differential, mass_bias, source)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (product_id, weight_lbs) do nothing
                """,
                (product_id, sku["weight_lbs"], sku["rg"], sku["differential"], sku["mass_bias"], sku["source"]),
            )
        for order, url in enumerate(ball["image_urls"]):
            cur.execute(
                """
                insert into product_images (product_id, image_type, source_url, display_order, is_thumbnail)
                values (%s, %s, %s, %s, %s)
                on conflict (product_id, source_url) do nothing
                returning id
                """,
                (product_id, "main" if order == 0 else "other", url, order, order == 0),
            )
            img = cur.fetchone()
            if img:
                image_jobs.append({"product_image_id": str(img[0]), "source_url": url})
        cur.execute(
            """
            insert into bowlerdepot_products (product_id, bigcommerce_product_id, bigcommerce_sku, match_status, last_synced_at)
            values (%s, %s, null, 'matched', now())
            on conflict (product_id) do nothing
            """,
            (product_id, ball["bigcommerce_product_id"]),
        )
    conn.commit()
    return {"product_id": str(product_id), "image_jobs": image_jobs}


def send_image_jobs(jobs: list) -> int:
    """Same message body the scrapers send (build_image_process_messages)."""
    import boto3

    queue_url = boto3.client("cloudformation").describe_stack_resource(
        StackName=STACK_NAME, LogicalResourceId="ImageProcessQueue")["StackResourceDetail"]["PhysicalResourceId"]
    return cb.publish_messages(boto3.client("sqs"), queue_url, cb.build_image_process_messages(jobs))


# --------------------------------------------------------------------
# Report + main
# --------------------------------------------------------------------

CSV_FIELDS = ["tier", "bigcommerce_product_id", "bc_name", "brand", "product_id", "our_name", "note",
              "import_name", "coverstock", "cover", "core", "core_type", "weights", "rg15", "diff15", "mb15",
              "images", "build_notes"]


def plan(entries: list, brands: dict, ours_by_brand: dict, part_numbers: dict, linked_ids: set,
         base_url: str, include: set) -> list:
    rows = []
    for entry in entries:
        row = classify(entry, ours_by_brand, linked_ids, part_numbers)
        row["write"] = row["tier"] == "missing" or (row["tier"] == "hold" and row["bigcommerce_product_id"] in include)
        if _brand_key(entry.get("brand")) not in brands:
            row["write"] = False
            row["note"] = (row["note"] + "; " if row["note"] else "") + "brand not in BowlerIQ"
        ball = build_ball(entry, base_url)
        row["ball"] = ball
        row["brand_id"] = brands.get(_brand_key(entry.get("brand")), (None, None))[0]
        sku15 = next((s for s in ball["skus"] if s["weight_lbs"] == rec.BOWLERDEPOT_REFERENCE_WEIGHT_LBS), {})
        row.update(import_name=ball["name"], coverstock=ball["coverstock_name"] or "",
                   cover=f"{ball['coverstock_material'] or ''} {ball['coverstock_type'] or ''}".strip(),
                   core=ball["core_name"] or "", core_type=ball["core_type"] or "",
                   weights=" ".join(map(str, ball["weights"])), rg15=sku15.get("rg") or "",
                   diff15=sku15.get("differential") or "", mb15=sku15.get("mass_bias") or "",
                   images=len(ball["image_urls"]), build_notes="; ".join(ball["notes"]))
        rows.append(row)
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("json_path", help="the hidden-balls JSON Spurline read from BigCommerce")
    ap.add_argument("--csv", default="bowlerdepot_hidden_import.csv", help="report path")
    ap.add_argument("--apply", action="store_true", help="write the `missing` balls (and any --include)")
    ap.add_argument("--include", default="", help="comma-separated BigCommerce ids from the `hold` tier to import too")
    args = ap.parse_args(argv)
    include = {s.strip() for s in args.include.split(",") if s.strip()}

    import psycopg2

    with open(args.json_path) as f:
        entries = json.load(f)
    conn = psycopg2.connect()
    try:
        brands, ours_by_brand, part_numbers, linked_ids, base_url = load_db(conn)
        rows = plan(entries, brands, ours_by_brand, part_numbers, linked_ids, base_url, include)
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(sorted(rows, key=lambda r: (TIERS.index(r["tier"]), r["brand"], r["bc_name"])))
        tiers = Counter(r["tier"] for r in rows)
        print(f"{len(entries)} store entries: " + ", ".join(f"{t} {tiers[t]}" for t in TIERS))
        to_write = [r for r in rows if r["write"]]
        print(f"To import: {len(to_write)} ({Counter(r['brand'] for r in to_write).most_common()})")
        print(f"Report: {args.csv}")
        if not args.apply:
            print("(dry run; --apply to write them)")
            return 0

        written, skipped, jobs = [], 0, []
        for r in to_write:
            result = insert_ball(conn, r["brand_id"], r["ball"])
            if result is None:
                skipped += 1
                continue
            written.append(result["product_id"])
            jobs.extend(result["image_jobs"])
        print(f"Imported {len(written)} ball(s); {skipped} already existed by url")
        if jobs:
            print(f"Queued {send_image_jobs(jobs)} image(s) for image_processor")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
