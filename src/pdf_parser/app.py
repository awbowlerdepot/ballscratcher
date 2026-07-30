"""
PDF "Info Sheet" parser for the Craft CMS brand family (Brunswick/Radical/
DV8). Consumes the info_sheet_url captured by the HTML product scraper
(src/product_scraper/app.py) and extracts the per-weight RG/DIFF/mass-bias
table these PDFs carry -- which, for many balls, is more complete than the
HTML page: real examples found during research include a retired ball
(Defender) whose HTML page only exposed a single 15 lb reference value,
while its PDF has the full 16/15/14/13/12 lb breakdown including mass bias.

Also found a real, genuine mismatch worth knowing about: Crown 78U's HTML
page lists 16 lb RG as 2.577, but its own PDF Info Sheet lists 2.557 for
the same ball/weight. Not a hypothetical edge case -- an actual manufacturer
data inconsistency, which is exactly what the review_queue table (see
db/migrations/001_init_schema.sql) and the "mismatched fields trigger
review" rule from the architecture doc exist to catch. find_mismatches()
below is a first cut at that comparison, meant to be called from wherever
HTML and PDF results for the same product are brought together (the admin
API / orchestration layer, not yet built).

All three Brunswick Info Sheet PDFs pulled during research (Crown 78U,
Crown Victory, Defender) share the same layout: a block of "Label Value"
lines (Part Number, Performance, Color, Core, Coverstock, Cover Type,
Finish, Weights, Warranty), followed by a weight header line ("16 lbs 15
lbs 14 lbs 13 lbs 12 lbs"), then RG/DIFF/[ASY] lines with one number per
weight column. This parser is built against that real, observed structure
-- not a guess.
"""
import logging
import re

logger = logging.getLogger()
logger.setLevel(logging.INFO)

WEIGHT_HEADER_LINE_RE = re.compile(r"(?:\d{1,2}\s*lbs?\s*){2,}")
WEIGHT_TOKEN_RE = re.compile(r"(\d{1,2})\s*lbs?")
NUMBER_RE = re.compile(r"-?\d+\.\d+")

