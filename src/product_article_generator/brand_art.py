"""
Brand lineups and the brand_art task (runbook 6fq, migration 058).

Al (2026-11-02, through the Bowler Depot brand-stores session): BowlerIQ
owns each brand's lineups, and Bowler Depot's brand-store art is a
standing TASK that BowlerIQ reruns whenever one of its trigger rules
fires, with every image at a stable URL. "This is one per brand, not a
one-off for Storm... adding the next brand should only mean loading its
lineups and creating its task." Then: "go ahead, include the weekly
sweep, newest releases for heroes."

Two event shapes on product_article_generator (handler routes them here):

  {"brand_art": "dispatch"}     -- the daily schedule. For every enabled
      task: file new releases into their lines (assign rules), work out
      every slot's inputs, and rerun only the slots whose inputs changed
      since they were drawn (or all of them once a week). Each task with
      work is run by an async self-invoke, so one slow brand can't hold up
      the others or the 15-minute timeout.
  {"brand_art": "run", "task_id", "slots"?, "trigger"?}
      -- draw those slots (all when absent). Admin's "Rerun now" sends
      this with trigger 'manual'.

TRIGGER RULES AS INPUT FINGERPRINTS. The request lists the rules: a ball
released or added to/removed from a line, a ball's photo changing, the
line or palette parameters changing, a featured product selling out or
being removed, a weekly sweep, and a manual rerun. Rather than hooking
every scraper, each slot's inputs (its products, their photo URLs, the
palette, the companion's stock) are hashed, and a slot whose hash differs
from the one stored with its image is rerun. So a new release that
becomes a hero pick, a ball filed into a line, a re-scraped photo or a bag
selling out all show up as a changed hash the next morning, and the run
log names which rule it was (_trigger_for).

HEROES. Al's types: a "product hero" is one product in focus -- a single
ball, or a non-ball product (bag, shoes) with at most one ball beside it;
a "lineup hero" is several products together. Each hero slot is a pick
rule in the task's params; the default ("newest releases for heroes") is
the brand's two newest released balls, then its newest stocked non-ball
product with the newest ball beside it (falling back to the third newest
ball when Bowler Depot stocks nothing else from the brand). A ball
product hero is drawn by the existing Gemini product-shot pipeline (Al:
"Build the product hero on that existing pipeline rather than a new
one"), with the same logo-match and logo-size checks the announcement
images use; non-ball and lineup heroes, and line images, are composited
from the real photos (brand_art_compose), since the request says "real
product photos only" for anything that isn't a ball and line images "stay
ball-only".

STOCK. "What counts as stocked: the product is in Bowler Depot's
BigCommerce catalog for that brand, visible, and has stock available to
sell." Read with the same Catalog v3 GET the reconciliation uses, per
task, by the task's bigcommerce_brand_id.
"""

from __future__ import annotations

import datetime
import hashlib
import io
import json
import logging
import re
import time
from urllib.parse import urljoin, urlparse

import brand_art_compose as compose

logger = logging.getLogger()

ART_PREFIX = "brand-art"
CACHE_CONTROL = "public, max-age=3600"   # "Keep the CDN cache short (around an hour)"
WEEKLY_SWEEP = datetime.timedelta(days=7)
LINE_SIZE = (1200, 900)
HERO_SIZE = (1600, 1200)
HERO_WIDE_SIZE = (1600, 900)
GEMINI_HERO_ATTEMPTS = 3
LISTING_CRAWL_DELAY_SECONDS = 10         # stormbowling.com robots.txt Crawl-delay (commercebuild_url_discovery)
LISTING_LINK_RE = re.compile(r'href="(/[a-z0-9-]+-bowling-ball)"')

# Default heroes for every brand (runbook 6fu). Al, through the Bowler
# Depot brand-stores session, the brand page's slides are "fixed in
# purpose": 1 = the newest ball, "including pre-orders" (the Ion Pro
# Pearl, on pre-order, not the Equinox Hybrid the in-stock-first rule
# picked); 2 = "Find your reaction", one ball from each of the brand's
# lines; 3 = "See them on the plotter", which the theme draws live, so it
# has no image. "Other brands should get the same three by default."
# The older pick types (a ranked newest_ball, newest_companion) still work
# when a task lists them in params.heroes.
DEFAULT_HEROES = [
    {"type": "product", "pick": "newest_ball", "include_preorder": True},
    {"type": "line_mix"},
]

# BigCommerce category roots that count as a non-ball "companion" for a
# hero ("bags, shoes, gloves and wrist supports, apparel and accessories").
# The rest of the roots (Balls, On Sale, Drilling, discontinued, Route) are
# not product kinds. On Sale is a second home for products already in one
# of these, so it's simply ignored.
COMPANION_ROOTS = {"bags": "bag", "shoes": "shoes", "accessories": "accessory", "apparel": "apparel"}


# ---------------------------------------------------------------- loading

def list_enabled_tasks(conn) -> list:
    with conn.cursor() as cur:
        cur.execute("""
            select t.id, t.owner, t.brand_id, b.name, t.kind, t.params, t.triggers, t.last_full_run_at
            from tasks t join brands b on b.id = t.brand_id
            where t.enabled and t.kind = 'brand_art'
            order by b.name
        """)
        return [_task_row(r) for r in cur.fetchall()]


