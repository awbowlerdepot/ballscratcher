#!/usr/bin/env python3
"""
Link published balls to their bowlerdepot.com listings, and give each link
the BigCommerce price source the APIs read (runbook 6fm).

Al: "can we look at which balls are linked to bowlerdepot.com and why they
wouldn't be showing their data in the API. and create a script to get them
linked. i think it is the price source but i could be wrong."

WHAT THE APIS READ. A ball is "linked" to bowlerdepot.com in two separate
tables, and each API field is gated on one of them:

  * bowlerdepot_products (bowlerdepot_reconciliation, one row per product
    since 018) -> partner_api Ball.bigcommerce_product_id (match_status =
    'matched' AND no other product's matched/ambiguous row on the same
    BigCommerce id, 6fg), public_api's article-hero / video-summary embeds
    and bowlerdepot_learn_links (match_status = 'matched' + published).
  * product_price_sources on the bowlerdepot.com price site (price_checker,
    014/016) -> public_api ecommerce_url/price/in_stock (Learn "Shop this
    ball", JSON-LD offers), partner_api Ball.bowlerdepot_url, and
    "recommendable" in the plotter neighbors: status = 'approved' AND
    is_active.

The second is downstream of the first: price discovery only ever turns an
EXISTING bowlerdepot_products row into a pending price source
(price_checker.discover_bigcommerce_candidates), it never searches
BigCommerce by name. So "it is the price source" is the symptom, and for
almost every ball it isn't the cause. Measured 2026-11-01 over the 1,924
published balls: 177 had a bowlerdepot_url, 165 a bigcommerce_product_id.
Of the 1,747 without a bowlerdepot_url, 1,732 had NO bowlerdepot_products
row at all (so no price source could ever exist), and only 15 failed on
the price source itself (pending, rejected, or never discovered).

WHY THE ROWS ARE MISSING. bowlerdepot_reconciliation matches only
`products.status = 'current'` (list_our_current_products). 1,679 of the
published balls are retired (050 publishes retired balls), and bowlerdepot
.com still sells hundreds of them as visible listings, so they are never
matched. The rest are current balls whose names don't normalize to
BowlerDepot's (Brunswick's "Sea Turtle" is listed as "Brunswick Sea Turtle
Viz-A-Ball"), or balls BowlerDepot really doesn't carry (all of SWAG; the
store has no SWAG brand).

WHAT THIS SCRIPT DOES. Pulls the BigCommerce catalog (Catalog v3, GET
only -- the token in bowling-scraper-bigcommerce can modify products since
6fj, this script never does), compares it with every published ball, and
sorts each ball into one reason (see REASONS). Tiers:

  * high: the normalized names are EXACTLY equal (bowlerdepot_reconciliation.
    _normalize_name, the same test reconciliation calls 'matched'), the
    listing is visible and purchasable, and nobody else claims it -- no
    existing bowlerdepot_products row on that BigCommerce id, and no other
    of our balls proposing it. --apply writes these as 'matched'.
  * medium: the names differ only in order, punctuation, generic words
    ("bowling ball", "edition") or the Viz-a-Ball suffix, or reconciliation's
    own fuzzy matcher accepts them. Never written by --apply: they go to the
    CSV with an empty `accept` column. Put `y` in it for the ones you've
    checked and pass the file back with --accept-file. This is the 6h.1
    lesson ("Storm iQ Tour" matched "Storm iQ Tour AI"): a near name is a
    question for a person, not an answer.

What --apply never does: touch an existing bowlerdepot_products row
(matched or ambiguous -- an ambiguous row is an admin decision waiting to
happen, and some were approved by hand from pre-6h.1 matches), link a
hidden/disabled listing (its storefront page 404s, so the URL would be a
dead "Shop this ball" link), or link a BigCommerce id two balls both want
(one listing for several colorways; 6fg hands out an id only when one ball
owns it).

PRICE SOURCES. For every ball whose link passes the partner gate (matched,
unshared, listing visible), the script plans its bowlerdepot.com price
source the way discover_bigcommerce_candidates would, plus two fixes it
can't do:

  * no bowlerdepot.com source -> create one, pending, match_confidence
    'high', source 'bigcommerce_api' (same row discovery writes);
  * a pending or rejected source pointing at a DIFFERENT BigCommerce id than
    the link (the stale wrong-ball row Al rejected, e.g. Roto Grip Hustle
    SOS -> "Hustle HSB") -> repoint it at the linked listing and set it back
    to pending. discover_bigcommerce_candidates would repoint the URL but
    leave it rejected, so it never shows;
  * pending on the right id -> approve, only with --approve-price-sources.

Everything else is reported, not changed: a source Al rejected on the SAME
listing (his call), an inactive one, and an approved one whose BigCommerce
id disagrees with the link (6h.1's leftover shape; fix in admin).

The price-source rows are pending by default because the module's design is
"never auto-approved" (price_checker docstring). --approve-price-sources is
the explicit opt-in for the high tier only, exactly the set Al has been
approving by hand in admin (156 of the 177 approved rows are 'high'), and
records resolved_by = 'link_bowlerdepot_products.py'. Approving bumps
products.content_changed_at for that ball: bowlerdepot_url is a Ball field,
but no 044 trigger watches product_price_sources, so without the bump
/v1/changes would never resend the ball with its new URL.

After an approving run, the new sources have no price yet (ecommerce_price
stays null until the first check). The script prints the price_checker
invoke that checks exactly those products now instead of waiting for the
daily run.

Usage (PG* from the bowling-scraper-db secret; AWS creds that can read the
bowling-scraper-bigcommerce secret):
    .venv/bin/python scripts/link_bowlerdepot_products.py                    # dry run, CSV + summary
    .venv/bin/python scripts/link_bowlerdepot_products.py --csv out.csv
    .venv/bin/python scripts/link_bowlerdepot_products.py --apply            # high-tier links + pending price sources
    .venv/bin/python scripts/link_bowlerdepot_products.py --apply --approve-price-sources
    .venv/bin/python scripts/link_bowlerdepot_products.py --apply --accept-file reviewed.csv
"""
import argparse
import csv
import importlib.util
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from urllib.parse import urljoin

