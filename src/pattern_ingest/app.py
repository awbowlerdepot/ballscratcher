"""
Oil pattern sheet ingest (runbook 6ej).

Al: "pulling in the patterns and using OCR is fine but they need approved
before they are added to the live catalog. similar pattern to the videos.
manual uploads of patterns as the first way they get pulled in".

Invoked asynchronously by admin_api (POST /patterns/{id}/extract) after an
admin uploads a sheet PDF to the private PatternSheetsBucket:

    {"pattern_id": "<uuid>"}

It renders the first page(s), saves a PNG next to the PDF for the review
screen, reads the sheet with Claude vision (extract.py), and writes the
fields into the PENDING oil_patterns row with extraction_status 'ready'.
Any failure sets extraction_status 'failed' with the reason, so the admin
can retry or type the pattern in. It never approves or publishes anything,
and it never overwrites a row that's already been approved or rejected.

Prototype on the Elite 48 sheet (2026-10-22): 12s, ~2k input / 2k output
tokens; every field right except one misread "crossed" (16 read as 14),
which oil_patterns.check flagged on the exact step. That is the point of
the review step.
"""
import json
import logging
import os

import extract

logger = logging.getLogger("pattern_ingest")
logger.setLevel(logging.INFO)

FIELDS = ("name", "category", "length_feet", "reverse_brush_drop_feet", "oil_per_board_ul", "forward_ml", "reverse_ml",
          "volume_ml", "machine", "steps", "track_zones")
JSON_FIELDS = {"machine", "steps", "track_zones"}


def get_db_connection():
    import boto3
    import psycopg2

    secret = json.loads(boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["DB_SECRET_ARN"])["SecretString"])
    return psycopg2.connect(host=secret["host"], port=secret.get("port", 5432), dbname=secret["dbname"],
                            user=secret["username"], password=secret["password"])


def _set(conn, pattern_id: str, **cols):
    keys = list(cols)
    with conn.cursor() as cur:
        cur.execute(
            f"update oil_patterns set {', '.join(f'{k} = %s' for k in keys)} where id = %s",
            [json.dumps(cols[k]) if k in JSON_FIELDS else cols[k] for k in keys] + [pattern_id],
        )
    conn.commit()


def ingest(conn, s3, bedrock, bucket: str, pattern_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("select upload_key, review_status, name from oil_patterns where id = %s", (pattern_id,))
        row = cur.fetchone()
    if not row:
        raise LookupError(f"No oil pattern {pattern_id}")
    upload_key, review_status, current_name = row
    if review_status != "pending":
        logger.info("pattern %s is %s; not overwriting", pattern_id, review_status)
        return {"pattern_id": pattern_id, "skipped": review_status}
    if not upload_key:
        raise ValueError("No sheet has been uploaded for this pattern")

    _set(conn, pattern_id, extraction_status="running", extraction_error=None)
    pdf = s3.get_object(Bucket=bucket, Key=upload_key)["Body"].read()
    pngs = extract.render_pages(pdf)
    if not pngs:
        raise ValueError("The PDF has no pages")
    image_key = upload_key.rsplit("/", 1)[0] + "/page-1.png"
    s3.put_object(Bucket=bucket, Key=image_key, Body=pngs[0], ContentType="image/png")

    # Kegel Pattern Library sheets carry a text layer: read it exactly
    # (runbook 6es). Image-only sheets (the older format) go to Claude vision.
    fields = extract.parse_pattern_library(extract.text_layer(pdf))
    model_used = extract.TEXT_MODEL_ID
    if fields is None:
        resp = bedrock.invoke_model(modelId=extract.MODEL_ID, contentType="application/json", accept="application/json",
                                    body=extract.build_request(pngs))
        body = json.loads(resp["body"].read())
        logger.info("pattern %s read: usage %s", pattern_id, body.get("usage"))
        fields = extract.parse_response(body["content"][0]["text"])
        model_used = extract.MODEL_ID
    else:
        logger.info("pattern %s read from the text layer (%d steps)", pattern_id, len(fields["steps"]))
    updates = {k: fields[k] for k in FIELDS if fields.get(k) not in (None, "", [], {})}
    # Keep a name the admin already typed over the placeholder from the filename.
    if current_name and not current_name.startswith("Uploaded sheet"):
        updates.pop("name", None)
    with conn.cursor() as cur:
        keys = list(updates)
        cur.execute(
            f"update oil_patterns set {''.join(f'{k} = %s, ' for k in keys)}page_image_key = %s, extraction_status = 'ready', "
            "extraction_error = null, extraction_model = %s, extracted_at = now() where id = %s and review_status = 'pending'",
            [json.dumps(updates[k]) if k in JSON_FIELDS else updates[k] for k in keys] + [image_key, model_used, pattern_id],
        )
    conn.commit()
    return {"pattern_id": pattern_id, "steps": len(fields.get("steps") or []), "fields": sorted(updates), "reader": model_used}


def handler(event, context):
    import boto3

    pattern_id = (event or {}).get("pattern_id")
    if not pattern_id:
        return {"statusCode": 400, "body": "pattern_id is required"}
    conn = get_db_connection()
    try:
        try:
            result = ingest(conn, boto3.client("s3"), boto3.client("bedrock-runtime"), os.environ["PATTERN_SHEETS_BUCKET"], pattern_id)
            logger.info("ingest done: %s", result)
            return {"statusCode": 200, "body": json.dumps(result)}
        except Exception as e:  # noqa: BLE001 -- report it on the row, for the admin
            logger.exception("ingest failed for %s", pattern_id)
            conn.rollback()
            _set(conn, pattern_id, extraction_status="failed", extraction_error=str(e)[:500])
            return {"statusCode": 500, "body": str(e)}
    finally:
        conn.close()