def load_task(conn, task_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("""
            select t.id, t.owner, t.brand_id, b.name, t.kind, t.params, t.triggers, t.last_full_run_at
            from tasks t join brands b on b.id = t.brand_id where t.id = %s
        """, (task_id,))
        row = cur.fetchone()
        return _task_row(row) if row else None


def _task_row(r) -> dict:
    return {"id": str(r[0]), "owner": r[1], "brand_id": str(r[2]), "brand_name": r[3], "kind": r[4],
            "params": r[5] or {}, "triggers": r[6] or [], "last_full_run_at": r[7]}


def list_brand_balls(conn, brand_id: str) -> list:
    """The brand's current, published balls, newest release first, with
    their photo candidates and Bowler Depot listing. Photos: source_url
    before stored_url, because the manufacturer's own PNG is often a real
    cut-out and our stored copy is flattened on white (image_processor);
    thumbnail first, then display_order, as everywhere else."""
    with conn.cursor() as cur:
        cur.execute("""
            select p.id, p.name, p.url, p.release_date, p.demand_score,
                   (select bdp.bigcommerce_product_id from bowlerdepot_products bdp
                     where bdp.product_id = p.id and bdp.match_status = 'matched') as bc_id,
                   (select array_agg(u order by o) from (
                        select coalesce(i.source_url, i.stored_url) as u,
                               (case when i.is_thumbnail then 0 else 1 end) * 1000 + coalesce(i.display_order, 999) as o
                        from product_images i
                        where i.product_id = p.id and coalesce(i.is_visible, true)
                          and coalesce(i.source_url, i.stored_url) is not null
                        union all
                        select i.stored_url, 5000 + coalesce(i.display_order, 999)
                        from product_images i
                        where i.product_id = p.id and coalesce(i.is_visible, true)
                          and i.stored_url is not null and i.source_url is not null
                    ) x) as photos
            from products p
            where p.brand_id = %s and p.product_type = 'ball' and p.status = 'current' and p.published
            order by p.release_date desc nulls last, p.name
        """, (brand_id,))
        return [{"product_id": str(r[0]), "name": r[1], "url": r[2], "release_date": r[3],
                 "demand_score": float(r[4]) if r[4] is not None else None,
                 "bigcommerce_product_id": int(r[5]) if r[5] and str(r[5]).isdigit() else None,
                 "photos": [u for u in (r[6] or []) if u]}
                for r in cur.fetchall()]


def list_lines(conn, brand_id: str) -> list:
    """Every line of the brand with its grouping and balls (in position
    order)."""
    with conn.cursor() as cur:
        cur.execute("""
            select g.key, g.label, l.id, l.key, l.label, l.text, l.assign_rule,
                   coalesce(array_agg(lb.product_id::text order by lb.position, lb.added_at)
                            filter (where lb.product_id is not null), '{}')
            from brand_groupings g
            join brand_lines l on l.grouping_id = g.id
            left join brand_line_balls lb on lb.line_id = l.id
            where g.brand_id = %s
            group by g.key, g.label, g.display_order, l.id, l.key, l.label, l.text, l.assign_rule, l.display_order
            order by g.display_order, g.key, l.display_order, l.key
        """, (brand_id,))
        return [{"grouping_key": r[0], "grouping_label": r[1], "line_id": str(r[2]), "key": r[3], "label": r[4],
                 "text": r[5], "assign_rule": r[6] or {"type": "manual"}, "ball_ids": list(r[7])}
                for r in cur.fetchall()]


def load_slots(conn, task_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            select slot_key, input_hash, featured, generated_at from brand_art_slots where task_id = %s
        """, (task_id,))
        return {r[0]: {"input_hash": r[1], "featured": r[2] or [], "generated_at": r[3]} for r in cur.fetchall()}


# ---------------------------------------------------------- assign rules

def _norm_path(url: str) -> str:
    return urlparse(url or "").path.rstrip("/").lower()


def listing_members(rule: dict, balls: list, fetch_html) -> set | None:
    """Product ids (from `balls`) that the brand's own filtered listing
    shows, or None when the listing couldn't be read (then nothing is
    added or removed). commercebuild only for now: stormbowling.com lists
    Storm, Roto Grip and 900 Global together, so a link only counts for
    the brand whose product URL it is. A colorway page
    (".../storm-mix-bowling-ball-platinum") belongs to its listing link
    (".../storm-mix-bowling-ball")."""
    if rule.get("platform") != "commercebuild":
        logger.warning("brand_listing rule for platform %r has no reader yet", rule.get("platform"))
        return None
    try:
        html = fetch_html(rule["url"])
    except Exception:
        logger.exception("couldn't read brand listing %s", rule.get("url"))
        return None
    paths = {_norm_path(urljoin(rule["url"], m)) for m in LISTING_LINK_RE.findall(html)}
    out = set()
    for b in balls:
        p = _norm_path(b["url"])
        if p in paths or any(p.startswith(x + "-") for x in paths):
            out.add(b["product_id"])
    return out


FIELD_COLUMNS = {"name", "color", "coverstock_type", "coverstock_material", "finish_category", "has_particle"}


def fields_members(rule: dict, conn, brand_id: str) -> set:
    """Product ids of the brand's current published balls matching every
    clause of a {"type": "fields", "all": [...]} rule. Clauses:
    {"field", "matches": regex} or {"field", "in": [values]}, on a small
    whitelist of products columns."""
    clauses = rule.get("all") or []
    with conn.cursor() as cur:
        cur.execute("""
            select id, name, color, coverstock_type::text, coverstock_material, finish_category::text,
                   has_particle::text
            from products
            where brand_id = %s and product_type = 'ball' and status = 'current' and published
        """, (brand_id,))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    out = set()
    for r in rows:
        ok = True
        for c in clauses:
            f = c.get("field")
            if f not in FIELD_COLUMNS:
                ok = False
                break
            v = r.get(f) or ""
            if "matches" in c and not re.search(c["matches"], str(v)):
                ok = False
            if "in" in c and str(v) not in [str(x) for x in c["in"]]:
                ok = False
        if ok:
            out.add(str(r["id"]))
    return out


def apply_assign_rules(conn, brand_id: str, balls: list, fetch_html, sleep=time.sleep) -> dict:
    """File the brand's current balls into lines by each line's
    assign_rule. Adds members the rule finds (added_by 'rule'); removes a
    member only if a rule added it and the rule no longer finds it (a
    'load' or 'admin' placement is a person's call and stays). A current
    ball that sits in no line of a grouping with at least one rule gets a
    review_queue item (deduped on the pending one), never a guess.
    Returns {"added": [...], "removed": [...], "unplaced": [...]}."""
    lines = list_lines(conn, brand_id)
    current = {b["product_id"] for b in balls}
    added, removed = [], []
    fetched = 0
    for line in lines:
        rule = line["assign_rule"] or {}
        kind = rule.get("type", "manual")
        if kind == "manual":
            continue
        if kind == "brand_listing":
            if fetched:
                sleep(LISTING_CRAWL_DELAY_SECONDS)
            fetched += 1
            want = listing_members(rule, balls, fetch_html)
        elif kind == "fields":
            want = fields_members(rule, conn, brand_id)
        else:
            logger.warning("unknown assign_rule type %r on line %s", kind, line["key"])
            continue
        if want is None:
            continue
        have = set(line["ball_ids"])
        with conn.cursor() as cur:
            for pid in sorted(want - have):
                cur.execute("""
                    insert into brand_line_balls (line_id, product_id, position, added_by)
                    values (%s, %s, (select coalesce(max(position), 0) + 1 from brand_line_balls where line_id = %s), 'rule')
                    on conflict do nothing
                """, (line["line_id"], pid, line["line_id"]))
                added.append({"line": line["key"], "product_id": pid})
            for pid in sorted((have & current) - want):
                cur.execute("""
                    delete from brand_line_balls where line_id = %s and product_id = %s and added_by = 'rule'
                """, (line["line_id"], pid))
                if cur.rowcount:
                    removed.append({"line": line["key"], "product_id": pid})
        conn.commit()

    # EXCLUSIVE lines (rule "exclusive": true) hold balls that sit outside
    # the brand's other groupings. Al, on the 22 Storm balls in no reaction
    # profile: "mix, ice, clear, and norm duke are spare balls. tropical
    # is entry level." Storm doesn't profile spare and entry balls, so a
    # ball in an exclusive line isn't "unplaced" anywhere, and an exclusive
    # line's own grouping never flags the balls that aren't in it.
    ruled, exempt = {}, set()
    for line in list_lines(conn, brand_id):
        rule = line["assign_rule"] or {}
        ruled.setdefault(line["grouping_key"], {"label": line["grouping_label"], "ruled": False, "members": set()})
        g = ruled[line["grouping_key"]]
        if rule.get("exclusive"):
            exempt |= set(line["ball_ids"])
        elif not rule.get("partial"):
            # A PARTIAL line (rule "partial": true, runbook 6fy) names only
            # some of the brand's balls -- Hammer's families (Black Widow,
            # Zero Mercy, Vibe...) leave out Raw Hammer, Axe, Spawn and the
            # rest -- so a ball in none of them isn't "unplaced".
            g["ruled"] = g["ruled"] or rule.get("type", "manual") != "manual"
        g["members"] |= set(line["ball_ids"])
    unplaced = []
    names = {b["product_id"]: b["name"] for b in balls}
    with conn.cursor() as cur:
        # A ball that's no longer a current published ball (retired, by
        # the brand or by a status override -- 6fs) can't be "unplaced":
        # its pending flags are closed. Runbook 6fw: Storm's Identity balls
        # kept four flags after Al retired them ("identity are discontinued
        # limited releases").
        cur.execute("""
            update review_queue r set status = 'rejected', resolved_at = now(), resolved_by = 'brand_art'
            from products p
            where p.id = r.product_id and r.field_name = 'brand_line' and r.status = 'pending'
              and p.brand_id = %s and not (p.status = 'current' and p.published and p.product_type = 'ball')
        """, (brand_id,))
        for gkey, g in ruled.items():
            # A pending item whose ball has since been placed (or is now
            # exempt) is settled by this run, not left for a person.
            cur.execute("""
                update review_queue set status = 'approved', resolved_at = now(), resolved_by = 'brand_art'
                where field_name = 'brand_line' and proposed_value = %s and status = 'pending'
                  and product_id = any(%s::uuid[])
            """, (gkey, sorted(g["members"] | exempt)))
            if not g["ruled"]:
                continue
            for pid in sorted(current - g["members"] - exempt):
                unplaced.append({"grouping": gkey, "product_id": pid})
                cur.execute("""
                    insert into review_queue (product_id, field_name, current_value, proposed_value, source, reason)
                    select %s, 'brand_line', null, %s, 'brand_art', %s
                    where not exists (select 1 from review_queue where product_id = %s and field_name = 'brand_line'
                                      and proposed_value = %s and status = 'pending')
                """, (pid, gkey, f"'{names.get(pid, pid)}' is in no {g['label']} line, and no line's rule placed it",
                      pid, gkey))
    conn.commit()
    return {"added": added, "removed": removed, "unplaced": unplaced}


# ------------------------------------------------------------ BigCommerce

def fetch_brand_catalog(bc_get, bigcommerce_brand_id: int) -> list:
    """Every BigCommerce product of the brand, with images and variants
    (for stock). bc_get(path, params) -> parsed JSON."""
    products, page = [], 1
    while True:
        d = bc_get("/v3/catalog/products", {"brand_id": bigcommerce_brand_id, "limit": 250, "page": page,
                                             "include": "images,variants"})
        products += d.get("data") or []
        pages = ((d.get("meta") or {}).get("pagination") or {}).get("total_pages") or 1
        if page >= pages:
            return products
        page += 1


def fetch_category_roots(bc_get) -> dict:
    """{category_id: full category path, lowercased, " > "-joined, e.g.
    "bags > roller bags > 3 ball rollers"}. The first part is the root."""
    cats, page = [], 1
    while True:
        d = bc_get("/v3/catalog/trees/categories", {"limit": 250, "page": page})
        cats += d.get("data") or []
        pages = ((d.get("meta") or {}).get("pagination") or {}).get("total_pages") or 1
        if page >= pages:
            break
        page += 1
    parent = {c["category_id"]: c.get("parent_id") or 0 for c in cats}
    name = {c["category_id"]: c["name"] for c in cats}
    paths = {}
    for cid in parent:
        x, seen, parts = cid, set(), []
        while x and x not in seen:
            seen.add(x)
            parts.append((name.get(x) or "").strip().lower())
            x = parent.get(x)
        paths[cid] = " > ".join(reversed(parts))
    return paths


def _root(path: str) -> str:
    return (path or "").split(" > ")[0]


def in_stock(p: dict) -> bool:
    """Visible, purchasable, and stock available to sell: product-level
    inventory, or any variant's when tracked by variant; untracked counts
    as available (BigCommerce sells it without a count)."""
    if not p.get("is_visible") or p.get("availability") == "disabled":
        return False
    tracking = p.get("inventory_tracking")
    if tracking == "none":
        return True
    if tracking == "variant":
        return any((v.get("inventory_level") or 0) > 0 and not v.get("purchasing_disabled")
                   for v in p.get("variants") or [])
    return (p.get("inventory_level") or 0) > 0


def stocked_companions(catalog: list, paths: dict) -> list:
    """The brand's stocked non-ball products with a photo, newest listing
    first: [{"bigcommerce_product_id", "name", "kind", "photo", "date",
    "price", "categories"}]. paths is fetch_category_roots()."""
    out = []
    for p in catalog:
        cats = [paths.get(c, "") for c in p.get("categories") or []]
        kinds = {COMPANION_ROOTS[_root(c)] for c in cats if _root(c) in COMPANION_ROOTS}
        if not kinds or "balls" in {_root(c) for c in cats}:
            continue
        if not in_stock(p):
            continue
        imgs = sorted(p.get("images") or [], key=lambda i: (not i.get("is_thumbnail"), i.get("sort_order") or 0))
        if not imgs:
            continue
        kind = next(k for k in ("bag", "shoes", "accessory", "apparel") if k in kinds)
        out.append({"bigcommerce_product_id": p["id"], "name": p["name"].strip(), "kind": kind,
                    "photo": imgs[0].get("url_zoom") or imgs[0].get("url_standard"),
                    "date": p.get("date_created") or "",
                    "price": float(p.get("calculated_price") or p.get("price") or 0),
                    "categories": cats})
    # BigCommerce dates are RFC 2822 strings; parse for a real newest-first order.
    def key(c):
        try:
            return datetime.datetime.strptime(c["date"], "%a, %d %b %Y %H:%M:%S %z").timestamp()
        except Exception:
            return 0
    return sorted(out, key=key, reverse=True)


COMPANION_KIND_ORDER = ("bag", "shoes", "accessory", "apparel")

# Al, on the first companion hero (a $79.95 1-ball roller): "can you use
# higher priced bags, 3 ball roller or triple totes? that is an entry
# level bag and isn't idea[l]". So the showcase bags are Bowler Depot's
# 3-ball (and bigger) rollers and totes -- read from the store's own
# category tree ("Bags > Roller Bags > 3 Ball Rollers", "Bags > Tote Bags
# > 3 Ball Totes"), with the name as a fallback -- and within a tier the
# most expensive first.
_SHOWCASE_BAG = re.compile(r"\b([3-6] ball|triple|three ball)\b")


def _bag_tier(c: dict) -> int:
    """0: 3+ ball roller, 1: 3+ ball tote, 2: other roller, 3: other bag."""
    text = " ".join(c.get("categories") or []) + " " + c["name"].lower()
    big = bool(_SHOWCASE_BAG.search(text))
    roller = "roller" in text
    return 0 if big and roller else 1 if big else 2 if roller else 3


# A companion's longest real dimension as photographed, in inches, so the
# ball beside it is drawn to scale (Al: "size ratios are way off").
# BigCommerce's own dimensions are placeholders on these listings (10 x 10
# x 10 on every Storm bag, 2026-11-02), so these are typical sizes by type,
# matched on the store category path plus the name, first match wins.
# Rollers are photographed with the handle up, so they include it.
# Calibrated on Al's rule for the Rolling Thunder 3 Ball Roller: "the ball
# should [be] around 1/3 the height of the white part of the bag" -- the
# body is ~70% of the photographed height with the handle up, so 36"
# overall puts an 8.5" ball at ~1/3 of the body (45" made it "two small").
# The other rollers and the 3-ball tote are scaled the same way (a ball is
# ~1/N of an N-ball bag's long side).
COMPANION_INCHES = [
    (re.compile(r"\b([3-6] ball|triple|three ball)\b.*roller|roller.*\b([3-6] ball|triple)\b"), 36),
    (re.compile(r"\b(2 ball|double)\b.*roller|roller.*\b(2 ball|double)\b"), 30),
    (re.compile(r"roller"), 24),
    (re.compile(r"\b([3-6] ball|triple|three ball)\b"), 27),       # 3-ball totes lie flat: three balls long
    (re.compile(r"\b(2 ball|double)\b"), 18),
    (re.compile(r"backpack"), 20),
    (re.compile(r"shoe bag"), 14),
    (re.compile(r"\b(1 ball|single|solo|spare kit)\b|tote"), 13),
    (re.compile(r"accessory bag"), 12),
    (re.compile(r"shoe"), 12),
    (re.compile(r"glove|wrist|support|positioner"), 9),
    (re.compile(r"shirt|jersey|hoodie|jacket|polo|apparel"), 30),
]
COMPANION_INCHES_DEFAULT = {"bag": 20, "shoes": 12, "accessory": 9, "apparel": 30}


def companion_inches(c: dict) -> float:
    text = " ".join(c.get("categories") or []) + " " + c["name"].lower()
    for pattern, inches in COMPANION_INCHES:
        if pattern.search(text):
            return inches
    return COMPANION_INCHES_DEFAULT.get(c["kind"], 20)


def pick_companion(companions: list, hero: dict) -> dict:
    """The non-ball product a companion hero features. A pinned
    bigcommerce_product_id (hero["bigcommerce_product_id"]) wins while it's
    stocked. Otherwise bags first, then shoes, then accessories and
    apparel; bags by _bag_tier (3-ball rollers, then 3-ball totes, then
    other rollers, then the rest); highest price first within a tier, then
    newest listing (companions is newest-first and sorted() is stable)."""
    pinned = hero.get("bigcommerce_product_id")
    for c in companions:
        if pinned and c["bigcommerce_product_id"] == pinned:
            return c
    return sorted(companions, key=lambda c: (COMPANION_KIND_ORDER.index(c["kind"]),
                                             _bag_tier(c) if c["kind"] == "bag" else 0,
                                             -(c.get("price") or 0)))[0]


def stocked_bc_ids(catalog: list) -> set:
    return {p["id"] for p in catalog if in_stock(p)}


# --------------------------------------------------------------- planning

def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _ball_item(b: dict) -> dict:
    return {"product_id": b["product_id"], "bigcommerce_product_id": b["bigcommerce_product_id"],
            "name": b["name"], "kind": "ball", "photos": b["photos"][:3]}


def _join(names: list) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def plan_slots(task: dict, balls: list, lines: list, companions: list, stocked: set,
               today: datetime.date) -> list:
    """Every slot the task draws, with its inputs and their fingerprint.
    Slots: the hero slots from params.heroes (DEFAULT_HEROES), plus one
    line image per line of the groupings named in params.line_groupings.

    Line images are OFF unless a task names groupings (runbook 6ft). Al,
    through the Bowler Depot brand-stores session: Bowler Depot "doesn't
    need line images after all. Each one only showed the balls listed right
    below it" -- so "drop line slots from the brand-art task for every
    brand". Heroes and lineups are still wanted. A slot with nothing to
    draw (a line with no current ball that has a photo) is left out."""
    params = task["params"]
    palette = params["palette"]
    brand = task["brand_name"]
    groupings = params.get("line_groupings") or []
    by_id = {b["product_id"]: b for b in balls if b["photos"]}
    newest_first = list(by_id.values())  # list_brand_balls order
    slots = []

    for line in lines:
        if line["grouping_key"] not in groupings:
            continue
        members = [by_id[p] for p in line["ball_ids"] if p in by_id]
        members.sort(key=lambda b: (b["release_date"] or datetime.date.min), reverse=True)
        members = members[:4]
        if not members:
            continue
        items = [_ball_item(b) for b in members]
        alt = f"{brand} {_join([b['name'] for b in members])} bowling ball{'s' if len(members) > 1 else ''}"
        slots.append({"slot_key": f"line-{line['key']}", "kind": "line", "line_id": line["line_id"],
                      "hero_type": None, "items": items, "alt": alt, "sizes": [LINE_SIZE],
                      "input_hash": _hash({"palette": palette, "items": items})})

    # Heroes: newest RELEASED balls (release_date today or earlier), and
    # among those the ones Bowler Depot has in stock first, so the slide's
    # "shop" link lands on something buyable. Then any released ball.
    released = [b for b in newest_first if b["release_date"] is None or b["release_date"] <= today]
    preferred = [b for b in released if b["bigcommerce_product_id"] in stocked] + \
                [b for b in released if b["bigcommerce_product_id"] not in stocked]
    used = set()

    def next_ball():
        """The newest preferred ball no earlier hero slot has taken, so
        rank 1, 2, 3 come out as the 1st, 2nd and 3rd newest."""
        pool = [b for b in preferred if b["product_id"] not in used]
        return pool[0] if pool else None

    for i, h in enumerate(params.get("heroes") or DEFAULT_HEROES, start=1):
        slot_key = f"hero-{i}"
        items, hero_type = [], h.get("type", "product")
        if hero_type == "line_mix":
            # "Find your reaction": the newest ball from each populated line
            # of one grouping (h["grouping"], else the brand's first grouping
            # with two or more populated lines), in the brand's line order,
            # no ball twice. Drawn like a lineup hero.
            hero_type = "lineup"
            # A ball in an exclusive line (a spare) is never featured, even
            # when a family also lists it: the Hammer theme maps Black
            # Widow Spare+ under Black Widow, and it's that family's newest.
            exclusive = {p for l in lines if (l["assign_rule"] or {}).get("exclusive") for p in l["ball_ids"]}
            populated = [l for l in lines if any(p in by_id and p not in exclusive for p in l["ball_ids"])
                         and not (l["assign_rule"] or {}).get("exclusive")]
            gkey = h.get("grouping") or next(
                (g for g in dict.fromkeys(l["grouping_key"] for l in populated)
                 if sum(1 for l in populated if l["grouping_key"] == g) >= 2), None)
            seen = set()
            for l in populated:
                if l["grouping_key"] != gkey:
                    continue
                # Newest; a same-day tie (Hammer's Zero Mercy Hybrid and
                # Tour) goes to the higher demand, so the pick is stable.
                members = sorted((by_id[p] for p in l["ball_ids"] if p in by_id and p not in seen and p not in exclusive),
                                 key=lambda b: (b["release_date"] or datetime.date.min,
                                                b.get("demand_score") if b.get("demand_score") is not None else -1),
                                 reverse=True)
                if members:
                    seen.add(members[0]["product_id"])
                    items.append(members[0])
            # Al: "the balls in order of demand with highest infront"
            # (products.demand_score, the one the partner API's demand
            # block reports). The first item is the front ball in the scene
            # and the front-center ball in the composite.
            items.sort(key=lambda b: b.get("demand_score") if b.get("demand_score") is not None else -1,
                       reverse=True)
            items = [_ball_item(b) for b in items[:6]]
        elif hero_type == "lineup":
            ids = h.get("ball_ids") or []
            if h.get("line"):
                line = next((l for l in lines if l["key"] == h["line"]), None)
                ids = line["ball_ids"] if line else []
            members = [by_id[p] for p in ids if p in by_id][:4] or preferred[:4]
            items = [_ball_item(b) for b in members]
        elif h.get("pick") == "newest_companion" and companions and preferred:
            # "a new Storm ball with a Storm roller bag": the ball beside the
            # companion is the brand's newest, even if hero-1 features it too.
            c = pick_companion(companions, h)
            items = [{"product_id": None, "bigcommerce_product_id": c["bigcommerce_product_id"], "name": c["name"],
                      "kind": c["kind"], "photos": [c["photo"]], "inches": companion_inches(c)},
                     _ball_item(preferred[0])]
        else:
            if h.get("include_preorder"):
                # Newest current ball whatever its release date or stock.
                ball = next((b for b in newest_first if b["product_id"] not in used), None)
            else:
                ball = next_ball()
            if ball:
                used.add(ball["product_id"])
                items = [_ball_item(ball)]
        if not items:
            continue
        shelf = []
        # No ball display behind the counter (6fv): with real balls given
        # as references (6, then 12), Gemini still invented balls to fill
        # the display in nearly every attempt and scene_check rejected them
        # all. Al's rule was about the balls -- "the rest of the products
        # were fine" -- so the shop keeps its bags, accessories and shoe
        # wall and shows no balls but the counter's. `shelf` stays for a
        # task that lists display balls explicitly (h["display_ball_ids"]).
        if hero_type == "lineup" and h.get("display_ball_ids"):
            shelf = [_ball_item(by_id[p]) for p in h["display_ball_ids"] if p in by_id][:DISPLAY_BALLS]
        if hero_type == "lineup":
            alt = f"{brand} {_join([x['name'] for x in items])} bowling balls"
        elif items[0]["kind"] == "ball":
            alt = f"{brand} {items[0]['name']} bowling ball"
        else:
            alt = items[0]["name"] + (f" with the {brand} {items[1]['name']} bowling ball" if len(items) > 1 else "")
        slots.append({"slot_key": slot_key, "kind": "hero", "line_id": None, "hero_type": hero_type,
                      "items": items, "shelf": shelf, "alt": alt, "sizes": [HERO_SIZE, HERO_WIDE_SIZE],
                      "input_hash": _hash({"palette": palette, "type": hero_type, "items": items,
                                           **({"shelf": shelf} if shelf else {})})})
    return slots


def _trigger_for(slot: dict, stored: dict | None, task: dict, weekly: bool) -> str:
    """Which of the request's trigger rules this slot's change is."""
    if stored is None or not stored.get("input_hash"):
        return "ball_released" if slot["kind"] == "hero" else "line_changed"
    old = {(f.get("product_id"), f.get("bigcommerce_product_id")) for f in stored.get("featured") or []}
    new = {(f.get("product_id"), f.get("bigcommerce_product_id")) for f in slot["items"]}
    if stored["input_hash"] == slot["input_hash"]:
        return "weekly_sweep" if weekly else "none"
    if old != new:
        if slot["kind"] == "line":
            return "line_changed"
        gone = old - new
        if any(o[0] is None for o in gone):
            return "companion_stock"
        return "ball_released"
    return "photo_changed"  # same products: a photo or the palette changed


def due_slots(task: dict, slots: list, stored: dict, now: datetime.datetime) -> dict:
    """{slot_key: trigger} for the slots this dispatch should redraw."""
    last_full = task.get("last_full_run_at")
    weekly = last_full is None or now - last_full >= WEEKLY_SWEEP
    out = {}
    for s in slots:
        trig = _trigger_for(s, stored.get(s["slot_key"]), task, weekly)
        if trig != "none":
            out[s["slot_key"]] = trig
    return out


# -------------------------------------------------------------- rendering

def first_photo(item: dict, fetch_bytes) -> tuple:
    """(bytes, url) of the first of the item's photo URLs that loads."""
    for url in item["photos"]:
        try:
            return fetch_bytes(url), url
        except Exception:
            logger.warning("photo %s didn't load", url)
    raise RuntimeError(f"no photo of {item['name']} could be loaded")


HERO_BACKDROP_THEME = (
    "a clean, premium studio backdrop in the brand's deep {background} with soft {accent} rim light and a gentle "
    "{accent} glow behind the ball; the ball sits in the right half of the frame and the left third is calm, empty "
    "backdrop (a headline is laid over it later in HTML)"
)
# Runbook 6gd. Al, via the Radical brand-store session, on Radical's
# hero-1 (palette #fff200 / #000000): "a dark black glow around the Intel
# Recon Hybrid ... please redraw both hero-1 files on a clean flat #fff200
# ... with no black vignette", and the 4:3 "reads amber/golden rather than
# #fff200". HERO_BACKDROP_THEME's "deep {background}" and "{accent} glow"
# are right for a dark band (Storm, Hammer) and wrong for a pale one, so a
# light background gets its own backdrop, and the result's backdrop is
# checked against the band's color (backdrop_matches).
HERO_BACKDROP_LIGHT = (
    "a seamless, flat studio backdrop of exactly {background} -- that exact color, not darker, warmer or more "
    "golden -- evenly lit from edge to edge: no vignette, no darkening toward the edges or corners, no colored glow, "
    "rim light or halo around the ball, only a soft natural contact shadow directly under the ball; the ball sits in "
    "the right half of the frame and the left third is calm, empty backdrop (a headline is laid over it later in "
    "HTML)"
)
BACKDROP_TOLERANCE = 36   # max channel difference from the band's color, at the frame's edges


def backdrop_matches(im, value: str, tolerance: int = BACKDROP_TOLERANCE) -> bool:
    """True when the frame's edges (top corners, left middle, top middle --
    away from the ball and its shadow) are within `tolerance` of the band's
    color, so the art blends into it."""
    want = compose.hex_rgb(value)
    w, h = im.size
    small = im.convert("RGB").resize((64, 48))
    spots = [(2, 2), (61, 2), (2, 24), (32, 2), (8, 12)]
    for x, y in spots:
        px = small.getpixel((x, y))
        if max(abs(a - b) for a, b in zip(px, want)) > tolerance:
            return False
    return True


HERO_NO_TEXT = (
    " There must be no text, words, letters, numbers, logos or brand marks anywhere in the image other than what "
    "is printed on the ball itself in the reference photo; no props, no lane, no pins, no people."
)


# Lineup heroes ("Find your reaction") as a pro shop scene (runbook 6fv).
# (The notes below are the history; DISPLAY_* after the prompt is current.)
# Al, on the plain row: "the balls just sitting there aren't doing it. can
# we set a pro shop scene and put them on the counter or something like
# that"; on the first prototype: "that looks fine". That prototype is the
# look -- this prompt is its wording, with two changes Al asked for:
#   * "we have wood counter tops, butcher block stained darker in most of
#     our shops" (the prototype had a dark polished top);
#   * "if you are going to put products on the shelves behind them pull it
#     from the poducts on the site. don't make things up" (the prototype's
#     shelves were full of invented balls): the shelves hold only the
#     brand's stocked Bowler Depot products, passed as reference photos,
#     or nothing. scene_check rejects a scene that shows anything else.
# (A rewrite that also moved the camera back and spread the balls out was
# "way better" in the original -- so nothing else here changes.)
# Then: "also a closer up view with the balls in order of demand with
# highest infront and the focal depth short so they start to blur after
# the first or second ball" -- so the row recedes from the camera in
# demand order with a shallow depth of field, and only the balls in focus
# can be logo-checked (SCENE_SHARP_BALLS).
SCENE_PROMPT = (
    "Create a photorealistic, premium photograph of a modern bowling pro shop. In the foreground is a clean pro shop "
    "counter with a butcher block wood top, stained dark, running across the frame. Behind it, softly out of focus, "
    "the shop: wall shelving with bowling bags, accessories and a wall of bowling shoes, lit in deep {background} "
    "with {accent} accent lighting. {display} "
    "A close-up, low camera angle along the counter top: exactly the {n} bowling balls from the first {n} "
    "reference photos stand in a row that recedes away from the camera along the counter, in the order of the "
    "references -- the first reference ball nearest the camera and largest in frame, at the right, then each next "
    "one further back toward the left. Very shallow depth of field: the first ball is tack sharp, the second is "
    "only just starting to soften, and from the third on the balls blur progressively into the background (the "
    "blurred left side is where a headline is laid over later in HTML). Each ball must be carried over EXACTLY as "
    "it appears in its own "
    "reference photo: same colors, swirl pattern and pearl/solid surface, and the same printed logo and graphics at "
    "the same size relative to the ball -- do not redraw, enlarge, simplify or swap logos between balls, and do not "
    "add any ball that isn't in the references. All balls are the same real size. "
    "There must be no text, words, letters, numbers, signs, posters, logos or brand marks anywhere in the image other "
    "than what is printed on the balls themselves. No people, hands, bowling pins, shoes or price tags."
)
# Al, on the version with only bags on the shelves: "now it doesn't look
# like a proshop. the rest of the products were fine" -- it was only the
# made-up BALLS in the prototype's ball display that were the problem. So
# the display holds real balls: the brand's other current balls, passed as
# reference photos after the counter's.
DISPLAY_WITH_BALLS = (
    "The ball display is small: it holds at most {m} bowling balls, and every one of them is one of the {m} balls in "
    "the last {m} reference photos, exactly as photographed -- no made-up ball designs, no repeats; the rest of the "
    "shelving holds bags and accessories, and a spot stays empty rather than hold an invented ball."
)
DISPLAY_EMPTY = ("There are NO bowling balls anywhere in the background -- no ball display, no balls on shelves; the only "
                 "bowling balls in the image are the ones on the counter.")
# 12, not 6: with six, every 16:9 attempt filled its wider display with
# invented balls and failed scene_check (2026-11-02 close-up render).
DISPLAY_BALLS = 12
SCENE_SHARP_BALLS = 2   # the front balls in focus, the only ones whose logo can be checked


def render_lineup_scene(items: list, size: tuple, palette: dict, deps: dict, shelf: list = ()):
    """A lineup hero as a pro shop scene from Gemini, or None when no
    attempt passes the checks (deps["gemini_scene"] returns None when a
    ball's logo or the background balls fail). The real photos are the
    references: the counter's balls, then the display's (`shelf`)."""
    scene = deps.get("gemini_scene")
    if not scene or deps.get("_scene_failed"):
        return None
    raws = [first_photo(i, deps["fetch_bytes"])[0] for i in items]
    shelf_raws = []
    for p in shelf:
        try:
            shelf_raws.append(first_photo(p, deps["fetch_bytes"])[0])
        except RuntimeError:
            pass
    aspect = "16:9" if size[0] / size[1] > 1.5 else "4:3"
    display = DISPLAY_WITH_BALLS.format(m=len(shelf_raws)) if shelf_raws else DISPLAY_EMPTY
    prompt = SCENE_PROMPT.format(n=len(raws), display=display, **palette)
    for attempt in range(GEMINI_HERO_ATTEMPTS):
        try:
            png = scene(raws, shelf_raws, prompt, aspect)
        except Exception:
            logger.exception("brand-art scene attempt %d failed", attempt + 1)
            _backoff(deps, attempt)
            continue
        if png:
            from PIL import Image, ImageOps
            return ImageOps.fit(Image.open(io.BytesIO(png)).convert("RGB"), size, Image.LANCZOS)
    deps["_scene_failed"] = True
    return None


GEMINI_BACKOFF_SECONDS = 20


def _backoff(deps: dict, attempt: int) -> None:
    """Runbook 6gc: wait before the next Gemini attempt after an error
    (20s, then 40s). Four brands drawn at once on 2026-11-03 drew Vertex
    AI's 429 "Resource has been exhausted" and fell back to composites; an
    immediate retry just hits the same limit."""
    if attempt < GEMINI_HERO_ATTEMPTS - 1:
        deps.get("sleep", time.sleep)(GEMINI_BACKOFF_SECONDS * (attempt + 1))


def render_ball_product_hero(item: dict, size: tuple, palette: dict, deps: dict) -> tuple:
    """A single-ball product hero from the existing Gemini product-shot
    pipeline (app.build_gemini_scene_prompt, call_gemini_for_image,
    check_logo_matches, logo_size_ok -- the announcement job's loop), with
    the brand's palette as the scene. Falls back to the composite when no
    attempt passes the logo checks. Returns (PIL image, method)."""
    gen = deps.get("gemini")
    raw, _ = first_photo(item, deps["fetch_bytes"])
    if gen and deps.get("_gemini_failed") != item["product_id"]:
        aspect = "16:9" if size[0] / size[1] > 1.5 else "4:3"
        light = compose.is_light(palette["background"])
        theme = (HERO_BACKDROP_LIGHT if light else HERO_BACKDROP_THEME).format(**palette)
        for attempt in range(GEMINI_HERO_ATTEMPTS):
            try:
                png = gen(item["product_id"], raw, theme, HERO_NO_TEXT, aspect)
            except Exception:
                logger.exception("brand-art hero attempt %d failed for %s", attempt + 1, item["name"])
                _backoff(deps, attempt)
                continue
            if png:
                from PIL import Image, ImageOps
                im = ImageOps.fit(Image.open(io.BytesIO(png)).convert("RGB"), size, Image.LANCZOS)
                if light and not backdrop_matches(im, palette["background"]):
                    logger.info("brand-art hero attempt %d for %s: backdrop isn't %s", attempt + 1, item["name"],
                                palette["background"])
                    continue
                return im, "gemini"
        # Don't spend the other size's attempts once one size has failed:
        # the slot falls back to the composite for both (render_slot).
        deps["_gemini_failed"] = item["product_id"]
    return compose.product_hero(size, palette, compose.cutout(raw, deps.get("remove_bg"))), "composite"


def render_slot(slot: dict, palette: dict, deps: dict) -> tuple:
    """[(size, PIL image)], method."""
    fetch = deps["fetch_bytes"]
    if slot["kind"] == "line":
        cuts = [compose.cutout(first_photo(i, fetch)[0], deps.get("remove_bg")) for i in slot["items"]]
        return [(LINE_SIZE, compose.line(LINE_SIZE, palette, cuts))], "composite"
    out, method = [], "composite"
    if slot["hero_type"] == "lineup":
        # Pro shop scene for both sizes, or the composite for both (a
        # slide's two sizes always match, as for product heroes).
        deps.pop("_scene_failed", None)
        scenes = [render_lineup_scene(slot["items"], size, palette, deps, slot.get("shelf") or [])
                  for size in slot["sizes"]]
        if all(im is not None for im in scenes):
            return list(zip(slot["sizes"], scenes)), "gemini"
        cuts = [compose.cutout(first_photo(i, fetch)[0], deps.get("remove_bg")) for i in slot["items"]]
        for size in slot["sizes"]:
            out.append((size, compose.lineup_hero(size, palette, cuts)))
        return out, method
    focus = slot["items"][0]
    if focus["kind"] == "ball":
        # Both sizes of one hero are the same slide (4:3 desktop, 16:9
        # phone), so they come from the same method: if Gemini can't make
        # one of them pass the logo checks, both are the composite.
        drawn = [render_ball_product_hero(focus, size, palette, deps) for size in slot["sizes"]]
        if all(m == "gemini" for _, m in drawn):
            return [(size, im) for size, (im, _) in zip(slot["sizes"], drawn)], "gemini"
        cut = compose.cutout(first_photo(focus, fetch)[0], deps.get("remove_bg"))
        return [(size, compose.product_hero(size, palette, cut)) for size in slot["sizes"]], "composite"
    cut = compose.cutout(first_photo(focus, fetch)[0], deps.get("remove_bg"))
    beside = compose.cutout(first_photo(slot["items"][1], fetch)[0], deps.get("remove_bg")) if len(slot["items"]) > 1 else None
    for size in slot["sizes"]:
        out.append((size, compose.product_hero(size, palette, cut, beside, focus.get("inches"))))
    return out, method


def slot_keys_for(slug: str, slot_key: str) -> tuple:
    return f"{ART_PREFIX}/{slug}/{slot_key}.webp", f"{ART_PREFIX}/{slug}/{slot_key}-16x9.webp"


def save_slot(conn, task_id: str, slot: dict, keys: tuple, method: str, error: str = None) -> None:
    # Shelf products are in the picture too, so they're listed (after the
    # balls), and the partner can tell them apart by kind.
    featured = [{k: i[k] for k in ("product_id", "bigcommerce_product_id", "name", "kind")}
                for i in slot["items"] + list(slot.get("shelf") or [])]
    w, h = slot["sizes"][0]
    with conn.cursor() as cur:
        if error:
            cur.execute("""
                insert into brand_art_slots (task_id, slot_key, kind, line_id, hero_type, error)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (task_id, slot_key) do update set error = excluded.error
            """, (task_id, slot["slot_key"], slot["kind"], slot["line_id"], slot["hero_type"], error[:500]))
        else:
            cur.execute("""
                insert into brand_art_slots (task_id, slot_key, kind, line_id, hero_type, featured, image_key,
                                             image_key_16x9, width, height, alt, method, input_hash, generated_at, error)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), null)
                on conflict (task_id, slot_key) do update set
                    kind = excluded.kind, line_id = excluded.line_id, hero_type = excluded.hero_type,
                    featured = excluded.featured, image_key = excluded.image_key,
                    image_key_16x9 = excluded.image_key_16x9, width = excluded.width, height = excluded.height,
                    alt = excluded.alt, method = excluded.method, input_hash = excluded.input_hash,
                    generated_at = now(), error = null
            """, (task_id, slot["slot_key"], slot["kind"], slot["line_id"], slot["hero_type"],
                  json.dumps(featured), keys[0], keys[1] if len(slot["sizes"]) > 1 else None, w, h, slot["alt"],
                  method, slot["input_hash"]))
    conn.commit()


# ---------------------------------------------------------------- running

def gather(conn, task: dict, deps: dict, assign: bool) -> tuple:
    """(slots, assign_result) for a task, reading BigCommerce once."""
    balls = list_brand_balls(conn, task["brand_id"])
    assigned = apply_assign_rules(conn, task["brand_id"], balls, deps["fetch_html"]) if assign else None
    lines = list_lines(conn, task["brand_id"])
    companions, stocked = [], set()
    bc_brand = task["params"].get("bigcommerce_brand_id")
    if bc_brand and deps.get("bc_get"):
        catalog = fetch_brand_catalog(deps["bc_get"], bc_brand)
        companions = stocked_companions(catalog, fetch_category_roots(deps["bc_get"]))
        stocked = stocked_bc_ids(catalog)
    slots = plan_slots(task, balls, lines, companions, stocked, deps.get("today") or datetime.date.today())
    return slots, assigned


def run_task(conn, task_id: str, deps: dict, slot_keys: list | None = None, trigger: str = "manual",
             triggers_by_slot: dict | None = None) -> dict:
    """Draw the given slots (all when None) and store them at their fixed
    keys. Logs a task_runs row. One slot failing doesn't stop the rest."""
    task = load_task(conn, task_id)
    if task is None:
        raise ValueError(f"no task {task_id}")
    with conn.cursor() as cur:
        cur.execute("update tasks set status = 'running', updated_at = now() where id = %s", (task_id,))
        cur.execute("insert into task_runs (task_id, trigger, slots) values (%s, %s, %s) returning id",
                    (task_id, trigger, slot_keys or []))
        run_id = cur.fetchone()[0]
    conn.commit()

    detail = {"slots": {}, "assigned": None}
    try:
        slots, assigned = gather(conn, task, deps, assign=trigger == "manual")
        detail["assigned"] = assigned
        slug = task["params"].get("slug") or re.sub(r"[^a-z0-9]+", "-", task["brand_name"].lower()).strip("-")
        palette = task["params"]["palette"]
        for slot in slots:
            if slot_keys and slot["slot_key"] not in slot_keys:
                continue
            keys = slot_keys_for(slug, slot["slot_key"])
            try:
                images, method = render_slot(slot, palette, deps)
                for (size, im), key in zip(images, keys):
                    deps["put_object"](key, compose.encode_webp(im), "image/webp", CACHE_CONTROL)
                save_slot(conn, task_id, slot, keys, method)
                detail["slots"][slot["slot_key"]] = {"ok": True, "method": method,
                                                     "trigger": (triggers_by_slot or {}).get(slot["slot_key"], trigger),
                                                     "featured": [i["name"] for i in slot["items"]]}
            except Exception as e:
                logger.exception("brand-art slot %s failed", slot["slot_key"])
                save_slot(conn, task_id, slot, keys, None, error=str(e) or type(e).__name__)
                detail["slots"][slot["slot_key"]] = {"ok": False, "error": str(e)[:200]}
        ok = [s for s in detail["slots"].values() if s["ok"]]
        status = "succeeded" if len(ok) == len(detail["slots"]) else ("partial" if ok else "failed")
        # Runbook 6ge: clear the image CDN's copies of this brand's art.
        # Al: "the images in the admin are not all correct ... it is
        # correct when you click on it but the image on the main page is
        # wrong" -- admin shows resized copies (?w=480), which the CDN
        # keeps for up to an hour after a redraw, as it does the full-size
        # file the partner's slides load.
        if ok and deps.get("invalidate"):
            try:
                deps["invalidate"](f"/{ART_PREFIX}/{slug}/*")
            except Exception:
                logger.exception("couldn't clear the CDN cache for brand-art/%s", slug)
    except Exception as e:
        logger.exception("brand-art task %s failed", task_id)
        detail["error"] = str(e)[:500]
        status = "failed"
    full = slot_keys is None or trigger == "weekly_sweep"
    with conn.cursor() as cur:
        cur.execute("update task_runs set status = %s, detail = %s, finished_at = now() where id = %s",
                    (status, json.dumps(detail, default=str), run_id))
        cur.execute(f"""
            update tasks set status = %s, last_run_at = now(), last_result = %s, updated_at = now()
                {", last_full_run_at = now()" if full and status != "failed" else ""}
            where id = %s
        """, ("failed" if status == "failed" else "idle", json.dumps({"run_id": str(run_id), "status": status,
                                                                    **detail}, default=str), task_id))
    conn.commit()
    return {"task_id": task_id, "run_id": str(run_id), "status": status, **detail}


def dispatch(conn, deps: dict, invoke_run) -> dict:
    """The daily schedule. For each enabled task: apply the assign rules,
    plan, and collect the slots whose inputs changed (or every slot, once a
    week). Then invoke_run(queue) with the whole list of runs, in brand
    order: the caller starts the FIRST run and passes the rest along, and
    each run starts the next when it finishes (runbook 6gc). Started all at
    once, six brands' Gemini heroes exhaust Vertex AI's quota."""
    now = deps.get("now") or datetime.datetime.now(datetime.timezone.utc)
    out, queue = {}, []
    for task in list_enabled_tasks(conn):
        try:
            slots, assigned = gather(conn, task, deps, assign=True)
            due = due_slots(task, slots, load_slots(conn, task["id"]), now)
            enabled_rules = set(task["triggers"] or [])
            due = {k: v for k, v in due.items() if v in enabled_rules}
            weekly = any(v == "weekly_sweep" for v in due.values())
            if due:
                trigger = "weekly_sweep" if weekly else sorted(set(due.values()))[0]
                with conn.cursor() as cur:
                    cur.execute("update tasks set status = 'queued', updated_at = now() where id = %s", (task["id"],))
                conn.commit()
                queue.append({"task_id": task["id"], "slots": sorted(due), "trigger": trigger,
                              "triggers_by_slot": due})
            out[task["brand_name"]] = {"due": due, "assigned": assigned}
        except Exception as e:
            logger.exception("brand-art dispatch failed for %s", task["brand_name"])
            out[task["brand_name"]] = {"error": str(e)[:200]}
    if queue:
        invoke_run(queue)
    return out