# Reuse reconciliation's own name normalization rather than a copy, so a
# "high" match here is exactly what reconciliation would call 'matched'.
# Loaded by path under its own module name: tests and scripts that do
# `import app` would otherwise shadow each other (CLAUDE.md).
_REC_PATH = os.path.join(os.path.dirname(__file__), "..", "src", "bowlerdepot_reconciliation", "app.py")
_spec = importlib.util.spec_from_file_location("bowlerdepot_reconciliation_app", _REC_PATH)
rec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rec)

BIGCOMMERCE_SECRET_ID = os.environ.get("BIGCOMMERCE_SECRET_ARN", "bowling-scraper-bigcommerce")
RESOLVED_BY = "link_bowlerdepot_products.py"
BALLS_CATEGORY_NAME = "Balls"  # bowlerdepot.com's root ball category (id 25 on 2026-11-01)

# Extra words that may separate our name from BowlerDepot's in the MEDIUM
# tier only: reconciliation's generic filler plus the Viz-a-Ball suffix
# BowlerDepot appends to Brunswick/Hammer/Track Viz-a-Balls ("Sea Turtle" vs
# "Sea Turtle Viz-A-Ball"). Never enough for --apply on its own.
MEDIUM_EXTRA_TOKENS = rec._GENERIC_NAME_SUFFIX_TOKENS | {"viz", "a"}

# Every reason a published ball can land in, in report order.
REASONS = [
    "linked_ok",                  # matched + unshared + approved, active price source: every API field shows
    "linked_price_source_issue",  # matched + unshared, but the price source is missing/pending/rejected/inactive/stale
    "linked_ambiguous",           # existing 'ambiguous' row: no partner id / hero; never written by this script
    "linked_shared",              # existing 'matched' row, but another ball claims the same BigCommerce id
    "propose_link",               # high tier: --apply links it
    "review_link",                # medium tier: needs `accept=y` in --accept-file
    "contested",                  # its listing is wanted by another of our balls, or already claimed
    "listing_hidden",             # BowlerDepot has the listing but it's hidden/disabled on the storefront
    "brand_not_carried",          # BowlerDepot has no products for this brand at all
    "not_on_bowlerdepot",         # no listing close enough
]


