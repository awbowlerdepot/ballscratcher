"""
HTML product scraper for the Craft CMS brand family (Brunswick, Radical, DV8
-- confirmed to share the same template/URL shape during architecture
research). Shopify-family brands (Hammer/Ebonite/Track/Powerhouse) don't need
this: pull /products/{handle}.json directly instead, per the architecture doc.

Deliberately does NOT select elements by CSS class or id. This site's actual
generated markup (Craft CMS + a page-builder) was never directly inspected --
the research tooling available only returned a markdown-converted view of
these pages, never raw HTML -- so any class/id names would be guesses, not
verified selectors. Instead this matches tables and fields by their visible
text content (row labels like "Part Number", header cells matching a
"<N> lb" pattern), which is also just a more resilient strategy in general:
marketing sites rebuild their front end far more often than they change
field labels.

Mass bias (ASY) is often only present on the PDF "Info Sheet", not this
page -- see resources["info_sheet_url"] in the return value, which is what
the (not-yet-built) PDF parser step should consume. When ASY does appear
inline in the spec table (observed on some retired-ball pages), it's parsed
here.
"""
import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger()
logger.setLevel(logging.INFO)

WEIGHT_HEADER_RE = re.compile(r"(\d{1,2})\s*lb", re.IGNORECASE)
STATUS_FROM_URL_RE = re.compile(r"/products/balls/(current|retired)/")

# Known Spec Table row labels. Detection requires only a majority match
# (not all of these) since real pages are inconsistent about which fields
# they include -- e.g. a retired Defender page has no Release Date row,
# while a current Crown 78U page does.
SPEC_TABLE_LABELS = {
    "level", "part number", "color", "core", "coverstock", "cover type",
    "finish", "weights", "rg", "diff", "asy", "mb", "warranty", "release date",
}

# Cover Type -> (coverstock_material, coverstock_type) per the schema. Some
# manufacturer values are compound ("Solid Reactive"), some are bare
# ("Urethane" with no type given) -- matched by substring rather than exact
# string so variants aren't silently dropped.
COVERSTOCK_MATERIAL_KEYWORDS = [
    ("urethane", "urethane"),
    ("polyester", "polyester_plastic"),
    ("reactive", "reactive_resin"),
]
COVERSTOCK_TYPE_KEYWORDS = [
    ("solid", "solid"),
    ("pearl", "pearl"),
    ("hybrid", "hybrid"),
]