# "Label Value" line prefixes, longest-first so "Part Number" matches before
# a hypothetical bare "Part" would. Matched at the start of a line only --
# these labels also appear as PDF form-title boilerplate elsewhere in the
# doc (e.g. "BALL PERFORMANCE INDEX CHART"), so anchoring to line-start
# avoids false matches inside that unrelated marketing content.
FIELD_LABELS = [
    "Part Number",
    "Performance",
    "Color",
    "Core",
    "Coverstock",
    "Cover Type",
    "Finish",
    "Weights",
    "Warranty",
]


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from the PDF. Kept separate from parsing so tests can
    feed real captured text without needing the PDF bytes or a network
    call -- see tests/fixtures/*_info_sheet.txt for provenance."""
    import io

    import pdfplumber

    text_parts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    return "\n".join(text_parts)


def _to_float(token: str):
    match = NUMBER_RE.search(token)
    return float(match.group()) if match else None


def parse_weight_table(text: str) -> list:
    """Finds the weight header line and the RG/DIFF/ASY lines that follow
    it, returning [{weight_lbs, rg, differential, mass_bias}, ...].

    Deliberately locates rows by leading label ("RG"/"DIFF"/"ASY") rather
    than by fixed line offset from the header, since Defender's real sheet
    has ASY present and Crown 78U's real sheet doesn't -- offset-based
    parsing would break on whichever sheet doesn't match the offset it was
    tuned against.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    weights = None
    for line in lines:
        if WEIGHT_HEADER_LINE_RE.fullmatch(line.replace(",", "")) or (
            WEIGHT_HEADER_LINE_RE.search(line) and len(WEIGHT_TOKEN_RE.findall(line)) >= 2
        ):
            weights = [int(w) for w in WEIGHT_TOKEN_RE.findall(line)]
            break

    if not weights:
        return []

    rg_values, diff_values, asy_values = None, None, None
    for line in lines:
        if line.startswith("RG "):
            rg_values = [_to_float(t) for t in line[len("RG "):].split()]
        elif line.startswith("DIFF "):
            diff_values = [_to_float(t) for t in line[len("DIFF "):].split()]
        elif line.startswith("ASY "):
            asy_values = [_to_float(t) for t in line[len("ASY "):].split()]

    results = []
    for idx, weight in enumerate(weights):
        rg = rg_values[idx] if rg_values and idx < len(rg_values) else None
        diff = diff_values[idx] if diff_values and idx < len(diff_values) else None
        mass_bias = asy_values[idx] if asy_values and idx < len(asy_values) else None
        if rg is None and diff is None:
            continue
        results.append({
            "weight_lbs": weight,
            "rg": rg,
            "differential": diff,
            "mass_bias": mass_bias,
        })
    return results


def parse_fields(text: str) -> dict:
    """Extracts the "Label Value" block. Matches only at line-start against
    the known label list (see module docstring for why)."""
    fields = {}
    for line in text.splitlines():
        line = line.strip()
        for label in FIELD_LABELS:
            prefix = label + " "
            if line.startswith(prefix):
                fields[label.lower().replace(" ", "_")] = line[len(prefix):].strip()
                break
    return fields


def parse_info_sheet(text: str) -> dict:
    fields = parse_fields(text)
    skus = parse_weight_table(text)

    return {
        "part_number": fields.get("part_number"),
        "performance": fields.get("performance"),
        "color": fields.get("color"),
        "core_name": fields.get("core"),
        "coverstock_name": fields.get("coverstock"),
        "cover_type": fields.get("cover_type"),
        "factory_finish": fields.get("finish"),
        "warranty": fields.get("warranty"),
        "skus": skus,
    }


def fetch_pdf(url: str, timeout: int = 30) -> bytes:
    """Fetch raw PDF bytes. Kept separate from extraction/parsing so tests
    can feed real captured text without a network call -- see
    tests/fixtures/*_info_sheet.txt."""
    import requests

    resp = requests.get(url, headers={"User-Agent": "bowling-scraper/1.0"}, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def find_mismatches(html_skus: list, pdf_skus: list, tolerance: float = 0.001) -> list:
    """Compares HTML-sourced and PDF-sourced SKU values for the same
    product, returning a list of mismatch dicts suitable for writing into
    review_queue. Real, motivating example from research: Crown 78U's HTML
    page has 16 lb RG = 2.577, its PDF has 2.557 -- a genuine manufacturer
    data inconsistency, not a hypothetical this function was built to
    handle in the abstract.

    Only flags fields present in both sources -- a field the HTML page
    doesn't have at all (e.g. mass bias, often only on the PDF) isn't a
    mismatch, it's exactly the PDF filling a gap, which is the normal,
    expected case and shouldn't be flagged for review.
    """
    html_by_weight = {s["weight_lbs"]: s for s in html_skus}
    pdf_by_weight = {s["weight_lbs"]: s for s in pdf_skus}

    mismatches = []
    for weight, pdf_sku in pdf_by_weight.items():
        html_sku = html_by_weight.get(weight)
        if html_sku is None:
            continue
        for field in ("rg", "differential", "mass_bias"):
            html_value = html_sku.get(field)
            pdf_value = pdf_sku.get(field)
            if html_value is None or pdf_value is None:
                continue
            if abs(html_value - pdf_value) > tolerance:
                mismatches.append({
                    "weight_lbs": weight,
                    "field_name": field,
                    "html_value": html_value,
                    "pdf_value": pdf_value,
                })
    return mismatches


# ---------------------------------------------------------------------
# Lambda handler + DB write. Same split as product_scraper/app.py: pure
# parsing above (tested against real fixture text, see
# tests/test_pdf_parser.py), mechanical DB code below, deferred-imported
# so the parsing tests don't need boto3/psycopg2 installed to run.
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


def sync_pdf_skus(conn, product_id: str, pdf_skus: list) -> list:
    """Reconciles PDF-derived SKU rows against whatever product_skus already
    exist for this product (normally html-sourced, written by
    product_scraper.upsert_product first).

    Three cases per weight, matching the "approval only on mismatched
    fields" rule from the architecture doc:
      - No existing row for that weight -> straight insert, source='pdf'.
        This is the common, unremarkable case (PDF has weights/rows HTML
        didn't, e.g. Defender's full 5-weight breakdown vs. HTML's single
        15 lb value) and needs no review.
      - Existing row, but it's missing a value the PDF has (mass_bias is
        the frequent one -- HTML often doesn't carry ASY at all) -> fill
        the gap via coalesce, no review needed; this isn't a
        disagreement, it's the PDF completing the record.
      - Existing row where both sources have a value and they disagree
        beyond tolerance (the real Crown 78U case: HTML 16 lb RG 2.577 vs
        PDF 2.557) -> do NOT overwrite; write a review_queue row instead
        and leave the stored value as-is until a human resolves it.

    Returns the list of review_queue rows written (field_name/proposed
    value/etc, same shape as find_mismatches' output) for logging/testing.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select weight_lbs, rg, differential, mass_bias from product_skus where product_id = %s",
            (product_id,),
        )
        existing_by_weight = {
            row[0]: {"weight_lbs": row[0], "rg": row[1], "differential": row[2], "mass_bias": row[3]}
            for row in cur.fetchall()
        }

    flagged = find_mismatches(list(existing_by_weight.values()), pdf_skus)
    flagged_fields = {(m["weight_lbs"], m["field_name"]) for m in flagged}

    with conn.cursor() as cur:
        for sku in pdf_skus:
            weight = sku["weight_lbs"]
            existing = existing_by_weight.get(weight)

            if existing is None:
                cur.execute(
                    """
                    insert into product_skus (product_id, weight_lbs, rg, differential, mass_bias, source)
                    values (%s, %s, %s, %s, %s, 'pdf')
                    on conflict (product_id, weight_lbs) do nothing
                    """,
                    (product_id, weight, sku["rg"], sku["differential"], sku["mass_bias"]),
                )
                continue

            # Only fill gaps (coalesce keeps the existing value when
            # present), and never touch a field flagged as a mismatch --
            # that one needs a human via review_queue, not a silent write.
            rg = existing["rg"] if (weight, "rg") in flagged_fields else None
            diff = existing["differential"] if (weight, "differential") in flagged_fields else None
            cur.execute(
                """
                update product_skus set
                    rg = coalesce(%s, coalesce(rg, %s)),
                    differential = coalesce(%s, coalesce(differential, %s)),
                    mass_bias = coalesce(mass_bias, %s),
                    updated_at = now()
                where product_id = %s and weight_lbs = %s
                """,
                (rg, sku["rg"], diff, sku["differential"], sku["mass_bias"], product_id, weight),
            )

        for mismatch in flagged:
            cur.execute(
                """
                insert into review_queue (product_id, field_name, current_value, proposed_value, source, reason, status)
                values (%s, %s, %s, %s, 'pdf_extraction', %s, 'pending')
                """,
                (
                    product_id,
                    f"{mismatch['field_name']}_{mismatch['weight_lbs']}lb",
                    str(mismatch["html_value"]),
                    str(mismatch["pdf_value"]),
                    "HTML vs PDF Info Sheet disagree beyond tolerance",
                ),
            )

    conn.commit()
    return flagged


def handler(event, context):
    """Expects event = {"info_sheet_url": "...", "product_id": "..."}.
    product_id is the row product_scraper.upsert_product already created --
    this function is meant to run as a second step against the same
    product, not standalone, since it needs existing product_skus rows to
    reconcile against. Wiring that hand-off (Step Functions/SQS after
    ProductScraperFunction) is the same not-yet-decided orchestration
    question noted in template.yaml, not something to guess at here."""
    info_sheet_url = event["info_sheet_url"]
    product_id = event["product_id"]

    logger.info("Fetching PDF info sheet %s", info_sheet_url)
    pdf_bytes = fetch_pdf(info_sheet_url)
    text = extract_pdf_text(pdf_bytes)
    parsed = parse_info_sheet(text)

    conn = get_db_connection()
    try:
        flagged = sync_pdf_skus(conn, product_id, parsed["skus"])
    finally:
        conn.close()

    logger.info(
        "Synced %d PDF SKUs for product %s (%d flagged for review)",
        len(parsed["skus"]), product_id, len(flagged),
    )

    return {
        "statusCode": 200,
        "body": json.dumps({
            "product_id": str(product_id),
            "sku_count": len(parsed["skus"]),
            "flagged_count": len(flagged),
        }),
    }