# --------------------------------------------------------------------
# BigCommerce (read-only)
# --------------------------------------------------------------------

def get_bigcommerce_credentials(secret_id: str = BIGCOMMERCE_SECRET_ID):
    import boto3

    client = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "us-west-1"))
    secret = json.loads(client.get_secret_value(SecretId=secret_id)["SecretString"])
    return secret["store_hash"], secret["auth_token"]


def fetch_all(store_hash: str, auth_token: str, path: str, params: dict, session=None, max_pages: int = 100) -> list:
    """GET every page of a Catalog v3 list endpoint. GET only, by design."""
    import requests

    session = session or requests.Session()
    out, page = [], 1
    while page <= max_pages:
        # BigCommerce answers 429 when the store's API quota window is spent
        # (hit live running this twice in a row, 2026-11-01) and says how long
        # until it resets in X-Rate-Limit-Time-Reset-Ms.
        for _attempt in range(5):
            resp = session.get(
                f"https://api.bigcommerce.com/stores/{store_hash}/v3/catalog/{path}",
                headers={"Accept": "application/json", "X-Auth-Token": auth_token},
                params={**params, "page": page, "limit": 250},
                timeout=60,
            )
            if resp.status_code != 429:
                break
            time.sleep(min(int(resp.headers.get("X-Rate-Limit-Time-Reset-Ms", 5000)), 30000) / 1000 + 0.5)
        resp.raise_for_status()
        body = resp.json()
        out.extend(body.get("data", []))
        if page >= body.get("meta", {}).get("pagination", {}).get("total_pages", page):
            break
        page += 1
    return out


def fetch_catalog(store_hash: str, auth_token: str, session=None) -> dict:
    products = fetch_all(store_hash, auth_token, "products", {
        "include_fields": "id,name,sku,brand_id,categories,is_visible,availability,custom_url",
    }, session=session)
    brands = fetch_all(store_hash, auth_token, "brands", {"include_fields": "id,name"}, session=session)
    categories = fetch_all(store_hash, auth_token, "trees/categories", {}, session=session)
    return {"products": products, "brands": brands, "categories": categories}


def ball_products(catalog: dict) -> list:
    """Products in the "Balls" category tree. Reconciliation matches against
    the whole store; this narrows to balls so a bag or shirt named after a
    ball can't win. Falls back to every product if the category is gone."""
    cats = {c["category_id"]: c for c in catalog.get("categories", [])}
    roots = {cid for cid, c in cats.items() if c.get("parent_id") == 0 and c.get("name") == BALLS_CATEGORY_NAME}
    if not roots:
        return list(catalog["products"])

    def under_balls(cid):
        seen = set()
        while cid and cid not in seen:
            if cid in roots:
                return True
            seen.add(cid)
            cid = cats.get(cid, {}).get("parent_id")
        return False

    return [p for p in catalog["products"] if any(under_balls(c) for c in p.get("categories", []))]


def is_purchasable(bc_product: dict) -> bool:
    """Visible on the storefront and not availability 'disabled'. A hidden
    product's page 404s, so its URL would be a dead link."""
    return bool(bc_product.get("is_visible")) and bc_product.get("availability") != "disabled"


def listing_url(bc_product: dict, base_url: str) -> str:
    """Same resolution as price_checker.extract_bigcommerce_price_fields."""
    path = (bc_product.get("custom_url") or {}).get("url")
    if path and base_url:
        return urljoin(base_url, path)
    return path


# --------------------------------------------------------------------
# Matching (pure)
# --------------------------------------------------------------------

def match_tier(our_full_name: str, bc_name: str, brand_name: str = ""):
    """'high' for an exact normalized match, 'medium' for a name that differs
    only in word order, punctuation, MEDIUM_EXTRA_TOKENS words, or that
    reconciliation's fuzzy matcher accepts; None otherwise. The two names
    must also share a word that is neither filler nor the brand, so
    "Brunswick Ball" can't reach "Brunswick Viz-a-Ball" on the brand alone."""
    if rec._normalize_name(our_full_name) == rec._normalize_name(bc_name):
        return "high"
    ours, theirs = rec._loose_tokens(our_full_name), rec._loose_tokens(bc_name)
    distinctive = (ours & theirs) - MEDIUM_EXTRA_TOKENS - rec._loose_tokens(brand_name)
    if (ours ^ theirs) <= MEDIUM_EXTRA_TOKENS and distinctive:
        return "medium"
    found, _ = rec.fuzzy_match_product(our_full_name, [{"name": bc_name}])
    return "medium" if found is not None else None