def fetch_page(url: str, timeout: int = 30) -> str:
    """Fetch raw HTML. Kept separate from parsing so tests can feed real
    fixture HTML without a network call."""
    import requests

    resp = requests.get(url, headers={"User-Agent": "bowling-scraper/1.0"}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _find_table_by_row_labels(soup: BeautifulSoup, known_labels: set, min_matches: int = 3):
    """Return the first <table> where at least `min_matches` row-label cells
    (case-insensitive) are found in `known_labels`."""
    for table in soup.find_all("table"):
        matches = 0
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if not cells:
                continue
            label = _clean(cells[0].get_text()).lower()
            if label in known_labels:
                matches += 1
        if matches >= min_matches:
            return table
    return None


def _find_core_numbers_table(soup: BeautifulSoup):
    """Return the first <table> whose header row cells look like weight
    labels ("16 lb", "15 lb", ...)."""
    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if header_row is None:
            continue
        header_cells = [_clean(c.get_text()) for c in header_row.find_all(["th", "td"])]
        weight_cells = [c for c in header_cells if WEIGHT_HEADER_RE.search(c)]
        if len(weight_cells) >= 2:
            return table
    return None


def parse_spec_table(table) -> dict:
    """Spec table is label/value row pairs, e.g. <tr><td>Part Number</td><td>60-108363-93X</td></tr>."""
    spec = {}
    for row in table.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        label = _clean(cells[0].get_text()).lower()
        value = _clean(cells[1].get_text())
        if label in SPEC_TABLE_LABELS:
            spec[label] = value
    return spec


def parse_core_numbers_table(table) -> list:
    """Returns a list of {weight_lbs, rg, differential, mass_bias} dicts, one
    per weight column. Handles RG/DIFF and, if present, an ASY/MB row too."""
    header_row = table.find("tr")
    header_cells = [_clean(c.get_text()) for c in header_row.find_all(["th", "td"])]

    weights_by_column = {}
    for idx, cell in enumerate(header_cells):
        match = WEIGHT_HEADER_RE.search(cell)
        if match:
            weights_by_column[idx] = int(match.group(1))

    values_by_row_label = {}
    for row in table.find_all("tr")[1:]:
        cells = row.find_all(["th", "td"])
        if not cells:
            continue
        row_label = _clean(cells[0].get_text()).lower()
        values_by_row_label[row_label] = [_clean(c.get_text()) for c in cells]

    results = []
    for col_idx, weight in weights_by_column.items():
        def value_at(row_label):
            cells = values_by_row_label.get(row_label)
            if not cells or col_idx >= len(cells):
                return None
            v = cells[col_idx]
            return v if v else None

        rg = value_at("rg")
        diff = value_at("diff") or value_at("differential")
        mass_bias = value_at("asy") or value_at("mb")

        if rg is None and diff is None:
            continue

        results.append({
            "weight_lbs": weight,
            "rg": _to_float(rg),
            "differential": _to_float(diff),
            "mass_bias": _to_float(mass_bias),
        })

    return results


def _to_float(value):
    if value is None:
        return None
    match = re.search(r"[\d.]+", value)
    return float(match.group()) if match else None


def parse_coverstock(cover_type_value: str) -> dict:
    """Splits a raw "Cover Type" spec value into the material/type facets
    the schema uses. Deliberately returns type=None rather than guessing
    when the value doesn't specify one (e.g. bare "Urethane") -- per the
    architecture doc, graphic/spare balls in particular often genuinely
    don't have a disclosed type."""
    if not cover_type_value:
        return {"coverstock_material": None, "coverstock_type": None}

    lowered = cover_type_value.lower()

    material = None
    for keyword, value in COVERSTOCK_MATERIAL_KEYWORDS:
        if keyword in lowered:
            material = value
            break

    cs_type = None
    for keyword, value in COVERSTOCK_TYPE_KEYWORDS:
        if keyword in lowered:
            cs_type = value
            break

    return {"coverstock_material": material, "coverstock_type": cs_type}


def parse_weights_available(weights_value: str):
    """Parses "16-12 lbs." into (low, high) = (12, 16). Returns None if it
    doesn't match the expected "<high>-<low> lbs" shape rather than guessing."""
    if not weights_value:
        return None
    match = re.search(r"(\d{1,2})\s*-\s*(\d{1,2})", weights_value)
    if not match:
        return None
    a, b = int(match.group(1)), int(match.group(2))
    return (min(a, b), max(a, b))


def parse_resources(soup: BeautifulSoup, base_url: str) -> dict:
    """Captures PDF resource links, keyed by a normalized name. The Info
    Sheet is what the (not-yet-built) PDF parser step consumes for mass
    bias when it's not inline in the HTML spec table."""
    resources = {}
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not href.lower().endswith(".pdf"):
            continue
        label = _clean(link.get_text()).lower()
        url = urljoin(base_url, href)
        if "info sheet" in label:
            resources["info_sheet_url"] = url
        elif "ball talker" in label:
            resources["ball_talker_url"] = url
        elif "flip card" in label:
            resources["flip_card_url"] = url
        else:
            resources.setdefault("other", []).append(url)
    return resources


def parse_images(soup: BeautifulSoup, base_url: str) -> list:
    """Main product image plus per-weight-range core callout images, matched
    by filename pattern rather than alt text -- alt text spells weights out
    in words ("sixteen to fourteen pound") inconsistently, filenames use the
    reliable "16-14" numeric pattern."""
    images = []
    seen_urls = set()

    for img in soup.find_all("img", src=True):
        src = urljoin(base_url, img["src"])
        if src in seen_urls:
            continue
        seen_urls.add(src)

        alt = _clean(img.get("alt", ""))
        callout_match = re.search(r"(\d{1,2})-(\d{1,2})_lb_Core", src)

        if callout_match:
            images.append({
                "image_type": "core_callout",
                "weight_lbs_context_high": int(callout_match.group(1)),
                "weight_lbs_context_low": int(callout_match.group(2)),
                "source_url": src,
                "alt": alt,
            })
        elif not images:  # first non-callout image encountered = main product shot
            images.append({"image_type": "main", "source_url": src, "alt": alt})
        else:
            images.append({"image_type": "other", "source_url": src, "alt": alt})

    return images


def parse_product_page(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    status_match = STATUS_FROM_URL_RE.search(url)
    status = status_match.group(1) if status_match else None

    h1 = soup.find("h1")
    name = _clean(h1.get_text()) if h1 else None

    spec_table = _find_table_by_row_labels(soup, SPEC_TABLE_LABELS)
    spec = parse_spec_table(spec_table) if spec_table is not None else {}

    core_numbers_table = _find_core_numbers_table(soup)
    skus = parse_core_numbers_table(core_numbers_table) if core_numbers_table is not None else []

    # Per the architecture review: when there's no per-weight breakdown,
    # a single RG/DIFF/ASY value in the spec table is conventionally the
    # 15 lb ball.
    if not skus and ("rg" in spec or "diff" in spec):
        skus = [{
            "weight_lbs": 15,
            "rg": _to_float(spec.get("rg")),
            "differential": _to_float(spec.get("diff")),
            "mass_bias": _to_float(spec.get("asy") or spec.get("mb")),
        }]

    coverstock = parse_coverstock(spec.get("cover type"))

    return {
        "url": url,
        "status": status,
        "name": name,
        "color": spec.get("color"),
        "core_name": spec.get("core"),
        "coverstock_name": spec.get("coverstock"),
        "coverstock_material": coverstock["coverstock_material"],
        "coverstock_type": coverstock["coverstock_type"],
        "factory_finish": spec.get("finish"),
        "part_number": spec.get("part number"),
        "weights_available": parse_weights_available(spec.get("weights")),
        "release_date_raw": spec.get("release date"),  # left as text; date parsing is a formatting detail, not a scraping one
        "skus": skus,
        "resources": parse_resources(soup, url),
        "images": parse_images(soup, url),
    }


# ---------------------------------------------------------------------
# Lambda handler + DB write. Kept below the pure parsing functions above
# on purpose -- those are the part worth testing carefully (see
# tests/test_product_scraper.py); this part is comparatively mechanical
# upsert logic, deferred-imported so the parsing tests don't need
# psycopg2/boto3 installed to run.
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


def upsert_product(conn, brand_id: str, parsed: dict) -> str:
    """Insert or update the products row and its product_skus rows for one
    scraped page. Returns the product id.

    Mismatches between a re-scrape and the stored value aren't silently
    overwritten for SKU fields sourced from html when a prior value came
    from pdf -- that's exactly what review_queue exists for (see the
    architecture doc's "mismatched" definition). This function writes the
    html-sourced fields directly since that's the higher-confidence, more
    complete source for RG/DIFF; reconciling against a pdf-sourced or
    bowwwl-sourced value is the PDF parser step's job, not this one's.
    """
    weights_range = None
    if parsed["weights_available"]:
        low, high = parsed["weights_available"]
        weights_range = f"[{low},{high}]"

    with conn.cursor() as cur:
        cur.execute(
            """
            insert into products (
                brand_id, name, url, color, coverstock_material, coverstock_type,
                coverstock_name, factory_finish, part_number, weights_available,
                status, source_platform
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::int4range, %s, 'craft_cms')
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
                updated_at = now()
            returning id
            """,
            (
                brand_id, parsed["name"], parsed["url"], parsed["color"],
                parsed["coverstock_material"], parsed["coverstock_type"],
                parsed["coverstock_name"], parsed["factory_finish"], parsed["part_number"],
                weights_range, parsed["status"],
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

        for image in parsed["images"]:
            cur.execute(
                """
                insert into product_images (product_id, image_type, source_url)
                values (%s, %s, %s)
                on conflict do nothing
                """,
                (product_id, image["image_type"], image["source_url"]),
            )

    conn.commit()
    return product_id


def handler(event, context):
    """Expects event = {"url": "...", "brand_id": "..."} -- one product page
    per invocation, matching what UrlDiscoveryFunction emits (new_urls/
    changed_urls). Wire these together with SQS or Step Functions once
    UrlDiscoveryFunction is deployed; not done here since that's an
    orchestration decision, not a scraping-logic one."""
    url = event["url"]
    brand_id = event["brand_id"]

    logger.info("Scraping %s", url)
    html = fetch_page(url)
    parsed = parse_product_page(html, url)

    conn = get_db_connection()
    try:
        product_id = upsert_product(conn, brand_id, parsed)
    finally:
        conn.close()

    logger.info("Upserted product %s (%d SKUs)", product_id, len(parsed["skus"]))

    return {
        "statusCode": 200,
        "body": json.dumps({"product_id": str(product_id), "sku_count": len(parsed["skus"])}),
    }
