"""
Product scraper for the commercebuild brand family: Storm, Roto Grip, and
900 Global, all served from one site (stormbowling.com) on the
commercebuild/XM Symphony platform. See COMMERCEBUILD_SCOPING.md at the
repo root for the full research trail this module is built from -- real
`curl` output against live product pages, not just a summary.

**Current products only.** Archived/retired products use a structurally
different template (confirmed real via curl against two archived
products, one Storm one 900 Global) -- no Brand: field, no JS variant
shell, and the full per-weight RG/Diff/PSA table sits directly in raw
HTML instead of being locked behind a PDF. That's a genuinely different
parser, not built here. Also unresolved: the archive collection listing's
own links 404 on a bare request (see COMMERCEBUILD_SCOPING.md), so even
URL discovery for retired balls isn't ready yet. This file only handles
the current-product template.

**Confirmed real via curl this session** (not the readability-tool view,
which would have missed the JS-locked variant data the same way it missed
Brunswick's lazy-load placeholder bug):

1. Every current product page (Storm Alpha Crux, Roto Grip Gremlin, 900
   Global Viking Conquest -- one per brand) shares the identical spec
   block shape: `<strong>Label:</strong> value` pairs for Brand, Line,
   Core, Weight Block, Finish, Durometer, Symmetry, Differential, Flare
   Potential, Radius of Gyration, Weight, Coverstock, Color, Release Date,
   Fragrance, Avail. for Sales Orders, PSA, and sometimes MatchMaker
   App/MatchMaker. Fields aren't 100% uniform (MatchMaker only appeared on
   Roto Grip's page) -- parsing is label-driven and tolerant of
   missing/extra fields, same philosophy as the Craft-CMS scraper.
2. Field VALUES carry a single-letter brand-code prefix baked in by
   commercebuild's custom-field system -- "S_" (Storm), "R_" (Roto Grip),
   "G_" (900 Global) -- e.g. `Symmetry: S_Asymmetrical`,
   `Weight Block: S_Catalyst_AI`. Stripped by _clean_field_value() below.
3. The weight shown in that spec block (`Weight: 16` on every product
   checked) is the ONLY weight a plain HTTP GET ever sees.
   `<div id="div-variant-product"></div>` -- the element that would carry
   other weights' data -- is a genuinely empty shell in raw HTML,
   populated only by a client-side JS module
   (`loadCBCustomisation`/`storage.googleapis.com/cb-customisations-dev`).
   No `<select>` tag exists anywhere in any checked page's raw HTML
   either. This is why the "Tech Data" PDF (see parse_tech_data_pdf below)
   is required reading, not a nice-to-have cross-check like it is for
   Brunswick.
4. Zero `data:` placeholder images found (`grep -c "data:image"` = 0 on
   every current-product page curled this session) -- the Brunswick
   lazy-load bug doesn't reproduce here, at least not on current-product
   pages. Main image comes from the `og:image` meta tag, which was
   present and correct on every page checked.
5. The "Tech Data" PDF's real table structure (confirmed by opening one
   by hand, then by running pdfplumber against it) is NOT one row per
   weight -- it's a single table row whose four cells each contain
   newline-joined values in matching order, e.g.:
     weight cell: "16 lb\\n15 lb\\n14 lb\\n13 lb\\n12 lb"
     RG cell:     "2.48\\n2.48\\n2.52\\n2.56\\n2.58"
   See parse_tech_data_pdf() for the real column layout (weight, RG,
   Diff, PSA -- no mass_bias column observed on the one PDF checked).

**Not yet confirmed, flagged rather than assumed:**
- The exact raw tag wrapping the product name/H1 and the Downloads
  section's link markup were only ever seen through the readability-tool
  view, not raw curl -- parse_product_name() and parse_tech_data_pdf_url()
  are built against reasonable, standard-HTML assumptions (a real `<h1>`,
  real `<a href="...pdf">` tags) but should be treated as first-deploy
  smoke-test targets, same as every other real bug this project has found
  by deploying and checking CloudWatch logs rather than by guessing
  further from a sandbox with no way to fully verify.
- Whether every product's Tech Data PDF follows the identical table shape
  as Alpha Crux's (n=1 real pdfplumber check). Different products/brands
  may use different PDF templates (Roto Grip's was even named
  differently: "Tech Doc_HP3_GREMLIN.pdf" vs "Alpha Crux Tech Data
  Final.pdf") -- parse_tech_data_pdf() skips a table it can't confidently
  parse rather than guessing, but hasn't been checked against more than
  one real PDF.
- coverstock/core-type parsing below is a reasonable-effort mapping from
  the real field values seen this session, not exhaustively checked
  against every product in the catalog.
"""
import io
import logging
import re
from datetime import datetime
from urllib.parse import urljoin

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DEFAULT_TOLERANCE = 0.01  # PSA/RG typically reported to 2-3 decimals; a little looser than Brunswick's 0.001 since these are cross-source (HTML's rounded display value vs the PDF's own rounded value), not two reads of the same source.

