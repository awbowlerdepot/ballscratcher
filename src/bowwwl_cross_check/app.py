"""
QA cross-check against bowwwl.com (a third-party normalized cross-brand
bowling ball database), per the architecture doc's "Decided: use bowwwl.com
as a QA cross-check" section: fetch the same ball's page there, compare its
RG/DIFF/mass-bias against what this pipeline scraped from the manufacturer
directly, and flag disagreements beyond a small tolerance to review_queue
rather than silently trusting either source.

LEGAL NOTE, explicitly surfaced rather than quietly built around: bowwwl.com's
own Terms & Conditions (read directly this session, not assumed) state "You
must not: Republish material... Reproduce, duplicate or copy material...
Redistribute content from bowwwl.com." This module does exactly that at a
mechanical level -- it fetches bowwwl pages on a schedule and stores specific
field values (in review_queue.proposed_value) so a human reviewer can see
what disagrees. Built this way on explicit instruction after that finding was
raised; if the operating posture on this ever changes, the two places to
adjust are compare_to_our_data() (stop passing bowwwl's actual value into the
returned mismatch dicts, only the fact/magnitude of a disagreement) and
record_bowwwl_match() (stop persisting bowwwl_url at all).

Platform confirmation and real, confirmed structural facts (all read
directly off live bowwwl.com pages via Chrome DOM inspection this session,
not guessed at):

1. bowwwl.com runs on Drupal 10 (confirmed via <meta name="generator">).
   Unlike MOTIV, this site fetches successfully through a plain non-browser
   request -- confirmed by fetching this exact fixture's source URL through
   this sandbox's own non-browser fetch tool this session and getting back
   full real content, not a blank response.
2. URL pattern: https://www.bowwwl.com/bowling-ball-database/{brand-slug}/
   {ball-slug}. Slugs are lowercase, spaces become hyphens, and -- this is
   the one non-obvious real rule, confirmed against "Fury Emerald/Black
   Hybrid" -> "fury-emeraldblack-hybrid" -- a "/" in the name is DROPPED
   entirely, not converted to a hyphen ("Emerald/Black" -> "emeraldblack",
   not "emerald-black"). Multi-word brand names keep their hyphen too
   ("900 Global" -> "900-global", "Columbia 300" -> "columbia-300", both
   seen in real cross-links on a live page this session).
3. Most fields render as Drupal's standard label/value pair markup:
   <div class="field ..."><div class="field__label">LABEL</div>
   <div class="field__item">VALUE</div></div> -- matched by LABEL TEXT here
   (e.g. "RG", "Diff", "MB Diff", "Type", "Core Type", "Factory Finish"),
   not by the field's own CSS class, same content-matching philosophy as
   every other scraper in this project. Confirmed necessary, not just
   theoretical caution: this session directly observed the RG field's own
   class name was never actually captured (see the fixture files' header
   comments), while the label text "RG" was -- label-matching is the only
   approach verified to work for every field here.
4. Date fields (Release Date, PBA Approval Date) carry a full ISO
   date on the nested <time datetime="..."> attribute even when the
   DISPLAYED text is only month-precision (real confirmed example: Fury
   Emerald/Black Hybrid displays "Jul 2026" but datetime="2026-07-
   16T12:00:00Z"). This parser reads the datetime attribute, not the
   display text, for full precision. PBA Approval Date maps onto this
   project's own existing (and previously always-empty)
   products.usbc_approval_date column -- this is the first real source in
   this project that can actually populate it. Not every ball has a PBA
   Approval Date field at all (confirmed: present on Defender, absent on
   Fury) -- treated as optional, not required.
5. The "Discontinued" field is a labelless boolean:
   <div class="field field--name-field-discontinued field--type-boolean
   field--label-hidden field__item">...</div> -- EMPTY when false
   (confirmed on Fury, a current ball) and containing literal
   <strong>Discontinued</strong> when true (confirmed on Defender, a
   retired ball). No field__label to match against here, so this one
   field IS matched by its field--name-field-discontinued CSS class
   (an exact class-token match, not a substring match, to avoid
   colliding with unrelated field--name-field-* classes) -- the one
   deliberate exception to point 3's label-matching rule, because there's
   no label to match. bowwwl's discontinued flag is a boolean, not a date
   -- it doesn't populate this project's discontinued_date/
   discontinued_detected_at columns (see migration 003), only feeds a
   review_queue flag if it disagrees with our own scraped status.
6. Coverstock and core each render as a linked card:
   <div class="field field--name-field-coverstock ..."><h5 class=
   "card-title"><a>NAME</a></h5>...nested Type field...</div> for
   coverstock, but <div class="field field--name-field-core ...">
   <article...><h5 class="card-header"><a>NAME</a></h5>...nested Core
   Type field...</article></div> for core -- a real, confirmed markup
   INCONSISTENCY (card-title vs. card-header) this session found by
   direct comparison, not assumed to be identical just because both are
   "linked card" fields. Matched here by finding the first <a> tag inside
   each field container (exact class-token match on field--name-field-
   coverstock / field--name-field-core) rather than relying on either
   heading class, sidestepping the inconsistency rather than hard-coding
   around it.
7. Per-weight specs live in repeated card elements
   (.paragraph--type--core-specs) under field--name-field-core-specs,
   each with a weight heading ("16 pounds", any heading tag) followed by
   the same label/value RG/Diff/MB-Diff fields as point 3. MB Diff is only
   present on asymmetric balls (confirmed: absent on every one of Fury's
   5 weight cards, present on every one of Defender's).

What this module deliberately does NOT do: fuzzy-match coverstock/core
NAME text or factory finish strings against our own values. Those are
free-text marketing names/descriptions where cosmetic phrasing
differences are expected and not meaningful "wrong data" signals (e.g. a
manufacturer's own name for a coverstock vs. bowwwl's transcription of it
could differ in capitalization or wording without either being incorrect).
Only the genuinely comparable NUMERIC fields (RG, differential, mass bias,
per weight) and release_date get compared -- see compare_to_our_data().
"""
import logging
import re
from datetime import datetime, date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BASE_URL = "https://www.bowwwl.com"
DEFAULT_TOLERANCE = 0.001  # matches pdf_parser's find_mismatches default


