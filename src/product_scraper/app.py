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
from datetime import datetime
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


def parse_release_date(release_date_raw: str):
    """Parses "April 2025" / "December 2025" -- the real format seen on
    Brunswick's own pages (see the architecture doc: Crown Victory =
    April 2025, Crown 78U = December 2025) -- into a date, defaulting to
    the 1st of the month since no day is ever given. Accepts both full
    ("April") and abbreviated ("Apr") month names since which one any
    given page uses hasn't been exhaustively checked. Returns None rather
    than guessing for anything that doesn't match this exact "Month YYYY"
    shape -- e.g. a blank release_date_raw (some retired pages, like
    Defender per the architecture doc, don't have this field at all)."""
    if not release_date_raw:
        return None
    cleaned = release_date_raw.strip()
    for fmt in ("%B %Y", "%b %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


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
        "release_date_raw": spec.get("release date"),  # kept as text too -- release_date below is the parsed version, raw stays for anything parse_release_date rejects
        "release_date": parse_release_date(spec.get("release date")),
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
#
# Orchestration: this function is now SQS-triggered from
# PRODUCT_SCRAPE_QUEUE_URL's queue (see UrlDiscoveryFunction, which
# publishes there) rather than only manually invoked. After a successful
# scrape it fans out two more jobs of its own: a PDF-parse job (when
# info_sheet_url was found) and an image-process job per product_images
# row that still needs mirroring (stored_url is null) -- see
# build_pdf_parse_message / build_image_process_messages below.
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


def upsert_product(conn, brand_id: str, parsed: dict) -> dict:
    """Insert or update the products row and its product_skus/product_images
    rows for one scraped page. Returns
    {"product_id": ..., "pending_image_jobs": [{"product_image_id", "source_url"}, ...]}
    -- the latter is every product_images row (new or pre-existing) that
    still has stored_url = null, i.e. still needs the image pipeline to
    run on it, which is what the handler uses to fan out image-process
    jobs without a separate query.

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

    # discontinued_detected_at logic (see migration 003's comments for the
    # full reasoning): on INSERT, set to now() if the product is already
    # retired the first time we ever see it. On UPDATE, the CASE
    # expression sets it only on a genuine current->retired transition
    # (comparing the existing row's status to the incoming one), leaves it
    # alone on a repeat 'retired' scrape, and clears it if status ever
    # reverts to 'current'.
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into products (
                brand_id, name, url, color, coverstock_material, coverstock_type,
                coverstock_name, factory_finish, part_number, weights_available,
                status, source_platform, release_date, discontinued_detected_at
            )
            values (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::int4range, %s, 'craft_cms', %s,
                case when %s = 'retired' then now() else null end
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
                weights_range, parsed["status"], parsed["release_date"], parsed["status"],
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
            # Changed from the original "on conflict do nothing" to "do
            # update" (re-setting image_type to its own value) purely so
            # this can RETURNING id/stored_url on every row, whether it was
            # just inserted or already existed -- needed to know which
            # rows still need an image-process job without a second query.
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


def build_pdf_parse_message(product_id: str, info_sheet_url: str) -> str:
    """Pure function, no SQS/boto3 dependency -- unit-testable on its own."""
    return json.dumps({"product_id": str(product_id), "info_sheet_url": info_sheet_url})


def build_image_process_messages(pending_image_jobs: list) -> list:
    return [json.dumps(job) for job in pending_image_jobs]


def publish_messages(sqs_client, queue_url: str, message_bodies: list) -> int:
    """Sends message_bodies to queue_url via SendMessageBatch, chunked to
    SQS's 10-message-per-call limit. Returns the count sent. Duplicated
    from url_discovery/app.py rather than shared -- each Lambda here is
    its own independent deployment package (CodeUri), and introducing a
    shared Lambda Layer for one seven-line helper isn't worth the added
    packaging complexity yet."""
    sent = 0
    for i in range(0, len(message_bodies), 10):
        chunk = message_bodies[i:i + 10]
        entries = [{"Id": str(idx), "MessageBody": body} for idx, body in enumerate(chunk)]
        sqs_client.send_message_batch(QueueUrl=queue_url, Entries=entries)
        sent += len(chunk)
    return sent


def _extract_jobs(event: dict) -> list:
    """Supports two invocation shapes: a real SQS trigger
    ({"Records": [{"body": "<json>", "messageId": "..."}, ...]}) and a
    direct/manual invocation ({"url": "...", "brand_id": "..."}). Returns
    a list of (job_dict, message_id_or_None) pairs so the handler can
    report per-message failures back to SQS (message_id is None for a
    direct invocation, where there's no batch to report against)."""
    if "Records" in event:
        return [(json.loads(r["body"]), r["messageId"]) for r in event["Records"]]
    return [(event, None)]


def _process_one(job: dict, sqs_client) -> dict:
    """Scrapes and upserts one product page, then fans out follow-up jobs.
    Raised exceptions propagate to the caller (handler), which decides how
    to report the failure -- kept separate so handler can catch per-job
    rather than letting one bad URL fail an entire SQS batch."""
    url = job["url"]
    brand_id = job["brand_id"]

    logger.info("Scraping %s", url)
    html = fetch_page(url)
    parsed = parse_product_page(html, url)

    conn = get_db_connection()
    try:
        result = upsert_product(conn, brand_id, parsed)
    finally:
        conn.close()

    product_id = result["product_id"]
    logger.info("Upserted product %s (%d SKUs)", product_id, len(parsed["skus"]))

    pdf_queue_url = os.environ.get("PDF_PARSE_QUEUE_URL")
    info_sheet_url = parsed["resources"].get("info_sheet_url")
    pdf_jobs_published = 0
    if info_sheet_url and pdf_queue_url:
        message = build_pdf_parse_message(product_id, info_sheet_url)
        pdf_jobs_published = publish_messages(sqs_client, pdf_queue_url, [message])

    image_queue_url = os.environ.get("IMAGE_PROCESS_QUEUE_URL")
    image_jobs_published = 0
    if result["pending_image_jobs"] and image_queue_url:
        messages = build_image_process_messages(result["pending_image_jobs"])
        image_jobs_published = publish_messages(sqs_client, image_queue_url, messages)

    return {
        "product_id": str(product_id),
        "sku_count": len(parsed["skus"]),
        "pdf_jobs_published": pdf_jobs_published,
        "image_jobs_published": image_jobs_published,
    }


def handler(event, context):
    """Handles both an SQS-triggered batch (ProductScrapeQueue, populated by
    UrlDiscoveryFunction) and a direct/manual invocation with
    {"url": "...", "brand_id": "..."}. When SQS-triggered, uses Lambda's
    partial batch response feature (ReportBatchItemFailures, set on the
    event source mapping in template.yaml) so one bad URL doesn't cause the
    whole batch to be retried -- only the failed message(s) go back on the
    queue."""
    jobs = _extract_jobs(event)

    sqs_client = None
    if any(os.environ.get(k) for k in ("PDF_PARSE_QUEUE_URL", "IMAGE_PROCESS_QUEUE_URL")):
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
                raise  # direct invocation with no batch to report against -- surface the error

    response = {"statusCode": 200, "body": json.dumps({"results": results})}
    if "Records" in event:
        response["batchItemFailures"] = batch_item_failures
    return response