def best_candidates(our_full_name: str, bc_balls: list, brand_name: str = "") -> list:
    """Every BigCommerce ball at the best tier this name reaches, as
    [(tier, product)]. More than one at that tier means we can't tell the
    listings apart, which is reported as contested."""
    by_tier = defaultdict(list)
    for p in bc_balls:
        tier = match_tier(our_full_name, p.get("name", ""), brand_name)
        if tier:
            by_tier[tier].append(p)
    for tier in ("high", "medium"):
        if by_tier[tier]:
            return [(tier, p) for p in by_tier[tier]]
    return []


def _brand_key(name: str) -> str:
    return rec._normalize_name(name or "").replace(" ", "")


def plan_links(our_balls: list, existing: dict, bc_products: list, bc_balls: list, bc_brands: list) -> list:
    """One plan row per published ball.

    our_balls: [{product_id, brand_name, name, status}]
    existing: {product_id: {bigcommerce_product_id, match_status}} from bowlerdepot_products
    bc_products: the whole store (to describe existing links); bc_balls: the Balls tree.
    """
    bc_by_id = {str(p["id"]): p for p in bc_products}
    carried_brands = {_brand_key(b["name"]) for b in bc_brands}

    # BigCommerce ids already claimed by an existing matched/ambiguous row.
    claimed = defaultdict(set)
    for pid, row in existing.items():
        if row.get("match_status") in ("matched", "ambiguous"):
            claimed[str(row["bigcommerce_product_id"])].add(pid)

    rows = []
    for ball in our_balls:
        full = f"{ball['brand_name']} {ball['name']}"
        base = {"product_id": ball["product_id"], "brand": ball["brand_name"], "name": ball["name"],
                "status": ball.get("status"), "tier": "", "bc_id": "", "bc_name": "", "bc_visible": "", "note": ""}
        row = existing.get(ball["product_id"])
        if row is not None:
            bc_id = str(row["bigcommerce_product_id"])
            bc = bc_by_id.get(bc_id, {})
            base.update(bc_id=bc_id, bc_name=bc.get("name", "(not in store)"), bc_visible=bc.get("is_visible", ""))
            if row["match_status"] == "ambiguous":
                base["reason"] = "linked_ambiguous"
            elif row["match_status"] != "matched":
                base["reason"] = "not_on_bowlerdepot"
                base["note"] = f"existing row is {row['match_status']}"
            elif claimed[bc_id] - {ball["product_id"]}:
                base["reason"] = "linked_shared"
            else:
                base["reason"] = "linked_ok"  # refined by plan_price_sources
            rows.append(base)
            continue

        cands = best_candidates(full, bc_balls, ball["brand_name"])
        if not cands:
            base["reason"] = ("brand_not_carried" if _brand_key(ball["brand_name"]) not in carried_brands
                              else "not_on_bowlerdepot")
            rows.append(base)
            continue

        tier = cands[0][0]
        live = [p for _, p in cands if is_purchasable(p)]
        pick = live[0] if live else cands[0][1]
        base.update(tier=tier, bc_id=str(pick["id"]), bc_name=pick.get("name", ""), bc_visible=pick.get("is_visible"))
        if not live:
            base["reason"] = "listing_hidden"
        elif len(live) > 1:
            base["reason"] = "contested"
            base["note"] = "several listings: " + ", ".join(f"{p['id']} {p['name']}" for p in live)
        elif claimed[str(pick["id"])]:
            base["reason"] = "contested"
            base["note"] = "listing already linked to another ball"
        else:
            base["reason"] = "propose_link" if tier == "high" else "review_link"
        rows.append(base)

    # Two of our balls proposing the same listing (four MOTIV "Ascend"
    # colorways, a current ball and its retired namesake): neither is safe.
    wanted = Counter(r["bc_id"] for r in rows if r["reason"] in ("propose_link", "review_link"))
    for r in rows:
        if r["reason"] in ("propose_link", "review_link") and wanted[r["bc_id"]] > 1:
            r["reason"] = "contested"
            r["note"] = f"{wanted[r['bc_id']]} of our balls want this listing"
    return rows