def fetch_page(url: str, timeout: int = 30) -> str:
    """Fetch raw HTML. Kept separate from parsing so tests can feed real
    fixture HTML without a network call. Confirmed fetchable via a plain
    non-browser request this session (see module docstring point 1) --
    higher confidence than MOTIV's fetch_page, which is an unverified
    workaround."""
    import requests

    resp = requests.get(url, headers={"User-Agent": "bowling-scraper/1.0"}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def slugify_bowwwl_brand(brand_name: str) -> str:
    """Lowercase, spaces -> hyphens. Confirmed real against "900 Global"
    -> "900-global" and "Columbia 300" -> "columbia-300" seen in real
    cross-links this session."""
    return re.sub(r"\s+", "-", brand_name.strip().lower())


def slugify_bowwwl_ball(ball_name: str) -> str:
    """Lowercase, "/" dropped entirely (not hyphenated), spaces -> hyphens.
    Confirmed real against "Fury Emerald/Black Hybrid" ->
    "fury-emeraldblack-hybrid" and "Jackal Onyx" -> "jackal-onyx"."""
    cleaned = ball_name.strip().lower().replace("/", "")
    return re.sub(r"\s+", "-", cleaned)


def build_bowwwl_url(brand_name: str, ball_name: str) -> str:
    return f"{BASE_URL}/bowling-ball-database/{slugify_bowwwl_brand(brand_name)}/{slugify_bowwwl_ball(ball_name)}"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _find_label_value(scope: BeautifulSoup, label_text: str):
    """Finds a div.field__label matching label_text (case-insensitive,
    exact) within scope, returns the sibling field__item's cleaned text,
    or None if not found. The one shared building block behind point 3's
    label-matching approach."""
    for label_el in scope.find_all("div", class_="field__label"):
        if _clean(label_el.get_text()).lower() == label_text.lower():
            item_el = label_el.find_next_sibling("div", class_="field__item")
            if item_el is not None:
                return _clean(item_el.get_text())
    return None


def _find_date_value(scope: BeautifulSoup, label_text: str):
    """Same as _find_label_value but reads the nested <time datetime="...">
    ISO attribute for full-precision parsing rather than the display
    text -- see module docstring point 4."""
    for label_el in scope.find_all("div", class_="field__label"):
        if _clean(label_el.get_text()).lower() == label_text.lower():
            item_el = label_el.find_next_sibling("div", class_="field__item")
            if item_el is None:
                continue
            time_el = item_el.find("time")
            if time_el is None or not time_el.get("datetime"):
                continue
            try:
                return datetime.strptime(time_el["datetime"], "%Y-%m-%dT%H:%M:%SZ").date()
            except ValueError:
                return None
    return None


def _has_exact_class(tag, class_name: str) -> bool:
    classes = tag.get("class") or []
    return class_name in classes


def parse_discontinued(soup: BeautifulSoup):
    """Returns True/False, or None if the field itself isn't present at
    all (shouldn't happen based on what was seen this session -- every
    real page checked had it -- but not assumed universal)."""
    field = soup.find(lambda tag: tag.name == "div" and _has_exact_class(tag, "field--name-field-discontinued"))
    if field is None:
        return None
    return bool(_clean(field.get_text()))


def parse_coverstock(soup: BeautifulSoup) -> dict:
    field = soup.find(lambda tag: tag.name == "div" and _has_exact_class(tag, "field--name-field-coverstock"))
    if field is None:
        return {"coverstock_name": None, "coverstock_type_raw": None}
    link = field.find("a")
    name = _clean(link.get_text()) if link else None
    type_raw = _find_label_value(field, "Type")
    return {"coverstock_name": name, "coverstock_type_raw": type_raw}


def parse_core(soup: BeautifulSoup) -> dict:
    field = soup.find(lambda tag: tag.name == "div" and _has_exact_class(tag, "field--name-field-core"))
    if field is None:
        return {"core_name": None, "core_type_raw": None}
    link = field.find("a")
    name = _clean(link.get_text()) if link else None
    type_raw = _find_label_value(field, "Core Type")
    return {"core_name": name, "core_type_raw": type_raw}


def _to_float(value):
    if value is None:
        return None
    match = re.search(r"-?(?:\d+\.?\d*|\.\d+)", value)
    return float(match.group()) if match else None


def parse_weight_cards(soup: BeautifulSoup) -> list:
    """Returns a list of {weight_lbs, rg, differential, mass_bias} dicts,
    one per .paragraph--type--core-specs card -- see module docstring
    point 7. mass_bias is None for symmetric balls (no MB Diff field on
    that card), a real number for asymmetric ones."""
    results = []
    for card in soup.find_all(lambda tag: tag.name == "div" and _has_exact_class(tag, "paragraph--type--core-specs")):
        heading = card.find(class_="card-title")
        if heading is None:
            continue
        match = re.search(r"(\d{1,2})\s*pounds", _clean(heading.get_text()), re.IGNORECASE)
        if not match:
            continue
        weight_lbs = int(match.group(1))

        rg = _to_float(_find_label_value(card, "RG"))
        differential = _to_float(_find_label_value(card, "Diff"))
        mass_bias = _to_float(_find_label_value(card, "MB Diff"))

        if rg is None and differential is None:
            continue

        results.append({
            "weight_lbs": weight_lbs,
            "rg": rg,
            "differential": differential,
            "mass_bias": mass_bias,
        })

    return sorted(results, key=lambda s: s["weight_lbs"])


def parse_ball_page(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    h1 = soup.find("h1")
    name = _clean(h1.get_text()) if h1 else None

    coverstock = parse_coverstock(soup)
    core = parse_core(soup)

    return {
        "url": url,
        "name": name,
        "release_date": _find_date_value(soup, "Release Date"),
        "pba_approval_date": _find_date_value(soup, "PBA Approval Date"),
        "discontinued": parse_discontinued(soup),
        "factory_finish": _find_label_value(soup, "Factory Finish"),
        "coverstock_name": coverstock["coverstock_name"],
        "coverstock_type_raw": coverstock["coverstock_type_raw"],
        "core_name": core["core_name"],
        "core_type_raw": core["core_type_raw"],
        "skus": parse_weight_cards(soup),
    }


def is_plausible_match(bowwwl_parsed: dict, our_ball_name: str) -> bool:
    """Confirms the fetched page is actually about the ball we asked for,
    not a soft-404/redirect-to-something-else. bowwwl's real 404 behavior
    (status code vs. a "soft 404" page) was never directly checked this
    session, so this matches defensively on content instead of trusting
    an HTTP status code alone: the page's own <h1> must be present and
    share at least one real word (3+ chars) with the name we searched
    for. Deliberately loose (not an exact-string match) since bowwwl's
    own name formatting won't always match ours exactly (e.g. punctuation,
    "Bowling Ball" suffixes)."""
    if not bowwwl_parsed.get("name"):
        return False
    their_words = {w for w in re.findall(r"[a-z0-9]+", bowwwl_parsed["name"].lower()) if len(w) >= 3}
    our_words = {w for w in re.findall(r"[a-z0-9]+", our_ball_name.lower()) if len(w) >= 3}
    return bool(their_words & our_words)


def compare_to_our_data(bowwwl_parsed: dict, our_release_date, our_skus: list, tolerance: float = DEFAULT_TOLERANCE) -> list:
    """Returns a list of {field_name, current_value, proposed_value,
    reason} dicts, review_queue-shaped (field_name uses the same
    "rg_16lb"/"differential_16lb"/"mass_bias_16lb" convention
    admin_api/service.py's SKU_FIELD_NAME_RE already expects). Only
    compares the fields disclosed in the module docstring as genuinely
    comparable -- numeric SKU fields and release_date, not free-text
    coverstock/core/finish descriptions."""
    mismatches = []

    our_skus_by_weight = {s["weight_lbs"]: s for s in our_skus}
    for bowwwl_sku in bowwwl_parsed["skus"]:
        weight = bowwwl_sku["weight_lbs"]
        ours = our_skus_by_weight.get(weight)
        if ours is None:
            continue  # bowwwl has a weight we don't -- a coverage gap, not a mismatch on an existing value
        for field, bowwwl_key in (("rg", "rg"), ("differential", "differential"), ("mass_bias", "mass_bias")):
            our_value = ours.get(field)
            their_value = bowwwl_sku.get(bowwwl_key)
            if our_value is None or their_value is None:
                continue
            if abs(our_value - their_value) > tolerance:
                mismatches.append({
                    "field_name": f"{field}_{weight}lb",
                    "current_value": str(our_value),
                    "proposed_value": str(their_value),
                    "reason": f"bowwwl_cross_check: {field} at {weight}lb disagrees by {abs(our_value - their_value):.4f} (tolerance {tolerance})",
                })

    if our_release_date and bowwwl_parsed.get("release_date") and our_release_date != bowwwl_parsed["release_date"]:
        mismatches.append({
            "field_name": "release_date",
            "current_value": str(our_release_date),
            "proposed_value": str(bowwwl_parsed["release_date"]),
            "reason": "bowwwl_cross_check: release_date disagrees",
        })

    return mismatches


# ---------------------------------------------------------------------
# Lambda handler + DB write. Same split as every other scraper in this
# project: pure parsing/matching/comparison above (tested against real
# fixtures), mechanical DB/scheduling glue below (deferred-imported).
# ---------------------------------------------------------------------

import json
import os


def get_db_connection():
    import boto3
    import psycopg2

    secret_arn = os.environ["DB_SECRET_ARN"]
    client = boto3.client("secretsmanager")
    secret = json.loads(client.get_secret_value(SecretId=secret_arn)["SecretString"])

    return psycopg2.connect(
        host=secret["host"],
        port=secret.get("port", 5432),
        dbname=secret["dbname"],
        user=secret["username"],
        password=secret["password"],
    )


def list_products_to_check(conn, limit: int = 25) -> list:
    """Published, current products that either have no bowwwl_products row
    yet, or haven't been checked in the last 30 days. Returns
    {product_id, brand_name, name, release_date} dicts. Oldest-checked
    (or never-checked) first, so a steady scheduled run works through the
    whole catalog over time rather than always hitting the same products."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select p.id, b.name, p.name, p.release_date
            from products p
            join brands b on b.id = p.brand_id
            left join bowwwl_products bp on bp.product_id = p.id
            where p.published = true and p.status = 'current'
            order by coalesce(bp.last_checked_at, 'epoch'::timestamptz) asc
            limit %s
            """,
            (limit,),
        )
        return [
            {"product_id": row[0], "brand_name": row[1], "name": row[2], "release_date": row[3]}
            for row in cur.fetchall()
        ]


def get_product_skus(conn, product_id) -> list:
    with conn.cursor() as cur:
        cur.execute(
            "select weight_lbs, rg, differential, mass_bias from product_skus where product_id = %s",
            (product_id,),
        )
        return [
            {"weight_lbs": row[0], "rg": row[1], "differential": row[2], "mass_bias": row[3]}
            for row in cur.fetchall()
        ]


def record_bowwwl_match(conn, product_id, bowwwl_url: str, match_status: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into bowwwl_products (product_id, bowwwl_url, match_status, last_checked_at)
            values (%s, %s, %s, now())
            on conflict (bowwwl_url) do update set
                product_id = excluded.product_id,
                match_status = excluded.match_status,
                last_checked_at = now()
            """,
            (product_id, bowwwl_url, match_status),
        )
    conn.commit()


def write_review_items(conn, product_id, mismatches: list):
    with conn.cursor() as cur:
        for m in mismatches:
            cur.execute(
                """
                insert into review_queue (product_id, field_name, current_value, proposed_value, source, reason)
                values (%s, %s, %s, %s, 'bowwwl_cross_check', %s)
                """,
                (product_id, m["field_name"], m["current_value"], m["proposed_value"], m["reason"]),
            )
    conn.commit()


def update_usbc_approval_date(conn, product_id, pba_approval_date: date):
    """bowwwl's "PBA Approval Date" is the first real source this project
    has for the previously-always-empty products.usbc_approval_date
    column (see module docstring point 4). Filled in directly (coalesce,
    not overwritten if already set) rather than routed through
    review_queue -- this isn't a disagreement to adjudicate, it's a gap
    this is the first thing able to fill."""
    if pba_approval_date is None:
        return
    with conn.cursor() as cur:
        cur.execute(
            "update products set usbc_approval_date = coalesce(usbc_approval_date, %s) where id = %s",
            (pba_approval_date, product_id),
        )
    conn.commit()


def _process_one(conn, product: dict) -> dict:
    url = build_bowwwl_url(product["brand_name"], product["name"])
    html = fetch_page(url)
    parsed = parse_ball_page(html, url)

    if not is_plausible_match(parsed, product["name"]):
        record_bowwwl_match(conn, product["product_id"], url, "unmatched")
        return {"product_id": str(product["product_id"]), "match_status": "unmatched"}

    record_bowwwl_match(conn, product["product_id"], url, "matched")
    update_usbc_approval_date(conn, product["product_id"], parsed.get("pba_approval_date"))

    our_skus = get_product_skus(conn, product["product_id"])
    mismatches = compare_to_our_data(parsed, product["release_date"], our_skus)
    if mismatches:
        write_review_items(conn, product["product_id"], mismatches)

    return {
        "product_id": str(product["product_id"]),
        "match_status": "matched",
        "mismatch_count": len(mismatches),
    }


def handler(event, context):
    """Scheduled invocation: works through a batch of published current
    products (list_products_to_check), oldest-checked-first. Also accepts
    a direct invocation with {"product_id": "...", "brand_name": "...",
    "name": "...", "release_date": "..."} for a single-product check."""
    conn = get_db_connection()
    try:
        if "product_id" in event:
            products = [event]
        else:
            limit = event.get("limit", 25) if isinstance(event, dict) else 25
            products = list_products_to_check(conn, limit)

        results = []
        for product in products:
            try:
                results.append(_process_one(conn, product))
            except Exception:
                logger.exception("Failed to cross-check product %r", product.get("product_id"))
                results.append({"product_id": str(product.get("product_id")), "error": True})
    finally:
        conn.close()

    return {"statusCode": 200, "body": json.dumps({"results": results})}
