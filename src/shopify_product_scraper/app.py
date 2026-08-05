"""
Product scraper for the Shopify brand family (Hammer Bowling to start --
see shopify_url_discovery/app.py's module docstring for the platform
confirmation).

Fetches each product via Shopify's own {product_url}.json convention
(confirmed real this session against
https://hammerbowling.com/products/black-widow-3-0-dynasty.json -- same
data as the HTML page, just structured, no HTML parsing needed to locate
it) rather than fetching and parsing the rendered HTML page the other
three families' scrapers all do. The spec data itself, though, still lives
inside one field of that JSON -- `body_html`, a themed HTML fragment
Hammer's product-page template renders directly into the page -- so this
module still does BeautifulSoup parsing, just against that fragment
instead of a full fetched page.

Two real, confirmed structural differences from every other family in this
codebase that shaped this module's design:

1. RG/DIFF/ASY (when the core is asymmetric) are already given per-weight
   directly in body_html's "RG / DIFF" (or "RG / DIFF / ASY") list --
   confirmed real across every product inspected this session, old and
   new. No PDF step needed at all for this platform's core numbers, unlike
   Brunswick (mass bias only in the PDF) -- see parse_core_type() for how
   asymmetric vs. symmetric is inferred when there's no explicit "CORE
   TYPE" field (only seen on some older retired listings, e.g. the real
   3-D Offset fixture).
2. Hammer's product page has NO current/retired signal of its own --
   unlike Brunswick's URL path or SWAG's "Production-status" attribute,
   nothing in body_html says whether a ball is still for sale. Status is
   determined once, at discovery time, by which Shopify collection the
   product was found under (see shopify_url_discovery/app.py) and stored
   on discovered_urls.status_path. This module reads that back via
   get_status_for_url() rather than re-deriving it from the page, and
   defaults to 'current' if the URL isn't in discovered_urls yet at all
   (e.g. a manual/direct scrape of a URL nobody's ever discovered through
   the normal collection-crawl path -- treated as "presumably still live"
   rather than failing the NOT NULL products.status constraint).

Real, confirmed formatting inconsistencies across eras that parse_ball_specs/
parse_rg_diff_list are built to tolerate (all three shapes seen live this
session, real fixtures for each in tests/fixtures/):
- Modern (Black Widow 3.0 Dynasty, Spawn, Fallout): "16 lb - RG (2.510)
  DIFF (0.048) ASY (0.015)", BALL SPECS labels like COLOR/CORE/COVERSTOCK/
  COVER TYPE all present as <strong>LABEL</strong><span>value</span> --
  though the value sometimes spills outside the <span> as plain trailing
  text (Fallout's "COVER TYPE" is <span>Solid</span> Reactive, not fully
  inside the span) -- parse_ball_specs slices the LI's full text by the
  label's own length rather than only reading the <span>, specifically to
  survive this.
- Older retired (3-D Offset): adds an explicit "CORE TYPE" field
  ("Asymmetric") BALL SPECS doesn't always carry on newer balls, and heading
  text "RG / DIFF / ASY" instead of "RG / DIFF" -- find_section() matches
  on "RG" as a prefix rather than either exact heading, so both match.
- Very old retired (Absolut Curve, 2018): completely different RG/DIFF
  list format -- "#10 RG (2.72) Diff (.031)" (a bare "#<weight>" instead of
  "<weight> lb -", lowercase "Diff", no ASY at all since this is a
  symmetric-core ball) and a "FACTORY FINISH"/"BEST LANE CONDITION"/
  "AVAILABLE WEIGHTS" label set instead of "FINISH"/"LANE CONDITION"/
  "WEIGHTS" -- parse_ball_specs's label lookup and
  WEIGHT_RE/RG_RE/DIFF_RE/ASY_RE regexes are all written to match either
  variant.

No resources/PDF-download parsing here at all (unlike every other
family's scraper) -- there's nothing downstream that needs those PDF URLs
for this platform (see point 1 above), so capturing them would just be
dead data with no consumer, not a real gap.
"""
import logging
import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Canonical BALL SPECS label -> parsed dict key. Both the modern and the
# very-old (Absolut Curve era) label spellings map to the same key, since
# they mean the same thing -- see module docstring.
BALL_SPEC_LABEL_MAP = {
    "PERFORMANCE": "performance_level_raw",
    "PART NUMBER": "part_number",
    "COLOR": "color",
    "CORE": "core_name",
    "CORE TYPE": "core_type_raw",
    "COVERSTOCK": "coverstock_name",
    "COVER TYPE": "coverstock_type_raw",
    "FINISH": "factory_finish",
    "FACTORY FINISH": "factory_finish",
    "WEIGHTS": "weights_raw",
    "AVAILABLE WEIGHTS": "weights_raw",
    "LANE CONDITION": "lane_condition",
    "BEST LANE CONDITION": "lane_condition",
    "REACTION": "reaction",
    "WARRANTY": "warranty",
    "RELEASE DATE": "release_date_raw",
}