def plan_price_sources(link_rows: list, sources: dict, bc_by_id: dict, base_url: str) -> list:
    """Price-source actions for every row that is (or will be, after
    --apply) a clean 'matched' link to a purchasable listing. Mutates
    link_rows' reason/note for existing links whose price source is the
    problem. sources: {product_id: [bowlerdepot.com product_price_sources rows]}.
    Returns [{product_id, action, source_id, product_url, bc_id, note}] with
    action in create / repoint / approve / none."""
    actions = []
    for r in link_rows:
        if r["reason"] not in ("linked_ok", "propose_link", "review_link"):
            continue
        bc = bc_by_id.get(r["bc_id"])
        if bc is None or not is_purchasable(bc):
            if r["reason"] == "linked_ok":
                r["reason"] = "linked_price_source_issue"
                r["note"] = "linked listing is hidden or gone from bowlerdepot.com"
            continue
        url = listing_url(bc, base_url)
        rows = sources.get(r["product_id"], [])
        approved = [s for s in rows if s["status"] == "approved" and s["is_active"]]
        act = {"product_id": r["product_id"], "bc_id": r["bc_id"], "product_url": url, "source_id": None, "note": ""}

        if approved:
            if any(str(s.get("external_product_id")) != r["bc_id"] for s in approved):
                act.update(action="none", note="approved source points at a different BigCommerce id (fix in admin)")
                r["reason"], r["note"] = "linked_price_source_issue", act["note"]
            else:
                act["action"] = "none"
            actions.append(act)
            continue

        if r["reason"] == "linked_ok":
            r["reason"] = "linked_price_source_issue"
        same = [s for s in rows if str(s.get("external_product_id")) == r["bc_id"]]
        stale = [s for s in rows if str(s.get("external_product_id")) != r["bc_id"] and s["status"] in ("pending", "rejected")]
        pending_same = [s for s in same if s["status"] == "pending"]
        if pending_same:
            act.update(action="approve", source_id=pending_same[0]["id"], note="pending on the right listing")
        elif [s for s in same if s["status"] == "rejected"]:
            act.update(action="none", note="rejected by an admin on this same listing; left alone")
        elif [s for s in same if s["status"] == "approved"]:
            act.update(action="none", note="approved but inactive; left alone")
        elif stale:
            act.update(action="repoint", source_id=stale[0]["id"],
                       note=f"{stale[0]['status']} source points at BigCommerce id {stale[0].get('external_product_id')}")
        else:
            act.update(action="create", note="no bowlerdepot.com price source")
        if r["reason"] == "linked_price_source_issue" and not r["note"]:
            r["note"] = act["note"]
        actions.append(act)
    return actions


def load_accepted(path: str) -> set:
    """product_ids with accept = y/yes/1 in a CSV this script wrote."""
    with open(path, newline="") as f:
        return {row["product_id"] for row in csv.DictReader(f)
                if (row.get("accept") or "").strip().lower() in ("y", "yes", "1", "true")}


# --------------------------------------------------------------------
# Database
# --------------------------------------------------------------------

def load_db(conn):
    with conn.cursor() as cur:
        cur.execute("""
            select p.id::text, b.name, p.name, p.status::text
            from products p join brands b on b.id = p.brand_id
            where p.product_type = 'ball' and p.published = true
            order by b.name, p.name
        """)
        balls = [{"product_id": r[0], "brand_name": r[1], "name": r[2], "status": r[3]} for r in cur.fetchall()]
        cur.execute("select product_id::text, bigcommerce_product_id, match_status::text from bowlerdepot_products "
                    "where product_id is not null")
        existing = {r[0]: {"bigcommerce_product_id": r[1], "match_status": r[2]} for r in cur.fetchall()}
        cur.execute("select id::text, base_url from price_sites where api_provider = 'bigcommerce' order by is_active desc limit 1")
        site = cur.fetchone()
        sources = defaultdict(list)
        if site:
            cur.execute("""
                select id::text, product_id::text, status::text, is_active, external_product_id, product_url
                from product_price_sources where price_site_id = %s
            """, (site[0],))
            for r in cur.fetchall():
                sources[r[1]].append({"id": r[0], "status": r[2], "is_active": r[3],
                                      "external_product_id": r[4], "product_url": r[5]})
    return balls, existing, (site[0] if site else None), (site[1] if site else None), sources