# <strong>Label:</strong> value  /  <b>Label:</b> value -- confirmed real
# against curled Storm/Roto Grip/900 Global product pages this session.
SPEC_LABEL_RE = re.compile(r"<(?:strong|b)>([A-Za-z. ]+):</(?:strong|b)>\s*([^<]*)")

# Brand-code prefixes confirmed real on every field value checked this
# session: "S_" (Storm), "R_" (Roto Grip), "G_" (900 Global).
_BRAND_PREFIX_RE = re.compile(r"^[SRG]_")


def _clean_field_value(value: str) -> str:
    """Strips the commercebuild brand-code prefix (S_/R_/G_) and replaces
    underscores with spaces, e.g. "S_Catalyst_AI" -> "Catalyst AI"."""
    value = _BRAND_PREFIX_RE.sub("", value.strip())
    return value.replace("_", " ").strip()


def parse_spec_fields(html: str) -> dict:
    """Returns {lowercased label: cleaned value} for every
    <strong>Label:</strong> value pair found. Label-driven, not
    position/CSS-driven -- tolerant of a product missing or adding fields
    (confirmed real variation: only Roto Grip's checked product had
    MatchMaker fields)."""
    fields = {}
    for label, value in SPEC_LABEL_RE.findall(html):
        fields[label.strip().lower()] = _clean_field_value(value)
    return fields


def _to_float(value):
    if value is None:
        return None
    match = re.search(r"-?\d+\.?\d*", str(value))
    return float(match.group()) if match else None


def parse_core_type(symmetry_value):
    """Symmetry field values seen: "Asymmetrical", "Symmetrical" (after
    _clean_field_value strips the brand prefix)."""
    if not symmetry_value:
        return None
    v = symmetry_value.lower()
    if "asymmetric" in v:
        return "asymmetric"
    if "symmetric" in v:
        return "symmetric"
    return None


def parse_coverstock(coverstock_value):
    """Real values seen this session (post _clean_field_value):
    "GI26 Solid" (Storm Alpha Crux), "V-R1 Pearl" (Roto Grip Gremlin),
    "94 Solid" (900 Global Viking Conquest). Same
    material-defaults-to-reactive_resin-unless-urethane-mentioned
    reasoning as the Craft-CMS scraper's parse_coverstock, since every
    coverstock value seen so far is a reactive-resin family cover; revisit
    if a real urethane product is checked."""
    if not coverstock_value:
        return {"coverstock_material": None, "coverstock_type": None}

    v = coverstock_value.lower()
    if "urethane" in v:
        material = "urethane"
    elif "polyester" in v or "plastic" in v:
        material = "polyester_plastic"
    else:
        material = "reactive_resin"

    if "hybrid" in v:
        cov_type = "hybrid"
    elif "pearl" in v:
        cov_type = "pearl"
    elif "solid" in v:
        cov_type = "solid"
    else:
        cov_type = None

    return {"coverstock_material": material, "coverstock_type": cov_type}