COVERSTOCK_MATERIAL_KEYWORDS = [
    ("urethane", "urethane"),
    ("polyester", "polyester_plastic"),
    ("plastic", "polyester_plastic"),
    ("reactive", "reactive_resin"),
]
COVERSTOCK_TYPE_KEYWORDS = [
    ("solid", "solid"),
    ("pearl", "pearl"),
    ("hybrid", "hybrid"),
]

WEIGHT_RE = re.compile(r"#?(\d{1,2})\s*(?:lb)?", re.IGNORECASE)
RG_RE = re.compile(r"RG\D*?([\d.]+)", re.IGNORECASE)
DIFF_RE = re.compile(r"DIFF\D*?([\d.]+)", re.IGNORECASE)
ASY_RE = re.compile(r"ASY\D*?([\d.]+)", re.IGNORECASE)
WEIGHT_TOKEN_RE = re.compile(r"(\d{1,2})")


def fetch_product_json(url: str, timeout: int = 30) -> dict:
    """Fetches {url}.json -- Shopify's built-in convention for getting a
    product's structured data without parsing the rendered page. Kept
    separate from parsing so tests can feed real fixture JSON without a
    network call."""
    import requests

    resp = requests.get(f"{url}.json", headers={"User-Agent": "bowling-scraper/1.0"}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()["product"]


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _find_section(soup: BeautifulSoup, heading_prefix: str):
    """Finds the <h3> whose cleaned, upper-cased text starts with
    heading_prefix, and returns its next sibling tag (the <ul> or <p> that
    actually holds the section's content). Matching by heading text
    content rather than any CSS class/position -- same "match by content,
    not markup" approach every other scraper in this codebase already
    uses, needed here too since "RG / DIFF" vs. "RG / DIFF / ASY" varies
    by product (see module docstring)."""
    for h3 in soup.find_all("h3"):
        if _clean(h3.get_text()).upper().startswith(heading_prefix):
            return h3.find_next_sibling(["ul", "p"])
    return None


def parse_ball_specs(specs_ul) -> dict:
    """Returns {canonical_key: raw_value_text}, normalized through
    BALL_SPEC_LABEL_MAP so callers never need to know which era's label
    spelling a given product used. Slices each <li>'s full text by the
    length of its own <strong> label text, rather than only reading the
    <span> that (usually) holds the value -- real, confirmed reason: some
    products have the <span> and <strong> tags butted directly against
    each other with no whitespace in between (e.g. Black Widow 3.0
    Dynasty's "CORE" li is literally
    <strong>CORE</strong><span>Gas Mask</span> with no space), while
    others spill part of the value outside the <span> entirely as trailing
    plain text (Fallout's "COVER TYPE" li is
    <strong>COVER TYPE</strong><span>Solid</span> Reactive) -- slicing the
    LI's own full concatenated text by len(label) handles both correctly
    without depending on where the tag boundaries happen to fall."""
    if specs_ul is None:
        return {}
    raw = {}
    for li in specs_ul.find_all("li", recursive=False):
        strong = li.find("strong")
        if strong is None:
            continue
        label_text = strong.get_text()
        label = _clean(label_text).upper()
        canonical = BALL_SPEC_LABEL_MAP.get(label)
        if canonical is None:
            continue
        value = _clean(li.get_text()[len(label_text):])
        if value:
            raw[canonical] = value
    return raw


def parse_rg_diff_list(rg_ul) -> list:
    """Parses the per-weight RG/DIFF/ASY list. WEIGHT_RE/RG_RE/DIFF_RE/
    ASY_RE are all written to tolerate both the modern "16 lb - RG (2.510)
    DIFF (0.048) ASY (0.015)" shape and the very old "#10 RG (2.72) Diff
    (.031)" shape (see module docstring) -- weight is always the first
    number token in the LI's text either way, so a single leftmost
    WEIGHT_RE.search() finds it correctly without needing to know which
    era's format this particular LI is in."""
    if rg_ul is None:
        return []
    skus = []
    for li in rg_ul.find_all("li", recursive=False):
        text = _clean(li.get_text())
        if not text:
            continue
        weight_match = WEIGHT_RE.search(text)
        if not weight_match:
            continue
        rg_match = RG_RE.search(text)
        diff_match = DIFF_RE.search(text)
        asy_match = ASY_RE.search(text)
        skus.append({
            "weight_lbs": int(weight_match.group(1)),
            "rg": float(rg_match.group(1)) if rg_match else None,
            "differential": float(diff_match.group(1)) if diff_match else None,
            "mass_bias": float(asy_match.group(1)) if asy_match else None,
        })
    return skus


def parse_coverstock(coverstock_name: str, coverstock_type_raw: str) -> dict:
    """Both material (reactive/urethane/polyester) and type (solid/pearl/
    hybrid) are inferred by keyword search across the combined COVERSTOCK
    + COVER TYPE text, e.g. Black Widow 3.0 Dynasty's "HK22 - Cohesion
    Solid" + "Solid Reactive" -> material=reactive_resin, type=solid.
    Unlike SWAG (see woocommerce_product_scraper.parse_coverstock), Hammer
    doesn't cleanly split material into its own dedicated field -- both
    keyword sets are searched across the same combined text here."""
    combined = f"{coverstock_name or ''} {coverstock_type_raw or ''}".lower()

    material = None
    for keyword, value in COVERSTOCK_MATERIAL_KEYWORDS:
        if keyword in combined:
            material = value
            break

    cs_type = None
    for keyword, value in COVERSTOCK_TYPE_KEYWORDS:
        if keyword in combined:
            cs_type = value
            break

    return {"coverstock_material": material, "coverstock_type": cs_type}


def parse_core_type(core_type_raw: str, skus: list):
    """Prefers the explicit "CORE TYPE" field when a product has one (only
    seen on some older retired listings this session, e.g. the real 3-D
    Offset fixture). Otherwise infers from whether any parsed SKU carries
    an ASY (mass bias) value -- present only for asymmetric cores, per
    every real product inspected this session -- rather than leaving
    core_type unset just because the page doesn't spell it out directly."""
    if core_type_raw:
        lowered = core_type_raw.lower()
        if "asymmetric" in lowered:
            return "asymmetric"
        if "symmetric" in lowered:
            return "symmetric"
    if skus:
        return "asymmetric" if any(sku["mass_bias"] is not None for sku in skus) else "symmetric"
    return None


def parse_weights_available(weights_raw: str):
    """Parses "16-12 Pounds" / "16-10 Pounds" / "10-16 LBS" into
    (low, high) -- same (low, high) output shape as
    product_scraper.parse_weights_available /
    woocommerce_product_scraper.parse_weights_available."""
    if not weights_raw:
        return None
    weights = [int(w) for w in WEIGHT_TOKEN_RE.findall(weights_raw)]
    if not weights:
        return None
    return (min(weights), max(weights))


def parse_release_date(release_date_raw: str):
    """"January 15, 2026" / "July 16, 2026" style -- confirmed real across
    every current-ball product inspected this session. Older retired
    listings don't always have a RELEASE DATE field at all (e.g. Absolut
    Curve/Absolut Flip), which is fine -- this just returns None."""
    if not release_date_raw:
        return None
    try:
        return datetime.strptime(release_date_raw.strip(), "%B %d, %Y").date()
    except ValueError:
        return None


def classify_image(alt: str, position: int) -> str:
    """Shopify gives real position + descriptive alt text directly on
    every image, unlike the Craft-CMS/WooCommerce families where image
    role has to be guessed from filename patterns or raw tag order. Real,
    confirmed alt-text convention across every product inspected this
    session: the main product photo's alt describes the ball's colors,
    core-callout images' alt text literally contains the word "core"
    (e.g. "The Black Widow 3.0 Dynasty core for sixteen to fourteen pound
    bowling balls."), and the performance-graphic image's alt contains
    "performance" -- matched by keyword rather than position, so this
    survives a product having more or fewer than the usual 4 images.
    "performance" is checked before "core" on purpose: the real badge
    image's own alt text also happens to mention "Core" as one of its
    labeled stats (e.g. "...with an 8.75 Finish, an 8.75 Core, an 11
    Cover..."), which would otherwise misclassify it as a core_callout --
    confirmed by a real test failure against this exact fixture."""
    lowered = (alt or "").lower()
    if "performance" in lowered:
        return "performance_badge"
    if "core" in lowered:
        return "core_callout"
    if position == 1:
        return "main"
    return "other"


def parse_images(product: dict) -> list:
    images = []
    for img in product.get("images", []):
        src = img.get("src")
        if not src:
            continue
        alt = _clean(img.get("alt") or "")
        images.append({
            "image_type": classify_image(alt, img.get("position", 0)),
            "source_url": src,
            "alt": alt,
        })
    return images


def parse_description(soup: BeautifulSoup) -> str:
    """Takes the first substantial (>=60 chars of text) <p> paragraph
    found in body_html. Real Hammer product pages open with a YouTube-
    embed <h3>, sometimes a marketing-tagline <h3>, then one or more
    descriptive paragraphs -- confirmed inconsistent across real fetches
    this session whether those paragraphs carry a "p1" CSS class (Black
    Widow 3.0 Dynasty, Fallout, Deep Ocean Vibe all do; the 2018-era
    Absolut Curve/Absolut Flip listings use plain untitled <p> tags) -- so
    this matches on paragraph substance, not a class name, before the
    structured BALL SPECS section kicks off. The 60-char threshold is
    deliberately higher than it might need to be: Absolut Curve's real
    fixture opens with three short marketing-teaser <p> tags ("Tough
    look? Check.", "Aggressive Roll? Check.", "Devastating backend
    reaction? You bet." -- the longest of the three is 39 chars) before
    its real description paragraph, and a lower threshold picks one of
    those teasers instead (confirmed by a real test failure against this
    exact fixture). Safe against accidentally grabbing the DOWNLOADS
    section's link-only paragraph instead: that <p> only ever appears
    later in document order (after BALL SPECS/RG-DIFF), and
    find_all("p") returns document order, so the real description is
    always reached first."""
    for p in soup.find_all("p"):
        text = _clean(p.get_text())
        if len(text) >= 60:
            return text
    return None


def parse_product_page(product: dict, url: str) -> dict:
    soup = BeautifulSoup(product.get("body_html") or "", "lxml")

    raw = parse_ball_specs(_find_section(soup, "BALL SPECS"))
    skus = parse_rg_diff_list(_find_section(soup, "RG"))
    coverstock = parse_coverstock(raw.get("coverstock_name"), raw.get("coverstock_type_raw"))

    return {
        "url": url,
        "name": product.get("title"),
        "color": raw.get("color"),
        "part_number": raw.get("part_number"),
        "core_name": raw.get("core_name"),
        "core_type": parse_core_type(raw.get("core_type_raw"), skus),
        "coverstock_name": raw.get("coverstock_name"),
        "coverstock_material": coverstock["coverstock_material"],
        "coverstock_type": coverstock["coverstock_type"],
        "factory_finish": raw.get("factory_finish"),
        "weights_available": parse_weights_available(raw.get("weights_raw")),
        "release_date_raw": raw.get("release_date_raw"),
        "release_date": parse_release_date(raw.get("release_date_raw")),
        "performance_level_raw": raw.get("performance_level_raw"),
        "skus": skus,
        "images": parse_images(product),
        "description": parse_description(soup),
    }


# ---------------------------------------------------------------------
# Lambda handler + DB write. Same split/orchestration pattern as
# product_scraper/app.py -- see that module for the reasoning (pure
# parsing above, tested; mechanical DB/SQS below, deferred-imported).
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


def get_status_for_url(conn, url: str) -> str:
    """Reads back the current/retired classification
    shopify_url_discovery/app.py already resolved at discovery time (via
    Shopify collection membership) and stored on
    discovered_urls.status_path -- see this module's docstring for why
    that's necessary on this platform. Defaults to 'current' when the URL
    isn't in discovered_urls at all (e.g. a manual/direct scrape of a URL
    the normal collection-crawl hasn't run across yet) rather than leaving
    status null, since products.status is NOT NULL."""
    with conn.cursor() as cur:
        cur.execute("select status_path from discovered_urls where url = %s", (url,))
        row = cur.fetchone()
    if row is None or row[0] is None:
        return "current"
    return row[0]


def get_or_create_core_id(conn, brand_id: str, core_name, core_type=None):
    """Same helper as product_scraper.get_or_create_core_id -- duplicated
    rather than shared, same reasoning as publish_messages elsewhere in
    this project (each Lambda here is its own independent CodeUri
    package). Unlike woocommerce_product_scraper, this one DOES have a
    real core_type most of the time (see parse_core_type)."""
    if not core_name:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into cores (brand_id, name, core_type)
            values (%s, %s, %s)
            on conflict (brand_id, name) do update set
                core_type = coalesce(cores.core_type, excluded.core_type)
            returning id
            """,
            (brand_id, core_name, core_type),
        )
        return cur.fetchone()[0]


def upsert_product(conn, brand_id: str, status: str, parsed: dict) -> dict:
    """Same shape/return value as product_scraper.upsert_product /
    woocommerce_product_scraper.upsert_product -- status is passed in
    separately (from get_status_for_url()) rather than being part of
    parsed{}, since it never comes from the page itself on this platform
    (see module docstring)."""
    weights_range = None
    if parsed["weights_available"]:
        low, high = parsed["weights_available"]
        weights_range = f"[{low},{high}]"

    core_id = get_or_create_core_id(conn, brand_id, parsed.get("core_name"), parsed.get("core_type"))

    with conn.cursor() as cur:
        cur.execute(
            """
            insert into products (
                brand_id, name, url, color, coverstock_material, coverstock_type,
                coverstock_name, factory_finish, part_number, weights_available,
                status, source_platform, release_date, description, discontinued_detected_at,
                core_id
            )
            values (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::int4range, %s, 'shopify', %s, %s,
                case when %s = 'retired' then now() else null end,
                %s
            )
            on conflict (url) do update set
                name = excluded.name,
                color = excluded.color,
                coverstock_material = excluded.coverstock_material,
                coverstock_type = excluded.coverstock_type,
                coverstock_name = excluded.coverstock_name,
                factory_finish = excluded.factory_finish,
                part_number = excluded.part_number,
                weights_available = excluded.weights_available,
                status = excluded.status,
                release_date = coalesce(excluded.release_date, products.release_date),
                description = coalesce(excluded.description, products.description),
                core_id = coalesce(excluded.core_id, products.core_id),
                discontinued_detected_at = case
                    when excluded.status = 'retired' and products.status <> 'retired' then now()
                    when excluded.status = 'current' then null
                    else products.discontinued_detected_at
                end,
                updated_at = now()
            returning id
            """,
            (
                brand_id, parsed["name"], parsed["url"], parsed["color"],
                parsed["coverstock_material"], parsed["coverstock_type"],
                parsed["coverstock_name"], parsed["factory_finish"], parsed["part_number"],
                weights_range, status, parsed["release_date"], parsed["description"],
                status, core_id,
            ),
        )
        product_id = cur.fetchone()[0]

        for sku in parsed["skus"]:
            cur.execute(
                """
                insert into product_skus (product_id, weight_lbs, rg, differential, mass_bias, source)
                values (%s, %s, %s, %s, %s, 'html')
                on conflict (product_id, weight_lbs) do update set
                    rg = excluded.rg,
                    differential = excluded.differential,
                    mass_bias = coalesce(excluded.mass_bias, product_skus.mass_bias),
                    updated_at = now()
                """,
                (product_id, sku["weight_lbs"], sku["rg"], sku["differential"], sku["mass_bias"]),
            )

        pending_image_jobs = []
        for image in parsed["images"]:
            cur.execute(
                """
                insert into product_images (product_id, image_type, source_url)
                values (%s, %s, %s)
                on conflict (product_id, source_url) do update set image_type = excluded.image_type
                returning id, stored_url
                """,
                (product_id, image["image_type"], image["source_url"]),
            )
            image_id, stored_url = cur.fetchone()
            if stored_url is None:
                pending_image_jobs.append({"product_image_id": str(image_id), "source_url": image["source_url"]})

    conn.commit()
    return {"product_id": product_id, "pending_image_jobs": pending_image_jobs}


def build_image_process_messages(pending_image_jobs: list) -> list:
    return [json.dumps(job) for job in pending_image_jobs]


def publish_messages(sqs_client, queue_url: str, message_bodies: list) -> int:
    """Duplicated from product_scraper/app.py rather than shared -- see
    that module's docstring for why."""
    sent = 0
    for i in range(0, len(message_bodies), 10):
        chunk = message_bodies[i:i + 10]
        entries = [{"Id": str(idx), "MessageBody": body} for idx, body in enumerate(chunk)]
        sqs_client.send_message_batch(QueueUrl=queue_url, Entries=entries)
        sent += len(chunk)
    return sent


def _extract_jobs(event: dict) -> list:
    """Same two-shape support (SQS batch or direct invoke) as
    product_scraper's _extract_jobs, and the same {"url", "brand_id"} job
    shape every other family's scraper accepts -- including from
    admin_api.queue_rescrape's manual/backfill trigger path."""
    if "Records" in event:
        return [(json.loads(r["body"]), r["messageId"]) for r in event["Records"]]
    return [(event, None)]


def _process_one(job: dict, sqs_client) -> dict:
    url = job["url"]
    brand_id = job["brand_id"]

    logger.info("Scraping %s", url)
    product = fetch_product_json(url)
    parsed = parse_product_page(product, url)

    conn = get_db_connection()
    try:
        status = get_status_for_url(conn, url)
        result = upsert_product(conn, brand_id, status, parsed)
    finally:
        conn.close()

    product_id = result["product_id"]
    logger.info("Upserted product %s (%d SKUs, status=%s)", product_id, len(parsed["skus"]), status)

    image_queue_url = os.environ.get("IMAGE_PROCESS_QUEUE_URL")
    image_jobs_published = 0
    if result["pending_image_jobs"] and image_queue_url:
        messages = build_image_process_messages(result["pending_image_jobs"])
        image_jobs_published = publish_messages(sqs_client, image_queue_url, messages)

    return {
        "product_id": str(product_id),
        "sku_count": len(parsed["skus"]),
        "image_jobs_published": image_jobs_published,
    }


def handler(event, context):
    """Handles both an SQS-triggered batch (ShopifyProductScrapeQueue,
    populated by ShopifyUrlDiscoveryFunction) and a direct/manual
    invocation with {"url": "...", "brand_id": "..."}, same
    batchItemFailures pattern as product_scraper's handler."""
    jobs = _extract_jobs(event)

    sqs_client = None
    if os.environ.get("IMAGE_PROCESS_QUEUE_URL"):
        import boto3

        sqs_client = boto3.client("sqs")

    results = []
    batch_item_failures = []
    for job, message_id in jobs:
        try:
            results.append(_process_one(job, sqs_client))
        except Exception:
            logger.exception("Failed to scrape/upsert job: %r", job)
            if message_id is not None:
                batch_item_failures.append({"itemIdentifier": message_id})
            else:
                raise

    response = {"statusCode": 200, "body": json.dumps({"results": results})}
    if "Records" in event:
        response["batchItemFailures"] = batch_item_failures
    return response