def apply_links(conn, rows: list) -> list:
    """Inserts 'matched' rows. ON CONFLICT DO NOTHING, and re-checks inside
    the transaction that nobody claimed the listing since the plan was made
    (reconciliation runs daily), so an existing row is never overwritten."""
    written = []
    with conn.cursor() as cur:
        for r in rows:
            cur.execute("""
                insert into bowlerdepot_products (product_id, bigcommerce_product_id, bigcommerce_sku, match_status, last_synced_at)
                select %s, %s, null, 'matched', now()
                where not exists (select 1 from bowlerdepot_products o
                                   where o.bigcommerce_product_id = %s and o.match_status in ('matched', 'ambiguous'))
                on conflict (product_id) do nothing
            """, (r["product_id"], r["bc_id"], r["bc_id"]))
            if cur.rowcount:
                written.append(r["product_id"])
    conn.commit()
    return written


def apply_price_sources(conn, actions: list, site_id: str, linked: set, approve: bool) -> dict:
    """create / repoint as pending; approve (and the create/repoint rows,
    when approve is on) as approved. Only for products whose link exists
    after apply_links (linked)."""
    done = Counter()
    approved_products = []
    with conn.cursor() as cur:
        for a in actions:
            if a["product_id"] not in linked or a["action"] == "none":
                continue
            status = "approved" if approve else "pending"
            if a["action"] == "create":
                cur.execute("""
                    insert into product_price_sources
                        (product_id, price_site_id, product_url, match_query, match_confidence,
                         external_product_id, status, source, resolved_at, resolved_by)
                    values (%s, %s, %s, 'bowlerdepot_products match', 'high', %s, %s, 'bigcommerce_api',
                            case when %s = 'approved' then now() end, case when %s = 'approved' then %s end)
                    on conflict (product_id, price_site_id, product_url) do nothing
                """, (a["product_id"], site_id, a["product_url"], a["bc_id"], status, status, status, RESOLVED_BY))
            elif a["action"] == "repoint":
                cur.execute("""
                    update product_price_sources
                       set product_url = %s, external_product_id = %s, match_confidence = 'high',
                           status = %s, resolved_at = case when %s = 'approved' then now() end,
                           resolved_by = case when %s = 'approved' then %s end
                     where id = %s and status in ('pending', 'rejected')
                """, (a["product_url"], a["bc_id"], status, status, status, RESOLVED_BY, a["source_id"]))
            elif a["action"] == "approve":
                if not approve:
                    continue
                cur.execute("""
                    update product_price_sources set status = 'approved', resolved_at = now(), resolved_by = %s
                     where id = %s and status = 'pending'
                """, (RESOLVED_BY, a["source_id"]))
            if cur.rowcount:
                done[a["action"]] += 1
                if approve:
                    approved_products.append(a["product_id"])
        # bowlerdepot_url is partner-visible but no 044 trigger watches
        # product_price_sources: bump so /v1/changes resends these balls.
        if approved_products:
            cur.execute("update products set content_changed_at = now() where id = any(%s::uuid[])",
                        (approved_products,))
    conn.commit()
    return {"counts": dict(done), "approved_products": approved_products}


# --------------------------------------------------------------------
# Report
# --------------------------------------------------------------------

CSV_FIELDS = ["reason", "tier", "accept", "status", "brand", "name", "bc_id", "bc_name", "bc_visible",
              "price_action", "price_url", "note", "product_id"]