def parse_release_date(raw):
    """Real format seen this session: "MM/DD/YY", e.g. "05/29/26",
    "07/18/25", "06/26/26" -- notably different from Brunswick's
    "Month D, YYYY" shape, confirmed on all three brands' products
    checked."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        return datetime.strptime(raw, "%m/%d/%y").date()
    except ValueError:
        return None


def parse_product_name(html: str) -> str:
    """First real <h1> on the page. Seen through the readability tool as
    a clean "# ALPHA CRUX" / "# GREMLIN" heading; NOT yet confirmed via
    raw curl that it's literally an <h1> tag -- flagged in the module
    docstring as a first-deploy smoke-test target."""
    m = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.I)
    return m.group(1).strip() if m else None


def parse_sku_code(html: str) -> str:
    """From the product:retailer_item_id meta tag -- confirmed present
    and correct (matched the visible "SKU: BBMVXA" text) on every page
    checked this session, via the readability tool's meta-tag summary.
    Matches either attribute order/quoting commonly used for this
    OpenGraph-style tag."""
    m = re.search(
        r'<meta[^>]+(?:property|name)=["\']product:retailer_item_id["\'][^>]+content=["\']([^"\']+)["\']',
        html, re.I,
    )
    if m:
        return m.group(1).strip()
    # attribute order can be reversed
    m = re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']product:retailer_item_id["\']',
        html, re.I,
    )
    return m.group(1).strip() if m else None


def parse_main_image_url(html: str, base_url: str) -> str:
    """From the og:image meta tag -- confirmed present and correct on
    every product page checked this session (readability tool's
    meta-og:image summary matched the real hero image)."""
    m = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        html, re.I,
    )
    if not m:
        m = re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            html, re.I,
        )
    return urljoin(base_url, m.group(1)) if m else None


def parse_tech_data_pdf_url(html: str, base_url: str):
    """Finds the "Tech Data" PDF link in the Downloads section by LINK
    TEXT content ("tech data", case-insensitive), not by filename pattern
    -- confirmed real filenames vary wildly ("Alpha Crux Tech Data
    Final.pdf" vs "Tech Doc_HP3_GREMLIN.pdf") but the link text itself
    reliably contains "Tech Data" on both products checked. Same
    content-over-structure matching philosophy as the Craft-CMS scraper's
    _nearby_label_text/parse_resources."""
    for m in re.finditer(r'<a[^>]+href="([^"]+\.pdf)"[^>]*>([^<]*)</a>', html, re.I):
        href, text = m.group(1), m.group(2)
        if "tech data" in text.lower():
            return urljoin(base_url, href)
    return None


def parse_product_page(html: str, url: str) -> dict:
    spec = parse_spec_fields(html)
    coverstock = parse_coverstock(spec.get("coverstock"))

    return {
        "url": url,
        "name": parse_product_name(html),
        "sku_code": parse_sku_code(html),
        "brand_name": spec.get("brand"),
        "line": spec.get("line"),
        "core_name": spec.get("weight block") or spec.get("core"),
        "coverstock_name": spec.get("coverstock"),
        "coverstock_material": coverstock["coverstock_material"],
        "coverstock_type": coverstock["coverstock_type"],
        "core_type": parse_core_type(spec.get("symmetry")),
        "factory_finish": spec.get("finish"),
        "color": spec.get("color"),
        "release_date_raw": spec.get("release date"),
        "release_date": parse_release_date(spec.get("release date")),
        # The single weight/RG/Diff/PSA this page's raw HTML actually
        # shows -- used as a cross-check against the Tech Data PDF's full
        # table, not stored as the primary SKU source (see
        # cross_check_html_vs_pdf below).
        "html_weight_lbs": int(_to_float(spec.get("weight"))) if spec.get("weight") else None,
        "html_rg": _to_float(spec.get("radius of gyration")),
        "html_differential": _to_float(spec.get("differential")),
        "tech_data_pdf_url": parse_tech_data_pdf_url(html, url),
        "main_image_url": parse_main_image_url(html, url),
    }


def _skus_from_table(table: list) -> list:
    """Pure logic for turning one pdfplumber-extracted table into a list
    of per-weight SKU dicts -- split out from parse_tech_data_pdf() so it
    can be tested directly against a synthetic table (matching the real
    confirmed shape) without needing to generate actual PDF bytes.

    Real, confirmed structure (Alpha Crux Tech Data PDF, checked via
    pdfplumber.extract_tables() this session): a SINGLE table row whose
    cells each hold newline-joined values for every weight in matching
    order -- not one row per weight. Real example:
      col 0 (weight): "16 lb\\n15 lb\\n14 lb\\n13 lb\\n12 lb"
      col 1 (RG):     "2.48\\n2.48\\n2.52\\n2.56\\n2.58"
      col 2 (Diff):   "0.052\\n0.053\\n0.051\\n0.034\\n0.031"
      col 3 (PSA):    "0.017\\n0.018\\n0.016\\n0.011\\n0.009"
    No mass_bias/MB column observed on this real example. Skips (doesn't
    guess at) any table whose columns don't line up count-wise, rather
    than silently mis-pairing values -- this is based on one real PDF, and
    other products' PDFs may have a different shape (see module
    docstring)."""
    if not table or not table[0] or len(table[0]) < 3:
        return []

    row = table[0]
    weight_cells = (row[0] or "").split("\n")
    rg_cells = (row[1] or "").split("\n")
    diff_cells = (row[2] or "").split("\n")
    psa_cells = (row[3] or "").split("\n") if len(row) > 3 and row[3] else [None] * len(weight_cells)

    if not (len(weight_cells) == len(rg_cells) == len(diff_cells) == len(psa_cells)):
        logger.warning(
            "Tech Data PDF table column lengths don't match (%d/%d/%d/%d) -- skipping, not guessing",
            len(weight_cells), len(rg_cells), len(diff_cells), len(psa_cells),
        )
        return []

    skus = []
    for w, rg, diff, psa in zip(weight_cells, rg_cells, diff_cells, psa_cells):
        weight_match = re.search(r"(\d{1,2})", w)
        if not weight_match:
            continue
        skus.append({
            "weight_lbs": int(weight_match.group(1)),
            "rg": _to_float(rg),
            "differential": _to_float(diff),
            "mass_bias": None,  # not observed in any real Tech Data PDF checked so far
            "psa": _to_float(psa),
        })
    return skus


def parse_tech_data_pdf(pdf_bytes: bytes) -> list:
    """Parses the per-weight spec table out of a Tech Data PDF -- see
    _skus_from_table()'s docstring for the real, confirmed table shape
    this is built against."""
    import pdfplumber

    skus = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                skus.extend(_skus_from_table(table))
    return skus


def cross_check_html_vs_pdf(parsed_page: dict, pdf_skus: list, tolerance: float = DEFAULT_TOLERANCE) -> list:
    """Compares the one weight/RG/Diff the product page's raw HTML shows
    against the matching weight row in the Tech Data PDF's full table.
    Real bug this project has hit twice already this deploy (pdf_parser,
    bowwwl_cross_check): DB-sourced values come back as decimal.Decimal
    via psycopg2, HTML/PDF-parsed values are plain float -- both operands
    are coerced to float here from the start, before either value could
    ever reach a DB round-trip, specifically to not repeat that bug a
    third time in a fourth module."""
    if parsed_page.get("html_weight_lbs") is None:
        return []

    match = next((s for s in pdf_skus if s["weight_lbs"] == parsed_page["html_weight_lbs"]), None)
    if match is None:
        return []

    mismatches = []
    for field, html_key, pdf_key in (
        ("rg", "html_rg", "rg"),
        ("differential", "html_differential", "differential"),
    ):
        html_value = parsed_page.get(html_key)
        pdf_value = match.get(pdf_key)
        if html_value is None or pdf_value is None:
            continue
        if abs(float(html_value) - float(pdf_value)) > tolerance:
            mismatches.append({
                "field_name": f"{field}_{parsed_page['html_weight_lbs']}lb",
                "current_value": str(html_value),
                "proposed_value": str(pdf_value),
                "reason": f"commercebuild html-vs-pdf: {field} at {parsed_page['html_weight_lbs']}lb disagrees (html={html_value}, pdf={pdf_value}, tolerance={tolerance})",
            })
    return mismatches


# ---------------------------------------------------------------------
# Lambda handler + DB write. Same split as every other scraper in this
# project: pure parsing above (tested against reconstructed fixtures
# built from real confirmed field values -- see
# tests/test_commercebuild_product_scraper.py), mechanical I/O below,
# deferred-imported so the parsing tests don't need requests/psycopg2/
# boto3/pdfplumber installed to run.
# ---------------------------------------------------------------------

import json
import os


def fetch_page(url: str, timeout: int = 30) -> str:
    import requests

    resp = requests.get(url, headers={"User-Agent": "bowling-scraper/1.0"}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def fetch_pdf(url: str, timeout: int = 30) -> bytes:
    import requests

    resp = requests.get(url, headers={"User-Agent": "bowling-scraper/1.0"}, timeout=timeout)
    resp.raise_for_status()
    return resp.content


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


def upsert_product(conn, brand_id: str, parsed: dict, pdf_skus: list, mismatches: list) -> dict:
    """Insert/update the products row, its product_skus rows (sourced
    from the Tech Data PDF -- source='pdf', the schema's existing
    spec_source enum already supports this, no migration needed), and a
    single product_images row for the main image. Returns
    {"product_id": ..., "pending_image_jobs": [...]}, same shape as the
    other scrapers' upsert_product.

    Any html-vs-pdf mismatches found are written to review_queue rather
    than silently preferring one source over the other -- same "flag,
    don't guess" pattern as the rest of this project."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into products (
                brand_id, name, url, color, coverstock_material, coverstock_type,
                coverstock_name, factory_finish, part_number, status, source_platform,
                release_date
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'current', 'commercebuild', %s)
            on conflict (url) do update set
                name = excluded.name,
                color = excluded.color,
                coverstock_material = excluded.coverstock_material,
                coverstock_type = excluded.coverstock_type,
                coverstock_name = excluded.coverstock_name,
                factory_finish = excluded.factory_finish,
                part_number = excluded.part_number,
                release_date = coalesce(excluded.release_date, products.release_date),
                updated_at = now()
            returning id
            """,
            (
                brand_id, parsed["name"], parsed["url"], parsed["color"],
                parsed["coverstock_material"], parsed["coverstock_type"],
                parsed["coverstock_name"], parsed["factory_finish"], parsed["sku_code"],
                parsed["release_date"],
            ),
        )
        product_id = cur.fetchone()[0]

        for sku in pdf_skus:
            cur.execute(
                """
                insert into product_skus (product_id, weight_lbs, rg, differential, mass_bias, source)
                values (%s, %s, %s, %s, %s, 'pdf')
                on conflict (product_id, weight_lbs) do update set
                    rg = excluded.rg,
                    differential = excluded.differential,
                    mass_bias = coalesce(excluded.mass_bias, product_skus.mass_bias),
                    updated_at = now()
                """,
                (product_id, sku["weight_lbs"], sku["rg"], sku["differential"], sku["mass_bias"]),
            )

        pending_image_jobs = []
        if parsed.get("main_image_url"):
            cur.execute(
                """
                insert into product_images (product_id, image_type, source_url)
                values (%s, 'main', %s)
                on conflict (product_id, source_url) do update set image_type = excluded.image_type
                returning id, stored_url
                """,
                (product_id, parsed["main_image_url"]),
            )
            image_id, stored_url = cur.fetchone()
            if stored_url is None:
                pending_image_jobs.append({"product_image_id": str(image_id), "source_url": parsed["main_image_url"]})

        for m in mismatches:
            cur.execute(
                """
                insert into review_queue (product_id, field_name, current_value, proposed_value, source, reason)
                values (%s, %s, %s, %s, 'commercebuild_html_vs_pdf', %s)
                """,
                (product_id, m["field_name"], m["current_value"], m["proposed_value"], m["reason"]),
            )

    conn.commit()
    return {"product_id": product_id, "pending_image_jobs": pending_image_jobs}


def build_image_process_messages(pending_image_jobs: list) -> list:
    return [json.dumps(job) for job in pending_image_jobs]


def publish_messages(sqs_client, queue_url: str, message_bodies: list) -> int:
    sent = 0
    for i in range(0, len(message_bodies), 10):
        chunk = message_bodies[i:i + 10]
        entries = [{"Id": str(idx), "MessageBody": body} for idx, body in enumerate(chunk)]
        sqs_client.send_message_batch(QueueUrl=queue_url, Entries=entries)
        sent += len(chunk)
    return sent


def _extract_jobs(event: dict) -> list:
    """Same shape-detection as every other scraper's handler: real SQS
    trigger ({"Records": [...]}) vs. direct/manual invocation
    ({"url": ..., "brand_id": ...})."""
    if "Records" in event:
        return [(json.loads(r["body"]), r["messageId"]) for r in event["Records"]]
    return [(event, None)]


def _process_one(job: dict) -> dict:
    url = job["url"]
    brand_id = job["brand_id"]

    html = fetch_page(url)
    parsed = parse_product_page(html, url)

    pdf_skus = []
    if parsed["tech_data_pdf_url"]:
        pdf_bytes = fetch_pdf(parsed["tech_data_pdf_url"])
        pdf_skus = parse_tech_data_pdf(pdf_bytes)
    else:
        logger.warning("No Tech Data PDF found for %s -- no per-weight SKU data will be stored", url)

    mismatches = cross_check_html_vs_pdf(parsed, pdf_skus)

    conn = get_db_connection()
    try:
        result = upsert_product(conn, brand_id, parsed, pdf_skus, mismatches)
    finally:
        conn.close()

    logger.info(
        "Scraped %s: %d SKUs from PDF, %d mismatches, %d pending image jobs",
        url, len(pdf_skus), len(mismatches), len(result["pending_image_jobs"]),
    )

    image_queue_url = os.environ.get("IMAGE_PROCESS_QUEUE_URL")
    image_jobs_published = 0
    if result["pending_image_jobs"] and image_queue_url:
        import boto3

        sqs = boto3.client("sqs")
        messages = build_image_process_messages(result["pending_image_jobs"])
        image_jobs_published = publish_messages(sqs, image_queue_url, messages)

    return {
        "product_id": str(result["product_id"]),
        "sku_count": len(pdf_skus),
        "mismatch_count": len(mismatches),
        "image_jobs_published": image_jobs_published,
    }


def handler(event, context):
    """SQS-triggered from CommercebuildProductScrapeQueue (populated by
    CommercebuildUrlDiscoveryFunction) -- also accepts direct/manual
    invocation with {"url": "...", "brand_id": "..."}. Same partial-batch
    -failure handling as every other scraper here."""
    jobs = _extract_jobs(event)

    results = []
    batch_item_failures = []
    for job, message_id in jobs:
        try:
            results.append(_process_one(job))
        except Exception:
            logger.exception("Failed to process commercebuild product job: %r", job)
            if message_id is not None:
                batch_item_failures.append({"itemIdentifier": message_id})
            else:
                raise

    response = {"statusCode": 200, "body": json.dumps({"results": results})}
    if "Records" in event:
        response["batchItemFailures"] = batch_item_failures
    return response