def write_csv(path: str, link_rows: list, actions: list) -> None:
    by_product = {a["product_id"]: a for a in actions}
    order = {r: i for i, r in enumerate(REASONS)}
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in sorted(link_rows, key=lambda r: (order.get(r["reason"], 99), r["brand"], r["name"])):
            a = by_product.get(r["product_id"], {})
            w.writerow({**r, "accept": "", "price_action": a.get("action", ""), "price_url": a.get("product_url") or ""})


def summarize(link_rows: list, actions: list) -> str:
    lines = ["Published balls by reason (current / retired / total):"]
    by = Counter((r["reason"], r["status"]) for r in link_rows)
    for reason in REASONS:
        c, t = by[(reason, "current")], by[(reason, "retired")]
        if c or t:
            lines.append(f"  {reason:27} {c:5} {t:6} {c + t:6}")
    lines.append(f"  {'total':27} {sum(v for (_, s), v in by.items() if s == 'current'):5} "
                 f"{sum(v for (_, s), v in by.items() if s == 'retired'):6} {len(link_rows):6}")
    acts = Counter(a["action"] for a in actions)
    lines.append("Price-source actions for clean links (existing + proposed high/medium): "
                 + ", ".join(f"{k} {v}" for k, v in sorted(acts.items())))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--csv", default="bowlerdepot_links.csv", help="report path (default ./bowlerdepot_links.csv)")
    ap.add_argument("--apply", action="store_true", help="write high-tier links and their price sources (pending)")
    ap.add_argument("--approve-price-sources", action="store_true",
                    help="with --apply: approve the high-tier links' bowlerdepot.com price sources")
    ap.add_argument("--accept-file", help="with --apply: also link review_link rows marked accept=y in this CSV")
    args = ap.parse_args(argv)
    if (args.approve_price_sources or args.accept_file) and not args.apply:
        ap.error("--approve-price-sources and --accept-file need --apply")

    import psycopg2

    store_hash, auth_token = get_bigcommerce_credentials()
    catalog = fetch_catalog(store_hash, auth_token)
    bc_balls = ball_products(catalog)
    bc_by_id = {str(p["id"]): p for p in catalog["products"]}
    print(f"bowlerdepot.com: {len(catalog['products'])} products, {len(bc_balls)} in the Balls tree, "
          f"{sum(1 for p in bc_balls if is_purchasable(p))} visible")

    conn = psycopg2.connect()
    try:
        balls, existing, site_id, base_url, sources = load_db(conn)
        if site_id is None:
            print("No price_sites row with api_provider = 'bigcommerce'; nothing to do.")
            return 1
        link_rows = plan_links(balls, existing, catalog["products"], bc_balls, catalog["brands"])
        actions = plan_price_sources(link_rows, sources, bc_by_id, base_url)
        write_csv(args.csv, link_rows, actions)
        print(summarize(link_rows, actions))
        print(f"Report: {args.csv}")

        if not args.apply:
            print("(dry run; --apply to write the propose_link rows)")
            return 0

        accepted = load_accepted(args.accept_file) if args.accept_file else set()
        to_link = [r for r in link_rows if r["reason"] == "propose_link"
                   or (r["reason"] == "review_link" and r["product_id"] in accepted)]
        written = set(apply_links(conn, to_link))
        print(f"Linked {len(written)} of {len(to_link)} proposed ball(s) as 'matched'")

        linked = written | {r["product_id"] for r in link_rows if r["reason"] in ("linked_ok", "linked_price_source_issue")}
        result = apply_price_sources(conn, actions, site_id, linked, args.approve_price_sources)
        print(f"Price sources: {result['counts'] or 'no changes'}"
              + ("" if args.approve_price_sources else " (pending: approve in admin, or re-run with --approve-price-sources)"))
        if result["approved_products"]:
            payload = json.dumps({"product_ids": result["approved_products"]})
            with open("bowlerdepot_price_check_payload.json", "w") as f:
                f.write(payload)
            print("Fill their prices now instead of at the daily check:\n"
                  "  aws lambda invoke --function-name bowling-scraper-price-checker "
                  "--payload file://bowlerdepot_price_check_payload.json --cli-binary-format raw-in-base64-out "
                  "/tmp/price-check.json && cat /tmp/price-check.json")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
